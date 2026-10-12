"""Pequenos componentes reutilizados pelas telas (interruptor, campo de senha, seções...)."""

from __future__ import annotations

from collections.abc import Sequence

from PySide6.QtCore import Property, QEasingCurve, QPropertyAnimation, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QAction, QIntValidator, QPainter, QPaintEvent
from PySide6.QtWidgets import (
    QAbstractButton,
    QButtonGroup,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ui import tema


def rotulo(texto: str, papel: str | None = None, quebra: bool = False) -> QLabel:
    """``QLabel`` com papel de estilo (``"titulo"``, ``"suave"``, ``"erro"``, ``"secao"``...)."""
    label = QLabel(texto)
    if papel:
        label.setProperty("papel", papel)
    label.setWordWrap(quebra)
    return label


def repolir(widget: QWidget) -> None:
    """Reaplica o QSS depois de mudar uma propriedade dinâmica (ex.: ``erro=true``)."""
    widget.style().unpolish(widget)
    widget.style().polish(widget)


class Cartao(QFrame):
    """Painel com fundo, borda e cantos arredondados."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("cartao")


class Interruptor(QAbstractButton):
    """Chave liga/desliga (estilo celular), com animação suave do "botãozinho"."""

    def __init__(self, texto: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setCheckable(True)
        self.setText(texto)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._posicao = 0.0
        self._animacao = QPropertyAnimation(self, b"posicao", self)
        self._animacao.setDuration(140)
        self._animacao.setEasingCurve(QEasingCurve.Type.OutCubic)
        self.toggled.connect(self._animar)

    def _animar(self, ligado: bool) -> None:
        self._animacao.stop()
        self._animacao.setEndValue(1.0 if ligado else 0.0)
        self._animacao.start()

    def setChecked(self, ligado: bool) -> None:  # noqa: N802 (API do Qt)
        super().setChecked(ligado)
        self._animacao.stop()
        self._posicao = 1.0 if ligado else 0.0
        self.update()

    def _ler_posicao(self) -> float:
        return self._posicao

    def _gravar_posicao(self, valor: float) -> None:
        self._posicao = valor
        self.update()

    posicao = Property(float, _ler_posicao, _gravar_posicao)

    def sizeHint(self) -> QSize:  # noqa: N802
        largura_texto = self.fontMetrics().horizontalAdvance(self.text()) + 10 if self.text() else 0
        return QSize(38 + largura_texto, 24)

    def paintEvent(self, _evento: QPaintEvent) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        trilho = QRectF(1, (self.height() - 20) / 2, 36, 20)
        desligado, ligado = tema.cor("borda"), tema.cor("destaque")
        mistura = ligado if self._posicao > 0.5 else desligado
        if not self.isEnabled():
            mistura = tema.cor("borda", 0.6)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(mistura)
        p.drawRoundedRect(trilho, 10, 10)
        x = trilho.left() + 3 + self._posicao * (trilho.width() - 20)
        p.setBrush(tema.cor("texto") if not self.isChecked() else tema.cor("destaque_texto"))
        p.drawEllipse(QRectF(x, trilho.top() + 3, 14, 14))
        if self.text():
            p.setPen(tema.cor("texto" if self.isEnabled() else "texto_fraco"))
            area = self.rect().adjusted(int(trilho.right()) + 10, 0, 0, 0)
            p.drawText(area, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, self.text())


class CampoNumero(QLineEdit):
    """Campo que só aceita números inteiros (ex.: portas), com o mesmo visual dos outros campos.

    Usamos ``QLineEdit`` em vez de ``QSpinBox`` porque o ``QSpinBox`` sem setinhas
    tem um defeito no Qt com folhas de estilo (o texto some).
    """

    def __init__(self, minimo: int, maximo: int, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setValidator(QIntValidator(minimo, maximo, self))
        self.setMaxLength(len(str(maximo)))

    def value(self) -> int:
        """Valor digitado (0 se estiver vazio)."""
        try:
            return int(self.text())
        except ValueError:
            return 0

    def setValue(self, valor: int) -> None:  # noqa: N802 (mesmo nome do QSpinBox)
        self.setText(str(valor))


class CampoSenha(QLineEdit):
    """Campo de senha com botão de olho para mostrar/ocultar."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setEchoMode(QLineEdit.EchoMode.Password)
        self._acao = QAction(self)
        self._acao.setToolTip("Mostrar/ocultar a senha")
        self._acao.triggered.connect(self._alternar)
        self.addAction(self._acao, QLineEdit.ActionPosition.TrailingPosition)
        self._atualizar_icone()
        # Método (e não lambda): o Qt desliga sozinho quando o widget é destruído.
        tema.sinais.tema_mudou.connect(self._atualizar_icone)

    def _alternar(self) -> None:
        visivel = self.echoMode() == QLineEdit.EchoMode.Normal
        self.setEchoMode(QLineEdit.EchoMode.Password if visivel else QLineEdit.EchoMode.Normal)
        self._atualizar_icone()

    def _atualizar_icone(self, _paleta: object = None) -> None:
        visivel = self.echoMode() == QLineEdit.EchoMode.Normal
        self._acao.setIcon(tema.icone("mdi6.eye-off-outline" if visivel else "mdi6.eye-outline"))


class SecaoRecolhivel(QWidget):
    """Título clicável que mostra/esconde um conteúdo (ex.: opções avançadas)."""

    def __init__(self, titulo: str, conteudo: QWidget, aberta: bool = False, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._conteudo = conteudo
        self._botao = QToolButton()
        self._botao.setText(titulo)
        self._botao.setProperty("papel", "segmento")
        self._botao.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self._botao.setCursor(Qt.CursorShape.PointingHandCursor)
        self._botao.clicked.connect(lambda: self.definir_aberta(not self._conteudo.isVisible()))
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        layout.addWidget(self._botao, alignment=Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(conteudo)
        self.definir_aberta(aberta)
        tema.sinais.tema_mudou.connect(self._tema_mudou)

    def _tema_mudou(self, _paleta: object = None) -> None:
        self.definir_aberta(self._conteudo.isVisible())

    def definir_aberta(self, aberta: bool) -> None:
        self._conteudo.setVisible(aberta)
        self._botao.setIcon(tema.icone("mdi6.chevron-down" if aberta else "mdi6.chevron-right"))


class GrupoSegmentos(QFrame):
    """Botões lado a lado em que só um fica marcado (ex.: seletor de layout)."""

    escolhido = Signal(str)

    def __init__(self, opcoes: Sequence[tuple[str, str, str | None]], parent: QWidget | None = None) -> None:
        """``opcoes``: lista de (valor, texto, dica)."""
        super().__init__(parent)
        self.setObjectName("grupoSegmentos")
        self._grupo = QButtonGroup(self)
        self._grupo.setExclusive(True)
        self._botoes: dict[str, QToolButton] = {}
        layout = QHBoxLayout(self)
        layout.setContentsMargins(3, 3, 3, 3)
        layout.setSpacing(2)
        for valor, texto, dica in opcoes:
            botao = QToolButton()
            botao.setText(texto)
            botao.setCheckable(True)
            botao.setProperty("papel", "segmento")
            botao.setCursor(Qt.CursorShape.PointingHandCursor)
            if dica:
                botao.setToolTip(dica)
            botao.clicked.connect(lambda _c=False, v=valor: self.escolhido.emit(v))
            self._grupo.addButton(botao)
            self._botoes[valor] = botao
            layout.addWidget(botao)

    def definir(self, valor: str) -> None:
        if valor in self._botoes:
            self._botoes[valor].setChecked(True)


def linha_horizontal(*widgets: QWidget, espaco: int = 8, esticar_no_fim: bool = False) -> QWidget:
    """Coloca widgets lado a lado num container sem margens."""
    container = QWidget()
    layout = QHBoxLayout(container)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(espaco)
    for widget in widgets:
        layout.addWidget(widget)
    if esticar_no_fim:
        layout.addStretch(1)
    return container

