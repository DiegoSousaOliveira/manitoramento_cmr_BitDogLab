"""Eventos: decide quando começam e terminam e grava clipe, fotos, JSON e o registro no banco.

Como funciona (uma thread própria, "eventos"):

1. **Entradas**: cada resultado da IA (via ``receber_resultado``, assinado no
   ``ServicoDeteccao``) e cada quadro do substream (via ``BufferQuadros``, assinado
   no leitor de cada câmera com "gravar eventos" ligado).
2. **Atividade** numa vista = movimento relevante (MOG2) OU objeto reconhecido que
   apareceu há pouco (``objeto_novo_s``). Um carro estacionado não prende o evento
   aberto para sempre; objetos que já estavam lá ao ligar a câmera não abrem evento.
   Com ``eventos.area: zona``, só conta o que estiver dentro da zona.
3. **Início**: atividade em ``confirmar_resultados`` análises seguidas (filtra ruído).
4. **Gravação**: o clipe começa ``pre_evento_s`` antes do início (os quadros estão
   no buffer) e é escrito com ATRASO de ``fim_evento_s``. Por quê? Só sabemos que o
   evento acabou 5 s depois da última atividade; gravando com esse atraso, o clipe
   termina exatamente em "última atividade + ``pos_evento_s``" sem guardar nada extra.
5. **Fim**: ``fim_evento_s`` sem atividade (ou ``max_evento_s`` de duração). Então são
   gravados ``foto.jpg`` (com caixas), ``foto_limpa.jpg``, ``miniatura.jpg``,
   ``deteccoes.json`` e o registro no banco; quem assinou é avisado.

Memória: o buffer guarda referências aos quadros (sem copiar); as imagens HD da IA
são descartadas do histórico — só a melhor candidata a foto é mantida.
"""

from __future__ import annotations

import json
import logging
import queue
import shutil
import threading
import time
from collections import Counter, deque
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from pathlib import Path

import cv2
import numpy as np

from camera.captura import salvar_jpg
from camera.gerenciador import GerenciadorCameras
from camera.stream import LeitorVideo
from core.config import Config
from core.modelos import DESCONHECIDO, Camera, Deteccao, Evento, Vista
from core.servico_cameras import ServicoCameras
from eventos.buffer import BufferQuadros
from eventos.clipe import EscritorClipe
from eventos.coletor import ColetorTreino
from eventos.desenho import desenhar_caixas
from eventos.retencao import aplicar_retencao
from vision.detector import ResultadoVista
from vision.zona import centro, ponto_no_poligono

log = logging.getLogger(__name__)
Chave = tuple[int, int]

ARQUIVO_CLIPE = "clipe.mp4"
ARQUIVO_FOTO = "foto.jpg"
ARQUIVO_FOTO_LIMPA = "foto_limpa.jpg"
ARQUIVO_MINIATURA = "miniatura.jpg"
ARQUIVO_DETECCOES = "deteccoes.json"
LARGURA_MINIATURA = 320


@dataclass(frozen=True)
class Parametros:
    area: str
    pre_s: float
    pos_s: float
    fim_s: float
    max_s: float
    objeto_novo_s: float
    aquecimento_s: float
    confirmar: int
    fps: float
    qualidade: int
    retencao_intervalo_s: float
    #: Lente sem zona com "Só dentro da zona": vale o mesmo ajuste dos alertas (parado.sem_zona).
    sem_zona_vigia_tudo: bool = True
    #: DESCONHECIDO desligado em ia.identificar: mancha de movimento sozinha não abre evento.
    movimento_abre: bool = True

    @classmethod
    def do_config(cls, config: Config) -> Parametros:
        e = lambda chave: config.obter(f"eventos.{chave}")  # noqa: E731
        return cls(
            area=str(e("area")),
            pre_s=float(e("pre_evento_s")),
            pos_s=float(e("pos_evento_s")),
            fim_s=float(e("fim_evento_s")),
            max_s=float(e("max_evento_s")),
            objeto_novo_s=float(e("objeto_novo_s")),
            aquecimento_s=float(e("aquecimento_s")),
            confirmar=max(1, int(e("confirmar_resultados"))),
            fps=float(e("fps_clipe")),
            qualidade=int(e("qualidade_clipe")),
            retencao_intervalo_s=float(e("intervalo_retencao_min")) * 60,
            sem_zona_vigia_tudo=str(config.obter_ou("parado.sem_zona", "ignorar")) == "imagem_inteira",
            movimento_abre=bool(dict(config.obter_ou("ia.identificar", {})).get("DESCONHECIDO", True)),
        )


