"""Prepara as imagens do mosaico FORA da thread da interface e entrega por sinais Qt.

A cada tique (a cada ~15 ms), para cada bloco VISÍVEL:
1. pega o quadro que deve aparecer AGORA (``quadro_para_exibir``: suavizado contra as rajadas
   do Wi-Fi), se for novo e se já passou o intervalo mínimo do bloco (``interface.fps_mosaico``);
2. recorta a lente da vista (câmeras de duas lentes numa imagem);
3. redimensiona para o tamanho exato do bloco e converte BGR -> RGB;
4. cria a ``QImage`` e emite ``quadro_pronto``.

Também entrega o resultado mais recente da IA de cada vista (``deteccoes_prontas``),
a situação dos alertas (``parado_pronto``: contador "parado 6/8 s" e borda vermelha)
e, se pedido, a máscara de movimento já convertida em imagem (``mascara_pronta``).

Tudo isso roda numa ``QThread`` própria. A thread da interface só recebe a
imagem pronta e desenha — por isso o mosaico continua fluido mesmo com várias
câmeras. Blocos escondidos (outra página, janela minimizada, outra tela) não
gastam nada além de uma comparação.
"""

from __future__ import annotations

import logging
import math
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

import cv2
import numpy as np
from PySide6.QtCore import QObject, Qt, QThread, QTimer, Signal, Slot
from PySide6.QtGui import QImage

from camera.stream import EstadoLeitor, LeitorVideo
from core.modelos import Vista

Chave = tuple[int, int]


@dataclass(frozen=True)
class Alvo:
    """Onde e em que tamanho uma vista está sendo exibida."""

    vista: Vista
    largura: int  # em pixels físicos da tela
    altura: int
    escala_tela: float  # devicePixelRatio (telas com zoom de 125%, 150%...)
    visivel: bool
    mascara: bool = False  # também enviar a máscara de movimento


log = logging.getLogger(__name__)
INTERVALO_AVISO_S = 10.0  # falha repetida (ex.: memória acabando) vira no máximo 1 aviso a cada X s
#: O tique é curto para cada quadro aparecer perto da hora certa; converter imagem só acontece quando
#: chega a vez de um quadro novo (no máximo ``fps`` vezes por segundo por bloco).
TIQUE_MS = 15

#: Cor da máscara de movimento (RGB) e opacidade (0 a 255).
COR_MASCARA = (34, 195, 230)
OPACIDADE_MASCARA = 120


def mascara_para_qimage(mascara: np.ndarray) -> QImage:
    """Máscara 0/255 -> imagem semitransparente colorida (pintada por cima do vídeo)."""
    altura, largura = mascara.shape[:2]
    rgba = np.zeros((altura, largura, 4), dtype=np.uint8)
    rgba[..., :3] = COR_MASCARA
    rgba[..., 3] = np.where(mascara > 0, OPACIDADE_MASCARA, 0).astype(np.uint8)
    return QImage(rgba.data, largura, altura, largura * 4, QImage.Format.Format_RGBA8888).copy()


def converter_para_qimage(imagem_bgr: np.ndarray, largura: int, altura: int) -> QImage:
    """Encaixa a imagem em largura x altura (sem distorcer) e devolve uma ``QImage`` RGB."""
    alt_orig, larg_orig = imagem_bgr.shape[:2]
    escala = min(largura / larg_orig, altura / alt_orig)
    nova_l, nova_a = max(1, round(larg_orig * escala)), max(1, round(alt_orig * escala))
    if (nova_l, nova_a) != (larg_orig, alt_orig):
        metodo = cv2.INTER_AREA if escala < 1 else cv2.INTER_LINEAR  # AREA é a melhor para reduzir
        imagem_bgr = cv2.resize(imagem_bgr, (nova_l, nova_a), interpolation=metodo)
    rgb = cv2.cvtColor(imagem_bgr, cv2.COLOR_BGR2RGB)
    # .copy(): a QImage passa a ter sua própria memória (o array do NumPy pode sumir depois)
    return QImage(rgb.data, nova_l, nova_a, rgb.strides[0], QImage.Format.Format_RGB888).copy()


