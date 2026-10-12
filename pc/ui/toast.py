"""Notificações discretas no canto inferior direito ("toasts").

Aparecem com uma animação de fade, empilham, somem sozinhas (o tempo pausa
enquanto o mouse está em cima) e podem ter miniatura e ação ao clicar —
na Fase 5, o toast de alerta abre o evento.
"""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QEasingCurve, QEvent, QObject, QPoint, QPropertyAnimation, Qt, QTimer, Signal
from PySide6.QtGui import QEnterEvent, QImage, QMouseEvent, QPixmap
from PySide6.QtWidgets import QFrame, QGraphicsOpacityEffect, QHBoxLayout, QLabel, QToolButton, QVBoxLayout, QWidget

from ui import tema

#: tipo -> (ícone, nome da cor na paleta)
TIPOS: dict[str, tuple[str, str]] = {
    "info": ("mdi6.information-outline", "destaque"),
    "sucesso": ("mdi6.check-circle-outline", "sucesso"),
    "aviso": ("mdi6.alert-circle-outline", "aviso"),
    "erro": ("mdi6.alert-circle-outline", "alerta"),
    "alerta": ("mdi6.alert-octagon-outline", "alerta"),
}
MARGEM = 20
ESPACO = 10
LARGURA = 360
MAXIMO_VISIVEIS = 4


class Toast(QFrame):
    clicado = Signal()
    fechado = Signal()

    def __init__(
        self,
        pai: QWidget,
        titulo: str,
        mensagem: str,
        tipo: str,
        miniatura: QImage | None,
        duracao_ms: int,
        clicavel: bool,
    ) -> None:
        super().__init__(pai)
        nome_icone, nome_cor = TIPOS.get(tipo, TIPOS["info"])
        p = tema.paleta_atual()
        cor_tipo = getattr(p, nome_cor)
        self.setObjectName("toast")
        self.setStyleSheet(
            f"#toast {{ background: {p.superficie}; border: 1px solid {p.borda}; border-radius: 12px; }}"
            f"#faixa {{ background: {cor_tipo}; border-radius: 2px; }}"
        )
        self.setFixedWidth(LARGURA)
        if clicavel:
            self.setCursor(Qt.CursorShape.PointingHandCursor)

        faixa = QFrame()
        faixa.setObjectName("faixa")
        faixa.setFixedWidth(4)
        icone = QLabel()
        icone.setPixmap(tema.icone(nome_icone, nome_cor).pixmap(22, 22))
        icone.setAlignment(Qt.AlignmentFlag.AlignTop)

        textos = QVBoxLayout()
        textos.setSpacing(2)
        rotulo_titulo = QLabel(titulo)
        rotulo_titulo.setStyleSheet("font-weight: 600;")
        rotulo_titulo.setWordWrap(True)
        textos.addWidget(rotulo_titulo)
        if mensagem:
            rotulo_mensagem = QLabel(mensagem)
            rotulo_mensagem.setProperty("papel", "suave")
            rotulo_mensagem.setWordWrap(True)
            textos.addWidget(rotulo_mensagem)

        fechar = QToolButton()
        fechar.setProperty("papel", "icone")
        fechar.setIcon(tema.icone("mdi6.close"))
        fechar.setCursor(Qt.CursorShape.PointingHandCursor)
        fechar.clicked.connect(self.fechar)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 12, 8, 12)
        layout.setSpacing(10)
        layout.addWidget(faixa)
        layout.addWidget(icone)
        layout.addLayout(textos, 1)
        if miniatura is not None and not miniatura.isNull():
            mini = QLabel()
            mini.setPixmap(
                QPixmap.fromImage(miniatura).scaled(
                    88, 50, Qt.AspectRatioMode.KeepAspectRatioByExpanding, Qt.TransformationMode.SmoothTransformation
                )
            )
            mini.setFixedSize(88, 50)
            mini.setStyleSheet("border-radius: 6px;")
            layout.addWidget(mini)
        layout.addWidget(fechar, alignment=Qt.AlignmentFlag.AlignTop)
        # Texto com quebra de linha: o tamanho "natural" (adjustSize) não conta as linhas a
        # mais e o texto ficava cortado. Pergunta ao layout a altura certa para esta largura.
        if layout.hasHeightForWidth():
            self.setFixedHeight(layout.totalHeightForWidth(LARGURA))

        self._opacidade = QGraphicsOpacityEffect(self)
        self._opacidade.setOpacity(0.0)
        self.setGraphicsEffect(self._opacidade)
        self._fade = QPropertyAnimation(self._opacidade, b"opacity", self)
        self._fade.setDuration(180)
        self._movimento = QPropertyAnimation(self, b"pos", self)
        self._movimento.setDuration(200)
        self._movimento.setEasingCurve(QEasingCurve.Type.OutCubic)

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(duracao_ms)
        self._timer.timeout.connect(self.fechar)
        self._fechando = False

    def aparecer(self) -> None:
        self.show()
        self.raise_()
        self._fade.setStartValue(0.0)
        self._fade.setEndValue(1.0)
        self._fade.start()
        self._timer.start()

    def mover_para(self, destino: QPoint, animar: bool = True) -> None:
        if not animar or not self.isVisible():
            self.move(destino)
            return
        self._movimento.stop()
        self._movimento.setStartValue(self.pos())
        self._movimento.setEndValue(destino)
        self._movimento.start()

    def fechar(self) -> None:
        if self._fechando:
            return
        self._fechando = True
        self._timer.stop()
        self._fade.stop()
        self._fade.setStartValue(self._opacidade.opacity())
        self._fade.setEndValue(0.0)
        self._fade.finished.connect(self._remover)
        self._fade.start()

    def _remover(self) -> None:
        self.fechado.emit()
        self.deleteLater()

    def enterEvent(self, evento: QEnterEvent) -> None:  # noqa: N802
        self._timer.stop()  # pausa enquanto o mouse está em cima
        super().enterEvent(evento)

    def leaveEvent(self, evento: QEvent) -> None:  # noqa: N802
        if not self._fechando:
            self._timer.start()
        super().leaveEvent(evento)

    def mousePressEvent(self, evento: QMouseEvent) -> None:  # noqa: N802
        if evento.button() == Qt.MouseButton.LeftButton:
            self.clicado.emit()
            self.fechar()


