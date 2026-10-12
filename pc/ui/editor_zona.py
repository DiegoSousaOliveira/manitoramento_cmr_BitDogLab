"""Editor da zona vigiada: uma camada transparente por cima do vídeo de UMA vista.

Como usar (as dicas aparecem no topo do vídeo):
- **clique** na imagem: cria um ponto (perto de um lado da zona, ele entra naquele lado);
- **arraste** um ponto para ajustar;
- **botão direito** num ponto: apaga o ponto;
- Desfazer / Limpar / Cancelar / Salvar ficam na barra da Tela da câmera.

Os pontos ficam em coordenadas normalizadas (0 a 1) dentro da área do vídeo
(``BlocoCamera.area_imagem``): a mesma zona serve para o substream e para o HD.
"""

from __future__ import annotations

from PySide6.QtCore import QEvent, QObject, QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QMouseEvent, QPainter, QPainterPath, QPaintEvent, QPen, QPolygonF
from PySide6.QtWidgets import QWidget

from ui import tema
from ui.bloco_camera import BlocoCamera
from vision.zona import indice_insercao

Ponto = tuple[float, float]

RAIO_PONTO = 6.0  # tamanho do ponto desenhado (px)
RAIO_TOQUE = 12.0  # distância para "pegar" um ponto com o mouse (px)
MAXIMO_DESFAZER = 50


