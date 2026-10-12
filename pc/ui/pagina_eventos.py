"""Aba "Eventos": eventos de TODAS as câmeras (filtro por câmera) + visualizador ao lado."""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QStackedWidget, QVBoxLayout, QWidget

from core.modelos import Camera, Evento
from core.servico_cameras import ServicoCameras
from ui import tema
from ui.componentes import rotulo
from ui.painel_eventos import PainelEventos
from ui.visualizador_evento import VisualizadorEvento


class PaginaEventos(QWidget):
    evento_excluido = Signal(int)

    def __init__(self, servico: ServicoCameras, notificar: Callable[..., object], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.painel = PainelEventos(servico, todas_cameras=True)
        self.painel.setFixedWidth(380)
        self.painel.evento_escolhido.connect(self.mostrar_evento)
        separador = QFrame()
        separador.setFixedWidth(1)
        separador.setStyleSheet(f"background: {tema.paleta_atual().borda};")

        self._icone = QLabel()
        self._icone.setAlignment(Qt.AlignmentFlag.AlignCenter)
        vazio = QWidget()
        vazio_layout = QVBoxLayout(vazio)
        vazio_layout.addStretch(1)
        vazio_layout.addWidget(self._icone)
        texto = rotulo("Escolha um evento na lista para ver a foto e o vídeo.", "suave")
        texto.setAlignment(Qt.AlignmentFlag.AlignCenter)
        vazio_layout.addWidget(texto)
        vazio_layout.addStretch(2)
        self.vazio = vazio

        self.visualizador = VisualizadorEvento(servico, notificar, "Fechar")
        self.visualizador.voltar.connect(lambda: self.pilha.setCurrentWidget(self.vazio))
        self.visualizador.excluido.connect(self._excluido)
        self.pilha = QStackedWidget()
        self.pilha.addWidget(vazio)
        self.pilha.addWidget(self.visualizador)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self.painel)
        layout.addWidget(separador)
        layout.addWidget(self.pilha, 1)
        self._aplicar_icone()
        tema.sinais.tema_mudou.connect(self._aplicar_icone)

    def _aplicar_icone(self, _paleta: object = None) -> None:
        self._icone.setPixmap(tema.icone("mdi6.movie-open-outline", "texto_fraco").pixmap(56, 56))

    def definir_cameras(self, cameras: list[Camera]) -> None:
        self.painel.definir_cameras(cameras)
        self.painel.recarregar()

    def mostrar_evento(self, evento: Evento) -> None:
        camera = self.painel.camera(evento.camera_id)
        if camera is None:
            return
        self.visualizador.abrir(evento, camera)
        self.pilha.setCurrentWidget(self.visualizador)
        self.painel.selecionar(evento.id or 0)

    def fechar_visualizador(self) -> None:
        self.visualizador.fechar()
        self.pilha.setCurrentWidget(self.vazio)

    def _excluido(self, evento_id: int) -> None:
        self.painel.eventos_removidos([evento_id])
        self.evento_excluido.emit(evento_id)