@dataclass
class _EventoAtivo:
    inicio: float  # time.monotonic() da primeira atividade
    ultimo_ativo: float
    pasta: Path
    pasta_relativa: str
    proximo_tick: float  # próximo instante a gravar no clipe (taxa constante)
    t0: float | None = None  # instante do primeiro quadro do clipe
    escritor: EscritorClipe | None = None
    ultima_imagem: np.ndarray | None = None
    linhas: list[tuple[float, tuple[Deteccao, ...]]] = field(default_factory=list)
    classe_por_id: dict[int, str] = field(default_factory=dict)
    maior_conf: float = 0.0
    melhor: tuple[np.ndarray, tuple[Deteccao, ...]] | None = None  # foto: objeto de maior confiança
    reserva: tuple[float, np.ndarray, tuple[Deteccao, ...]] | None = None  # foto se não houver objeto
    alerta: bool = False


@dataclass
class _VistaEventos:
    vista: Vista
    camera: Camera
    zona: list[tuple[float, float]] | None
    historico: deque[ResultadoVista] = field(default_factory=deque)  # sem as imagens (memória!)
    confirmacoes: deque[tuple[float, bool]] = field(default_factory=deque)
    candidata: tuple[float, float, np.ndarray, tuple[Deteccao, ...]] | None = None  # (conf, instante, imagem, dets)
    primeiro: float | None = None
    ultimo_fim_clipe: float = 0.0
    atual: _EventoAtivo | None = None