class EditorZona(QWidget):
    mudou = Signal()  # os pontos mudaram (a tela atualiza os botões)

    def __init__(self, bloco: BlocoCamera, pontos: list[Ponto] | None) -> None:
        super().__init__(bloco)
        self._bloco = bloco
        self.pontos: list[Ponto] = list(pontos or [])
        self._historico: list[list[Ponto]] = []
        self._antes_de_arrastar: list[Ponto] | None = None
        self._arrastando: int | None = None
        self._sobre: int | None = None
        self.setMouseTracking(True)
        self.setCursor(Qt.CursorShape.CrossCursor)
        self.setGeometry(bloco.rect())
        bloco.installEventFilter(self)  # acompanha o tamanho do bloco
        bloco.definir_editando_zona(True)
        self.show()
        self.raise_()

    def encerrar(self) -> None:
        """Remove o editor (a zona salva, se houver, volta a ser desenhada pelo bloco)."""
        self._bloco.removeEventFilter(self)
        self._bloco.definir_editando_zona(False)
        self.hide()
        self.deleteLater()

    # ------------------------------------------------------------ ações
    @property
    def pode_desfazer(self) -> bool:
        return bool(self._historico)

    def desfazer(self) -> None:
        if self._historico:
            self.pontos = self._historico.pop()
            self._sobre = self._arrastando = None
            self._avisar()

    def limpar(self) -> None:
        if self.pontos:
            self._guardar()
            self.pontos = []
            self._avisar()

    def _guardar(self) -> None:
        self._historico.append(list(self.pontos))
        del self._historico[:-MAXIMO_DESFAZER]

    def _avisar(self) -> None:
        self.update()
        self.mudou.emit()

    # ------------------------------------------------------------ coordenadas
    def _area(self) -> QRectF:
        return self._bloco.area_imagem

    def _para_tela(self, ponto: Ponto) -> QPointF:
        area = self._area()
        return QPointF(area.left() + ponto[0] * area.width(), area.top() + ponto[1] * area.height())

    def _para_zona(self, posicao: QPointF) -> Ponto:
        area = self._area()
        x = (posicao.x() - area.left()) / max(1.0, area.width())
        y = (posicao.y() - area.top()) / max(1.0, area.height())
        return min(1.0, max(0.0, x)), min(1.0, max(0.0, y))

    def ponto_na_tela(self, indice: int) -> QPointF:
        """Posição de um ponto no editor (usado pelos testes para "clicar" nele)."""
        return self._para_tela(self.pontos[indice])

    def _ponto_em(self, posicao: QPointF) -> int | None:
        melhor, indice = RAIO_TOQUE, None
        for i, ponto in enumerate(self.pontos):
            tela = self._para_tela(ponto)
            distancia = ((tela.x() - posicao.x()) ** 2 + (tela.y() - posicao.y()) ** 2) ** 0.5
            if distancia <= melhor:
                melhor, indice = distancia, i
        return indice

    # ------------------------------------------------------------ mouse
    def mousePressEvent(self, evento: QMouseEvent) -> None:  # noqa: N802
        posicao = evento.position()
        indice = self._ponto_em(posicao)
        if evento.button() == Qt.MouseButton.RightButton:
            if indice is not None:
                self._guardar()
                del self.pontos[indice]
                self._sobre = None
                self._avisar()
        elif evento.button() == Qt.MouseButton.LeftButton:
            if indice is None:
                if not self._area().contains(posicao):
                    return
                self._guardar()
                ponto = self._para_zona(posicao)
                area = self._area()
                indice = indice_insercao(self.pontos, ponto, area.height() / max(1.0, area.width()))
                self.pontos.insert(indice, ponto)
                self._avisar()
            else:
                self._antes_de_arrastar = list(self.pontos)  # vira "desfazer" só se o ponto mexer
            self._arrastando = indice
        evento.accept()

    def mouseMoveEvent(self, evento: QMouseEvent) -> None:  # noqa: N802
        posicao = evento.position()
        if self._arrastando is not None and self._arrastando < len(self.pontos):
            self.pontos[self._arrastando] = self._para_zona(posicao)
            self._avisar()
            return
        sobre = self._ponto_em(posicao)
        if sobre != self._sobre:
            self._sobre = sobre
            self.setCursor(Qt.CursorShape.SizeAllCursor if sobre is not None else Qt.CursorShape.CrossCursor)
            self.update()

    def mouseReleaseEvent(self, evento: QMouseEvent) -> None:  # noqa: N802
        if self._antes_de_arrastar is not None and self._antes_de_arrastar != self.pontos:
            self._historico.append(self._antes_de_arrastar)
            del self._historico[:-MAXIMO_DESFAZER]
            self.mudou.emit()
        self._antes_de_arrastar = None
        self._arrastando = None
        evento.accept()

    def mouseDoubleClickEvent(self, evento: QMouseEvent) -> None:  # noqa: N802
        evento.accept()  # não deixa o duplo clique virar "tela cheia" no bloco de baixo

    def eventFilter(self, objeto: QObject, evento: QEvent) -> bool:  # noqa: N802
        if objeto is self._bloco and evento.type() == QEvent.Type.Resize:
            self.setGeometry(self._bloco.rect())
        return False

    # ------------------------------------------------------------ desenho
    def dica(self) -> str:
        if not self.pontos:
            return "Clique na imagem para marcar os cantos da zona · sem zona, a imagem inteira é vigiada"
        if len(self.pontos) < 3:
            return f"Continue clicando: a zona precisa de pelo menos 3 pontos ({len(self.pontos)} até agora)"
        return "Arraste os pontos para ajustar · clique perto de um lado para criar outro · botão direito apaga"

    def paintEvent(self, _evento: QPaintEvent) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        area = self._area()
        destaque = tema.cor("destaque")
        tela = [self._para_tela(ponto) for ponto in self.pontos]

        if len(tela) >= 3:
            poligono = QPolygonF(tela)
            fora = QPainterPath()
            fora.addRect(area)
            dentro = QPainterPath()
            dentro.addPolygon(poligono)
            dentro.closeSubpath()
            p.fillPath(fora.subtracted(dentro), QColor(0, 0, 0, 110))  # escurece o que NÃO é vigiado
            p.setBrush(tema.cor("destaque", 0.16))
            p.setPen(QPen(destaque, 2))
            p.drawPolygon(poligono)
        elif len(tela) == 2:
            p.setPen(QPen(destaque, 2, Qt.PenStyle.DashLine))
            p.drawLine(tela[0], tela[1])

        for i, ponto in enumerate(tela):
            raio = RAIO_PONTO + (2 if i in (self._sobre, self._arrastando) else 0)
            p.setPen(QPen(destaque, 2))
            p.setBrush(QColor(255, 255, 255))
            p.drawEllipse(ponto, raio, raio)

        self._desenhar_dica(p, area)

    def _desenhar_dica(self, p: QPainter, area: QRectF) -> None:
        fonte = QFont(self.font())
        fonte.setPointSizeF(fonte.pointSizeF() * 0.95)
        fonte.setWeight(QFont.Weight.DemiBold)
        p.setFont(fonte)
        metricas = p.fontMetrics()
        texto = metricas.elidedText(self.dica(), Qt.TextElideMode.ElideRight, int(area.width() - 48))
        largura = metricas.horizontalAdvance(texto) + 28
        caixa = QRectF(area.center().x() - largura / 2, area.top() + 12, largura, metricas.height() + 12)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(0, 0, 0, 185))
        p.drawRoundedRect(caixa, caixa.height() / 2, caixa.height() / 2)
        p.setPen(QColor(255, 255, 255))
        p.drawText(caixa, Qt.AlignmentFlag.AlignCenter, texto)
