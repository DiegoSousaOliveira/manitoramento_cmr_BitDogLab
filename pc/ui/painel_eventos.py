"""Lista de eventos: painel lateral da Tela da câmera e aba "Eventos" (todas as câmeras).

- Mais recente primeiro, agrupada por dia ("Hoje", "Ontem", "08/10/2026").
- Cada item: miniatura, hora, duração, câmera/lente, chips das classes e selo ALERTA.
- Filtros: dia (calendário), classe, "só alertas" e — na aba Eventos — câmera.
- Atualiza sozinha quando um evento termina (``evento_gravado``).

Desempenho: a lista é um ``QListView`` com um *delegate* que DESENHA cada item
(em vez de criar widgets por item); as miniaturas são carregadas só quando
aparecem na tela e ficam em cache.
"""

from __future__ import annotations

from datetime import date, timedelta

from PySide6.QtCore import QAbstractListModel, QDate, QModelIndex, QPersistentModelIndex, QRect, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPixmap
from PySide6.QtWidgets import (
    QComboBox,
    QDateEdit,
    QHBoxLayout,
    QListView,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QVBoxLayout,
    QWidget,
)

from core.modelos import CLASSES, CORES_CLASSES, Camera, Evento
from core.servico_cameras import ServicoCameras
from eventos.gravador import ARQUIVO_MINIATURA
from eventos.retencao import pasta_do_evento
from ui import tema
from ui.componentes import Interruptor, rotulo

DIAS_SEMANA = ("segunda", "terça", "quarta", "quinta", "sexta", "sábado", "domingo")
ALTURA_DIA = 34
ALTURA_EVENTO = 88
LIMITE = 300  # eventos carregados por consulta


def nome_do_dia(dia: date, hoje: date | None = None) -> str:
    hoje = hoje or date.today()
    if dia == hoje:
        return "Hoje"
    if dia == hoje - timedelta(days=1):
        return "Ontem"
    return f"{dia:%d/%m/%Y} · {DIAS_SEMANA[dia.weekday()]}"


def formatar_duracao(segundos: float) -> str:
    segundos = max(0, round(segundos))
    return f"{segundos} s" if segundos < 60 else f"{segundos // 60} min {segundos % 60:02d} s"


class ModeloEventos(QAbstractListModel):
    """Linhas: um ``date`` (cabeçalho do dia) ou um ``Evento``."""

    def __init__(self) -> None:
        super().__init__()
        self._linhas: list[date | Evento] = []

    def rowCount(self, _pai: QModelIndex | QPersistentModelIndex = QModelIndex()) -> int:  # noqa: N802, B008
        return len(self._linhas)

    def data(self, indice: QModelIndex | QPersistentModelIndex, papel: int = Qt.ItemDataRole.DisplayRole) -> object:
        if papel == Qt.ItemDataRole.UserRole and 0 <= indice.row() < len(self._linhas):
            return self._linhas[indice.row()]
        return None

    def flags(self, indice: QModelIndex | QPersistentModelIndex) -> Qt.ItemFlag:
        if isinstance(self.data(indice, Qt.ItemDataRole.UserRole), Evento):
            return Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
        return Qt.ItemFlag.NoItemFlags

    def definir(self, eventos: list[Evento]) -> None:
        """Recebe eventos do mais recente para o mais antigo e insere os cabeçalhos dos dias."""
        self.beginResetModel()
        self._linhas = []
        dia_atual: date | None = None
        for evento in eventos:
            if evento.inicio.date() != dia_atual:
                dia_atual = evento.inicio.date()
                self._linhas.append(dia_atual)
            self._linhas.append(evento)
        self.endResetModel()

    def eventos(self) -> list[Evento]:
        return [linha for linha in self._linhas if isinstance(linha, Evento)]

    def adicionar(self, evento: Evento) -> None:
        """Evento novo vai para o topo (se for de hoje, debaixo do cabeçalho "Hoje")."""
        self.definir(sorted([evento, *self.eventos()], key=lambda e: e.inicio, reverse=True))

    def remover(self, ids: set[int]) -> None:
        self.definir([e for e in self.eventos() if e.id not in ids])

    def linha_do_evento(self, evento_id: int) -> int:
        return next((i for i, l in enumerate(self._linhas) if isinstance(l, Evento) and l.id == evento_id), -1)


