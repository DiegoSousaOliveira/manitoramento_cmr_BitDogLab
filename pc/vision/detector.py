"""Detector YOLO compartilhado + serviço de inferência em rodízio.

Ideias principais:
- **Um único modelo** carregado na memória (da GPU, se houver) atende TODAS as
  vistas. Carregar um modelo por câmera gastaria memória à toa.
- **Rodízio no ritmo ``ia_fps``**: cada vista é analisada no máximo ``ia_fps`` vezes
  por segundo (padrão 6). O vídeo continua a 12+ FPS na tela; a IA não precisa de
  todos os quadros.
- **Lote (batch)**: as vistas "vencidas" no mesmo instante vão juntas numa única
  chamada ao YOLO — na GPU isso custa quase o mesmo que uma imagem só.
- Tudo numa thread própria: carregar o modelo (segundos) e inferir nunca travam a interface.
- Para cada vista: movimento (MOG2) -> YOLO -> rastreador (ByteTrack + DESCONHECIDO).

Ultralytics: desligamos a coleta de estatísticas de uso (``sync``) e o modo verboso.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

import numpy as np

from camera.stream import LeitorVideo
from core.config import Config
from core.modelos import CLASSES_COCO, Camera, Deteccao, Vista
from vision.motion import AjustesMovimento, DetectorMovimento, ResultadoMovimento
from vision.rastreador import AjustesRastreio, DeteccaoBruta, RastreadorVista

log = logging.getLogger(__name__)
Chave = tuple[int, int]

os.environ.setdefault("YOLO_VERBOSE", "False")  # o Ultralytics lê isto ao ser importado


class ErroModelo(Exception):
    """Não foi possível carregar nenhum modelo de IA (mensagem pronta para o usuário)."""


class Detector(Protocol):
    """O que o serviço precisa de um modelo (permite trocar por um falso nos testes)."""

    descricao: str

    def detectar(self, imagens: list[np.ndarray], tamanho: int | None = None) -> list[list[DeteccaoBruta]]: ...


# ===================================================================== modelo YOLO
class ModeloYolo:
    """Envolve o Ultralytics YOLO: carrega uma vez, detecta em lote."""

    def __init__(self, caminho: Path, dispositivo: str, tamanho: int, conf: float) -> None:
        from ultralytics import YOLO
        from ultralytics.cfg import DEFAULT_CFG_DICT

        self.caminho = caminho
        self.dispositivo = dispositivo
        self.meia_precisao = dispositivo.startswith("cuda")  # 16 bits: 2x mais leve na GPU
        # O Ultralytics 8.4.1xx trocou ``half=True`` por ``quantize=16`` — e com o nome antigo
        # imprime um aviso a CADA inferência. Usa o nome que a versão instalada conhece.
        if not self.meia_precisao:
            self._precisao: dict[str, object] = {}
        elif "quantize" in DEFAULT_CFG_DICT:
            self._precisao = {"quantize": 16}
        else:
            self._precisao = {"half": True}
        self.tamanho = tamanho
        self.conf = conf
        self._modelo = YOLO(str(caminho), task="detect")
        self._classes = sorted(CLASSES_COCO)
        self.descricao = f"{caminho.stem} · {_nome_dispositivo(dispositivo)}"

    def detectar(self, imagens: list[np.ndarray], tamanho: int | None = None) -> list[list[DeteccaoBruta]]:
        """``tamanho``: lado da imagem dentro do YOLO (640 no substream, 1280 no HD)."""
        if not imagens:
            return []
        resultados = self._modelo.predict(
            imagens,
            imgsz=tamanho or self.tamanho,
            conf=self.conf,
            classes=self._classes,
            device=self.dispositivo,
            verbose=False,
            **self._precisao,
        )
        saida: list[list[DeteccaoBruta]] = []
        for resultado in resultados:
            caixas = resultado.boxes
            if caixas is None or len(caixas) == 0:
                saida.append([])
                continue
            xyxyn = caixas.xyxyn.cpu().numpy()
            confs = caixas.conf.cpu().numpy()
            classes = caixas.cls.cpu().numpy().astype(int)
            saida.append(
                [
                    DeteccaoBruta(int(c), float(p), (float(b[0]), float(b[1]), float(b[2]), float(b[3])))
                    for b, p, c in zip(xyxyn, confs, classes, strict=True)
                ]
            )
        return saida

    def aquecer(self, tamanhos: tuple[int, ...] = ()) -> None:
        """A primeira inferência de cada tamanho (sobretudo na GPU) é lenta: fazemos uma "de mentira" antes."""
        for tamanho in tamanhos or (self.tamanho,):
            self.detectar([np.zeros((tamanho * 9 // 16, tamanho, 3), dtype=np.uint8)], tamanho)


def _nome_dispositivo(dispositivo: str) -> str:
    if dispositivo.startswith("cuda"):
        try:
            import torch

            return f"GPU {torch.cuda.get_device_name(0)}"
        except Exception:  # noqa: BLE001
            return "GPU"
    return "CPU"


def desligar_estatisticas_ultralytics() -> None:
    try:
        from ultralytics.utils import SETTINGS

        if SETTINGS.get("sync", False):
            SETTINGS.update({"sync": False})  # não enviar dados de uso para a Ultralytics
    except Exception:  # noqa: BLE001
        log.debug("Não foi possível ajustar as configurações do Ultralytics", exc_info=True)


def escolher_dispositivo(preferencia: str) -> str:
    """``auto`` usa a GPU NVIDIA (CUDA) se houver; senão a CPU."""
    try:
        import torch

        tem_cuda = torch.cuda.is_available()
    except Exception:  # noqa: BLE001
        tem_cuda = False
    if preferencia == "cuda" and not tem_cuda:
        log.warning("ia.dispositivo = cuda, mas não há GPU CUDA disponível: usando a CPU")
    return "cuda:0" if tem_cuda and preferencia in ("auto", "cuda") else "cpu"


def caminho_modelo_exportado(pasta: Path, nome: str) -> Path | None:
    """Modelo exportado para CPU (OpenVINO tem preferência; depois ONNX), se existir."""
    base = Path(nome).stem
    for candidato in (pasta / f"{base}_openvino_model", pasta / f"{base}.onnx"):
        if candidato.exists():
            return candidato
    return None


def preparar_pesos(pasta: Path, nome: str) -> Path:
    """Garante o arquivo de pesos em ``pasta`` (baixa do GitHub da Ultralytics na 1ª vez, ~6 MB)."""
    destino = pasta / nome
    if destino.is_file():
        return destino
    pasta.mkdir(parents=True, exist_ok=True)
    from ultralytics.utils.downloads import attempt_download_asset

    log.info("Baixando o modelo %s (só na primeira vez)…", nome)
    baixado = Path(attempt_download_asset(destino))
    if not baixado.is_file():
        raise ErroModelo(f"não foi possível baixar {nome} (sem internet?)")
    return baixado


def carregar_modelo(config: Config) -> ModeloYolo:
    """Carrega o modelo principal; se falhar, o reserva. Na CPU, usa o exportado se existir."""
    desligar_estatisticas_ultralytics()
    pasta = config.resolver_caminho(str(config.obter("ia.pasta_modelos")))
    dispositivo = escolher_dispositivo(str(config.obter("ia.dispositivo")))
    tamanho = int(config.obter("ia.tamanho_imagem"))
    conf = float(config.obter("ia.conf_deteccao"))
    erros: list[str] = []
    for nome in (str(config.obter("ia.modelo")), str(config.obter("ia.modelo_reserva"))):
        try:
            caminho = preparar_pesos(pasta, nome)
            if dispositivo == "cpu" and (exportado := caminho_modelo_exportado(pasta, nome)):
                log.info("Usando o modelo exportado para CPU: %s", exportado.name)
                caminho = exportado
            modelo = ModeloYolo(caminho, dispositivo, tamanho, conf)
            modelo.aquecer((tamanho, int(config.obter("ia.tamanho_imagem_hd"))))
            log.info("IA pronta: %s", modelo.descricao)
            return modelo
        except Exception as erro:  # noqa: BLE001 (qualquer falha: tenta o reserva)
            log.warning("Falha ao carregar %s: %s", nome, erro)
            erros.append(f"{nome}: {erro}")
    raise ErroModelo("Nenhum modelo de IA carregou. " + " | ".join(erros))


def exportar_modelo(config: Config, formato: str) -> Path:
    """Exporta o modelo para ``onnx`` ou ``openvino`` (mais rápido na CPU). Demora alguns minutos."""
    if formato not in ("onnx", "openvino"):
        raise ValueError("Formato deve ser onnx ou openvino")
    from ultralytics import YOLO

    desligar_estatisticas_ultralytics()
    pasta = config.resolver_caminho(str(config.obter("ia.pasta_modelos")))
    pesos = preparar_pesos(pasta, str(config.obter("ia.modelo")))
    saida = YOLO(str(pesos), task="detect").export(format=formato, imgsz=int(config.obter("ia.tamanho_imagem")))
    return Path(saida)


# ===================================================================== serviço
class EstadoIA(StrEnum):
    CARREGANDO = "carregando"
    PRONTO = "pronto"
    ERRO = "erro"
    PARADO = "parado"


@dataclass(frozen=True)
class ResultadoVista:
    """O que a IA viu numa vista, no quadro mais recente analisado."""

    chave: Chave
    numero: int  # cresce a cada resultado novo
    instante: float  # time.monotonic() do quadro analisado
    deteccoes: tuple[Deteccao, ...]
    desde: dict[int, float] = field(default_factory=dict)  # id -> quando apareceu
    movimento: ResultadoMovimento = field(default_factory=ResultadoMovimento)
    inferiu: bool = True  # False = sem movimento, repetiu o resultado anterior sem rodar o YOLO
    resolucao: tuple[int, int] = (0, 0)  # largura x altura da imagem analisada (HD ou substream)
    #: A imagem analisada (recorte da lente; HD se ia.fonte = principal). Usada na foto do
    #: evento e no coletor de treino. Não entra em comparações nem no repr.
    imagem: np.ndarray | None = field(default=None, compare=False, repr=False)


@dataclass
class _Vista:
    """Estado da IA para uma vista (só a thread do serviço mexe nos objetos internos)."""

    vista: Vista
    ia_fps: float
    so_com_movimento: bool
    usar_yolo: bool  # False: câmera só com "gravar" ligado (detecção desligada): só movimento
    assinatura: tuple[Any, ...]
    movimento: DetectorMovimento
    rastreador: RastreadorVista
    classes: frozenset[str] = frozenset()  # o que a IA identifica (ia.identificar); o resto é descartado
    proximo: float = 0.0
    ultimo_numero: int = -1
    ultimo: ResultadoVista | None = None
    contador: int = 0
    reiniciar_fundo: bool = False  # a câmera girou (PTZ): o detector de movimento recomeça
    ultima_inferencia: float = float("-inf")  # quando o YOLO rodou pela última vez nesta vista


def valor_da_camera(config: Config, camera: Camera, chave: str) -> Any:
    """Ajuste por câmera (``camera.ajustes["ia.ia_fps"]``) ou, se não houver, o do config.yaml."""
    return camera.ajustes.get(chave, config.obter(chave))


def classes_identificadas(config: Config) -> frozenset[str]:
    """As classes marcadas em ``ia.identificar`` (Ajustes -> IA). As outras somem da tela, dos
    eventos e dos alertas — ex.: desligar DESCONHECIDO tira as manchas de folhas e sombras."""
    return frozenset(str(c) for c, ligado in dict(config.obter("ia.identificar")).items() if ligado)


def repetir_sem_movimento(ultimo: ResultadoVista) -> tuple[tuple[Deteccao, ...], dict[int, float]]:
    """O que pode ser repetido quando a cena parou e o YOLO não rodou.

    Só os objetos que o YOLO VIU no último resultado. Ficam de fora:
    - manchas de movimento (DESCONHECIDO de movimento): sem movimento, a mancha acabou;
    - objetos que a IA já tinha deixado de ver (``ausente_s > 0``, mantidos por 1 s para não piscar).
    Antes (até 12/10/2026) tudo era repetido: uma mancha ou um objeto que já tinha ido embora ficava
    "congelado" no lugar, parecia PARADO e virava alerta na placa sem nada na imagem.
    """
    deteccoes = tuple(d for d in ultimo.deteccoes if d.origem != "movimento" and d.ausente_s == 0)
    ids = {d.id_rastreio for d in deteccoes}
    return deteccoes, {i: t for i, t in ultimo.desde.items() if i in ids}


class ServicoDeteccao:
    """Thread de inferência que atende todas as vistas com detecção ligada."""

    def __init__(
        self,
        config: Config,
        obter_leitor: Callable[[Vista], LeitorVideo | None],
        carregar: Callable[[], Detector] | None = None,
    ) -> None:
        self.config = config
        self._obter_leitor = obter_leitor
        self._carregar = carregar or (lambda: carregar_modelo(config))
        self._trava = threading.Lock()
        self._vistas: dict[Chave, _Vista] = {}
        self._resultados: dict[Chave, ResultadoVista] = {}
        self._assinantes: list[Callable[[ResultadoVista], None]] = []
        self._inferencias: deque[float] = deque(maxlen=600)
        self._parar = threading.Event()
        self._recarregar = threading.Event()
        self._thread = threading.Thread(target=self._executar, name="ia-deteccao", daemon=True)
        self.estado = EstadoIA.CARREGANDO
        self.mensagem = "carregando o modelo…"
        self.descricao = ""
        self.tempo_lote_ms = 0.0

    # ------------------------------------------------------------ controle (qualquer thread)
    def iniciar(self) -> ServicoDeteccao:
        self._thread.start()
        return self

    def parar(self, esperar_s: float = 3.0) -> None:
        self._parar.set()
        if self._thread.is_alive():
            self._thread.join(esperar_s)
        self.estado = EstadoIA.PARADO

    def recarregar_modelo(self) -> None:
        """Ajustes mudaram (modelo ou dispositivo): carrega de novo SEM parar a IA.

        O modelo antigo continua trabalhando até o novo ficar pronto; se o novo falhar,
        o antigo fica (e ``mensagem`` explica).
        """
        self._recarregar.set()

    def assinar(self, funcao: Callable[[ResultadoVista], None]) -> None:
        """Recebe cada resultado novo (chamada na thread da IA: seja rápido!). Usado nas Fases 4 e 5."""
        self._assinantes.append(funcao)

    def definir_cameras(self, cameras: list[Camera]) -> None:
        """Acompanha as vistas das câmeras com detecção OU gravação ligada.

        Com a detecção desligada mas a gravação ligada, roda só o detector de movimento
        (sem YOLO): os eventos continuam sendo gravados, sem caixas.
        """
        desejadas: dict[Chave, tuple[Vista, tuple[Any, ...], Camera]] = {}
        for camera in cameras:
            if not camera.deteccao and not camera.gravar:
                continue
            for vista in camera.vistas():
                assinatura = (
                    vista.regiao,
                    float(valor_da_camera(self.config, camera, "ia.ia_fps")),
                    bool(valor_da_camera(self.config, camera, "ia.ia_so_com_movimento")),
                    float(valor_da_camera(self.config, camera, "ia.conf_minima")),
                    camera.deteccao,
                    float(valor_da_camera(self.config, camera, "ia.movimento_area_minima")),
                    tuple(sorted(classes_identificadas(self.config))),
                )
                desejadas[vista.chave] = (vista, assinatura, camera)
        with self._trava:
            for chave in list(self._vistas):
                if chave not in desejadas or desejadas[chave][1] != self._vistas[chave].assinatura:
                    del self._vistas[chave]
                    self._resultados.pop(chave, None)
            for chave, (vista, assinatura, camera) in desejadas.items():
                if chave not in self._vistas:
                    self._vistas[chave] = self._criar_vista(vista, assinatura, camera)

    def _criar_vista(self, vista: Vista, assinatura: tuple[Any, ...], camera: Camera) -> _Vista:
        c = self.config
        ia_fps = assinatura[1]
        movimento = DetectorMovimento(
            AjustesMovimento(
                largura_trabalho=int(c.obter("ia.movimento_largura")),
                historico=int(c.obter("ia.movimento_historico")),
                limiar=float(c.obter("ia.movimento_limiar")),
                area_minima=float(valor_da_camera(c, camera, "ia.movimento_area_minima")),
                aquecimento_quadros=max(1, round(float(c.obter("ia.movimento_aquecimento_s")) * ia_fps)),
            )
        )
        rastreador = RastreadorVista(
            AjustesRastreio(
                ia_fps=ia_fps,
                perdido_s=float(c.obter("ia.rastreio_perdido_s")),
                conf_minima=assinatura[3],
                conf_deteccao=float(c.obter("ia.conf_deteccao")),
                conf_movimento=float(c.obter("ia.conf_movimento")),
                sobreposicao=float(c.obter("ia.movimento_sobreposicao")),
                manter_s=float(c.obter("ia.manter_perdido_s")),
                reassociar=float(c.obter("ia.reassociar_sobreposicao")),
            )
        )
        return _Vista(vista, ia_fps, assinatura[2], assinatura[4], assinatura, movimento, rastreador,
                      classes=frozenset(assinatura[6]))

    def reiniciar_fundo(self, chave: Chave) -> None:
        """A lente girou (PTZ): esquece o fundo do detector de movimento dessa vista.

        Só marca; quem reinicia é a própria thread da IA (o detector não é compartilhado).
        """
        with self._trava:
            if (estado_vista := self._vistas.get(chave)) is not None:
                estado_vista.reiniciar_fundo = True

    def resultado(self, chave: Chave) -> ResultadoVista | None:
        with self._trava:
            return self._resultados.get(chave)

    def resultado_da_vista(self, vista: Vista) -> ResultadoVista | None:
        return self.resultado(vista.chave)

    @property
    def inferencias_por_s(self) -> float:
        """Imagens analisadas pelo YOLO por segundo (média dos últimos 5 s)."""
        agora = time.monotonic()
        return sum(1 for t in list(self._inferencias) if agora - t <= 5.0) / 5.0

    # ------------------------------------------------------------ thread da IA
    def _executar(self) -> None:
        try:
            detector = self._carregar()
        except Exception as erro:  # noqa: BLE001
            self.estado, self.mensagem = EstadoIA.ERRO, str(erro)
            log.error("IA indisponível: %s", erro)
            return
        self.descricao = detector.descricao
        self.estado, self.mensagem = EstadoIA.PRONTO, "pronto"
        falhas_seguidas = 0
        tamanho_sub = int(self.config.obter("ia.tamanho_imagem"))
        tamanho_hd = int(self.config.obter("ia.tamanho_imagem_hd"))
        while not self._parar.is_set():
            if self._recarregar.is_set():
                self._recarregar.clear()
                detector = self._trocar_modelo(detector)
            lote, espera = self._coletar()
            if not lote:
                self._parar.wait(espera)
                continue
            inicio = time.perf_counter()
            try:
                brutas = self._detectar_por_tamanho(detector, lote, tamanho_sub, tamanho_hd)
                falhas_seguidas = 0
            except Exception as erro:  # noqa: BLE001 (um quadro ruim não pode derrubar a IA)
                falhas_seguidas += 1
                log.exception("Falha na inferência (%d seguidas)", falhas_seguidas)
                if falhas_seguidas >= 10:
                    self.estado, self.mensagem = EstadoIA.ERRO, f"a inferência falhou repetidamente: {erro}"
                    return
                continue
            self.tempo_lote_ms = (time.perf_counter() - inicio) * 1000
            agora = time.monotonic()
            for (estado_vista, imagem, movimento, instante), deteccoes_brutas in zip(lote, brutas, strict=True):
                self._inferencias.append(agora)
                deteccoes = estado_vista.rastreador.atualizar(deteccoes_brutas, movimento.regioes, instante)
                deteccoes = [d for d in deteccoes if d.classe in estado_vista.classes]  # ia.identificar
                desde = {
                    d.id_rastreio: estado_vista.rastreador.desde(d.id_rastreio) or instante
                    for d in deteccoes
                    if d.id_rastreio is not None
                }
                estado_vista.contador += 1
                altura, largura = imagem.shape[:2]
                self._publicar(
                    estado_vista,
                    ResultadoVista(
                        estado_vista.vista.chave,
                        estado_vista.contador,
                        instante,
                        tuple(deteccoes),
                        desde,
                        movimento,
                        resolucao=(largura, altura),
                        imagem=imagem,
                    ),
                )

    def _trocar_modelo(self, atual: Detector) -> Detector:
        """Carrega o modelo dos ajustes atuais; se falhar, continua com o atual."""
        log.info("Trocando o modelo de IA…")
        self.estado, self.mensagem = EstadoIA.CARREGANDO, "trocando o modelo…"
        try:
            novo = self._carregar()
        except Exception as erro:  # noqa: BLE001
            log.error("Não foi possível trocar o modelo (continua o anterior): %s", erro)
            self.estado, self.mensagem = EstadoIA.PRONTO, f"o modelo novo não carregou ({erro}); usando o anterior"
            return atual
        self.descricao = novo.descricao
        self.estado, self.mensagem = EstadoIA.PRONTO, "pronto"
        return novo

    @staticmethod
    def _detectar_por_tamanho(
        detector: Detector,
        lote: list[tuple[_Vista, np.ndarray, ResultadoMovimento, float]],
        tamanho_sub: int,
        tamanho_hd: int,
    ) -> list[list[DeteccaoBruta]]:
        """Imagens HD (largas) vão ao YOLO em ``tamanho_hd``; as do substream em ``tamanho_sub``.

        Cada tamanho é um lote separado (o YOLO processa um lote num tamanho só).
        """
        grupos: dict[int, list[int]] = {}
        for indice, (_v, imagem, _m, _i) in enumerate(lote):
            tamanho = tamanho_hd if imagem.shape[1] >= tamanho_hd else tamanho_sub
            grupos.setdefault(tamanho, []).append(indice)
        saida: list[list[DeteccaoBruta]] = [[] for _ in lote]
        for tamanho, indices in grupos.items():
            resultados = detector.detectar([lote[i][1] for i in indices], tamanho)
            for indice, resultado in zip(indices, resultados, strict=True):
                saida[indice] = resultado
        return saida

    def _coletar(self) -> tuple[list[tuple[_Vista, np.ndarray, ResultadoMovimento, float]], float]:
        """Junta as vistas "vencidas" com quadro novo. Devolve (lote, quanto esperar se vazio)."""
        agora = time.monotonic()
        with self._trava:
            vistas = list(self._vistas.values())
        if not vistas:
            return [], 0.2
        lote = []
        for estado_vista in vistas:
            if agora < estado_vista.proximo:
                continue
            intervalo = 1.0 / max(0.1, estado_vista.ia_fps)
            # Se ficou para trás (ex.: GPU ocupada), não tenta "compensar" rodando várias seguidas.
            estado_vista.proximo = max(estado_vista.proximo + intervalo, agora + intervalo * 0.5)
            leitor = self._obter_leitor(estado_vista.vista)
            quadro = leitor.ultimo_quadro() if leitor is not None else None
            if quadro is None or quadro.numero == estado_vista.ultimo_numero:
                continue
            estado_vista.ultimo_numero = quadro.numero
            imagem = estado_vista.vista.recortar(quadro.imagem)
            if estado_vista.reiniciar_fundo:
                estado_vista.reiniciar_fundo = False
                estado_vista.movimento.reiniciar()
                log.info("Câmera girou: fundo do detector de movimento reiniciado (%s)", estado_vista.vista.chave)
            movimento = estado_vista.movimento.processar(imagem)
            if not estado_vista.usar_yolo:  # só gravação: movimento, sem objetos
                estado_vista.contador += 1
                altura, largura = imagem.shape[:2]
                self._publicar(
                    estado_vista,
                    ResultadoVista(
                        estado_vista.vista.chave, estado_vista.contador, quadro.instante, (), {}, movimento,
                        inferiu=False, resolucao=(largura, altura), imagem=imagem,
                    ),
                )
                continue
            if estado_vista.so_com_movimento and not movimento.tem_movimento and estado_vista.ultimo is not None:
                deteccoes, desde = repetir_sem_movimento(estado_vista.ultimo)
                # Cena parada: os objetos vistos continuam onde estavam e o YOLO descansa — mas, se há
                # objetos, ele CONFERE de tempos em tempos se eles ainda estão lá (pode ter ido embora
                # sem o detector de movimento perceber: longe, de noite, devagar...).
                verificar_s = float(self.config.obter("ia.verificar_parado_s"))
                if not deteccoes or agora - estado_vista.ultima_inferencia < verificar_s:
                    estado_vista.contador += 1
                    self._publicar(
                        estado_vista,
                        replace(
                            estado_vista.ultimo,
                            numero=estado_vista.contador,
                            instante=quadro.instante,
                            deteccoes=deteccoes,
                            desde=desde,
                            movimento=movimento,
                            inferiu=False,
                            imagem=imagem,
                        ),
                    )
                    continue
            estado_vista.ultima_inferencia = agora
            lote.append((estado_vista, np.ascontiguousarray(imagem), movimento, quadro.instante))
        espera = min((v.proximo for v in vistas), default=agora + 0.2) - agora
        return lote, max(0.002, min(0.2, espera))

    def _publicar(self, estado_vista: _Vista, resultado: ResultadoVista) -> None:
        estado_vista.ultimo = resultado
        with self._trava:
            if self._vistas.get(resultado.chave) is not estado_vista:
                return  # a vista foi removida/alterada enquanto analisávamos
            self._resultados[resultado.chave] = resultado
        for funcao in list(self._assinantes):
            try:
                funcao(resultado)
            except Exception:  # noqa: BLE001
                log.exception("Assinante da IA falhou")
