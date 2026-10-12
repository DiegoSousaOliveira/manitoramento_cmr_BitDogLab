"""Visualizador de evento: player do clipe com as caixas + foto + dados + ações.

- Player: play/pausa, quadro a quadro, barra de tempo, velocidade 0,5x/1x/2x.
  As caixas vêm do ``deteccoes.json``: para cada quadro mostramos as detecções
  analisadas mais perto (até 0,5 s antes) daquele instante.
- Foto do momento principal (clique para ver grande, com as caixas).
- Dados: câmera, início, fim, duração, classes e quantidades, maior confiança, alerta.
- Ações: Abrir pasta, Exportar (copia o clipe), Excluir.
"""

from __future__ import annotations

import bisect
import json
import logging
import shutil
from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import QUrl, Qt, Signal
from PySide6.QtGui import QDesktopServices, QImage, QMouseEvent, QPixmap
from PySide6.QtWidgets import (
    QFileDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QSlider,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from core.modelos import Camera, Deteccao, Evento, Vista
from core.servico_cameras import ServicoCameras
from eventos.gravador import ARQUIVO_FOTO
from eventos.retencao import pasta_do_evento, remover_evento
from ui import tema
from ui.bloco_camera import BlocoCamera
from ui.componentes import Cartao, GrupoSegmentos, rotulo
from ui.painel_eventos import formatar_duracao
from ui.reprodutor import Reprodutor

log = logging.getLogger(__name__)
JANELA_CAIXAS_S = 0.5  # mostra as detecções analisadas até este tempo antes do quadro


def carregar_deteccoes(caminho: Path) -> tuple[list[float], list[tuple[Deteccao, ...]]]:
    """Lê o ``deteccoes.json``: (tempos em ordem, detecções de cada tempo)."""
    if not caminho.is_file():
        return [], []
    dados = json.loads(caminho.read_text(encoding="utf-8"))
    tempos: list[float] = []
    grupos: list[tuple[Deteccao, ...]] = []
    for quadro in sorted(dados.get("quadros", []), key=lambda q: q["t"]):
        tempos.append(float(quadro["t"]))
        grupos.append(
            tuple(
                Deteccao(d["classe"], float(d["conf"]), tuple(d["caixa"]), d.get("id"), d.get("origem", "ia"))  # type: ignore[arg-type]
                for d in quadro["deteccoes"]
            )
        )
    return tempos, grupos


def deteccoes_no_tempo(
    tempos: list[float], grupos: list[tuple[Deteccao, ...]], t: float
) -> tuple[Deteccao, ...]:
    """Detecções da análise mais recente até ``t`` (se não for mais velha que ``JANELA_CAIXAS_S``)."""
    i = bisect.bisect_right(tempos, t + 1e-6) - 1
    if i < 0 or t - tempos[i] > JANELA_CAIXAS_S:
        return ()
    return grupos[i]


def formatar_tempo(segundos: float) -> str:
    segundos = max(0.0, segundos)
    return f"{int(segundos // 60)}:{segundos % 60:04.1f}"


class _FotoClicavel(QLabel):
    clicada = Signal()

    def mousePressEvent(self, evento: QMouseEvent) -> None:  # noqa: N802
        if evento.button() == Qt.MouseButton.LeftButton:
            self.clicada.emit()


class VisualizadorEvento(QWidget):
    voltar = Signal()
    excluido = Signal(int)  # id do evento

    def __init__(
        self,
        servico: ServicoCameras,
        notificar: Callable[..., object],
        texto_voltar: str = "Voltar ao vivo",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.servico = servico
        self._notificar = notificar
        self.evento: Evento | None = None
        self.camera: Camera | None = None
        self._pasta = Path()
        self._tempos: list[float] = []
        self._grupos: list[tuple[Deteccao, ...]] = []
        self._duracao = 0.0
        self._tocando = False
        self._modo_foto = False
        self._arrastando = False

        self.reprodutor = Reprodutor()
        self.reprodutor.aberto.connect(self._aberto)
        self.reprodutor.quadro.connect(self._quadro)
        self.reprodutor.tocando.connect(self._estado_tocando)
        self.reprodutor.erro.connect(lambda texto: self._notificar("Vídeo do evento", texto, "erro"))

        # ---------- topo
        self.botao_voltar = QPushButton(texto_voltar)
        self.botao_voltar.setProperty("variante", "fantasma")
        self.botao_voltar.clicked.connect(self.voltar)
        self.titulo = rotulo("", "titulo")
        self.modo = GrupoSegmentos([("video", "Vídeo", None), ("foto", "Foto", None)])
        self.modo.definir("video")
        self.modo.escolhido.connect(lambda valor: self.mostrar_foto(valor == "foto"))
        topo = QHBoxLayout()
        topo.setContentsMargins(16, 10, 16, 0)
        topo.addWidget(self.botao_voltar)
        topo.addWidget(self.titulo, 1)
        topo.addWidget(self.modo)

        # ---------- vídeo + controles
        self.tela = BlocoCamera(Vista(0, 0, 1, 1, (0.0, 0.0, 1.0, 1.0)), "")
        self.tela.setCursor(Qt.CursorShape.ArrowCursor)
        self.tela.configurar_sobreposicao(caixas=True, chips=False)
        self.tela.definir_estado("online", "", 0)
        self.tela.duplo_clique.connect(lambda _c: self.alternar())
        self.botao_anterior = self._botao_icone("mdi6.skip-previous", "Quadro anterior", lambda: self.passo(-1))
        self.botao_tocar = self._botao_icone("mdi6.play", "Tocar / pausar (espaço)", self.alternar)
        self.botao_proximo = self._botao_icone("mdi6.skip-next", "Próximo quadro", lambda: self.passo(1))
        self.barra_tempo = QSlider(Qt.Orientation.Horizontal)
        self.barra_tempo.setRange(0, 1000)
        self.barra_tempo.sliderPressed.connect(self._inicio_arraste)
        self.barra_tempo.sliderReleased.connect(self._fim_arraste)
        self.barra_tempo.sliderMoved.connect(self._arrastou)
        self.tempo = rotulo("0:00.0 / 0:00.0", "suave")
        self.velocidade = GrupoSegmentos([("0.5", "0,5x", None), ("1", "1x", None), ("2", "2x", None)])
        self.velocidade.definir("1")
        self.velocidade.escolhido.connect(lambda v: self.reprodutor.pedir_velocidade.emit(float(v)))
        controles = QHBoxLayout()
        controles.setSpacing(6)
        for widget in (self.botao_anterior, self.botao_tocar, self.botao_proximo):
            controles.addWidget(widget)
        controles.addWidget(self.barra_tempo, 1)
        controles.addWidget(self.tempo)
        controles.addSpacing(6)
        controles.addWidget(self.velocidade)
        coluna_video = QVBoxLayout()
        coluna_video.setSpacing(10)
        coluna_video.addWidget(self.tela, 1)
        coluna_video.addLayout(controles)

        # ---------- lateral: foto, dados e ações
        self.foto = _FotoClicavel()
        self.foto.setFixedSize(280, 158)
        self.foto.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.foto.setCursor(Qt.CursorShape.PointingHandCursor)
        self.foto.setToolTip("Clique para ver a foto grande")
        self.foto.setStyleSheet("background: #07080B; border-radius: 8px;")
        self.foto.clicada.connect(lambda: (self.modo.definir("foto"), self.mostrar_foto(True)))
        self.dados = QFormLayout()
        self.dados.setHorizontalSpacing(12)
        self.dados.setVerticalSpacing(6)
        self.botao_pasta = QPushButton("Abrir pasta")
        self.botao_pasta.clicked.connect(self.abrir_pasta)
        self.botao_exportar = QPushButton("Exportar")
        self.botao_exportar.clicked.connect(self.exportar)
        self.botao_excluir = QPushButton("Excluir")
        self.botao_excluir.clicked.connect(self.excluir)
        acoes = QGridLayout()  # 2 linhas: os textos cabem inteiros
        acoes.setSpacing(8)
        acoes.addWidget(self.botao_pasta, 0, 0)
        acoes.addWidget(self.botao_exportar, 0, 1)
        acoes.addWidget(self.botao_excluir, 1, 0, 1, 2)
        lateral = Cartao()
        lateral.setFixedWidth(312)
        lateral_layout = QVBoxLayout(lateral)
        lateral_layout.setContentsMargins(16, 14, 16, 16)
        lateral_layout.setSpacing(10)
        lateral_layout.addWidget(rotulo("FOTO DO MOMENTO PRINCIPAL", "secao"))
        lateral_layout.addWidget(self.foto)
        lateral_layout.addSpacing(4)
        lateral_layout.addWidget(rotulo("DADOS DO EVENTO", "secao"))
        lateral_layout.addLayout(self.dados)
        lateral_layout.addStretch(1)
        lateral_layout.addLayout(acoes)

        corpo = QHBoxLayout()
        corpo.setContentsMargins(16, 12, 16, 16)
        corpo.setSpacing(14)
        corpo.addLayout(coluna_video, 1)
        corpo.addWidget(lateral)
        raiz = QVBoxLayout(self)
        raiz.setContentsMargins(0, 0, 0, 0)
        raiz.addLayout(topo)
        raiz.addLayout(corpo, 1)
        self._aplicar_icones()
        tema.sinais.tema_mudou.connect(self._aplicar_icones)

    def _botao_icone(self, icone: str, dica: str, acao: Callable[[], None]) -> QToolButton:
        botao = QToolButton()
        botao.setProperty("papel", "icone")
        botao.setProperty("icone", icone)
        botao.setToolTip(dica)
        botao.setCursor(Qt.CursorShape.PointingHandCursor)
        botao.setIconSize(botao.iconSize() * 1.4)
        botao.clicked.connect(acao)
        return botao

    def _aplicar_icones(self, _paleta: object = None) -> None:
        for botao in (self.botao_anterior, self.botao_proximo):
            botao.setIcon(tema.icone(str(botao.property("icone")), "texto"))
        self.botao_tocar.setIcon(tema.icone("mdi6.pause" if self._tocando else "mdi6.play", "texto"))
        self.botao_voltar.setIcon(tema.icone("mdi6.arrow-left", "texto"))
        self.botao_pasta.setIcon(tema.icone("mdi6.folder-open-outline", "texto"))
        self.botao_exportar.setIcon(tema.icone("mdi6.export-variant", "texto"))
        self.botao_excluir.setIcon(tema.icone("mdi6.delete-outline", "texto"))

    # ================================================================ abrir
    def abrir(self, evento: Evento, camera: Camera) -> None:
        self.evento, self.camera = evento, camera
        self._pasta = pasta_do_evento(self.servico, camera, evento)
        vista = next((v for v in camera.vistas() if v.numero == evento.vista), None)
        nome = camera.nome_vista(vista) if vista else camera.nome
        self.titulo.setText(f"{nome} — {evento.inicio:%d/%m/%Y %H:%M:%S}")
        self.tela.definir_titulo(nome)
        try:
            self._tempos, self._grupos = carregar_deteccoes(self._pasta / evento.deteccoes)
        except (OSError, ValueError, KeyError) as erro:
            log.warning("deteccoes.json ilegível em %s: %s", self._pasta, erro)
            self._tempos, self._grupos = [], []
        foto = QPixmap(str(self._pasta / (evento.foto or ARQUIVO_FOTO)))
        self.foto.setPixmap(
            foto.scaled(self.foto.size(), Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
            if not foto.isNull() else QPixmap()
        )
        self._preencher_dados(evento, nome)
        self.modo.definir("video")
        self._modo_foto = False
        self.reprodutor.pedir_abrir.emit(str(self._pasta / evento.clipe))

    def _preencher_dados(self, evento: Evento, nome: str) -> None:
        while self.dados.rowCount():
            self.dados.removeRow(0)
        classes = ", ".join(f"{c} {n}" for c, n in evento.classes.items()) or "só movimento"
        linhas = [
            ("Câmera", nome),
            ("Início", f"{evento.inicio:%d/%m/%Y %H:%M:%S}"),
            ("Fim", f"{evento.fim:%H:%M:%S}" if evento.fim else "—"),
            ("Duração", formatar_duracao(evento.duracao_s)),
            ("Objetos", classes),
            ("Maior confiança", f"{evento.maior_conf * 100:.0f}%" if evento.maior_conf else "—"),
            ("Alerta", "SIM" if evento.alerta else "não"),
        ]
        for chave, valor in linhas:
            rotulo_valor = QLabel(valor)
            rotulo_valor.setWordWrap(True)
            if chave == "Alerta" and evento.alerta:
                rotulo_valor.setStyleSheet(f"color: {tema.paleta_atual().alerta}; font-weight: 700;")
            self.dados.addRow(rotulo(chave, "suave"), rotulo_valor)

    def fechar(self) -> None:
        """Para o vídeo e solta o arquivo (antes de excluir/sair)."""
        self.reprodutor.pedir_fechar.emit()
        self._estado_tocando(False)

    def encerrar(self) -> None:
        self.reprodutor.encerrar()

    # ================================================================ player
    def alternar(self) -> None:
        if self._modo_foto:
            self.modo.definir("video")
            self.mostrar_foto(False)
        if self._tocando:
            self.reprodutor.pedir_pausar.emit()
        else:
            self.reprodutor.pedir_tocar.emit()

    def passo(self, delta: int) -> None:
        if self._modo_foto:
            self.modo.definir("video")
            self.mostrar_foto(False)
        self.reprodutor.pedir_passo.emit(delta)

    def _aberto(self, duracao: float, _total: int, _fps: float) -> None:
        self._duracao = duracao

    def _quadro(self, imagem: QImage, t: float, _indice: int) -> None:
        if self._modo_foto:
            return
        self.tela.definir_imagem(imagem)
        self.tela.definir_deteccoes(deteccoes_no_tempo(self._tempos, self._grupos, t))
        self.tela.definir_selo(f"{formatar_tempo(t)} / {formatar_tempo(self._duracao)}", "destaque")
        self.tempo.setText(f"{formatar_tempo(t)} / {formatar_tempo(self._duracao)}")
        if not self._arrastando and self._duracao > 0:
            self.barra_tempo.blockSignals(True)
            self.barra_tempo.setValue(round(t / self._duracao * 1000))
            self.barra_tempo.blockSignals(False)

    def _estado_tocando(self, tocando: bool) -> None:
        self._tocando = tocando
        self.botao_tocar.setIcon(tema.icone("mdi6.pause" if tocando else "mdi6.play", "texto"))

    def _inicio_arraste(self) -> None:
        self._arrastando = True
        self.reprodutor.pedir_pausar.emit()

    def _arrastou(self, valor: int) -> None:
        self.reprodutor.pedir_ir_para.emit(valor / 1000 * self._duracao)

    def _fim_arraste(self) -> None:
        self._arrastando = False
        self.reprodutor.pedir_ir_para.emit(self.barra_tempo.value() / 1000 * self._duracao)

    def mostrar_foto(self, ligar: bool) -> None:
        """Modo "Foto": a foto do momento principal ocupa o lugar do vídeo."""
        self._modo_foto = ligar
        if ligar:
            self.reprodutor.pedir_pausar.emit()
            imagem = QImage(str(self._pasta / (self.evento.foto if self.evento else ARQUIVO_FOTO)))
            if not imagem.isNull():
                self.tela.definir_imagem(imagem)
                self.tela.definir_deteccoes(None)  # a foto já tem as caixas desenhadas
                self.tela.definir_selo("Foto do momento principal", "destaque")
        else:
            self.reprodutor.pedir_passo.emit(0)  # volta a mostrar o quadro atual do vídeo

    # ================================================================ ações
    def abrir_pasta(self) -> None:
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(self._pasta)))

    def exportar(self) -> None:
        if self.evento is None or self.camera is None:
            return
        sugestao = f"{self.camera.codigo}_{self.evento.inicio:%Y%m%d_%H%M%S}.mp4"
        destino, _filtro = QFileDialog.getSaveFileName(
            self, "Exportar o clipe do evento", str(Path.home() / sugestao), "Vídeo MP4 (*.mp4)"
        )
        if not destino:
            return
        try:
            shutil.copy2(self._pasta / self.evento.clipe, destino)
            foto = self._pasta / self.evento.foto
            if foto.is_file():
                shutil.copy2(foto, Path(destino).with_suffix(".jpg"))
        except OSError as erro:
            self._notificar("Não foi possível exportar", str(erro), "erro")
            return
        self._notificar("Evento exportado", f"{destino} (+ foto .jpg)", "sucesso")

    def excluir(self) -> None:
        if self.evento is None or self.evento.id is None:
            return
        resposta = QMessageBox.question(
            self, "Excluir evento",
            f"Excluir o evento de {self.evento.inicio:%d/%m/%Y %H:%M:%S}? O vídeo e as fotos serão apagados.",
        )
        if resposta != QMessageBox.StandardButton.Yes:
            return
        evento_id = self.evento.id
        self._estado_tocando(False)
        self.reprodutor.fechar_e_esperar()  # solta o clipe antes de apagar
        remover_evento(self.servico, self.evento, self.camera)
        self.evento = None
        self.excluido.emit(evento_id)
        self.voltar.emit()
