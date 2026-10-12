"""Um bloco (tile) do mosaico: o vídeo ao vivo de UMA vista.

Desenhado à mão no ``paintEvent`` (mais leve e bonito que empilhar widgets):
- vídeo com cantos arredondados;
- faixa em gradiente embaixo com o nome e o ponto de status
  (verde online, amarelo conectando, cinza offline);
- borda que acende suavemente no hover e fica azul quando selecionado;
- tela "Offline" com o motivo, contagem para a nova tentativa e botão "Reconectar".

- caixas de detecção coloridas por classe ("PESSOA #7 0.87"), chips com a
  contagem por classe ("PESSOA 2") e, opcionalmente, a máscara de movimento.
  As caixas vêm normalizadas (0 a 1) e são desenhadas sobre a imagem já
  posicionada — por isso servem igual para o substream e para o stream principal.
- alertas (Fase 5): zona semitransparente, contador "parado 6/8 s" no rótulo dos
  objetos dentro da zona, caixa vermelha "PARADO 12 s" no objeto alertado, borda
  vermelha pulsando e selo "ALERTA" enquanto houver alerta ativo na vista.
"""

from __future__ import annotations

from PySide6.QtCore import QEasingCurve, QPoint, QPointF, QRectF, Qt, QTimer, QVariantAnimation, Signal
from PySide6.QtGui import (
    QColor,
    QContextMenuEvent,
    QEnterEvent,
    QFont,
    QImage,
    QLinearGradient,
    QMouseEvent,
    QPainter,
    QPainterPath,
    QPaintEvent,
    QPen,
    QPolygonF,
    QResizeEvent,
)
from PySide6.QtWidgets import QPushButton, QWidget

from core.modelos import CLASSES, Deteccao, Vista
from ui import tema
from vision.dwell import Progresso, SituacaoParado

Ponto = tuple[float, float]

def _cor_do_texto(fundo: QColor) -> QColor:
    """Texto escuro sobre cores claras, branco sobre cores escuras (legibilidade)."""
    luminancia = 0.299 * fundo.red() + 0.587 * fundo.green() + 0.114 * fundo.blue()
    return QColor(15, 18, 24) if luminancia > 150 else QColor(255, 255, 255)


#: estado do leitor -> (cor do ponto na paleta, texto)
ESTADOS: dict[str, tuple[str, str]] = {
    "online": ("sucesso", "Online"),
    "conectando": ("aviso", "Conectando"),
    "offline": ("offline", "Offline"),
    "parado": ("offline", "Parado"),
}


