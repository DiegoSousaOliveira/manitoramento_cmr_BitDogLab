"""Revisão das imagens em que o PROFESSOR ficou em dúvida (opcional, mas melhora o treino).

A imagem já vem com as caixas do professor. Você:

- **aceita** (Enter) se estiver tudo certo — ela entra no treino como "revisada por você";
- **corrige**: clique numa caixa para escolhê-la, mude a classe (lista ou teclas 1 a 8) ou
  apague (Del); arraste na imagem para desenhar uma caixa nova (objeto que ficou sem caixa);
- **descarta** (D) se a imagem não serve (borrada, só chuva...);
- **pula** (→) para decidir depois.

Importante: TODO objeto das nossas classes precisa ter caixa. Um objeto sem caixa ensina a IA
a ignorá-lo.
"""

from __future__ import annotations

from datetime import datetime

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QImage, QKeyEvent, QMouseEvent, QPainter, QPaintEvent, QPen
from PySide6.QtWidgets import QComboBox, QDialog, QHBoxLayout, QPushButton, QVBoxLayout, QWidget

from core.modelos import CLASSES_COCO, CORES_CLASSES
from treino.dados import REVISAR, ROTULADAS, Amostra, ConjuntoTreino, Rotulo, caixa_de_rotulo, rotulo_de_caixa
from ui import tema
from ui.componentes import rotulo

#: Classes que dá para marcar (na ordem das teclas 1 a 8).
CLASSES_REVISAO: list[tuple[int, str]] = sorted(CLASSES_COCO.items())
TAMANHO_MINIMO = 0.01  # caixa menor que 1% da imagem: foi só um clique, não um arraste