class Notificador(QObject):
    """Mostra e empilha toasts sobre uma janela."""

    def __init__(self, janela: QWidget) -> None:
        super().__init__(janela)
        self._janela = janela
        self._toasts: list[Toast] = []
        janela.installEventFilter(self)

    def mostrar(
        self,
        titulo: str,
        mensagem: str = "",
        tipo: str = "info",
        miniatura: QImage | None = None,
        ao_clicar: Callable[[], None] | None = None,
        duracao_ms: int = 4500,
    ) -> Toast:
        toast = Toast(self._janela, titulo, mensagem, tipo, miniatura, duracao_ms, clicavel=ao_clicar is not None)
        if ao_clicar:
            toast.clicado.connect(ao_clicar)
        toast.fechado.connect(lambda t=toast: self._saiu(t))
        toast.adjustSize()
        self._toasts.append(toast)
        while len(self._toasts) > MAXIMO_VISIVEIS:
            self._toasts.pop(0).fechar()
        self._posicionar(novo=toast)
        toast.aparecer()
        return toast

    def _saiu(self, toast: Toast) -> None:
        if toast in self._toasts:
            self._toasts.remove(toast)
        self._posicionar()

    def _posicionar(self, novo: Toast | None = None) -> None:
        """Empilha de baixo para cima; o mais novo fica embaixo."""
        y = self._janela.height() - MARGEM
        for toast in reversed(self._toasts):
            y -= toast.height()
            destino = QPoint(self._janela.width() - LARGURA - MARGEM, y)
            toast.mover_para(destino, animar=toast is not novo)
            y -= ESPACO

    def eventFilter(self, objeto: QObject, evento: QEvent) -> bool:  # noqa: N802
        if objeto is self._janela and evento.type() == QEvent.Type.Resize:
            for toast in self._toasts:
                toast._movimento.stop()  # noqa: SLF001
            self._posicionar()
        return False