class BlocoCamera(QWidget):
    clicado = Signal(object)  # chave da vista
    duplo_clique = Signal(object)
    menu_pedido = Signal(object, QPoint)  # chave, posição global do clique
    reconectar_pedido = Signal(object)

    def __init__(self, vista: Vista, titulo: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.vista = vista
        self.titulo = titulo
        self._imagem: QImage | None = None
        self._estado = "conectando"
        self._mensagem = ""
        self._segundos = 0
        self._selecionado = False
        self._ja_conectou = False
        self._falhou = False  # caiu e ainda não voltou: as novas tentativas não "piscam" a tela
        self._hover = 0.0
        self._pontos = 0  # animação "Conectando..."
        self._selo: tuple[str, str] | None = None  # (texto, cor do ponto) no canto inferior direito
        self._deteccoes: tuple[Deteccao, ...] = ()
        self._mascara: QImage | None = None
        self._destino = QRectF()  # onde a imagem foi desenhada (para posicionar as caixas)
        self._zona: list[Ponto] | None = None
        self._editando_zona = False  # o editor desenha a zona por cima (esta fica escondida)
        self._progresso: dict[int, Progresso] = {}
        self._alerta = False
        self._pulso = 0.0
        self.mostrar_caixas = True
        self.mostrar_chips = True
        self.mostrar_zona = False

        self.setMinimumSize(160, 90)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover)

        self._anim_hover = QVariantAnimation(self)
        self._anim_hover.setDuration(160)
        self._anim_hover.valueChanged.connect(self._mudar_hover)

        # Pulso da borda vermelha (só roda enquanto há alerta).
        self._anim_alerta = QVariantAnimation(self)
        self._anim_alerta.setStartValue(0.0)
        self._anim_alerta.setKeyValueAt(0.5, 1.0)
        self._anim_alerta.setEndValue(0.0)
        self._anim_alerta.setDuration(1400)
        self._anim_alerta.setLoopCount(-1)
        self._anim_alerta.setEasingCurve(QEasingCurve.Type.InOutSine)
        self._anim_alerta.valueChanged.connect(self._mudar_pulso)

        self._timer_pontos = QTimer(self)
        self._timer_pontos.setInterval(450)
        self._timer_pontos.timeout.connect(self._avancar_pontos)
        self._timer_pontos.start()

        self._botao = QPushButton("Reconectar", self)
        self._botao.setProperty("variante", "primario")
        self._botao.setCursor(Qt.CursorShape.PointingHandCursor)
        self._botao.clicked.connect(lambda: self.reconectar_pedido.emit(self.vista.chave))
        self._botao.hide()
        self._atualizar_icone_botao()
        # Método (e não lambda): o Qt desliga sozinho quando o widget é destruído.
        tema.sinais.tema_mudou.connect(self._tema_mudou)

    # ------------------------------------------------------------ dados vindos de fora
    def definir_imagem(self, imagem: QImage) -> None:
        self._imagem = imagem
        self.update()

    def definir_estado(self, estado: str, mensagem: str, segundos: int) -> None:
        if estado == "online":
            self._ja_conectou, self._falhou = True, False
        elif estado == "offline":
            self._falhou = True
        self._estado, self._segundos = estado, segundos
        if estado != "conectando":
            self._mensagem = mensagem
        self._botao.setVisible(self._mostra_offline() and self.height() >= 150)
        if estado == "conectando":
            self._timer_pontos.start()
        else:
            self._timer_pontos.stop()
        self._posicionar_botao()
        self.update()

    def definir_selecionado(self, selecionado: bool) -> None:
        self._selecionado = selecionado
        self.update()

    def definir_titulo(self, titulo: str) -> None:
        self.titulo = titulo
        self.update()

    def definir_selo(self, texto: str | None, cor_ponto: str = "sucesso") -> None:
        """Etiqueta no canto superior direito (ex.: "HD · 2304×1296 · 12 FPS")."""
        selo = (texto, cor_ponto) if texto else None
        if selo != self._selo:
            self._selo = selo
            self.update()

    def definir_deteccoes(self, deteccoes: tuple[Deteccao, ...] | None) -> None:
        self._deteccoes = deteccoes or ()
        self.update()

    def definir_mascara(self, mascara: QImage | None) -> None:
        self._mascara = mascara
        self.update()

    def configurar_sobreposicao(self, caixas: bool, chips: bool, zona: bool | None = None) -> None:
        self.mostrar_caixas, self.mostrar_chips = caixas, chips
        if zona is not None:
            self.mostrar_zona = zona
        self.update()

    def definir_zona(self, pontos: list[Ponto] | None) -> None:
        """Polígono normalizado (0 a 1) da zona; ``None`` = sem zona (quadro inteiro)."""
        self._zona = list(pontos) if pontos else None
        self.update()

    def definir_editando_zona(self, editando: bool) -> None:
        self._editando_zona = editando
        self.update()

    def definir_parado(self, situacao: SituacaoParado | None) -> None:
        """Contadores "parado 6/8 s" e se há alerta ativo (vem do serviço de alertas)."""
        self._progresso = dict(situacao.progresso) if situacao is not None else {}
        self.definir_alerta(situacao is not None and situacao.alerta)
        self.update()

    def definir_alerta(self, ativo: bool) -> None:
        if ativo == self._alerta:
            return
        self._alerta = ativo
        if ativo:
            self._anim_alerta.start()
        else:
            self._anim_alerta.stop()
            self._pulso = 0.0
        self.update()

    @property
    def alerta(self) -> bool:
        return self._alerta

    @property
    def zona(self) -> list[Ponto] | None:
        return self._zona

    @property
    def progresso(self) -> dict[int, Progresso]:
        return self._progresso

    @property
    def area_imagem(self) -> QRectF:
        """Retângulo (no bloco) onde o vídeo aparece — as coordenadas 0 a 1 valem dentro dele."""
        return self._destino_para(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5))

    @property
    def deteccoes(self) -> tuple[Deteccao, ...]:
        return self._deteccoes

    @property
    def selo(self) -> str | None:
        return self._selo[0] if self._selo else None

    @property
    def estado(self) -> str:
        return self._estado

    def _mostra_offline(self) -> bool:
        """Offline, ou reconectando depois de uma falha (mantém a mesma tela)."""
        return self._estado in ("offline", "parado") or (self._estado == "conectando" and self._falhou)

    @property
    def tem_imagem(self) -> bool:
        return self._imagem is not None

    # ------------------------------------------------------------ eventos
    def _tema_mudou(self, _paleta: object = None) -> None:
        self._atualizar_icone_botao()
        self.update()

    def _atualizar_icone_botao(self) -> None:
        self._botao.setIcon(tema.icone("mdi6.refresh", "destaque_texto"))

    def _mudar_hover(self, valor: float) -> None:
        self._hover = float(valor)
        self.update()

    def _mudar_pulso(self, valor: float) -> None:
        self._pulso = float(valor)
        self.update()

    def _avancar_pontos(self) -> None:
        self._pontos = (self._pontos + 1) % 4
        if self._estado == "conectando":
            self.update()

    def _animar_hover(self, destino: float) -> None:
        self._anim_hover.stop()
        self._anim_hover.setStartValue(self._hover)
        self._anim_hover.setEndValue(destino)
        self._anim_hover.start()

    def enterEvent(self, evento: QEnterEvent) -> None:  # noqa: N802
        self._animar_hover(1.0)
        super().enterEvent(evento)

    def leaveEvent(self, evento: object) -> None:  # noqa: N802
        self._animar_hover(0.0)
        super().leaveEvent(evento)  # type: ignore[arg-type]

    def mousePressEvent(self, evento: QMouseEvent) -> None:  # noqa: N802
        if evento.button() == Qt.MouseButton.LeftButton:
            self.clicado.emit(self.vista.chave)
        super().mousePressEvent(evento)

    def mouseDoubleClickEvent(self, evento: QMouseEvent) -> None:  # noqa: N802
        if evento.button() == Qt.MouseButton.LeftButton:
            self.duplo_clique.emit(self.vista.chave)

    def contextMenuEvent(self, evento: QContextMenuEvent) -> None:  # noqa: N802
        self.menu_pedido.emit(self.vista.chave, evento.globalPos())

    def resizeEvent(self, evento: QResizeEvent) -> None:  # noqa: N802
        self._posicionar_botao()
        self._botao.setVisible(self._mostra_offline() and self.height() >= 150)
        super().resizeEvent(evento)

    def _posicionar_botao(self) -> None:
        self._botao.adjustSize()
        x = (self.width() - self._botao.width()) // 2
        y = self.height() // 2 + 34
        self._botao.move(x, y)

    # ------------------------------------------------------------ desenho
    def paintEvent(self, _evento: QPaintEvent) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        area = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        contorno = QPainterPath()
        contorno.addRoundedRect(area, tema.RAIO, tema.RAIO)

        p.save()
        p.setClipPath(contorno)
        p.fillRect(area, tema.cor("video_fundo"))
        online = self._estado == "online"
        if self._imagem is not None:
            self._desenhar_imagem(p, area)
            if online:
                if self._mascara is not None:
                    p.drawImage(self._destino, self._mascara)
                if self.mostrar_zona and self._zona and not self._editando_zona:
                    self._desenhar_zona(p)
                if self.mostrar_caixas and self._deteccoes:
                    self._desenhar_caixas(p)
                if self.mostrar_chips and self._deteccoes:
                    self._desenhar_chips(p, area)
                if self._alerta:
                    self._desenhar_selo_alerta(p, area)
            else:
                p.fillRect(area, QColor(0, 0, 0, 165))  # escurece o último quadro
        if not online:
            self._desenhar_situacao(p, area)
        self._desenhar_rodape(p, area)
        p.restore()

        self._desenhar_borda(p, area)

    def _destino_para(self, area: QRectF) -> QRectF:
        """Onde a imagem fica dentro de ``area``: centralizada, sem distorcer."""
        if self._imagem is None or self._imagem.isNull():
            return QRectF(area)
        escala = self._imagem.devicePixelRatio() or 1.0
        larg, alt = self._imagem.width() / escala, self._imagem.height() / escala
        fator = min(area.width() / larg, area.height() / alt)
        destino = QRectF(0, 0, larg * fator, alt * fator)
        destino.moveCenter(area.center())
        return destino

    def _desenhar_imagem(self, p: QPainter, area: QRectF) -> None:
        assert self._imagem is not None
        # A imagem já chega no tamanho certo; aqui só centralizamos (e ajustamos se a
        # janela mudou de tamanho e o próximo quadro ainda não chegou).
        destino = self._destino_para(area)
        p.drawImage(destino, self._imagem)
        self._destino = destino

    def _desenhar_zona(self, p: QPainter) -> None:
        """Zona vigiada: preenchimento suave + contorno tracejado na cor de destaque."""
        d = self._destino
        poligono = QPolygonF([QPointF(d.left() + x * d.width(), d.top() + y * d.height()) for x, y in self._zona or []])
        p.setBrush(tema.cor("destaque", 0.10))
        p.setPen(QPen(tema.cor("destaque", 0.75), 1.5, Qt.PenStyle.DashLine))
        p.drawPolygon(poligono)

    def _desenhar_selo_alerta(self, p: QPainter, area: QRectF) -> None:
        """Selo vermelho "ALERTA" no canto superior direito (cor + texto: não depende só da cor)."""
        fonte = QFont(self.font())
        fonte.setPointSizeF(fonte.pointSizeF() * 0.85)
        fonte.setWeight(QFont.Weight.Bold)
        p.setFont(fonte)
        texto = "ALERTA"
        largura = p.fontMetrics().horizontalAdvance(texto) + 26
        selo = QRectF(area.right() - 10 - largura, area.top() + 10, largura, p.fontMetrics().height() + 6)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(tema.cor("alerta"))
        p.drawRoundedRect(selo, selo.height() / 2, selo.height() / 2)
        p.setBrush(QColor(255, 255, 255, round(140 + 115 * self._pulso)))
        p.drawEllipse(QRectF(selo.left() + 8, selo.center().y() - 3.5, 7, 7))
        p.setPen(QColor(255, 255, 255))
        p.drawText(selo.adjusted(18, 0, -6, 0), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, texto)

    def _desenhar_caixas(self, p: QPainter) -> None:
        """Caixa colorida + rótulo ("PESSOA #7 0.87"); DESCONHECIDO de movimento tracejado."""
        destino = self._destino
        compacto = destino.height() < 300
        fonte = QFont(self.font())
        fonte.setPointSizeF(fonte.pointSizeF() * (0.8 if compacto else 0.92))
        fonte.setWeight(QFont.Weight.DemiBold)
        p.setFont(fonte)
        metricas = p.fontMetrics()
        ocupados: list[QRectF] = []  # rótulos já desenhados (para não sobrepor)
        for d in sorted(self._deteccoes, key=lambda d: (d.caixa[1], d.caixa[0])):
            progresso = self._progresso.get(d.id_rastreio) if d.id_rastreio is not None else None
            alertado = progresso is not None and progresso.alertado
            cor = tema.cor("alerta") if alertado else QColor(tema.CORES_CLASSES.get(d.classe, "#94A3B8"))
            x1, y1, x2, y2 = d.caixa
            caixa = QRectF(
                destino.left() + x1 * destino.width(),
                destino.top() + y1 * destino.height(),
                (x2 - x1) * destino.width(),
                (y2 - y1) * destino.height(),
            )
            if d.ausente_s > 0:  # a IA deixou de ver há instantes: mais apagado
                cor.setAlphaF(0.55)
            caneta = QPen(cor, (1.5 if compacto else 2.0) + (1.0 if alertado else 0.0))
            if d.origem == "movimento":
                caneta.setStyle(Qt.PenStyle.DashLine)
            p.setPen(caneta)
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRoundedRect(caixa, 3, 3)

            rotulo = d.classe
            if d.id_rastreio is not None:
                rotulo += f" #{d.id_rastreio}"
            if not compacto and d.origem == "ia":
                rotulo += f" {d.confianca:.2f}"
            if alertado:
                assert progresso is not None
                rotulo += f" · PARADO {progresso.segundos:.0f} s"
            elif progresso is not None and progresso.segundos >= 1:
                contador = f"{progresso.segundos:.0f}/{progresso.alvo:.0f} s"
                rotulo += f" · {contador}" if compacto else f" · parado {contador}"
            largura = metricas.horizontalAdvance(rotulo) + 10
            altura = metricas.height() + 2
            topo = caixa.top() - altura if caixa.top() - altura >= destino.top() else caixa.top()
            etiqueta = QRectF(caixa.left(), topo, largura, altura)
            if etiqueta.right() > destino.right():  # objeto na borda direita: rótulo não sai da imagem
                etiqueta.moveRight(destino.right())
            if etiqueta.left() < destino.left():
                etiqueta.moveLeft(destino.left())
            for _ in range(6):  # se bater em outro rótulo, desce uma linha (dentro da caixa)
                if not any(etiqueta.intersects(outro) for outro in ocupados):
                    break
                etiqueta.translate(0, altura + 1)
            ocupados.append(etiqueta)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(cor)
            p.drawRoundedRect(etiqueta, 3, 3)
            p.setPen(_cor_do_texto(cor))
            p.drawText(etiqueta, Qt.AlignmentFlag.AlignCenter, rotulo)

    def _desenhar_chips(self, p: QPainter, area: QRectF) -> None:
        """Contagem por classe no canto superior esquerdo: "PESSOA 2", "CARRO 1"."""
        contagem: dict[str, int] = {}
        for d in self._deteccoes:
            contagem[d.classe] = contagem.get(d.classe, 0) + 1
        fonte = QFont(self.font())
        fonte.setPointSizeF(fonte.pointSizeF() * 0.85)
        fonte.setWeight(QFont.Weight.DemiBold)
        p.setFont(fonte)
        metricas = p.fontMetrics()
        x, y = area.left() + 10, area.top() + 10
        for classe in sorted(contagem, key=CLASSES.index):
            texto = f"{classe} {contagem[classe]}"
            largura = metricas.horizontalAdvance(texto) + 24
            if x + largura > area.right() - 10:
                break
            chip = QRectF(x, y, largura, metricas.height() + 6)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(0, 0, 0, 160))
            p.drawRoundedRect(chip, chip.height() / 2, chip.height() / 2)
            p.setBrush(QColor(tema.CORES_CLASSES.get(classe, "#94A3B8")))
            p.drawEllipse(QRectF(chip.left() + 8, chip.center().y() - 3.5, 7, 7))
            p.setPen(QColor(255, 255, 255))
            p.drawText(chip.adjusted(18, 0, -6, 0), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, texto)
            x += largura + 6

    def _desenhar_situacao(self, p: QPainter, area: QRectF) -> None:
        """Tela de "Conectando..." ou "Offline" no meio do bloco."""
        compacto = area.height() < 150
        centro_y = area.center().y() - (6 if compacto else 18)
        if self._mostra_offline():
            icone, titulo, cor_icone = "mdi6.video-off-outline", "Offline", "offline"
            detalhe = self._mensagem
            if self._estado == "conectando":
                detalhe += " · tentando reconectar" + "." * self._pontos
            elif self._segundos > 0:
                detalhe += f" · nova tentativa em {self._segundos} s"
        else:
            icone, titulo = "mdi6.cctv", "Conectando" + "." * self._pontos
            detalhe, cor_icone = "", "aviso"

        tamanho = 26 if compacto else 34
        pix = tema.icone(icone, cor_icone).pixmap(tamanho, tamanho)
        p.drawPixmap(int(area.center().x() - tamanho / 2), int(centro_y - tamanho - 6), pix)

        fonte = QFont(self.font())
        fonte.setPointSizeF(fonte.pointSizeF() * (1.05 if compacto else 1.25))
        fonte.setWeight(QFont.Weight.DemiBold)
        p.setFont(fonte)
        p.setPen(QColor(235, 238, 245))
        caixa = QRectF(area.left() + 12, centro_y, area.width() - 24, 26)
        p.drawText(caixa, Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop, titulo)

        if detalhe and not compacto:
            fonte_pequena = QFont(self.font())
            fonte_pequena.setPointSizeF(fonte_pequena.pointSizeF() * 0.92)
            p.setFont(fonte_pequena)
            p.setPen(QColor(170, 176, 190))
            texto = p.fontMetrics().elidedText(detalhe, Qt.TextElideMode.ElideRight, int(area.width() - 32))
            p.drawText(
                QRectF(area.left() + 16, centro_y + 26, area.width() - 32, 20),
                Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop,
                texto,
            )

    def _desenhar_rodape(self, p: QPainter, area: QRectF) -> None:
        """Gradiente escuro embaixo + ponto de status + nome."""
        altura = min(64.0, area.height() * 0.4)
        faixa = QRectF(area.left(), area.bottom() - altura, area.width(), altura)
        gradiente = QLinearGradient(faixa.topLeft(), faixa.bottomLeft())
        gradiente.setColorAt(0.0, QColor(0, 0, 0, 0))
        gradiente.setColorAt(1.0, QColor(0, 0, 0, 190))
        p.fillRect(faixa, gradiente)

        cor_nome, _texto = ESTADOS.get(self._estado, ESTADOS["offline"])
        raio = 4.5
        base_y = area.bottom() - 16
        centro_ponto = (area.left() + 18, base_y - 5)
        if self._estado == "online":  # "halo" suave em volta do ponto verde
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(tema.cor(cor_nome, 0.25))
            p.drawEllipse(QRectF(centro_ponto[0] - 8, centro_ponto[1] - 8, 16, 16))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(tema.cor(cor_nome))
        p.drawEllipse(QRectF(centro_ponto[0] - raio, centro_ponto[1] - raio, raio * 2, raio * 2))

        reservado = self._desenhar_selo(p, area, base_y) if self._selo is not None else 0.0
        fonte = QFont(self.font())
        fonte.setWeight(QFont.Weight.DemiBold)
        if area.height() > 480:  # bloco grande (layout 1 ou tela cheia): nome maior
            fonte.setPointSizeF(fonte.pointSizeF() * 1.3)
        p.setFont(fonte)
        p.setPen(QColor(255, 255, 255))
        x_texto = area.left() + 32
        largura = area.width() - (x_texto - area.left()) - 14 - reservado
        titulo = p.fontMetrics().elidedText(self.titulo, Qt.TextElideMode.ElideRight, int(largura))
        p.drawText(QRectF(x_texto, base_y - 18, largura, 24), Qt.AlignmentFlag.AlignVCenter, titulo)

    def _desenhar_selo(self, p: QPainter, area: QRectF, base_y: float) -> float:
        """Selo no canto inferior direito (o canto de cima costuma ter a data/hora da câmera).

        Devolve a largura ocupada, para o nome da câmera não passar por baixo.
        """
        texto, cor_ponto = self._selo  # type: ignore[misc]
        fonte = QFont(self.font())
        fonte.setPointSizeF(fonte.pointSizeF() * 0.9)
        p.setFont(fonte)
        texto = p.fontMetrics().elidedText(texto, Qt.TextElideMode.ElideRight, int(area.width() * 0.55))
        largura_texto = p.fontMetrics().horizontalAdvance(texto)
        direita = area.right() - 14
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(tema.cor(cor_ponto))
        p.drawEllipse(QRectF(direita - largura_texto - 13, base_y - 8.5, 7, 7))
        p.setPen(QColor(225, 230, 240))
        p.drawText(
            QRectF(direita - largura_texto, base_y - 18, largura_texto, 24),
            Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight,
            texto,
        )
        return largura_texto + 30

    def _desenhar_borda(self, p: QPainter, area: QRectF) -> None:
        if self._alerta:  # vermelho pulsando: brilho largo e suave + traço firme
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.setPen(QPen(tema.cor("alerta", 0.18 + 0.32 * self._pulso), 7))
            p.drawRoundedRect(area.adjusted(3, 3, -3, -3), tema.RAIO - 2, tema.RAIO - 2)
            p.setPen(QPen(tema.cor("alerta"), 2 + 1.5 * self._pulso))
            p.drawRoundedRect(area.adjusted(1, 1, -1, -1), tema.RAIO, tema.RAIO)
            return
        if self._selecionado:
            caneta = QPen(tema.cor("destaque"), 2)
            area = area.adjusted(0.5, 0.5, -0.5, -0.5)
        else:
            base = tema.cor("borda")
            acesa = tema.cor("destaque")
            t = self._hover * 0.75
            cor = QColor(
                round(base.red() + (acesa.red() - base.red()) * t),
                round(base.green() + (acesa.green() - base.green()) * t),
                round(base.blue() + (acesa.blue() - base.blue()) * t),
            )
            caneta = QPen(cor, 1)
        p.setPen(caneta)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRoundedRect(area, tema.RAIO, tema.RAIO)