class DelegadoEventos(QStyledItemDelegate):
    """Desenha cada linha (cabeçalho do dia ou cartão do evento)."""

    def __init__(self, painel: PainelEventos) -> None:
        super().__init__(painel)
        self._painel = painel

    def sizeHint(self, opcao: QStyleOptionViewItem, indice: QModelIndex | QPersistentModelIndex) -> QSize:  # noqa: N802
        item = indice.data(Qt.ItemDataRole.UserRole)
        return QSize(opcao.rect.width(), ALTURA_EVENTO if isinstance(item, Evento) else ALTURA_DIA)

    def paint(self, p: QPainter, opcao: QStyleOptionViewItem, indice: QModelIndex | QPersistentModelIndex) -> None:
        item = indice.data(Qt.ItemDataRole.UserRole)
        p.save()
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        if isinstance(item, Evento):
            self._evento(p, opcao, item)
        elif isinstance(item, date):
            fonte = QFont(opcao.font)
            fonte.setPointSizeF(fonte.pointSizeF() * 0.85)
            fonte.setWeight(QFont.Weight.DemiBold)
            p.setFont(fonte)
            p.setPen(tema.cor("texto_fraco"))
            p.drawText(opcao.rect.adjusted(14, 8, -8, 0), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                       nome_do_dia(item).upper())
        p.restore()

    def _evento(self, p: QPainter, opcao: QStyleOptionViewItem, evento: Evento) -> None:
        cartao = QRectF(opcao.rect).adjusted(8, 3, -8, -3)
        selecionado = bool(opcao.state & QStyle.StateFlag.State_Selected)
        hover = bool(opcao.state & QStyle.StateFlag.State_MouseOver)
        p.setPen(tema.cor("destaque") if selecionado else tema.cor("borda"))
        p.setBrush(tema.cor("superficie_alta") if (selecionado or hover) else tema.cor("superficie"))
        p.drawRoundedRect(cartao, 10, 10)

        miniatura = QRectF(cartao.left() + 8, cartao.top() + 8, 128, 72)
        caminho = QPainterPath()
        caminho.addRoundedRect(miniatura, 6, 6)
        p.save()
        p.setClipPath(caminho)
        p.fillRect(miniatura, tema.cor("video_fundo"))
        imagem = self._painel.miniatura(evento)
        if imagem is not None:
            escalada = imagem.scaled(
                miniatura.size().toSize(), Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                Qt.TransformationMode.SmoothTransformation,
            )
            p.drawPixmap(miniatura.topLeft(), escalada)
        p.restore()

        x = miniatura.right() + 12
        largura = cartao.right() - x - 10
        fonte = QFont(opcao.font)
        fonte.setWeight(QFont.Weight.DemiBold)
        p.setFont(fonte)
        p.setPen(tema.cor("texto"))
        hora = f"{evento.inicio:%H:%M:%S}"
        p.drawText(QRectF(x, cartao.top() + 8, largura, 20), Qt.AlignmentFlag.AlignVCenter, hora)
        largura_hora = p.fontMetrics().horizontalAdvance(hora)
        fonte_normal = QFont(opcao.font)
        p.setFont(fonte_normal)
        p.setPen(tema.cor("texto_suave"))
        p.drawText(QRectF(x + largura_hora + 6, cartao.top() + 8, largura, 20), Qt.AlignmentFlag.AlignVCenter,
                   f"· {formatar_duracao(evento.duracao_s)}")

        if evento.alerta:  # vermelho só para alertas
            fonte_selo = QFont(opcao.font)
            fonte_selo.setPointSizeF(fonte_selo.pointSizeF() * 0.78)
            fonte_selo.setWeight(QFont.Weight.Bold)
            p.setFont(fonte_selo)
            texto = "ALERTA"
            l_selo = p.fontMetrics().horizontalAdvance(texto) + 14
            selo = QRectF(cartao.right() - l_selo - 10, cartao.top() + 10, l_selo, 18)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(tema.cor("alerta"))
            p.drawRoundedRect(selo, 9, 9)
            p.setPen(QColor(255, 255, 255))
            p.drawText(selo, Qt.AlignmentFlag.AlignCenter, texto)

        fonte_pequena = QFont(opcao.font)
        fonte_pequena.setPointSizeF(fonte_pequena.pointSizeF() * 0.88)
        p.setFont(fonte_pequena)
        p.setPen(tema.cor("texto_suave"))
        subtitulo = p.fontMetrics().elidedText(self._painel.nome_vista(evento), Qt.TextElideMode.ElideRight, int(largura))
        p.drawText(QRectF(x, cartao.top() + 30, largura, 18), Qt.AlignmentFlag.AlignVCenter, subtitulo)

        chips = [(c, n) for c, n in sorted(evento.classes.items(), key=lambda par: CLASSES.index(par[0])
                 if par[0] in CLASSES else 99)] or [("", 0)]
        cx = x
        for classe, quantidade in chips:
            texto = f"{classe} {quantidade}" if classe else "Movimento"
            l_chip = p.fontMetrics().horizontalAdvance(texto) + 20
            if cx + l_chip > cartao.right() - 8:
                break
            chip = QRectF(cx, cartao.top() + 54, l_chip, 20)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(tema.cor("fundo"))
            p.drawRoundedRect(chip, 10, 10)
            p.setBrush(QColor(CORES_CLASSES.get(classe, "#94A3B8")))
            p.drawEllipse(QRectF(chip.left() + 7, chip.center().y() - 3, 6, 6))
            p.setPen(tema.cor("texto_suave"))
            p.drawText(chip.adjusted(16, 0, -4, 0), Qt.AlignmentFlag.AlignVCenter, texto)
            cx += l_chip + 5


