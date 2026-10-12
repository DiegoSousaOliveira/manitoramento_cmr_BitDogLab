"""Barra lateral: navegação (Câmeras, Eventos, Configurações) e status no rodapé.

Rodapé: status da placa BitDogLab (verde = conectada; detalhes na dica) e uso de CPU/GPU,
atualizados a cada 2 s. A GPU é lida pela biblioteca da NVIDIA (``pynvml``);
sem GPU NVIDIA, o indicador some.
"""

from __future__ import annotations

import logging

import psutil
from PySide6.QtCore import QEvent, QObject, QSize, Qt, QTimer, Signal
from PySide6.QtWidgets import QButtonGroup, QFrame, QLabel, QToolButton, QVBoxLayout, QWidget

from ui import tema

log = logging.getLogger(__name__)

PAGINAS: list[tuple[str, str, str]] = [
    ("cameras", "mdi6.view-grid-outline", "Câmeras"),
    ("eventos", "mdi6.bell-outline", "Eventos"),
    ("treino", "mdi6.school-outline", "Treino"),
    ("config", "mdi6.cog-outline", "Ajustes"),
]


class _MonitorGpu:
    """Lê o uso da GPU NVIDIA; desliga sozinho se não houver GPU/driver."""

    def __init__(self) -> None:
        self._handle = None
        try:
            import pynvml

            pynvml.nvmlInit()
            self._pynvml = pynvml
            self._handle = pynvml.nvmlDeviceGetHandleByIndex(0)
            self.nome = str(pynvml.nvmlDeviceGetName(self._handle))
        except Exception:  # noqa: BLE001 (sem GPU NVIDIA, sem driver ou sem a biblioteca)
            self.nome = ""
            log.debug("Uso de GPU indisponível", exc_info=True)

    @property
    def disponivel(self) -> bool:
        return self._handle is not None

    def uso(self) -> int | None:
        if self._handle is None:
            return None
        try:
            return int(self._pynvml.nvmlDeviceGetUtilizationRates(self._handle).gpu)
        except Exception:  # noqa: BLE001
            return None


class BarraLateral(QFrame):
    pagina_escolhida = Signal(str)
    placa_clicada = Signal()  # clique no status da placa: abre Ajustes → Placa

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("barraLateral")
        self.setFixedWidth(76)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 16, 8, 14)
        layout.setSpacing(6)

        self._logo = QLabel()
        self._logo.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._logo.setToolTip("Sentinela Casa")
        layout.addWidget(self._logo)
        layout.addSpacing(18)

        self._grupo = QButtonGroup(self)
        self._botoes: dict[str, QToolButton] = {}
        for nome, icone, texto in PAGINAS:
            botao = QToolButton()
            botao.setObjectName("itemLateral")
            botao.setText(texto)
            botao.setToolTip(texto)
            botao.setCheckable(True)
            botao.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextUnderIcon)
            botao.setIconSize(QSize(22, 22))
            botao.setFixedSize(60, 58)
            botao.setCursor(Qt.CursorShape.PointingHandCursor)
            botao.setStyleSheet("font-size: 11px;")
            botao.setProperty("icone", icone)
            botao.clicked.connect(lambda _c=False, n=nome: self.pagina_escolhida.emit(n))
            self._grupo.addButton(botao)
            self._botoes[nome] = botao
            layout.addWidget(botao, alignment=Qt.AlignmentFlag.AlignHCenter)
        layout.addStretch(1)

        self._placa = QLabel()
        self._placa.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._texto_placa = QLabel("Placa")
        self._texto_placa.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._texto_placa.setProperty("papel", "fraco")
        for rotulo_placa in (self._placa, self._texto_placa):  # clicar abre Ajustes → Placa
            rotulo_placa.setCursor(Qt.CursorShape.PointingHandCursor)
            rotulo_placa.installEventFilter(self)
        layout.addWidget(self._placa)
        layout.addWidget(self._texto_placa)
        layout.addSpacing(10)

        self._ia = QLabel("IA …")
        self._ia.setToolTip("Detecção de objetos (YOLO): carregando…")
        layout.addWidget(self._ia)
        layout.addSpacing(4)
        self._cpu = QLabel("CPU –")
        self._gpu = QLabel("GPU –")
        for rotulo in (self._ia, self._cpu, self._gpu):
            rotulo.setAlignment(Qt.AlignmentFlag.AlignCenter)
            rotulo.setProperty("papel", "fraco")
            layout.addWidget(rotulo)

        self._monitor_gpu = _MonitorGpu()
        self._gpu.setVisible(self._monitor_gpu.disponivel)
        if self._monitor_gpu.disponivel:
            self._gpu.setToolTip(self._monitor_gpu.nome)
        psutil.cpu_percent(None)  # a primeira leitura só "zera" o contador
        self._timer = QTimer(self)
        self._timer.setInterval(2000)
        self._timer.timeout.connect(self._atualizar_uso)
        self._timer.start()

        self._placa_conectada = False
        self._detalhe_placa = "BitDogLab desconectada"
        self._aplicar_icones()
        # Método (e não lambda): o Qt desliga sozinho quando o widget é destruído.
        tema.sinais.tema_mudou.connect(self._aplicar_icones)

    def eventFilter(self, objeto: QObject, evento: QEvent) -> bool:  # noqa: N802
        if objeto in (self._placa, self._texto_placa) and evento.type() == QEvent.Type.MouseButtonRelease:
            self.placa_clicada.emit()
            return True
        return False

    def definir_pagina(self, nome: str) -> None:
        if nome in self._botoes:
            self._botoes[nome].setChecked(True)

    def definir_placa(self, conectada: bool, detalhe: str) -> None:
        if (conectada, detalhe) == (self._placa_conectada, self._detalhe_placa):
            return
        self._placa_conectada = conectada
        self._detalhe_placa = detalhe
        self._aplicar_icones()

    @property
    def placa_conectada(self) -> bool:
        return self._placa_conectada

    @property
    def detalhe_placa(self) -> str:
        return self._detalhe_placa

    def definir_ia(self, texto: str, dica: str, cor: str) -> None:
        """Status da IA no rodapé (cor = nome de cor da paleta: sucesso, aviso, alerta...)."""
        self._ia.setText(texto)
        self._ia.setToolTip(dica)
        self._ia.setStyleSheet(f"color: {getattr(tema.paleta_atual(), cor)}; font-weight: 600;")

    def _aplicar_icones(self, _paleta: object = None) -> None:
        self._logo.setPixmap(tema.icone("mdi6.shield-home-outline", "destaque").pixmap(30, 30))
        for botao in self._botoes.values():
            botao.setIcon(tema.icone(str(botao.property("icone")), "texto_fraco", cor_ativa="destaque"))
        cor = "sucesso" if self._placa_conectada else "offline"
        self._placa.setPixmap(tema.icone("mdi6.chip", cor).pixmap(22, 22))
        self._placa.setToolTip(self._detalhe_placa)
        self._texto_placa.setToolTip(self._detalhe_placa)
        self._texto_placa.setText("Placa ✓" if self._placa_conectada else "Placa")
        self._texto_placa.setStyleSheet(
            f"color: {tema.paleta_atual().sucesso}; font-weight: 600;" if self._placa_conectada else ""
        )

    def _atualizar_uso(self) -> None:
        self._cpu.setText(f"CPU {psutil.cpu_percent(None):.0f}%")
        uso = self._monitor_gpu.uso()
        if uso is not None:
            self._gpu.setText(f"GPU {uso}%")
