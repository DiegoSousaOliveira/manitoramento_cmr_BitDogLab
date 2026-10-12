"""Painel PTZ da Tela da câmera: setas, parar, posições salvas e o indicador de direção.

- **Segurar** uma seta move a câmera; **soltar** para. Enquanto segura, o comando é
  repetido a cada ``REPETIR_MS`` (o serviço para a câmera se ficar 3 s sem comando).
- O **indicador** (bolinha dentro do círculo) mostra a velocidade pedida agora — venha
  ela das setas, do teclado ou do joystick da placa. Com o PTZ simulado, é ele que
  "se mexe".
- "Posições": ir para / salvar as posições 1 a 4 (presets da câmera). Dica: salve na 1
  a posição em que você desenhou a zona.
"""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QPainter, QPaintEvent, QPen
from PySide6.QtWidgets import QFrame, QGridLayout, QHBoxLayout, QMenu, QToolButton, QVBoxLayout, QWidget

from camera.ptz import PRESETS, EstadoPTZ
from ui import tema
from ui.componentes import rotulo

REPETIR_MS = 1000

#: botão -> (ícone, direção x, direção y, dica)
SETAS: dict[str, tuple[str, int, int, str]] = {
    "cima": ("mdi6.chevron-up", 0, 1, "Inclinar para cima (seta ↑)"),
    "baixo": ("mdi6.chevron-down", 0, -1, "Inclinar para baixo (seta ↓)"),
    "esquerda": ("mdi6.chevron-left", -1, 0, "Girar para a esquerda (seta ←)"),
    "direita": ("mdi6.chevron-right", 1, 0, "Girar para a direita (seta →)"),
}


