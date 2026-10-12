"""Tela da câmera: vídeo grande em resolução máxima (stream principal).

- Ao abrir, liga o stream principal da câmera; ao sair (Voltar, outra página ou
  fechar o app), ele é ENCERRADO — alta resolução gasta rede e CPU, então só
  fica ligada enquanto você está olhando.
- Enquanto a alta resolução não chega (segundos, no Wi-Fi), mostra o substream
  com o selo "Carregando alta resolução…": a tela nunca fica preta.
- Câmeras com várias vistas (canais e/ou lentes): abas "Todas | Lente 1 | Lente 2".
  "Todas" mostra lado a lado; uma aba mostra só aquela vista, bem grande.
- Duplo clique no vídeo (ou o botão) = tela cheia; Esc sai.

- Botões "Detecções" (caixas da IA por cima do vídeo) e "Movimento" (máscara do
  MOG2 em azul). Faixa "Detectado agora": classe, ID, confiança e tempo na cena.
  As caixas vêm da IA rodando no SUBSTREAM; como são normalizadas (0 a 1), são
  desenhadas na resolução do stream principal sem conversão nenhuma.

- Painel de eventos à direita (botão "Eventos" mostra/esconde): clicar num evento
  abre o visualizador (foto + clipe com as caixas) no lugar do vídeo ao vivo;
  "Voltar ao vivo" retorna.

- Botão "Zona": desenha a zona vigiada de uma vista (``ui/editor_zona.py``). Objetos
  dentro dela mostram o contador "parado 6/8 s"; o alertado fica vermelho e o bloco
  ganha a borda vermelha pulsando. Salvar grava no banco e avisa os serviços.

- PTZ (câmeras com motor): botão "PTZ" mostra o painel no canto do vídeo (setas, parar,
  posições e o indicador de direção). As setas do TECLADO também movem a câmera. O
  joystick da placa move a câmera aberta aqui (a janela avisa o link).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable

from PySide6.QtCore import QEvent, QObject, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QImage, QKeyEvent, QResizeEvent
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QPushButton,
    QScrollArea,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from camera.gerenciador import GerenciadorCameras
from camera.ptz import ServicoPTZ
from camera.stream import EstadoLeitor, LeitorVideo
from core.config import Config
from core.modelos import Camera, Evento, TipoFonte, Vista
from core.servico_cameras import ServicoCameras
from eventos.alertas import ServicoAlertas
from ui import grade, tema
from ui.bloco_camera import BlocoCamera
from ui.componentes import GrupoSegmentos, rotulo
from ui.controles_ptz import ControlesPTZ
from ui.distribuidor import Alvo, DistribuidorQuadros, converter_para_qimage
from ui.editor_zona import EditorZona
from ui.painel_eventos import PainelEventos
from ui.visualizador_evento import VisualizadorEvento
from vision.detector import ResultadoVista, ServicoDeteccao
from vision.dwell import SituacaoParado
from vision.zona import validar_zona

log = logging.getLogger(__name__)
Chave = tuple[int, int]
TODAS = "todas"
MARGEM = 16
#: tecla -> direção (x, y) do PTZ
TECLAS_PTZ = {Qt.Key.Key_Left: (-1, 0), Qt.Key.Key_Right: (1, 0), Qt.Key.Key_Up: (0, 1), Qt.Key.Key_Down: (0, -1)}


class _AreaVideo(QWidget):
    """Área onde os blocos são posicionados (avisa a tela quando muda de tamanho)."""

    redimensionada = Signal()

    def resizeEvent(self, evento: QResizeEvent) -> None:  # noqa: N802
        super().resizeEvent(evento)
        self.redimensionada.emit()


class FaixaDetectados(QFrame):
    """ "Detectado agora": um item por objeto rastreado nas vistas visíveis."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("faixaDetectados")
        self._itens = QHBoxLayout()
        self._itens.setContentsMargins(0, 0, 0, 0)
        self._itens.setSpacing(8)
        conteudo = QWidget()
        linha = QHBoxLayout(conteudo)
        linha.setContentsMargins(0, 0, 0, 0)
        linha.addLayout(self._itens)
        linha.addStretch(1)
        rolagem = QScrollArea()
        rolagem.setWidget(conteudo)
        rolagem.setWidgetResizable(True)
        rolagem.setFrameShape(QFrame.Shape.NoFrame)
        rolagem.setFixedHeight(40)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(16, 6, 16, 10)
        layout.setSpacing(12)
        layout.addWidget(rotulo("DETECTADO AGORA", "secao"))
        layout.addWidget(rolagem, 1)
        self.textos: list[str] = []

    def mostrar(self, linhas: list[tuple[str, str]]) -> None:
        """``linhas``: (classe, texto). Recria os itens só se algo mudou."""
        textos = [texto for _, texto in linhas] or ["Nada detectado agora"]
        if textos == self.textos:
            return
        self.textos = textos
        while self._itens.count():
            item = self._itens.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        if not linhas:
            self._itens.addWidget(rotulo("Nada detectado agora", "suave"))
            return
        for classe, texto in linhas:
            cor = tema.CORES_CLASSES.get(classe, "#94A3B8")
            item = QLabel(f"<span style='color:{cor}; font-size:14px'>●</span>&nbsp;{texto}")
            item.setObjectName("itemDetectado")
            item.setStyleSheet(
                f"#itemDetectado {{ background: {tema.paleta_atual().superficie_alta}; border-radius: 12px;"
                " padding: 4px 10px; }"
            )
            self._itens.addWidget(item)