class PainelEventos(QWidget):
    evento_escolhido = Signal(object)  # Evento

    def __init__(self, servico: ServicoCameras, todas_cameras: bool, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.servico = servico
        self.todas_cameras = todas_cameras
        self._camera_id: int | None = None
        self._cameras: dict[int, Camera] = {}
        self._miniaturas: dict[int, QPixmap | None] = {}

        self.filtro_camera = QComboBox()
        self.filtro_camera.setVisible(todas_cameras)
        self.filtro_classe = QComboBox()
        self.filtro_classe.addItem("Todas as classes", None)
        for classe in CLASSES:
            self.filtro_classe.addItem(classe.capitalize(), classe)
        self.so_alertas = Interruptor("Só alertas")
        self.filtrar_dia = Interruptor("Dia")
        self.dia = QDateEdit(QDate.currentDate())
        self.dia.setCalendarPopup(True)
        self.dia.setDisplayFormat("dd/MM/yyyy")
        self.dia.setEnabled(False)

        self.modelo = ModeloEventos()
        self.lista = QListView()
        self.lista.setModel(self.modelo)
        self.lista.setItemDelegate(DelegadoEventos(self))
        self.lista.setMouseTracking(True)
        self.lista.setVerticalScrollMode(QListView.ScrollMode.ScrollPerPixel)
        self.lista.setFrameShape(QListView.Shape.NoFrame)
        self.lista.setStyleSheet("QListView { background: transparent; }")
        self.lista.setUniformItemSizes(False)
        self.lista.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.lista.clicked.connect(self._clicado)
        self.vazio = rotulo("Nenhum evento encontrado.", "suave")
        self.vazio.setAlignment(Qt.AlignmentFlag.AlignCenter)

        topo = QVBoxLayout()
        topo.setContentsMargins(12, 12, 12, 6)
        topo.setSpacing(8)
        titulo = rotulo("EVENTOS", "secao")
        topo.addWidget(titulo)
        if todas_cameras:
            topo.addWidget(self.filtro_camera)
        linha1 = QHBoxLayout()
        linha1.addWidget(self.filtro_classe, 1)
        linha1.addWidget(self.so_alertas)
        linha2 = QHBoxLayout()
        linha2.addWidget(self.filtrar_dia)
        linha2.addWidget(self.dia, 1)
        topo.addLayout(linha1)
        topo.addLayout(linha2)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addLayout(topo)
        layout.addWidget(self.lista, 1)
        layout.addWidget(self.vazio, 1)

        self.filtro_camera.currentIndexChanged.connect(lambda _i: self.recarregar())
        self.filtro_classe.currentIndexChanged.connect(lambda _i: self.recarregar())
        self.so_alertas.toggled.connect(lambda _v: self.recarregar())
        self.filtrar_dia.toggled.connect(self._dia_ligado)
        self.dia.dateChanged.connect(lambda _d: self.recarregar())

    # ------------------------------------------------------------ dados
    def definir_cameras(self, cameras: list[Camera]) -> None:
        self._cameras = {c.id or 0: c for c in cameras}
        if self.todas_cameras:
            atual = self.filtro_camera.currentData()
            self.filtro_camera.blockSignals(True)
            self.filtro_camera.clear()
            self.filtro_camera.addItem("Todas as câmeras", None)
            for camera in cameras:
                self.filtro_camera.addItem(camera.nome, camera.id)
            self.filtro_camera.setCurrentIndex(max(0, self.filtro_camera.findData(atual)))
            self.filtro_camera.blockSignals(False)

    def definir_camera(self, camera_id: int | None) -> None:
        """Painel de UMA câmera (Tela da câmera)."""
        self._camera_id = camera_id
        self.recarregar()

    def _camera_filtrada(self) -> int | None:
        return self.filtro_camera.currentData() if self.todas_cameras else self._camera_id

    def _dia_filtrado(self) -> date | None:
        return self.dia.date().toPython() if self.filtrar_dia.isChecked() else None  # type: ignore[return-value]

    def _dia_ligado(self, ligado: bool) -> None:
        self.dia.setEnabled(ligado)
        self.recarregar()

    def recarregar(self) -> None:
        if not self.todas_cameras and self._camera_id is None:
            self.modelo.definir([])
        else:
            eventos = self.servico.banco.listar_eventos(
                camera_id=self._camera_filtrada(),
                dia=self._dia_filtrado(),
                classe=self.filtro_classe.currentData(),
                so_alertas=self.so_alertas.isChecked(),
                limite=LIMITE,
            )
            self.modelo.definir(eventos)
        self._atualizar_vazio()

    def _passa_no_filtro(self, evento: Evento) -> bool:
        camera = self._camera_filtrada()
        dia = self._dia_filtrado()
        classe = self.filtro_classe.currentData()
        if not self.todas_cameras and self._camera_id is None:
            return False
        return (
            (camera is None or evento.camera_id == camera)
            and (dia is None or evento.inicio.date() == dia)
            and (classe is None or classe in evento.classes)
            and (not self.so_alertas.isChecked() or evento.alerta)
        )

    def evento_gravado(self, evento: Evento, _camera: Camera | None = None) -> None:
        """Chamado quando um evento novo termina (atualização em tempo real)."""
        if self._passa_no_filtro(evento):
            self.modelo.adicionar(evento)
            self._atualizar_vazio()

    def eventos_removidos(self, ids: list[int]) -> None:
        self.modelo.remover(set(ids))
        for evento_id in ids:
            self._miniaturas.pop(evento_id, None)
        self._atualizar_vazio()

    def _atualizar_vazio(self) -> None:
        tem = self.modelo.rowCount() > 0
        self.lista.setVisible(tem)
        self.vazio.setVisible(not tem)

    def _clicado(self, indice: QModelIndex) -> None:
        evento = indice.data(Qt.ItemDataRole.UserRole)
        if isinstance(evento, Evento):
            self.evento_escolhido.emit(evento)

    def selecionar(self, evento_id: int) -> None:
        linha = self.modelo.linha_do_evento(evento_id)
        if linha >= 0:
            self.lista.setCurrentIndex(self.modelo.index(linha))

    # ------------------------------------------------------------ usados pelo delegate
    def nome_vista(self, evento: Evento) -> str:
        camera = self._cameras.get(evento.camera_id)
        if camera is None:
            return "Câmera removida"
        vista = next((v for v in camera.vistas() if v.numero == evento.vista), None)
        nome = camera.nome_vista(vista) if vista else camera.nome
        return nome if self.todas_cameras or len(camera.vistas()) > 1 else "Movimento detectado"

    def miniatura(self, evento: Evento) -> QPixmap | None:
        if evento.id in self._miniaturas:
            return self._miniaturas[evento.id or 0]
        camera = self._cameras.get(evento.camera_id)
        imagem = None
        if camera is not None:
            caminho = pasta_do_evento(self.servico, camera, evento) / ARQUIVO_MINIATURA
            if caminho.is_file():
                imagem = QPixmap(str(caminho))
        self._miniaturas[evento.id or 0] = imagem
        return imagem

    def camera(self, camera_id: int) -> Camera | None:
        return self._cameras.get(camera_id)

    def sizeHint(self) -> QSize:  # noqa: N802
        return QSize(360, 600)

    def area_lista(self) -> QRect:
        return self.lista.geometry()