class ServicoEventos:
    """Thread que transforma resultados da IA + quadros em eventos gravados."""

    def __init__(
        self,
        config: Config,
        servico: ServicoCameras,
        gerenciador: GerenciadorCameras,
        coletor: ColetorTreino | None = None,
    ) -> None:
        self.config = config
        self.servico = servico
        self.gerenciador = gerenciador
        self.coletor = coletor
        self.p = Parametros.do_config(config)
        self._fila: queue.Queue[tuple[str, object]] = queue.Queue()
        self._vistas: dict[Chave, _VistaEventos] = {}
        self._buffers: dict[tuple[int, int], tuple[LeitorVideo, BufferQuadros]] = {}
        self._ao_gravar: list[Callable[[Evento, Camera], None]] = []
        self._ao_remover: list[Callable[[list[int]], None]] = []
        self._parar = threading.Event()
        self._em_andamento: set[Chave] = set()
        self._trava_retencao = threading.Lock()  # limpeza automática x botão "Limpar agora"
        self._thread = threading.Thread(target=self._executar, name="eventos", daemon=True)

    # ================================================================ controle (qualquer thread)
    def iniciar(self) -> ServicoEventos:
        self._thread.start()
        return self

    def parar(self, esperar_s: float = 10.0) -> None:
        """Para a thread; eventos em andamento são finalizados (gravados) antes."""
        self._parar.set()
        if self._thread.is_alive():
            self._thread.join(esperar_s)

    def receber_resultado(self, resultado: ResultadoVista) -> None:
        """Assinante do ``ServicoDeteccao`` (roda na thread da IA: só enfileira)."""
        self._fila.put(("resultado", resultado))

    def definir_cameras(self, cameras: list[Camera]) -> None:
        """Chamar DEPOIS de ``GerenciadorCameras.sincronizar`` (os leitores já existem)."""
        self._fila.put(("cameras", list(cameras)))

    def marcar_alerta(self, chave: Chave, desde: float | None = None) -> None:
        """Marca o evento atual da vista como ALERTA (abre um, se não houver).

        ``desde`` (``time.monotonic()``): quando o objeto PAROU. Um evento aberto pelo
        alerta começa ali — e não no instante do alerta, o que daria "duração 0 s".
        """
        self._fila.put(("alerta", (chave, desde)))

    def recarregar_ajustes(self) -> None:
        """Os ajustes de eventos mudaram no config.yaml: valem para os próximos eventos."""
        self._fila.put(("ajustes", None))

    def assinar_gravacao(self, funcao: Callable[[Evento, Camera], None]) -> None:
        """``funcao(evento, camera)`` quando um evento termina de ser gravado (thread "eventos")."""
        self._ao_gravar.append(funcao)

    def assinar_remocao(self, funcao: Callable[[list[int]], None]) -> None:
        """``funcao(ids)`` quando a retenção apaga eventos (thread "eventos")."""
        self._ao_remover.append(funcao)

    def em_andamento(self, chave: Chave) -> bool:
        return chave in self._em_andamento

    # ================================================================ thread "eventos"
    def _executar(self) -> None:
        proxima_retencao = time.monotonic() + 30  # a 1ª limpeza espera o app "acordar"
        while not self._parar.is_set():
            try:
                item: tuple[str, object] | None = self._fila.get(timeout=0.1)
            except queue.Empty:
                item = None
            while item is not None:
                try:
                    self._tratar(*item)
                except Exception:  # noqa: BLE001 (um problema num evento não pode parar a gravação)
                    log.exception("Falha ao tratar %s", item[0])
                try:
                    item = self._fila.get_nowait()
                except queue.Empty:
                    item = None
            agora = time.monotonic()
            for estado in list(self._vistas.values()):
                if estado.atual is not None:
                    try:
                        self._avancar(estado, agora)
                    except Exception:  # noqa: BLE001
                        log.exception("Falha ao gravar o evento de %s", estado.camera.nome)
                        self._descartar(estado)
            if agora >= proxima_retencao:
                proxima_retencao = agora + self.p.retencao_intervalo_s
                self.aplicar_retencao()
        for estado in list(self._vistas.values()):  # fechando o app: grava o que estiver em andamento
            if estado.atual is not None:
                self._finalizar(estado, estado.atual.ultimo_ativo, time.monotonic())
        for leitor, buffer in self._buffers.values():
            leitor.cancelar_assinatura(buffer.adicionar)
        self._buffers.clear()

    def aplicar_retencao(self) -> list[int]:
        """Apaga eventos velhos/que passam do limite (thread "eventos" ou o botão em Ajustes)."""
        try:
            with self._trava_retencao:
                removidos, _liberados = aplicar_retencao(self.servico, self.config)
        except Exception:  # noqa: BLE001
            log.exception("Falha na retenção de eventos")
            return []
        if removidos:
            for funcao in list(self._ao_remover):
                try:
                    funcao(removidos)
                except Exception:  # noqa: BLE001
                    log.exception("Assinante de remoção falhou")
        return removidos

    def _tratar(self, tipo: str, dado: object) -> None:
        if tipo == "resultado":
            self._resultado(dado)  # type: ignore[arg-type]
        elif tipo == "cameras":
            self._aplicar_cameras(dado)  # type: ignore[arg-type]
        elif tipo == "ajustes":
            self.p = Parametros.do_config(self.config)
            for _leitor, buffer in self._buffers.values():  # o pré-evento pode ter mudado
                buffer.segundos = max(self.p.pre_s, self.p.fim_s) + 2
            if self.coletor is not None:
                self.coletor.recarregar(self.config)
            log.info("Ajustes de eventos recarregados")
        elif tipo == "alerta":
            chave, desde = dado  # type: ignore[misc]
            estado = self._vistas.get(chave)
            if estado is not None and estado.camera.gravar and self._buffer(estado.vista) is not None:
                agora = time.monotonic()
                if estado.atual is None:
                    # Começa quando o objeto parou, mas nunca antes do fim do evento anterior
                    # nem tão cedo que o evento passasse de max_evento_s antes do pós-evento.
                    limite = agora - max(0.0, self.p.max_s - self.p.fim_s - self.p.pos_s)
                    inicio = max(desde, estado.ultimo_fim_clipe, limite) if desde is not None else agora
                    self._iniciar(estado, min(inicio, agora))
                if estado.atual is not None:
                    estado.atual.alerta = True
                    estado.atual.ultimo_ativo = agora

    # ---------------------------------------------------------------- câmeras e buffers
    def _aplicar_cameras(self, cameras: list[Camera]) -> None:
        segundos = max(self.p.pre_s, self.p.fim_s) + 2
        desejados = {(c.id or 0, canal) for c in cameras if c.gravar for canal in c.numeros_canais}
        for chave, (leitor, buffer) in list(self._buffers.items()):
            if chave not in desejados or self.gerenciador.leitor(*chave) is not leitor:
                leitor.cancelar_assinatura(buffer.adicionar)  # câmera removida ou leitor recriado
                del self._buffers[chave]
        for chave in desejados - set(self._buffers):
            leitor = self.gerenciador.leitor(*chave)
            if leitor is not None:
                buffer = BufferQuadros(segundos)
                leitor.assinar(buffer.adicionar)
                self._buffers[chave] = (leitor, buffer)

        novas: dict[Chave, tuple[Vista, Camera]] = {v.chave: (v, c) for c in cameras for v in c.vistas()}
        for chave in list(self._vistas):
            estado = self._vistas[chave]
            if chave not in novas or novas[chave][0] != estado.vista:
                if estado.atual is not None:
                    self._finalizar(estado, estado.atual.ultimo_ativo, time.monotonic())
                del self._vistas[chave]
        for chave, (vista, camera) in novas.items():
            zona = self.servico.banco.obter_zona(camera.id or 0, vista.numero)
            if chave in self._vistas:
                self._vistas[chave].camera, self._vistas[chave].zona = camera, zona
            else:
                self._vistas[chave] = _VistaEventos(vista, camera, zona)

    def _buffer(self, vista: Vista) -> BufferQuadros | None:
        par = self._buffers.get((vista.camera_id, vista.canal))
        return par[1] if par else None

    # ---------------------------------------------------------------- resultados da IA
    def _resultado(self, resultado: ResultadoVista) -> None:
        estado = self._vistas.get(resultado.chave)
        if estado is None:
            return
        if self.coletor is not None:
            try:
                self.coletor.processar(resultado, estado.camera)
            except Exception:  # noqa: BLE001
                log.exception("Coletor de treino falhou")
        if estado.primeiro is None:
            estado.primeiro = resultado.instante
        self._atualizar_candidata(estado, resultado)
        estado.historico.append(replace(resultado, imagem=None))
        while estado.historico and resultado.instante - estado.historico[0].instante > self.p.pre_s + 1:
            estado.historico.popleft()

        ativo = self._atividade(estado, resultado)
        estado.confirmacoes.append((resultado.instante, ativo))
        while len(estado.confirmacoes) > self.p.confirmar:
            estado.confirmacoes.popleft()

        if estado.atual is None:
            confirmado = len(estado.confirmacoes) == self.p.confirmar and all(a for _, a in estado.confirmacoes)
            if confirmado and estado.camera.gravar and self._buffer(estado.vista) is not None:
                self._iniciar(estado, estado.confirmacoes[0][0])
            return
        if ativo:
            estado.atual.ultimo_ativo = max(estado.atual.ultimo_ativo, resultado.instante)
        self._registrar(estado.atual, resultado, resultado.imagem)

    def _atividade(self, estado: _VistaEventos, resultado: ResultadoVista) -> bool:
        area = str(estado.camera.ajustes.get("eventos.area", self.p.area))  # a câmera pode ter a sua
        if area == "zona" and not estado.zona and not self.p.sem_zona_vigia_tudo:
            return False  # "Só dentro da zona" numa lente sem zona desenhada: não há o que vigiar
        zona = estado.zona if area == "zona" else None
        movimento_abre = self.p.movimento_abre or not estado.camera.deteccao  # sem IA, só há movimento
        if movimento_abre and any(ponto_no_poligono(centro(r), zona) for r in resultado.movimento.regioes):
            return True
        for d in resultado.deteccoes:
            if d.origem != "ia" or d.ausente_s > 0 or d.id_rastreio is None:
                continue
            desde = resultado.desde.get(d.id_rastreio)
            if desde is None or (estado.primeiro is not None and desde <= estado.primeiro + self.p.aquecimento_s):
                continue  # já estava lá quando a câmera ligou (ex.: carro do vizinho)
            if resultado.instante - desde <= self.p.objeto_novo_s and ponto_no_poligono(centro(d.caixa), zona):
                return True
        return False

    def _atualizar_candidata(self, estado: _VistaEventos, resultado: ResultadoVista) -> None:
        """Melhor foto dos últimos ``pre_evento_s`` (se o evento começar, ela já está aqui)."""
        if resultado.imagem is None:
            return
        conf = max(
            (d.confianca for d in resultado.deteccoes if d.origem == "ia" and d.ausente_s == 0 and d.classe != DESCONHECIDO),
            default=0.0,
        )
        atual = estado.candidata
        if atual is None or resultado.instante - atual[1] > self.p.pre_s or conf > atual[0]:
            estado.candidata = (conf, resultado.instante, resultado.imagem, resultado.deteccoes)

    # ---------------------------------------------------------------- ciclo do evento
    def _iniciar(self, estado: _VistaEventos, inicio: float) -> None:
        camera, vista = estado.camera, estado.vista
        quando = self._relogio(inicio)
        sufixo = f"_v{vista.numero}" if len(camera.vistas()) > 1 else ""
        base = f"{quando:%Y-%m-%d}/{quando:%H%M%S}{sufixo}"
        relativa, n = base, 2
        raiz = self.servico.pastas(camera)[0]
        while (raiz / relativa).exists():
            relativa, n = f"{base}-{n}", n + 1
        pasta = raiz / relativa
        pasta.mkdir(parents=True, exist_ok=True)

        buffer = self._buffer(vista)
        inicio_clipe = max(inicio - self.p.pre_s, estado.ultimo_fim_clipe)
        if buffer is not None and (antigo := buffer.mais_antigo()) is not None:
            inicio_clipe = max(inicio_clipe, antigo)
        evento = _EventoAtivo(inicio, inicio, pasta, relativa, inicio_clipe)
        for resultado in estado.historico:
            if resultado.instante >= inicio_clipe:
                self._registrar(evento, resultado, None)
        if estado.candidata is not None and estado.candidata[1] >= inicio_clipe:
            conf, _instante, imagem, deteccoes = estado.candidata
            evento.melhor = (imagem, deteccoes) if conf > 0 else None
            evento.reserva = (0.0, imagem, deteccoes)
        estado.atual = evento
        self._em_andamento.add(vista.chave)
        log.info("Evento iniciado: %s (%s)", camera.nome_vista(vista), relativa)

    def _registrar(self, evento: _EventoAtivo, resultado: ResultadoVista, imagem: np.ndarray | None) -> None:
        deteccoes = tuple(d for d in resultado.deteccoes if d.ausente_s == 0)
        evento.linhas.append((resultado.instante, deteccoes))
        for d in deteccoes:
            if d.id_rastreio is not None and (d.classe != DESCONHECIDO or d.id_rastreio not in evento.classe_por_id):
                evento.classe_por_id[d.id_rastreio] = d.classe
            if d.origem == "ia" and d.classe != DESCONHECIDO and d.confianca > evento.maior_conf:
                evento.maior_conf = d.confianca
                if imagem is not None:
                    evento.melhor = (imagem, deteccoes)
        if imagem is not None:
            fracao = resultado.movimento.fracao
            if evento.reserva is None or fracao > evento.reserva[0]:
                evento.reserva = (fracao, imagem, deteccoes)

    def _avancar(self, estado: _VistaEventos, agora: float) -> None:
        evento = estado.atual
        assert evento is not None
        self._escrever_ate(estado, agora - self.p.fim_s)
        if agora - evento.ultimo_ativo >= self.p.fim_s:
            self._finalizar(estado, evento.ultimo_ativo, agora)
        elif agora - evento.inicio >= self.p.max_s:
            log.info("Evento passou de %.0f s: fechando e abrindo outro se continuar", self.p.max_s)
            self._finalizar(estado, agora, agora)

    def _escrever_ate(self, estado: _VistaEventos, limite: float) -> None:
        """Grava no clipe, a taxa constante, os quadros do buffer até ``limite``."""
        evento = estado.atual
        assert evento is not None
        buffer = self._buffer(estado.vista)
        passo = 1.0 / self.p.fps
        while evento.proximo_tick <= limite:
            quadro = buffer.ate(evento.proximo_tick) if buffer is not None else None
            if quadro is None:
                if evento.escritor is None:  # ainda não há quadros: o clipe começa depois
                    evento.proximo_tick += passo
                    continue
                imagem = evento.ultima_imagem  # câmera caiu no meio: repete o último (mantém o tempo certo)
            else:
                imagem = estado.vista.recortar(quadro.imagem)
            assert imagem is not None
            if evento.escritor is None:
                altura, largura = imagem.shape[:2]
                evento.escritor = EscritorClipe(
                    evento.pasta / ARQUIVO_CLIPE, largura, altura, self.p.fps, self.p.qualidade
                )
                evento.t0 = evento.proximo_tick
            evento.escritor.escrever(imagem)
            evento.ultima_imagem = imagem
            evento.proximo_tick += passo

    def _finalizar(self, estado: _VistaEventos, fim: float, agora: float) -> None:
        evento = estado.atual
        if evento is None:
            return
        fim_clipe = min(fim + self.p.pos_s, agora)
        try:
            self._escrever_ate(estado, fim_clipe)
        finally:
            estado.atual = None
            self._em_andamento.discard(estado.vista.chave)
        estado.ultimo_fim_clipe = fim_clipe
        if evento.escritor is None or evento.t0 is None:
            log.warning("Evento sem quadros de vídeo descartado (%s)", evento.pasta_relativa)
            shutil.rmtree(evento.pasta, ignore_errors=True)
            return
        try:
            evento.escritor.fechar()
            registro = self._salvar_arquivos(estado, evento, fim)
        except Exception:  # noqa: BLE001 (ex.: disco cheio)
            log.exception("Falha ao finalizar o evento %s", evento.pasta_relativa)
            shutil.rmtree(evento.pasta, ignore_errors=True)
            return
        log.info(
            "Evento gravado: %s · %.0f s · %s",
            estado.camera.nome_vista(estado.vista),
            registro.duracao_s,
            ", ".join(f"{c} {n}" for c, n in registro.classes.items()) or "movimento",
        )
        for funcao in list(self._ao_gravar):
            try:
                funcao(registro, estado.camera)
            except Exception:  # noqa: BLE001
                log.exception("Assinante de eventos falhou")

    def _descartar(self, estado: _VistaEventos) -> None:
        evento = estado.atual
        estado.atual = None
        self._em_andamento.discard(estado.vista.chave)
        if evento is not None:
            if evento.escritor is not None:
                try:
                    evento.escritor.fechar()
                except Exception:  # noqa: BLE001
                    pass
            shutil.rmtree(evento.pasta, ignore_errors=True)

    def _salvar_arquivos(self, estado: _VistaEventos, evento: _EventoAtivo, fim: float) -> Evento:
        assert evento.escritor is not None and evento.t0 is not None
        pasta = evento.pasta
        if evento.melhor is not None:
            imagem, deteccoes = evento.melhor
        elif evento.reserva is not None:
            _fracao, imagem, deteccoes = evento.reserva
        else:
            imagem, deteccoes = evento.ultima_imagem, ()
        assert imagem is not None
        salvar_jpg(pasta / ARQUIVO_FOTO_LIMPA, imagem)
        foto = desenhar_caixas(imagem, deteccoes)
        salvar_jpg(pasta / ARQUIVO_FOTO, foto)
        altura, largura = foto.shape[:2]
        miniatura = cv2.resize(
            foto, (LARGURA_MINIATURA, max(1, round(altura * LARGURA_MINIATURA / largura))), interpolation=cv2.INTER_AREA
        )
        salvar_jpg(pasta / ARQUIVO_MINIATURA, miniatura, qualidade=85)

        duracao = evento.escritor.duracao_s
        quadros = [
            {
                "t": round(instante - evento.t0, 3),
                "deteccoes": [
                    {"classe": d.classe, "id": d.id_rastreio, "conf": round(d.confianca, 3),
                     "caixa": [round(v, 5) for v in d.caixa], "origem": d.origem}
                    for d in dets
                ],
            }
            for instante, dets in evento.linhas
            if 0 <= instante - evento.t0 <= duracao
        ]
        dados = {
            "versao": 1,
            "camera": estado.camera.nome,
            "vista": estado.vista.numero,
            "fps": self.p.fps,
            "duracao_s": round(duracao, 3),
            "inicio_clipe": self._relogio(evento.t0).isoformat(timespec="milliseconds"),
            "formato": evento.escritor.formato,
            "quadros": quadros,
        }
        (pasta / ARQUIVO_DETECCOES).write_text(json.dumps(dados, ensure_ascii=False), encoding="utf-8")

        registro = Evento(
            camera_id=estado.camera.id or 0,
            vista=estado.vista.numero,
            inicio=self._relogio(evento.inicio),
            fim=self._relogio(fim),
            classes=dict(Counter(evento.classe_por_id.values())),
            maior_conf=round(evento.maior_conf, 3),
            alerta=evento.alerta,
            pasta=evento.pasta_relativa,
            clipe=ARQUIVO_CLIPE,
            foto=ARQUIVO_FOTO,
            foto_limpa=ARQUIVO_FOTO_LIMPA,
            deteccoes=ARQUIVO_DETECCOES,
        )
        return self.servico.banco.adicionar_evento(registro)

    @staticmethod
    def _relogio(instante: float) -> datetime:
        return relogio(instante)


def relogio(instante: float) -> datetime:
    """``time.monotonic()`` -> data e hora do relógio (com milissegundos)."""
    quando = datetime.now() - timedelta(seconds=time.monotonic() - instante)
    return quando.replace(microsecond=quando.microsecond // 1000 * 1000)