class TelaRotulos(QWidget):
    """A imagem com as caixas: escolher, desenhar e apagar."""

    mudou = Signal()
    selecao_mudou = Signal(object)  # índice da caixa escolhida (ou None)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMinimumSize(640, 360)
        self.setCursor(Qt.CursorShape.CrossCursor)
        self._imagem = QImage()
        self.rotulos: list[Rotulo] = []
        self.selecionado: int | None = None
        self.classe_nova = CLASSES_REVISAO[0][0]
        self._inicio: QPointF | None = None
        self._atual: QPointF | None = None

    def definir(self, imagem: QImage, rotulos: list[Rotulo]) -> None:
        self._imagem = imagem
        self.rotulos = list(rotulos)
        self.selecionar(None)
        self.update()

    # ------------------------------------------------------------ coordenadas
    def area(self) -> QRectF:
        if self._imagem.isNull():
            return QRectF(self.rect())
        escala = min(self.width() / self._imagem.width(), self.height() / self._imagem.height())
        area = QRectF(0, 0, self._imagem.width() * escala, self._imagem.height() * escala)
        area.moveCenter(QRectF(self.rect()).center())
        return area

    def _normalizar(self, ponto: QPointF) -> tuple[float, float]:
        area = self.area()
        return (min(1.0, max(0.0, (ponto.x() - area.left()) / area.width())),
                min(1.0, max(0.0, (ponto.y() - area.top()) / area.height())))

    def _retangulo(self, rotulo_caixa: Rotulo) -> QRectF:
        area = self.area()
        x1, y1, x2, y2 = caixa_de_rotulo(rotulo_caixa)
        return QRectF(area.left() + x1 * area.width(), area.top() + y1 * area.height(),
                      (x2 - x1) * area.width(), (y2 - y1) * area.height())

    # ------------------------------------------------------------ ações
    def selecionar(self, indice: int | None) -> None:
        self.selecionado = indice
        self.selecao_mudou.emit(indice)
        self.update()

    def mudar_classe(self, classe_coco: int) -> None:
        """Muda a classe da caixa escolhida (e a das próximas caixas desenhadas)."""
        self.classe_nova = classe_coco
        if self.selecionado is not None:
            _c, cx, cy, w, h = self.rotulos[self.selecionado]
            self.rotulos[self.selecionado] = (classe_coco, cx, cy, w, h)
            self.mudou.emit()
            self.update()

    def apagar_selecionada(self) -> None:
        if self.selecionado is not None:
            del self.rotulos[self.selecionado]
            self.selecionar(None)
            self.mudou.emit()

    def adicionar(self, caixa: tuple[float, float, float, float]) -> None:
        self.rotulos.append(rotulo_de_caixa(self.classe_nova, caixa))
        self.selecionar(len(self.rotulos) - 1)
        self.mudou.emit()

    # ------------------------------------------------------------ mouse
    def mousePressEvent(self, evento: QMouseEvent) -> None:  # noqa: N802
        ponto = evento.position()
        tocadas = [i for i, r in enumerate(self.rotulos) if self._retangulo(r).contains(ponto)]
        if tocadas:  # a menor caixa sob o mouse (caixas pequenas dentro de grandes)
            self.selecionar(min(tocadas, key=lambda i: self.rotulos[i][3] * self.rotulos[i][4]))
            return
        self._inicio = self._atual = ponto

    def mouseMoveEvent(self, evento: QMouseEvent) -> None:  # noqa: N802
        if self._inicio is not None:
            self._atual = evento.position()
            self.update()

    def mouseReleaseEvent(self, evento: QMouseEvent) -> None:  # noqa: N802
        if self._inicio is None:
            return
        (ax, ay), (bx, by) = self._normalizar(self._inicio), self._normalizar(evento.position())
        self._inicio = self._atual = None
        caixa = (min(ax, bx), min(ay, by), max(ax, bx), max(ay, by))
        if caixa[2] - caixa[0] >= TAMANHO_MINIMO and caixa[3] - caixa[1] >= TAMANHO_MINIMO:
            self.adicionar(caixa)
        else:
            self.selecionar(None)
        self.update()

    # ------------------------------------------------------------ desenho
    def paintEvent(self, _evento: QPaintEvent) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.fillRect(self.rect(), tema.cor("video_fundo"))
        if not self._imagem.isNull():
            p.drawImage(self.area(), self._imagem)
        fonte = QFont(self.font())
        fonte.setWeight(QFont.Weight.DemiBold)
        p.setFont(fonte)
        for indice, rotulo_caixa in enumerate(self.rotulos):
            classe = CLASSES_COCO.get(rotulo_caixa[0], "?")
            cor = QColor(CORES_CLASSES.get(classe, "#94A3B8"))
            retangulo = self._retangulo(rotulo_caixa)
            escolhida = indice == self.selecionado
            p.setPen(QPen(cor, 4 if escolhida else 2, Qt.PenStyle.SolidLine if escolhida else Qt.PenStyle.SolidLine))
            p.setBrush(QColor(cor.red(), cor.green(), cor.blue(), 50) if escolhida else Qt.BrushStyle.NoBrush)
            p.drawRect(retangulo)
            texto = f"{indice + 1}. {classe}"
            largura = p.fontMetrics().horizontalAdvance(texto) + 10
            etiqueta = QRectF(retangulo.left(), retangulo.top() - 20, largura, 20)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(cor)
            p.drawRect(etiqueta)
            p.setPen(QColor(15, 18, 24))
            p.drawText(etiqueta, Qt.AlignmentFlag.AlignCenter, texto)
        if self._inicio is not None and self._atual is not None:  # caixa sendo desenhada
            p.setPen(QPen(tema.cor("destaque"), 2, Qt.PenStyle.DashLine))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRect(QRectF(self._inicio, self._atual).normalized())


