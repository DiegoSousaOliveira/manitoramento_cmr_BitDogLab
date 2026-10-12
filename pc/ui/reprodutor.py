"""Reprodutor do clipe do evento: decodifica com OpenCV numa thread própria.

Por que OpenCV e não o player do Qt? A especificação pede que o player não
dependa dos codecs instalados no sistema: o OpenCV traz o próprio FFmpeg.

A interface conversa com o reprodutor só por SINAIS (ele vive em outra thread):
peça ``abrir``, ``tocar``, ``pausar``, ``ir_para``, ``passo`` e ``velocidade``;
ele responde com ``aberto`` (duração/quadros/FPS), ``quadro`` (imagem + tempo)
e ``tocando`` (True/False).
"""

from __future__ import annotations

import logging

import cv2
from PySide6.QtCore import QMetaObject, QObject, Qt, QThread, QTimer, Signal, Slot
from PySide6.QtGui import QImage

log = logging.getLogger(__name__)


class Reprodutor(QObject):
    aberto = Signal(float, int, float)  # duração (s), total de quadros, FPS
    quadro = Signal(QImage, float, int)  # imagem, tempo (s), índice
    tocando = Signal(bool)
    erro = Signal(str)

    # pedidos vindos da interface (conectados aos slots abaixo, executados na thread do reprodutor)
    pedir_abrir = Signal(str)
    pedir_tocar = Signal()
    pedir_pausar = Signal()
    pedir_ir_para = Signal(float)
    pedir_passo = Signal(int)
    pedir_velocidade = Signal(float)
    pedir_fechar = Signal()

    def __init__(self) -> None:
        super().__init__()
        self._captura: cv2.VideoCapture | None = None
        self._fps = 12.0
        self._total = 0
        self._indice = -1  # último quadro mostrado
        self._velocidade = 1.0
        self._timer: QTimer | None = None
        self._thread = QThread()
        self._thread.setObjectName("reprodutor")
        self.moveToThread(self._thread)
        self._thread.started.connect(self._comecar)
        conexao = Qt.ConnectionType.QueuedConnection
        self.pedir_abrir.connect(self._abrir, conexao)
        self.pedir_tocar.connect(self._tocar, conexao)
        self.pedir_pausar.connect(self._pausar, conexao)
        self.pedir_ir_para.connect(self._ir_para, conexao)
        self.pedir_passo.connect(self._passo, conexao)
        self.pedir_velocidade.connect(self._definir_velocidade, conexao)
        self.pedir_fechar.connect(self._fechar, conexao)
        self._thread.start()

    def fechar_e_esperar(self) -> None:
        """Fecha o vídeo e SÓ VOLTA quando o arquivo foi solto (necessário antes de apagá-lo:
        no Windows, arquivo aberto não pode ser apagado). Chamar da thread da interface."""
        if self._thread.isRunning():
            QMetaObject.invokeMethod(self, "_fechar", Qt.ConnectionType.BlockingQueuedConnection)

    def encerrar(self) -> None:
        """Para a thread (chamar ao fechar o app)."""
        self.pedir_fechar.emit()
        self._thread.quit()
        self._thread.wait(3000)

    # ------------------------------------------------------------ thread do reprodutor
    @Slot()
    def _comecar(self) -> None:
        self._timer = QTimer()
        self._timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._timer.timeout.connect(self._tique)

    @Slot(str)
    def _abrir(self, caminho: str) -> None:
        self._fechar()
        captura = cv2.VideoCapture(caminho, cv2.CAP_FFMPEG)
        if not captura.isOpened():
            self.erro.emit(f"Não foi possível abrir o vídeo: {caminho}")
            return
        self._captura = captura
        self._fps = captura.get(cv2.CAP_PROP_FPS) or 12.0
        self._total = int(captura.get(cv2.CAP_PROP_FRAME_COUNT))
        self._indice = -1
        self.aberto.emit(self._total / self._fps if self._fps else 0.0, self._total, self._fps)
        self._ler_e_enviar()  # mostra o primeiro quadro (pausado)

    @Slot()
    def _tocar(self) -> None:
        if self._captura is None or self._timer is None:
            return
        if self._indice >= self._total - 1:  # no fim: recomeça
            self._ir_para(0.0)
        self._timer.start(max(5, round(1000 / (self._fps * self._velocidade))))
        self.tocando.emit(True)

    @Slot()
    def _pausar(self) -> None:
        if self._timer is not None and self._timer.isActive():
            self._timer.stop()
        self.tocando.emit(False)

    @Slot(float)
    def _ir_para(self, segundos: float) -> None:
        if self._captura is None:
            return
        indice = max(0, min(self._total - 1, round(segundos * self._fps)))
        self._captura.set(cv2.CAP_PROP_POS_FRAMES, indice)
        self._indice = indice - 1
        self._ler_e_enviar()

    @Slot(int)
    def _passo(self, delta: int) -> None:
        """Quadro a quadro: +1 avança, -1 volta."""
        self._pausar()
        if delta == 1:
            self._ler_e_enviar()
        else:
            self._ir_para(max(0, self._indice + delta) / self._fps)

    @Slot(float)
    def _definir_velocidade(self, fator: float) -> None:
        self._velocidade = fator
        if self._timer is not None and self._timer.isActive():
            self._timer.start(max(5, round(1000 / (self._fps * fator))))

    @Slot()
    def _fechar(self) -> None:
        if self._timer is not None:
            self._timer.stop()
        if self._captura is not None:
            self._captura.release()
            self._captura = None

    @Slot()
    def _tique(self) -> None:
        if not self._ler_e_enviar():
            self._pausar()  # chegou ao fim

    def _ler_e_enviar(self) -> bool:
        if self._captura is None:
            return False
        ok, imagem = self._captura.read()
        if not ok or imagem is None:
            return False
        self._indice += 1
        rgb = cv2.cvtColor(imagem, cv2.COLOR_BGR2RGB)
        altura, largura = rgb.shape[:2]
        qimagem = QImage(rgb.data, largura, altura, rgb.strides[0], QImage.Format.Format_RGB888).copy()
        self.quadro.emit(qimagem, self._indice / self._fps, self._indice)
        return True