class TelaCamera(QWidget):
    voltar = Signal()
    alternar_tela_cheia = Signal()
    info_mudou = Signal(str)  # resumo para a barra superior (resolução, FPS...)
    evento_excluido = Signal(int)  # a aba Eventos também precisa saber
    zona_salva = Signal(int)  # id da câmera: os serviços de eventos e alertas releem a zona

    def __init__(
        self,
        config: Config,
        servico: ServicoCameras,
        gerenciador: GerenciadorCameras,
        notificar: Callable[..., object],
        deteccao: ServicoDeteccao | None = None,
        alertas: ServicoAlertas | None = None,
        ptz: ServicoPTZ | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._ptz = ptz
        self._teclas_ptz: set[int] = set()
        self._servico = servico
        self._gerenciador = gerenciador
        self._notificar = notificar
        self._deteccao = deteccao
        self._resultados: dict[Chave, ResultadoVista | None] = {}
        self._situacoes: dict[Chave, SituacaoParado | None] = {}
        self._zonas: dict[Chave, list[tuple[float, float]] | None] = {}
        self._editor: EditorZona | None = None
        self._vista_zona: Vista | None = None
        self.camera: Camera | None = None
        self._blocos: dict[Chave, BlocoCamera] = {}
        self._foco: int | None = None  # número da vista em destaque; None = todas
        self._info = ""

        # Distribuidor próprio, no ritmo da alta resolução, usando o melhor leitor disponível.
        self.distribuidor = DistribuidorQuadros(
            gerenciador.melhor_leitor,
            float(config.obter("interface.fps_tela_camera")),
            deteccao.resultado_da_vista if deteccao else None,
            alertas.situacao_da_vista if alertas else None,
        )
        self.distribuidor.quadro_pronto.connect(self._receber_quadro)
        self.distribuidor.deteccoes_prontas.connect(self._receber_deteccoes)
        self.distribuidor.mascara_pronta.connect(self._receber_mascara)
        self.distribuidor.parado_pronto.connect(self._receber_parado)

        self.botao_voltar = QPushButton("Voltar")
        self.botao_voltar.setProperty("variante", "fantasma")
        self.botao_voltar.clicked.connect(self.voltar)
        self.abas = GrupoSegmentos([])
        self._container_abas = QWidget()
        self._layout_abas = QHBoxLayout(self._container_abas)
        self._layout_abas.setContentsMargins(0, 0, 0, 0)
        self.botao_deteccoes = QPushButton("Detecções")
        self.botao_deteccoes.setCheckable(True)
        self.botao_deteccoes.setChecked(True)
        self.botao_deteccoes.setToolTip("Mostrar as caixas dos objetos identificados pela IA")
        self.botao_deteccoes.toggled.connect(self._alternar_deteccoes)
        self.botao_movimento = QPushButton("Movimento")
        self.botao_movimento.setCheckable(True)
        self.botao_movimento.setToolTip("Mostrar a máscara de movimento (o que o detector de movimento vê)")
        self.botao_movimento.toggled.connect(self._alternar_movimento)
        self.botao_ptz = QPushButton("PTZ")
        self.botao_ptz.setCheckable(True)
        self.botao_ptz.setChecked(True)
        self.botao_ptz.setToolTip("Mostrar os controles do motor (também: setas do teclado)")
        self.botao_ptz.toggled.connect(self._mostrar_ptz)
        self.botao_ptz.hide()
        self.botao_zona = QPushButton("Zona")
        self.botao_zona.setToolTip("Desenhar a zona vigiada: onde um objeto parado gera alerta")
        self.botao_zona.clicked.connect(lambda: self.editar_zona())
        self.botao_foto = QPushButton("Tirar foto")
        self.botao_foto.clicked.connect(self.tirar_foto)
        self.botao_tela_cheia = QPushButton("Tela cheia")
        self.botao_tela_cheia.setToolTip("Tela cheia (duplo clique no vídeo; Esc para sair)")
        self.botao_tela_cheia.clicked.connect(self.alternar_tela_cheia)
        self.botao_eventos = QPushButton("Eventos")
        self.botao_eventos.setCheckable(True)
        self.botao_eventos.setChecked(True)
        self.botao_eventos.setToolTip("Mostrar/esconder a lista de eventos desta câmera")
        self.botao_eventos.toggled.connect(lambda ligado: self.painel.setVisible(ligado))

        # Botões do modo "editando a zona" (trocam de lugar com os normais).
        self.texto_zona = rotulo("", "suave")
        self.botao_desfazer = QPushButton("Desfazer")
        self.botao_desfazer.clicked.connect(self._desfazer_zona)
        self.botao_limpar_zona = QPushButton("Limpar")
        self.botao_limpar_zona.setToolTip("Apaga todos os pontos (sem zona = a imagem inteira é vigiada)")
        self.botao_limpar_zona.clicked.connect(self._limpar_zona)
        self.botao_cancelar_zona = QPushButton("Cancelar")
        self.botao_cancelar_zona.setProperty("variante", "fantasma")
        self.botao_cancelar_zona.clicked.connect(self.cancelar_zona)
        self.botao_salvar_zona = QPushButton("Salvar zona")
        self.botao_salvar_zona.setProperty("variante", "primario")
        self.botao_salvar_zona.clicked.connect(self.salvar_zona)

        self.barra = QFrame()
        self.barra.setObjectName("barraCamera")
        barra = QHBoxLayout(self.barra)
        barra.setContentsMargins(16, 10, 16, 0)
        barra.setSpacing(10)
        barra.addWidget(self.botao_voltar)
        barra.addWidget(self._container_abas)
        barra.addWidget(self.texto_zona)
        barra.addStretch(1)
        barra.addWidget(self.botao_deteccoes)
        barra.addWidget(self.botao_movimento)
        barra.addWidget(self.botao_zona)
        barra.addWidget(self.botao_ptz)
        barra.addSpacing(8)
        barra.addWidget(self.botao_foto)
        barra.addWidget(self.botao_tela_cheia)
        barra.addWidget(self.botao_eventos)
        for botao in (self.botao_desfazer, self.botao_limpar_zona, self.botao_cancelar_zona, self.botao_salvar_zona):
            barra.addWidget(botao)
        self._modo_normal: list[QWidget] = [
            self.botao_voltar, self._container_abas, self.botao_deteccoes, self.botao_movimento, self.botao_zona,
            self.botao_foto, self.botao_tela_cheia, self.botao_eventos,
        ]
        self._modo_zona: list[QWidget] = [
            self.texto_zona, self.botao_desfazer, self.botao_limpar_zona, self.botao_cancelar_zona,
            self.botao_salvar_zona,
        ]
        for widget in self._modo_zona:
            widget.hide()

        self.area = _AreaVideo()
        self.area.redimensionada.connect(self._reposicionar)
        self.controles_ptz = ControlesPTZ(ptz.velocidade_botoes if ptz else 0.5, self.area)
        self.controles_ptz.mover.connect(self._ptz_mover)
        self.controles_ptz.parar.connect(self._ptz_parar)
        self.controles_ptz.ir_preset.connect(self._ptz_ir_preset)
        self.controles_ptz.salvar_preset.connect(self._ptz_salvar_preset)
        self.controles_ptz.hide()
        self._timer_ptz = QTimer(self)
        self._timer_ptz.setInterval(66)  # o indicador acompanha o joystick (~15 vezes por segundo)
        self._timer_ptz.timeout.connect(self._atualizar_ptz)
        self.faixa = FaixaDetectados()
        self.faixa.setVisible(deteccao is not None)

        self.visualizador = VisualizadorEvento(servico, notificar, "Voltar ao vivo")
        self.visualizador.voltar.connect(self.voltar_ao_vivo)
        self.visualizador.excluido.connect(self._evento_excluido)
        self.pilha = QStackedWidget()
        self.pilha.addWidget(self.area)
        self.pilha.addWidget(self.visualizador)
        self.painel = PainelEventos(servico, todas_cameras=False)
        self.painel.setFixedWidth(360)
        self.painel.evento_escolhido.connect(self.mostrar_evento)

        meio = QHBoxLayout()
        meio.setContentsMargins(0, 0, 0, 0)
        meio.setSpacing(0)
        meio.addWidget(self.pilha, 1)
        meio.addWidget(self.painel)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self.barra)
        layout.addLayout(meio, 1)
        layout.addWidget(self.faixa)

        self._timer = QTimer(self)
        self._timer.setInterval(500)
        self._timer.timeout.connect(self._atualizar_estados)
        self._aplicar_icones()
        tema.sinais.tema_mudou.connect(self._aplicar_icones)

    # ================================================================ ciclo de vida
    def iniciar(self) -> None:
        self.distribuidor.iniciar()

    def parar(self) -> None:
        self.fechar()
        self.distribuidor.parar()
        self.visualizador.encerrar()

    def abrir(self, camera: Camera, vista_numero: int | None = None) -> None:
        """Mostra a câmera e liga o stream principal. ``vista_numero``: vista em destaque."""
        self.fechar()
        self.camera = camera
        self._gerenciador.abrir_principal(camera)
        vistas = camera.vistas()
        for vista in vistas:
            bloco = BlocoCamera(vista, camera.nome_vista(vista), self.area)
            ligado = self.botao_deteccoes.isChecked()
            bloco.configurar_sobreposicao(caixas=ligado, chips=False, zona=ligado)
            self._zonas[vista.chave] = self._servico.banco.obter_zona(camera.id or 0, vista.numero)
            bloco.definir_zona(self._zonas[vista.chave])
            bloco.duplo_clique.connect(self._duplo_clique)
            bloco.reconectar_pedido.connect(self._reconectar)
            bloco.hide()
            self._blocos[vista.chave] = bloco
        self._montar_abas(camera, vistas)
        self._foco = vista_numero if len(vistas) > 1 and vista_numero in [v.numero for v in vistas] else None
        if len(vistas) == 1:
            self._foco = None
        self.abas.definir(str(self._foco) if self._foco else TODAS)
        self.painel.definir_cameras([camera])
        self.painel.definir_camera(camera.id)
        self.pilha.setCurrentWidget(self.area)
        self._configurar_ptz(camera)
        self._reposicionar()
        self._atualizar_estados()
        self._timer.start()
        log.info("Tela da câmera aberta: %s (stream principal ligado)", camera.nome)

    def fechar(self) -> None:
        """Sai da câmera: ENCERRA o stream principal e libera os blocos."""
        if self.camera is None:
            return
        self.cancelar_zona()
        self._timer.stop()
        self._desligar_ptz()
        self.visualizador.fechar()
        self.pilha.setCurrentWidget(self.area)
        self.distribuidor.definir_alvos([])
        self._gerenciador.fechar_principal(self.camera.id)
        for bloco in self._blocos.values():
            bloco.deleteLater()
        self._blocos.clear()
        self._resultados.clear()
        self._situacoes.clear()
        self._zonas.clear()
        log.info("Tela da câmera fechada: %s (a tela liberou o stream principal)", self.camera.nome)
        self.camera = None
        self._info = ""

    @property
    def aberta(self) -> bool:
        return self.camera is not None

    @property
    def vendo_evento(self) -> bool:
        return self.pilha.currentWidget() is self.visualizador

    def mostrar_evento(self, evento: Evento) -> None:
        """Abre o visualizador no lugar do vídeo ao vivo."""
        if self.camera is None:
            return
        self.cancelar_zona()
        self.visualizador.abrir(evento, self.camera)
        self.pilha.setCurrentWidget(self.visualizador)
        self._atualizar_alvos()  # o ao vivo ficou escondido: para de converter quadros

    def voltar_ao_vivo(self) -> None:
        self.visualizador.fechar()
        self.pilha.setCurrentWidget(self.area)
        self._reposicionar()

    def _evento_excluido(self, evento_id: int) -> None:
        self.painel.eventos_removidos([evento_id])
        self.evento_excluido.emit(evento_id)

    @property
    def info(self) -> str:
        """Resumo atual (resolução, FPS...) para a barra superior."""
        return self._info

    def definir_modo_tela_cheia(self, ativo: bool) -> None:
        self.barra.setVisible(not ativo)
        self.faixa.setVisible(not ativo and self._deteccao is not None)

    # ================================================================ vistas
    def _montar_abas(self, camera: Camera, vistas: list[Vista]) -> None:
        self._layout_abas.removeWidget(self.abas)
        self.abas.deleteLater()
        opcoes: list[tuple[str, str, str | None]] = []
        if len(vistas) > 1:
            opcoes.append((TODAS, "Todas", "Todas as vistas lado a lado"))
            for vista in vistas:
                nome = camera.nome_vista(vista).removeprefix(f"{camera.nome} · ")
                opcoes.append((str(vista.numero), nome, None))
        self.abas = GrupoSegmentos(opcoes)
        self.abas.escolhido.connect(self._aba_escolhida)
        self.abas.setVisible(len(vistas) > 1)
        self._layout_abas.addWidget(self.abas)

    def _aba_escolhida(self, valor: str) -> None:
        self.definir_foco(None if valor == TODAS else int(valor))

    def definir_foco(self, numero: int | None) -> None:
        self._foco = numero
        self.abas.definir(str(numero) if numero else TODAS)
        self._reposicionar()

    def vistas_visiveis(self) -> list[Vista]:
        if self.camera is None:
            return []
        vistas = self.camera.vistas()
        return [v for v in vistas if self._foco is None or v.numero == self._foco]

    def bloco(self, chave: Chave) -> BlocoCamera | None:
        return self._blocos.get(chave)

    def _reposicionar(self) -> None:
        visiveis = self.vistas_visiveis()
        area = self.area.rect().adjusted(MARGEM, MARGEM, -MARGEM, -MARGEM)
        colunas, linhas = grade.melhor_grade(len(visiveis), area.width(), area.height(), tema.ESPACO)
        retangulos = grade.posicionar(len(visiveis), colunas, linhas, area.width(), area.height(), tema.ESPACO)
        posicoes = {v.chave: r for v, r in zip(visiveis, retangulos, strict=False)}
        for chave, bloco in self._blocos.items():
            if chave in posicoes:
                x, y, largura, altura = posicoes[chave]
                bloco.setGeometry(area.x() + x, area.y() + y, largura, altura)
                bloco.show()
            else:
                bloco.hide()
        if self.controles_ptz.isVisible():
            self._posicionar_ptz()
        self._atualizar_alvos()

    def _posicionar_ptz(self) -> None:
        """Painel PTZ no canto inferior direito DENTRO do vídeo (da lente com motor, se visível),
        logo acima da faixa com o nome — sem cobrir o selo da resolução."""
        self.controles_ptz.adjustSize()
        largura, altura = self.controles_ptz.width(), self.controles_ptz.height()
        visiveis = [b for b in self._blocos.values() if not b.isHidden()]
        lente = self.camera.lente_ptz if self.camera else 1
        bloco = next((b for b in visiveis if b.vista.lente == lente), visiveis[-1] if visiveis else None)
        if bloco is None:
            return
        imagem = bloco.area_imagem.translated(bloco.x(), bloco.y())
        x = imagem.right() - largura - 12
        y = imagem.bottom() - altura - 46
        self.controles_ptz.move(round(max(imagem.left() + 8, x)), round(max(imagem.top() + 8, y)))
        self.controles_ptz.raise_()

    def _atualizar_alvos(self) -> None:
        escala = self.devicePixelRatioF()
        alvos = [
            Alvo(
                vista=bloco.vista,
                largura=round(bloco.width() * escala),
                altura=round(bloco.height() * escala),
                escala_tela=escala,
                visivel=self.isVisible() and bloco.isVisibleTo(self),
                mascara=self.botao_movimento.isChecked(),
            )
            for bloco in self._blocos.values()
        ]
        self.distribuidor.definir_alvos(alvos)

    def showEvent(self, evento: object) -> None:  # noqa: N802
        super().showEvent(evento)  # type: ignore[arg-type]
        self._atualizar_alvos()

    def hideEvent(self, evento: object) -> None:  # noqa: N802
        super().hideEvent(evento)  # type: ignore[arg-type]
        self._atualizar_alvos()

    # ================================================================ dados
    def _receber_quadro(self, chave: Chave, imagem: QImage) -> None:
        bloco = self._blocos.get(chave)
        if bloco is not None:
            bloco.definir_imagem(imagem)

    def _receber_deteccoes(self, chave: Chave, resultado: ResultadoVista | None) -> None:
        bloco = self._blocos.get(chave)
        if bloco is not None:
            self._resultados[chave] = resultado
            bloco.definir_deteccoes(resultado.deteccoes if resultado else None)

    def _receber_parado(self, chave: Chave, situacao: SituacaoParado | None) -> None:
        bloco = self._blocos.get(chave)
        if bloco is not None:
            self._situacoes[chave] = situacao
            bloco.definir_parado(situacao)

    def _receber_mascara(self, chave: Chave, mascara: QImage | None) -> None:
        bloco = self._blocos.get(chave)
        if bloco is not None and self.botao_movimento.isChecked():
            bloco.definir_mascara(mascara)

    def _alternar_deteccoes(self, ligado: bool) -> None:
        for bloco in self._blocos.values():
            bloco.configurar_sobreposicao(caixas=ligado, chips=False, zona=ligado)

    def _alternar_movimento(self, ligado: bool) -> None:
        if not ligado:
            for bloco in self._blocos.values():
                bloco.definir_mascara(None)
        self._atualizar_alvos()

    def linhas_detectadas(self) -> list[tuple[str, str]]:
        """(classe, texto) dos objetos nas vistas visíveis: "PESSOA #7 · 87% · 12 s"."""
        if self.camera is None:
            return []
        agora = time.monotonic()
        visiveis = self.vistas_visiveis()
        varias = len(visiveis) > 1
        linhas: list[tuple[str, str]] = []
        for vista in visiveis:
            resultado = self._resultados.get(vista.chave)
            if resultado is None or agora - resultado.instante > 3.0:  # resultado velho: não mostra
                continue
            prefixo = self.camera.nome_vista(vista).removeprefix(f"{self.camera.nome} · ") + " · " if varias else ""
            situacao = self._situacoes.get(vista.chave)
            progresso = situacao.progresso if situacao is not None else {}
            for d in sorted(resultado.deteccoes, key=lambda d: (d.classe, d.id_rastreio or 0)):
                partes = [f"{prefixo}{d.classe} #{d.id_rastreio}" if d.id_rastreio else f"{prefixo}{d.classe}"]
                if d.origem == "ia":
                    partes.append(f"{d.confianca * 100:.0f}%")
                desde = resultado.desde.get(d.id_rastreio or -1)
                if desde is not None:
                    partes.append(f"{max(0.0, agora - desde):.0f} s")
                parado = progresso.get(d.id_rastreio or -1)
                if parado is not None and parado.alertado:
                    partes.append(f"PARADO {parado.segundos:.0f} s · ALERTA")
                elif parado is not None and parado.segundos >= 1:
                    partes.append(f"parado {parado.segundos:.0f}/{parado.alvo:.0f} s")
                linhas.append((d.classe, " · ".join(partes)))
        return linhas

    def _atualizar_estados(self) -> None:
        """A cada meio segundo: estado, selo (HD/carregando), "Detectado agora" e resumo."""
        if self.camera is None:
            return
        if self._deteccao is not None:
            self.faixa.mostrar(self.linhas_detectadas())
        resumo = ""
        for vista in self.camera.vistas():
            bloco = self._blocos.get(vista.chave)
            if bloco is None:
                continue
            principal = self._gerenciador.leitor_principal(vista.camera_id, vista.canal)
            sub = self._gerenciador.leitor_da_vista(vista)
            hd = principal is not None and principal.estado is EstadoLeitor.ONLINE and principal.ultimo_quadro()
            if hd:
                assert principal is not None
                largura, altura = _tamanho_da_vista(principal, vista)
                fps = f" · {principal.fps:.0f} FPS" if principal.fps else ""
                bloco.definir_estado("online", principal.mensagem, 0)
                bloco.definir_selo(f"HD · {largura}×{altura}{fps}", "sucesso")
                resumo = resumo or f"Alta resolução · {principal.mensagem}{fps}"
            elif sub is not None and sub.estado is EstadoLeitor.ONLINE:
                bloco.definir_estado("online", sub.mensagem, 0)
                if self.camera.fonte is not TipoFonte.RTSP:
                    origem = "Webcam" if self.camera.fonte is TipoFonte.WEBCAM else "Arquivo de vídeo"
                    fps = f" · {sub.fps:.0f} FPS" if sub.fps else ""
                    bloco.definir_selo(f"{origem} · {sub.mensagem}{fps}", "sucesso")
                    resumo = resumo or f"{origem} · {sub.mensagem}"
                elif principal is not None and principal.estado is EstadoLeitor.OFFLINE:
                    bloco.definir_selo("Alta resolução indisponível: mostrando o substream", "aviso")
                    resumo = resumo or f"Alta resolução indisponível ({principal.mensagem})"
                else:
                    bloco.definir_selo("Carregando alta resolução…", "aviso")
                    resumo = resumo or "Conectando à alta resolução…"
            else:
                origem = principal or sub
                if origem is not None:
                    bloco.definir_estado(
                        origem.estado.value, origem.mensagem, int(origem.segundos_para_tentar() + 0.999)
                    )
                bloco.definir_selo(None)
                resumo = resumo or "Câmera offline"
        if resumo != self._info:
            self._info = resumo
            self.info_mudou.emit(resumo)

    # ================================================================ zona
    @property
    def editando_zona(self) -> bool:
        return self._editor is not None

    @property
    def editor(self) -> EditorZona | None:
        return self._editor

    def editar_zona(self, numero: int | None = None) -> None:
        """Entra no modo de edição da zona de uma vista (pergunta qual, se houver várias à vista)."""
        if self.camera is None or self._editor is not None:
            return
        if self.vendo_evento:
            self.voltar_ao_vivo()
        visiveis = self.vistas_visiveis()
        if numero is None and len(visiveis) > 1:
            menu = QMenu(self)
            for vista in visiveis:
                nome = self.camera.nome_vista(vista).removeprefix(f"{self.camera.nome} · ")
                menu.addAction(f"Zona de “{nome}”", lambda n=vista.numero: self.editar_zona(n))
            menu.exec(self.botao_zona.mapToGlobal(self.botao_zona.rect().bottomLeft()))
            return
        if numero is None:  # a vista que está na tela (aba "Lente 2" -> zona da Lente 2), nunca "a primeira"
            vista = visiveis[0] if visiveis else None
        else:
            vista = next((v for v in self.camera.vistas() if v.numero == numero), None)
        bloco = self._blocos.get(vista.chave) if vista else None
        if vista is None or bloco is None:
            return
        if not bloco.tem_imagem:
            self._notificar("Ainda sem imagem", "Espere o vídeo aparecer para desenhar a zona.", "aviso")
            return
        if len(self.camera.vistas()) > 1:
            self.definir_foco(vista.numero)  # só a vista editada, bem grande
        self._vista_zona = vista
        self._editor = EditorZona(bloco, self._zonas.get(vista.chave))
        self._editor.mudou.connect(self._editor_mudou)
        self.texto_zona.setText(f"Editando a zona de {self.camera.nome_vista(vista)}")
        self._trocar_barra(editando=True)
        self._editor_mudou()

    def salvar_zona(self) -> bool:
        """Valida e grava a zona no banco. Devolve True se salvou."""
        editor, vista, camera = self._editor, self._vista_zona, self.camera
        if editor is None or vista is None or camera is None:
            return False
        if problema := validar_zona(editor.pontos):
            self._notificar("Zona não salva", problema, "aviso")
            return False
        # Arredonda como o banco grava: a tela e os serviços usam exatamente a mesma zona.
        pontos = [(round(x, 5), round(y, 5)) for x, y in editor.pontos] or None
        try:
            self._servico.banco.salvar_zona(camera.id or 0, vista.numero, pontos)
        except Exception as erro:  # noqa: BLE001 (ex.: banco travado)
            self._notificar("Não foi possível salvar a zona", str(erro), "erro")
            return False
        self._zonas[vista.chave] = pontos
        bloco = self._blocos.get(vista.chave)
        if bloco is not None:
            bloco.definir_zona(pontos)
        self._sair_da_edicao()
        log.info("Zona salva: %s (%s)", camera.nome_vista(vista), f"{len(pontos)} pontos" if pontos else "imagem inteira")
        vigia_tudo = str(self._servico.config.obter_ou("parado.sem_zona", "ignorar")) == "imagem_inteira"
        sem_zona = "a imagem inteira é vigiada" if vigia_tudo else "esta lente não gera alertas (sem zona)"
        self._notificar(
            "Zona salva" if pontos else "Zona removida",
            f"{camera.nome_vista(vista)} · " + ("objetos parados dentro dela geram alerta" if pontos else sem_zona),
            "sucesso",
        )
        self.zona_salva.emit(camera.id or 0)
        return True

    def cancelar_zona(self) -> None:
        if self._editor is not None:
            self._sair_da_edicao()

    def _sair_da_edicao(self) -> None:
        if self._editor is not None:
            self._editor.encerrar()
        self._editor = None
        self._vista_zona = None
        self._trocar_barra(editando=False)

    def _trocar_barra(self, editando: bool) -> None:
        for widget in self._modo_normal:
            widget.setVisible(not editando)
        for widget in self._modo_zona:
            widget.setVisible(editando)
        self.botao_ptz.setVisible(not editando and self.tem_ptz)
        self._mostrar_ptz(self.botao_ptz.isChecked() and not editando)

    def _editor_mudou(self) -> None:
        if self._editor is not None:
            self.botao_desfazer.setEnabled(self._editor.pode_desfazer)
            self.botao_limpar_zona.setEnabled(bool(self._editor.pontos))

    def _desfazer_zona(self) -> None:
        if self._editor is not None:
            self._editor.desfazer()

    def _limpar_zona(self) -> None:
        if self._editor is not None:
            self._editor.limpar()

    # ================================================================ PTZ
    @property
    def tem_ptz(self) -> bool:
        return self._ptz is not None and self.camera is not None and self.camera.tem_ptz

    def _configurar_ptz(self, camera: Camera) -> None:
        self.botao_ptz.setVisible(self.tem_ptz)
        if not self.tem_ptz:
            self._mostrar_ptz(False)
            return
        vista = next((v for v in camera.vistas() if v.lente == camera.lente_ptz), camera.vistas()[0])
        self.controles_ptz.titulo.setText("PTZ")
        self.controles_ptz.titulo.setToolTip(f"Motor: {camera.nome_vista(vista)}")
        QApplication.instance().installEventFilter(self)  # setas do teclado
        self._mostrar_ptz(self.botao_ptz.isChecked())

    def _desligar_ptz(self) -> None:
        if self.controles_ptz.segurando or self._teclas_ptz:
            self._teclas_ptz.clear()
            self.controles_ptz.soltar()  # soltar = parar a câmera
        QApplication.instance().removeEventFilter(self)
        self._mostrar_ptz(False)

    def _mostrar_ptz(self, mostrar: bool) -> None:
        mostrar = mostrar and self.tem_ptz and not self.editando_zona
        self.controles_ptz.setVisible(mostrar)
        if mostrar:
            self._timer_ptz.start()
            self._atualizar_ptz()
            self._reposicionar()
        else:
            self._timer_ptz.stop()

    def _atualizar_ptz(self) -> None:
        if self._ptz is not None and self.camera is not None:
            self.controles_ptz.mostrar_estado(self._ptz.estado(self.camera.id))

    def _ptz_mover(self, x: float, y: float) -> None:
        if self._ptz is not None and self.camera is not None:
            self._ptz.mover(self.camera.id, x, y)

    def _ptz_parar(self) -> None:
        if self._ptz is not None and self.camera is not None:
            self._ptz.parar_camera(self.camera.id)

    def _ptz_ir_preset(self, numero: int) -> None:
        if self._ptz is not None and self.camera is not None:
            self._ptz.ir_preset(self.camera.id, numero)
            self._notificar(f"Indo para a posição {numero}", self.camera.nome, "info")

    def _ptz_salvar_preset(self, numero: int) -> None:
        if self._ptz is not None and self.camera is not None:
            self._ptz.salvar_preset(self.camera.id, numero)
            self._notificar(f"Posição {numero} salva", f"{self.camera.nome}: a câmera volta aqui com “Ir para a posição {numero}”.", "sucesso")

    def eventFilter(self, objeto: QObject, evento: QEvent) -> bool:  # noqa: N802
        """Setas do teclado = PTZ (só com a Tela da câmera na frente e sem campo de texto em foco)."""
        tipo = evento.type()
        if tipo not in (QEvent.Type.KeyPress, QEvent.Type.KeyRelease) or not self.controles_ptz.isVisible():
            return False
        assert isinstance(evento, QKeyEvent)
        tecla = evento.key()
        if tecla not in TECLAS_PTZ or self.vendo_evento or not self.isVisible():
            return False
        if isinstance(QApplication.focusWidget(), QLineEdit):
            return False
        if evento.isAutoRepeat():
            return True  # segurar a tecla gera repetições: o painel já repete o comando
        if tipo == QEvent.Type.KeyPress:
            self._teclas_ptz.add(tecla)
        else:
            self._teclas_ptz.discard(tecla)
        dx = sum(TECLAS_PTZ[t][0] for t in self._teclas_ptz)
        dy = sum(TECLAS_PTZ[t][1] for t in self._teclas_ptz)
        if dx or dy:
            self.controles_ptz.segurar_direcao(max(-1, min(1, dx)), max(-1, min(1, dy)))
        else:
            self.controles_ptz.soltar()
        return True

    # ================================================================ ações
    def _duplo_clique(self, _chave: Chave) -> None:
        self.alternar_tela_cheia.emit()

    def _reconectar(self, chave: Chave) -> None:
        self._gerenciador.reconectar(chave[0])

    def tirar_foto(self) -> list[str]:
        """Salva a imagem de cada vista visível na melhor resolução disponível."""
        camera = self.camera
        if camera is None:
            return []
        salvas: list[str] = []
        ultima = None
        for vista in self.vistas_visiveis():
            leitor = self._gerenciador.melhor_leitor(vista)
            quadro = leitor.ultimo_quadro() if leitor else None
            if quadro is None:
                continue
            imagem = vista.recortar(quadro.imagem)
            try:
                caminho = self._servico.salvar_foto(camera, vista, imagem)
            except OSError as erro:
                self._notificar("Não foi possível salvar a foto", str(erro), "erro")
                return salvas
            salvas.append(str(caminho))
            ultima = (caminho, imagem)
        if ultima is None:
            self._notificar("Sem imagem para fotografar", "A câmera ainda não está online.", "aviso")
            return salvas
        caminho, imagem = ultima
        altura, largura = imagem.shape[:2]
        quantidade = f"{len(salvas)} fotos" if len(salvas) > 1 else "Foto"
        self._notificar(
            f"{quantidade} em {largura}×{altura}",
            f"{camera.nome} — clique para abrir a pasta",
            "sucesso",
            miniatura=converter_para_qimage(imagem, 176, 100),
            ao_clicar=lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(caminho.parent))),
        )
        return salvas

    def _aplicar_icones(self, _paleta: object = None) -> None:
        self.botao_voltar.setIcon(tema.icone("mdi6.arrow-left", "texto"))
        self.botao_foto.setIcon(tema.icone("mdi6.camera-outline", "texto"))
        self.botao_deteccoes.setIcon(tema.icone("mdi6.vector-rectangle", "texto"))
        self.botao_movimento.setIcon(tema.icone("mdi6.motion-sensor", "texto"))
        self.botao_zona.setIcon(tema.icone("mdi6.vector-polygon", "texto"))
        self.botao_desfazer.setIcon(tema.icone("mdi6.undo", "texto"))
        self.botao_limpar_zona.setIcon(tema.icone("mdi6.eraser", "texto"))
        self.botao_salvar_zona.setIcon(tema.icone("mdi6.check", "destaque_texto"))
        self.botao_tela_cheia.setIcon(tema.icone("mdi6.fullscreen", "texto"))
        self.botao_eventos.setIcon(tema.icone("mdi6.bell-outline", "texto"))


def _tamanho_da_vista(leitor: LeitorVideo, vista: Vista) -> tuple[int, int]:
    """Resolução da vista dentro do quadro (ex.: 2304x2592 empilhada -> 2304x1296)."""
    quadro = leitor.ultimo_quadro()
    if quadro is None:
        return 0, 0
    altura, largura = vista.recortar(quadro.imagem).shape[:2]
    return largura, altura
