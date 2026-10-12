"""Alertas: aplica a regra de "parado na zona" aos resultados da IA e avisa quem precisa.

Uma thread própria ("alertas"):

1. Cada resultado da IA (assinado no ``ServicoDeteccao``) passa pela ``RegraParado``
   da vista — só câmeras com detecção ligada (a regra precisa dos objetos).
2. A cada ``INTERVALO_S``, mesmo sem resultado novo, a regra "expira" quem sumiu: se a
   câmera cair com um alerta ativo, o LIMPAR sai depois de ``limpar_apos_sumico_s``.
3. ALERTA: o evento atual da vista vira ALERTA (``ServicoEventos.marcar_alerta``),
   ganha uma miniatura do objeto e os assinantes são avisados (interface: toast;
   Fase 6: a placa BitDogLab). LIMPAR e PASSOU também vão aos assinantes.
4. A ``SituacaoParado`` de cada vista (contador "parado 6/8 s" e se há alerta) fica
   disponível para a tela em ``situacao(chave)`` — objetos imutáveis, seguros entre threads.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, replace

import numpy as np

from core.config import Config
from core.modelos import Camera, Vista
from core.servico_cameras import ServicoCameras
from eventos.desenho import recorte_do_objeto
from eventos.gravador import ServicoEventos, relogio
from vision.detector import ResultadoVista
from vision.dwell import AjustesParado, Aviso, RegraParado, SituacaoParado, TipoAviso

log = logging.getLogger(__name__)
Chave = tuple[int, int]

#: De quanto em quanto tempo a regra confere quem sumiu (independe da câmera mandar imagem).
INTERVALO_S = 0.2


@dataclass
class _VistaAlertas:
    vista: Vista
    camera: Camera
    regra: RegraParado
    ultimo_numero: int = -1


class ServicoAlertas:
    """Thread que transforma resultados da IA em ALERTA / LIMPAR / PASSOU."""

    def __init__(self, config: Config, servico: ServicoCameras, eventos: ServicoEventos | None = None) -> None:
        self.config = config
        self.servico = servico
        self.eventos = eventos
        self._fila: queue.Queue[tuple[str, object]] = queue.Queue()
        self._vistas: dict[Chave, _VistaAlertas] = {}  # só a thread "alertas" mexe
        self._trava = threading.Lock()
        self._situacoes: dict[Chave, SituacaoParado] = {}
        self._ativos: dict[tuple[Chave, int], tuple[Aviso, Camera]] = {}
        self._versao = 0
        self._assinantes: list[Callable[[Aviso, Camera], None]] = []
        self._parar = threading.Event()
        self._thread = threading.Thread(target=self._executar, name="alertas", daemon=True)

    # ================================================================ controle (qualquer thread)
    def iniciar(self) -> ServicoAlertas:
        self._thread.start()
        return self

    def parar(self, esperar_s: float = 3.0) -> None:
        self._parar.set()
        if self._thread.is_alive():
            self._thread.join(esperar_s)

    def receber_resultado(self, resultado: ResultadoVista) -> None:
        """Assinante do ``ServicoDeteccao`` (roda na thread da IA: só enfileira)."""
        self._fila.put(("resultado", resultado))

    def definir_cameras(self, cameras: list[Camera]) -> None:
        """Câmeras atuais. Chamar também depois de salvar uma zona (ela é relida do banco)."""
        self._fila.put(("cameras", list(cameras)))

    def reiniciar_vista(self, chave: Chave) -> None:
        """A lente girou (PTZ): a regra recomeça o aquecimento dessa vista."""
        self._fila.put(("reiniciar", chave))

    def assinar(self, funcao: Callable[[Aviso, Camera], None]) -> None:
        """``funcao(aviso, camera)`` a cada ALERTA, LIMPAR e PASSOU (chamada na thread "alertas")."""
        self._assinantes.append(funcao)

    def situacao(self, chave: Chave) -> SituacaoParado | None:
        with self._trava:
            return self._situacoes.get(chave)

    def situacao_da_vista(self, vista: Vista) -> SituacaoParado | None:
        return self.situacao(vista.chave)

    def ativos(self) -> list[tuple[Aviso, Camera]]:
        """Alertas ativos agora (para a placa, Fase 6: reenviar o de maior nível)."""
        with self._trava:
            return list(self._ativos.values())

    # ================================================================ thread "alertas"
    def _executar(self) -> None:
        proximo = time.monotonic() + INTERVALO_S
        while not self._parar.is_set():
            try:
                item: tuple[str, object] | None = self._fila.get(timeout=max(0.01, proximo - time.monotonic()))
            except queue.Empty:
                item = None
            while item is not None:
                try:
                    self._tratar(*item)
                except Exception:  # noqa: BLE001 (um resultado ruim não pode parar os alertas)
                    log.exception("Falha ao tratar %s", item[0])
                try:
                    item = self._fila.get_nowait()
                except queue.Empty:
                    item = None
            agora = time.monotonic()
            if agora >= proximo:
                proximo = agora + INTERVALO_S
                for estado in list(self._vistas.values()):
                    try:
                        self._emitir(estado, estado.regra.expirar(agora), None)
                        self._publicar(estado, agora)
                    except Exception:  # noqa: BLE001
                        log.exception("Falha ao conferir os alertas de %s", estado.camera.nome)

    def _tratar(self, tipo: str, dado: object) -> None:
        if tipo == "resultado":
            self._resultado(dado)  # type: ignore[arg-type]
        elif tipo == "cameras":
            self._aplicar_cameras(dado)  # type: ignore[arg-type]
        elif tipo == "reiniciar":
            if (estado := self._vistas.get(dado)) is not None:  # type: ignore[arg-type]
                estado.regra.reiniciar(time.monotonic())
                log.info("Câmera girou: aquecimento da regra de parado reiniciado (%s)", estado.camera.nome_vista(estado.vista))

    def _resultado(self, resultado: ResultadoVista) -> None:
        estado = self._vistas.get(resultado.chave)
        if estado is None:
            return
        if resultado.numero < estado.ultimo_numero:
            # A IA recriou a vista (ex.: ajuste mudou): os IDs recomeçam do 1. Começa do zero.
            self._emitir(estado, estado.regra.encerrar(resultado.instante), None)
        estado.ultimo_numero = resultado.numero
        largura, altura = resultado.resolucao
        proporcao = altura / largura if largura > 0 and altura > 0 else 1.0
        avisos = estado.regra.atualizar(resultado.deteccoes, resultado.instante, proporcao)
        self._emitir(estado, avisos, resultado.imagem)
        self._publicar(estado, resultado.instante)

    def _aplicar_cameras(self, cameras: list[Camera]) -> None:
        agora = time.monotonic()
        novas: dict[Chave, tuple[Vista, Camera, AjustesParado, list[tuple[float, float]] | None]] = {}
        for camera in cameras:
            if not camera.deteccao or camera.id is None:
                continue  # sem detecção não há objetos para vigiar
            try:
                ajustes = AjustesParado.do_config(self.config, camera)
            except ValueError as erro:
                log.error("Ajustes de alerta inválidos em %s (usando os do config.yaml): %s", camera.nome, erro)
                ajustes = AjustesParado.do_config(self.config)
            for vista in camera.vistas():
                novas[vista.chave] = (vista, camera, ajustes, self.servico.banco.obter_zona(camera.id, vista.numero))
        for chave in list(self._vistas):
            estado = self._vistas[chave]
            if chave not in novas or novas[chave][0] != estado.vista:
                self._emitir(estado, estado.regra.encerrar(agora), None)  # LIMPAR o que estava ativo
                del self._vistas[chave]
                with self._trava:
                    self._situacoes.pop(chave, None)
        for chave, (vista, camera, ajustes, zona) in novas.items():
            estado = self._vistas.get(chave)
            if estado is None:
                self._vistas[chave] = _VistaAlertas(vista, camera, RegraParado(chave, ajustes, zona))
            else:  # mantém os objetos acompanhados; só troca ajustes e zona
                estado.camera, estado.regra.ajustes, estado.regra.zona = camera, ajustes, zona

    def _emitir(self, estado: _VistaAlertas, avisos: list[Aviso], imagem: np.ndarray | None) -> None:
        camera = estado.camera
        nome = camera.nome_vista(estado.vista)
        for aviso in avisos:
            aviso = replace(aviso, quando=relogio(aviso.instante))
            chave_ativo = (aviso.chave, aviso.id_rastreio)
            if aviso.tipo is TipoAviso.ALERTA:
                if imagem is not None:
                    aviso = replace(aviso, miniatura=recorte_do_objeto(imagem, aviso.caixa))
                with self._trava:
                    self._ativos[chave_ativo] = (aviso, camera)
                if self.eventos is not None:
                    self.eventos.marcar_alerta(aviso.chave, aviso.instante - aviso.segundos)
                log.warning(
                    "ALERTA: %s #%d parado há %.0f s em %s (nível %s)",
                    aviso.classe, aviso.id_rastreio, aviso.segundos, nome, aviso.nivel.name,
                )
            elif aviso.tipo is TipoAviso.LIMPAR:
                with self._trava:
                    self._ativos.pop(chave_ativo, None)
                log.info("LIMPAR: %s #%d em %s (%s)", aviso.classe, aviso.id_rastreio, nome, aviso.motivo)
            else:
                log.info("PASSOU: %s #%d em %s", aviso.classe, aviso.id_rastreio, nome)
            for funcao in list(self._assinantes):
                try:
                    funcao(aviso, camera)
                except Exception:  # noqa: BLE001
                    log.exception("Assinante de alertas falhou")

    def _publicar(self, estado: _VistaAlertas, agora: float) -> None:
        """Atualiza a situação da vista para a tela (só cria um objeto novo se algo mudou)."""
        chave = estado.vista.chave
        progresso = estado.regra.progresso(agora)
        alerta = bool(estado.regra.ativos())
        with self._trava:
            anterior = self._situacoes.get(chave)
            if anterior is not None and anterior.progresso == progresso and anterior.alerta == alerta:
                return
            self._versao += 1
            self._situacoes[chave] = SituacaoParado(self._versao, progresso, alerta)