class DialogoRevisao(QDialog):
    """Passa pelas imagens de ``revisar/``, uma por vez."""

    def __init__(self, conjunto: ConjuntoTreino, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Revisar imagens do treino")
        self.resize(1280, 800)
        self._conjunto = conjunto
        self._fila: list[Amostra] = conjunto.amostras(REVISAR)
        self._indice = 0
        self.aceitas = self.descartadas = 0

        self.tela = TelaRotulos()
        self.tela.selecao_mudou.connect(self._selecao_mudou)
        self.titulo = rotulo("", "titulo")
        self.info = rotulo("", "suave", quebra=True)
        self.classe = QComboBox()
        for numero, (coco, nome) in enumerate(CLASSES_REVISAO, start=1):
            self.classe.addItem(f"{numero}. {nome}", coco)
        self.classe.activated.connect(lambda _i: self.tela.mudar_classe(int(self.classe.currentData())))
        self.botao_apagar = QPushButton("Apagar caixa (Del)")
        self.botao_apagar.clicked.connect(self.tela.apagar_selecionada)
        self.botao_aceitar = QPushButton("Aceitar e próxima (Enter)")
        self.botao_aceitar.setProperty("variante", "primario")
        self.botao_aceitar.clicked.connect(self.aceitar)
        self.botao_descartar = QPushButton("Descartar imagem (D)")
        self.botao_descartar.clicked.connect(self.descartar)
        self.botao_pular = QPushButton("Pular (→)")
        self.botao_pular.clicked.connect(self.pular)

        lateral = QVBoxLayout()
        lateral.setSpacing(10)
        lateral.addWidget(self.titulo)
        lateral.addWidget(self.info)
        lateral.addSpacing(8)
        lateral.addWidget(rotulo("CLASSE DA CAIXA ESCOLHIDA (OU DA PRÓXIMA)", "secao"))
        lateral.addWidget(self.classe)
        lateral.addWidget(self.botao_apagar)
        lateral.addWidget(rotulo(
            "Arraste na imagem para desenhar uma caixa nova. Clique numa caixa para escolhê-la. "
            "Todo objeto das classes da lista precisa de caixa — um objeto sem caixa ensina a IA a ignorá-lo.",
            "dica", quebra=True,
        ))
        lateral.addStretch(1)
        lateral.addWidget(self.botao_aceitar)
        lateral.addWidget(self.botao_descartar)
        lateral.addWidget(self.botao_pular)
        painel = QWidget()
        painel.setLayout(lateral)
        painel.setFixedWidth(300)
        layout = QHBoxLayout(self)
        layout.addWidget(self.tela, 1)
        layout.addWidget(painel)
        self._mostrar()

    @property
    def atual(self) -> Amostra | None:
        return self._fila[self._indice] if self._indice < len(self._fila) else None

    def _mostrar(self) -> None:
        amostra = self.atual
        if amostra is None:
            self.titulo.setText("Tudo revisado!")
            self.info.setText(f"{self.aceitas} aceita(s), {self.descartadas} descartada(s). Elas entram no próximo treino.")
            self.tela.definir(QImage(), [])
            for botao in (self.botao_aceitar, self.botao_descartar, self.botao_pular, self.botao_apagar):
                botao.setEnabled(False)
            return
        meta = amostra.meta()
        self.titulo.setText(f"Imagem {self._indice + 1} de {len(self._fila)}")
        self.info.setText(
            f"{meta.get('camera', '?')} · {amostra.data:%d/%m/%Y %H:%M} · motivo: {meta.get('motivo', '?')}\n"
            f"O professor ficou em dúvida em {meta.get('duvidas', '?')} caixa(s)."
        )
        self.tela.definir(QImage(str(amostra.imagem)), amostra.rotulos())

    def _selecao_mudou(self, indice: object) -> None:
        self.botao_apagar.setEnabled(indice is not None)
        if isinstance(indice, int):
            self.classe.setCurrentIndex(max(0, self.classe.findData(self.tela.rotulos[indice][0])))

    def aceitar(self) -> None:
        amostra = self.atual
        if amostra is None:
            return
        amostra.gravar_rotulos(self.tela.rotulos)
        amostra.atualizar_meta(rotulo="humano", rotulada_em=datetime.now().isoformat(timespec="seconds"))
        self._conjunto.mover(amostra, ROTULADAS)
        self.aceitas += 1
        self._proxima()

    def descartar(self) -> None:
        amostra = self.atual
        if amostra is None:
            return
        self._conjunto.apagar(amostra)
        self.descartadas += 1
        self._proxima()

    def pular(self) -> None:
        self._proxima()

    def _proxima(self) -> None:
        self._indice += 1
        self._mostrar()

    def keyPressEvent(self, evento: QKeyEvent) -> None:  # noqa: N802
        tecla = evento.key()
        if tecla in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.aceitar()
        elif tecla == Qt.Key.Key_D:
            self.descartar()
        elif tecla == Qt.Key.Key_Right:
            self.pular()
        elif tecla in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
            self.tela.apagar_selecionada()
        elif Qt.Key.Key_1 <= tecla <= Qt.Key.Key_8:
            indice = tecla - Qt.Key.Key_1
            self.classe.setCurrentIndex(indice)
            self.tela.mudar_classe(CLASSES_REVISAO[indice][0])
        else:
            super().keyPressEvent(evento)