class IndicadorPTZ(QWidget):
    """Círculo com a "bolinha" do joystick: mostra para onde a câmera está sendo movida."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedSize(76, 76)
        self.x, self.y, self.movendo = 0.0, 0.0, False

    def definir(self, x: float, y: float, movendo: bool) -> None:
        if (x, y, movendo) != (self.x, self.y, self.movendo):
            self.x, self.y, self.movendo = x, y, movendo
            self.update()

    def paintEvent(self, _evento: QPaintEvent) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        area = QRectF(self.rect()).adjusted(4, 4, -4, -4)
        p.setPen(QPen(tema.cor("borda"), 1.5))
        p.setBrush(QColor(0, 0, 0, 90))
        p.drawEllipse(area)
        centro = area.center()
        raio = area.width() / 2 - 9
        p.setPen(QPen(tema.cor("texto_fraco"), 1, Qt.PenStyle.DotLine))
        p.drawLine(QPointF(area.left() + 6, centro.y()), QPointF(area.right() - 6, centro.y()))
        p.drawLine(QPointF(centro.x(), area.top() + 6), QPointF(centro.x(), area.bottom() - 6))
        bolinha = QPointF(centro.x() + self.x * raio, centro.y() - self.y * raio)  # y > 0 = para cima
        if self.movendo:
            p.setPen(QPen(tema.cor("destaque", 0.7), 2))
            p.drawLine(centro, bolinha)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(tema.cor("destaque") if self.movendo else tema.cor("texto_suave"))
        p.drawEllipse(bolinha, 7, 7)


class ControlesPTZ(QFrame):
    mover = Signal(float, float)
    parar = Signal()
    ir_preset = Signal(int)
    salvar_preset = Signal(int)

    def __init__(self, velocidade: float, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("controlesPTZ")
        self._velocidade = velocidade
        self._segurando: tuple[float, float] | None = None
        self._repetir = QTimer(self)
        self._repetir.setInterval(REPETIR_MS)
        self._repetir.timeout.connect(self._repetir_movimento)

        self.titulo = rotulo("PTZ", "secao")
        self.tipo = rotulo("", "fraco")
        cabecalho = QHBoxLayout()
        cabecalho.setSpacing(6)
        cabecalho.addWidget(self.titulo)
        cabecalho.addStretch(1)
        cabecalho.addWidget(self.tipo)

        self.botoes: dict[str, QToolButton] = {}
        grade = QGridLayout()
        grade.setSpacing(2)
        posicoes = {"cima": (0, 1), "esquerda": (1, 0), "direita": (1, 2), "baixo": (2, 1)}
        for nome, (linha, coluna) in posicoes.items():
            _icone, dx, dy, dica = SETAS[nome]
            botao = self._botao(dica)
            botao.pressed.connect(lambda dx=dx, dy=dy: self._segurar(dx, dy))
            botao.released.connect(self._soltar)
            self.botoes[nome] = botao
            grade.addWidget(botao, linha, coluna)
        self.botao_parar = self._botao("Parar a câmera")
        self.botao_parar.clicked.connect(self._parar_agora)
        grade.addWidget(self.botao_parar, 1, 1)

        self.indicador = IndicadorPTZ()
        meio = QHBoxLayout()
        meio.setSpacing(12)
        meio.addLayout(grade)
        meio.addWidget(self.indicador, alignment=Qt.AlignmentFlag.AlignVCenter)

        self.botao_posicoes = QToolButton()
        self.botao_posicoes.setText("Posições")
        self.botao_posicoes.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.botao_posicoes.setToolTipDuration(8000)
        self.botao_posicoes.setToolTip("Ir para uma posição salva da câmera, ou salvar a posição atual.\n"
                                       "Dica: salve na 1 a posição em que você desenhou a zona.")
        menu = QMenu(self.botao_posicoes)
        for numero in PRESETS:
            menu.addAction(f"Ir para a posição {numero}", lambda n=numero: self.ir_preset.emit(n))
        menu.addSeparator()
        for numero in PRESETS:
            menu.addAction(f"Salvar a posição atual como {numero}", lambda n=numero: self.salvar_preset.emit(n))
        self.botao_posicoes.setMenu(menu)

        self.aviso = rotulo("", "erro", quebra=True)
        self.aviso.setMaximumWidth(230)
        self.aviso.hide()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 12)
        layout.setSpacing(8)
        layout.addLayout(cabecalho)
        layout.addLayout(meio)
        layout.addWidget(self.botao_posicoes)
        layout.addWidget(self.aviso)
        self._aplicar_tema()
        tema.sinais.tema_mudou.connect(self._aplicar_tema)

    def _botao(self, dica: str) -> QToolButton:
        botao = QToolButton()
        botao.setToolTip(dica)
        botao.setFixedSize(QSize(34, 34))
        botao.setIconSize(QSize(22, 22))
        botao.setCursor(Qt.CursorShape.PointingHandCursor)
        botao.setAutoRepeat(False)
        return botao

    def _aplicar_tema(self, _paleta: object = None) -> None:
        p = tema.paleta_atual()
        fundo = tema.cor("superficie", 0.88)  # um pouco transparente: o vídeo aparece por trás
        self.setStyleSheet(
            f"#controlesPTZ {{ background: rgba({fundo.red()}, {fundo.green()}, {fundo.blue()}, {fundo.alpha()});"
            f" border: 1px solid {p.borda}; border-radius: 12px; }}"
        )
        for nome, botao in self.botoes.items():
            botao.setIcon(tema.icone(SETAS[nome][0], "texto"))
        self.botao_parar.setIcon(tema.icone("mdi6.stop", "alerta"))
        self.botao_posicoes.setIcon(tema.icone("mdi6.map-marker-outline", "texto"))
        self.botao_posicoes.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)

    # ------------------------------------------------------------ comandos
    def segurar_direcao(self, dx: int, dy: int) -> None:
        """Também usado pelo teclado (setas)."""
        self._segurar(dx, dy)

    def soltar(self) -> None:
        self._soltar()

    def _segurar(self, dx: int, dy: int) -> None:
        self._segurando = (dx * self._velocidade, dy * self._velocidade)
        self.mover.emit(*self._segurando)
        self._repetir.start()

    def _repetir_movimento(self) -> None:
        if self._segurando is not None:
            self.mover.emit(*self._segurando)

    def _soltar(self) -> None:
        if self._segurando is not None:
            self._segurando = None
            self._repetir.stop()
            self.parar.emit()

    def _parar_agora(self) -> None:
        self._segurando = None
        self._repetir.stop()
        self.parar.emit()

    @property
    def segurando(self) -> bool:
        return self._segurando is not None

    # ------------------------------------------------------------ estado vindo do serviço
    def mostrar_estado(self, estado: EstadoPTZ | None) -> None:
        if estado is None:
            self.tipo.setText("")
            self.indicador.definir(0.0, 0.0, False)
            return
        texto = {"onvif": "ONVIF", "falso": "simulado", "conectando": "conectando…"}.get(estado.tipo, estado.tipo)
        if self.tipo.text() != texto:
            self.tipo.setText(texto)
            self.tipo.setToolTip(estado.descricao)
        self.indicador.definir(estado.x, estado.y, estado.movendo)
        if estado.aviso != self.aviso.text():
            self.aviso.setText(estado.aviso)
            self.aviso.setVisible(bool(estado.aviso))
            self.adjustSize()