class DistribuidorQuadros(QObject):
    """Converte quadros em ``QImage`` numa thread própria, no ritmo do mosaico."""

    quadro_pronto = Signal(object, QImage)  # chave da vista, imagem
    estado_mudou = Signal(object, str, str, int)  # chave, estado, mensagem, segundos até nova tentativa
    deteccoes_prontas = Signal(object, object)  # chave, ResultadoVista (ou None)
    mascara_pronta = Signal(object, object)  # chave, QImage (ou None)
    parado_pronto = Signal(object, object)  # chave, SituacaoParado (ou None)
    _pedir_intervalo = Signal(int)  # interno: troca o ritmo dentro da thread do distribuidor

    def __init__(
        self,
        obter_leitor: Callable[[Vista], LeitorVideo | None],
        fps: float,
        obter_resultado: Callable[[Vista], object | None] | None = None,
        obter_parado: Callable[[Vista], object | None] | None = None,
    ) -> None:
        """``obter_leitor``: de onde vêm os quadros de cada vista (substream no mosaico;
        o principal, quando disponível, na Tela da câmera). ``obter_resultado``: último
        resultado da IA da vista (``ServicoDeteccao.resultado_da_vista``).
        ``obter_parado``: situação dos alertas (``ServicoAlertas.situacao_da_vista``)."""
        super().__init__()
        self._obter_leitor = obter_leitor
        self._obter_resultado = obter_resultado
        self._obter_parado = obter_parado
        self._ultimo_resultado: dict[Chave, int | None] = {}
        self._ultimo_parado: dict[Chave, int | None] = {}
        self._intervalo_ms = max(15, int(1000 / max(1.0, fps)))
        self._trava = threading.Lock()
        self._alvos: dict[Chave, Alvo] = {}
        # último quadro enviado: (id do leitor, número) — o leitor pode trocar (substream -> principal)
        self._ultimo_numero: dict[Chave, tuple[int, int]] = {}
        self._ultima_conversao: dict[Chave, float] = {}
        self._ultimo_estado: dict[Chave, tuple[str, str, int]] = {}
        self._ultimo_aviso_falha = -INTERVALO_AVISO_S
        self._timer: QTimer | None = None
        self._thread = QThread()
        self._thread.setObjectName("distribuidor-quadros")
        self.moveToThread(self._thread)
        self._thread.started.connect(self._comecar)
        self._thread.finished.connect(self._terminar)
        self._pedir_intervalo.connect(self._aplicar_intervalo)

    # ------------------------------------------------------------ chamadas pela interface
    def iniciar(self) -> None:
        self._thread.start()

    def parar(self) -> None:
        self._thread.quit()
        self._thread.wait(3000)

    def definir_fps(self, fps: float) -> None:
        """Novo ritmo (Ajustes). O timer vive na thread do distribuidor: o pedido vai por sinal."""
        self._intervalo_ms = max(15, int(1000 / max(1.0, fps)))
        self._pedir_intervalo.emit(self._intervalo_ms)

    @property
    def intervalo_ms(self) -> int:
        return self._intervalo_ms

    def definir_alvos(self, alvos: list[Alvo]) -> None:
        """Substitui a lista de vistas acompanhadas (chamado a cada mudança no mosaico)."""
        with self._trava:
            novos = {a.vista.chave: a for a in alvos}
            for chave, alvo in novos.items():
                antigo = self._alvos.get(chave)
                if antigo is None or (antigo.largura, antigo.altura, antigo.visivel, antigo.mascara) != (
                    alvo.largura, alvo.altura, alvo.visivel, alvo.mascara
                ):
                    self._ultimo_numero.pop(chave, None)  # tamanho mudou: reenviar mesmo sem quadro novo
                    self._ultimo_resultado.pop(chave, None)
                    self._ultimo_parado.pop(chave, None)
            for chave in set(self._alvos) - set(novos):
                self._ultimo_numero.pop(chave, None)
                self._ultimo_estado.pop(chave, None)
                self._ultimo_resultado.pop(chave, None)
                self._ultimo_parado.pop(chave, None)
            self._alvos = novos

    # ------------------------------------------------------------ thread do distribuidor
    @Slot()
    def _comecar(self) -> None:
        self._timer = QTimer()
        self._timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._timer.timeout.connect(self._tique)
        self._timer.start(min(TIQUE_MS, self._intervalo_ms))

    @Slot(int)
    def _aplicar_intervalo(self, intervalo_ms: int) -> None:
        if self._timer is not None:
            self._timer.setInterval(min(TIQUE_MS, intervalo_ms))

    @Slot()
    def _terminar(self) -> None:
        if self._timer is not None:
            self._timer.stop()
            self._timer.deleteLater()
            self._timer = None

    @Slot()
    def _tique(self) -> None:
        with self._trava:
            alvos = list(self._alvos.values())
            ultimos = dict(self._ultimo_numero)
        for alvo in alvos:
            chave = alvo.vista.chave
            leitor = self._obter_leitor(alvo.vista)
            if leitor is None:
                self._avisar_estado(chave, EstadoLeitor.OFFLINE.value, "sem leitor", 0)
                continue
            self._avisar_estado(chave, leitor.estado.value, leitor.mensagem, math.ceil(leitor.segundos_para_tentar()))
            if not alvo.visivel or alvo.largura < 8 or alvo.altura < 8:
                continue
            self._enviar_resultado(alvo)
            agora = time.monotonic()
            if agora - self._ultima_conversao.get(chave, 0.0) < self._intervalo_ms / 1000 * 0.8:
                continue  # este bloco já foi atualizado há pouco (limite de atualizações por segundo)
            quadro = leitor.quadro_para_exibir(agora)
            if quadro is None or (id(leitor), quadro.numero) == ultimos.get(chave):
                continue
            self._ultima_conversao[chave] = agora
            try:
                imagem = converter_para_qimage(alvo.vista.recortar(quadro.imagem), alvo.largura, alvo.altura)
            except (cv2.error, MemoryError) as erro:
                # Ex.: o computador ficou sem memória por um instante (12/10/2026, durante um treino).
                # Pula só este quadro; os outros blocos e os próximos quadros seguem normalmente.
                self._avisar_falha(chave, erro)
                continue
            imagem.setDevicePixelRatio(alvo.escala_tela)
            with self._trava:
                if chave not in self._alvos:  # removida enquanto convertíamos
                    continue
                self._ultimo_numero[chave] = (id(leitor), quadro.numero)
            self.quadro_pronto.emit(chave, imagem)

    def _enviar_resultado(self, alvo: Alvo) -> None:
        """Manda o resultado da IA quando ele muda (e a máscara, se pedida) e a situação dos alertas."""
        chave = alvo.vista.chave
        if self._obter_parado is not None:
            situacao = self._obter_parado(alvo.vista)
            versao = getattr(situacao, "versao", None)
            with self._trava:
                mudou = chave not in self._ultimo_parado or self._ultimo_parado[chave] != versao
                self._ultimo_parado[chave] = versao
            if mudou:
                self.parado_pronto.emit(chave, situacao)
        if self._obter_resultado is None:
            return
        resultado = self._obter_resultado(alvo.vista)
        marca = getattr(resultado, "numero", None)
        with self._trava:
            if chave in self._ultimo_resultado and self._ultimo_resultado[chave] == marca:
                return
            self._ultimo_resultado[chave] = marca
        self.deteccoes_prontas.emit(chave, resultado)
        if alvo.mascara:
            movimento = getattr(resultado, "movimento", None)
            mascara = getattr(movimento, "mascara", None)
            self.mascara_pronta.emit(chave, mascara_para_qimage(mascara) if mascara is not None else None)

    def _avisar_falha(self, chave: Chave, erro: BaseException) -> None:
        agora = time.monotonic()
        if agora - self._ultimo_aviso_falha >= INTERVALO_AVISO_S:
            self._ultimo_aviso_falha = agora
            log.warning("Quadro de %s pulado: %s", chave, str(erro).strip().splitlines()[-1] if str(erro) else
                        type(erro).__name__)

    def _avisar_estado(self, chave: Chave, estado: str, mensagem: str, segundos: int) -> None:
        atual = (estado, mensagem, segundos)
        if self._ultimo_estado.get(chave) != atual:
            self._ultimo_estado[chave] = atual
            self.estado_mudou.emit(chave, estado, mensagem, segundos)
