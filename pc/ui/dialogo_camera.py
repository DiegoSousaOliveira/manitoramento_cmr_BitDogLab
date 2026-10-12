"""Diálogo "Adicionar câmera" / "Editar câmera".

Esquerda: o formulário (identificação, fonte do vídeo, imagem/lentes, PTZ, opções
e o modelo de URL em "Avançado"). Direita: "Testar conexão", com miniatura ao
vivo, resolução/FPS, teste do ONVIF e sugestão automática de lentes.

Validação: cada campo mostra seu erro logo abaixo, em português. Nada é gravado
enquanto houver erro. Quem grava é o ``ServicoCameras`` (banco + cofre + pastas).
"""

from __future__ import annotations

import logging
import time
from dataclasses import replace
from pathlib import Path

from PySide6.QtCore import QLineF, QPointF, QRectF, QSize, Qt, QTimer
from PySide6.QtGui import QCloseEvent, QColor, QFont, QImage, QPainter, QPainterPath, QPaintEvent, QPen
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from camera.diagnostico import ResultadoOnvif, testar_onvif
from camera.sonda_rtsp import ResultadoSonda, sondar_rtsp
from camera.stream import EstadoLeitor, LeitorVideo, LimitesLeitura
from core.banco import ErroBanco
from core.config import Config
from core.modelos import Camera, DisposicaoLentes, TipoFonte, TipoStream
from core.segredos import ErroCofre
from core.servico_cameras import ServicoCameras
from core.validacao import (
    CODIGO_MAX,
    NOME_MAX,
    gerar_codigo,
    normalizar_nome,
    validar_codigo,
    validar_ip,
    validar_modelo_url,
    validar_nome,
    validar_porta,
)
from ui import tema
from ui.componentes import (
    CampoNumero,
    CampoSenha,
    Cartao,
    GrupoSegmentos,
    Interruptor,
    SecaoRecolhivel,
    repolir,
    rotulo,
)
from ui.distribuidor import converter_para_qimage
from ui.tarefas import executar

log = logging.getLogger(__name__)

TEXTOS_LENTES: dict[DisposicaoLentes, str] = {
    DisposicaoLentes.UNICA: "Uma lente (imagem inteira)",
    DisposicaoLentes.VERTICAL: "Duas lentes empilhadas (em cima e embaixo)",
    DisposicaoLentes.HORIZONTAL: "Duas lentes lado a lado",
}
PERSONALIZADO = "personalizado"


# ===================================================================== prévia
class PreviaVideo(QWidget):
    """Miniatura do teste de conexão, com as linhas de divisão das lentes."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._imagem: QImage | None = None
        self._texto = "Clique em “Testar conexão” para ver a imagem."
        self._disposicao = DisposicaoLentes.UNICA
        self.setMinimumSize(QSize(340, 220))

    def definir_imagem(self, imagem: QImage | None) -> None:
        self._imagem = imagem
        self.update()

    def definir_texto(self, texto: str) -> None:
        self._texto = texto
        self.update()

    def definir_disposicao(self, disposicao: DisposicaoLentes) -> None:
        self._disposicao = disposicao
        self.update()

    def tamanho_util(self) -> QSize:
        return self.size() * self.devicePixelRatioF()

    def paintEvent(self, _evento: QPaintEvent) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        area = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        caminho = QPainterPath()
        caminho.addRoundedRect(area, 10, 10)
        p.setClipPath(caminho)
        p.fillRect(area, tema.cor("video_fundo"))
        if self._imagem is None:
            p.setPen(QColor(170, 176, 190))
            p.drawText(area.adjusted(20, 0, -20, 0), Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap, self._texto)
            return
        escala = self._imagem.devicePixelRatio() or 1.0
        larg, alt = self._imagem.width() / escala, self._imagem.height() / escala
        fator = min(area.width() / larg, area.height() / alt)
        destino = QRectF(0, 0, larg * fator, alt * fator)
        destino.moveCenter(area.center())
        p.drawImage(destino, self._imagem)
        if self._disposicao is DisposicaoLentes.UNICA:
            return
        caneta = QPen(tema.cor("destaque"), 2, Qt.PenStyle.DashLine)
        p.setPen(caneta)
        if self._disposicao is DisposicaoLentes.VERTICAL:
            y = destino.center().y()
            p.drawLine(QLineF(destino.left(), y, destino.right(), y))
            cantos = [destino.topLeft(), QPointF(destino.left(), y)]
        else:
            x = destino.center().x()
            p.drawLine(QLineF(x, destino.top(), x, destino.bottom()))
            cantos = [destino.topLeft(), QPointF(x, destino.top())]
        fonte = QFont(self.font())
        fonte.setWeight(QFont.Weight.Bold)
        p.setFont(fonte)
        for numero, canto in enumerate(cantos, start=1):
            etiqueta = QRectF(canto.x() + 8, canto.y() + 8, 62, 22)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(tema.cor("destaque"))
            p.drawRoundedRect(etiqueta, 6, 6)
            p.setPen(tema.cor("destaque_texto"))
            p.drawText(etiqueta, Qt.AlignmentFlag.AlignCenter, f"Lente {numero}")


class LinhaResultado(QWidget):
    """Uma linha do resultado do teste: ícone + texto (+ botão opcional)."""

    ICONES = {
        "ok": ("mdi6.check-circle-outline", "sucesso"),
        "erro": ("mdi6.alert-circle-outline", "alerta"),
        "aviso": ("mdi6.information-outline", "aviso"),
        "pendente": ("mdi6.dots-horizontal", "texto_fraco"),
    }

    def __init__(self, situacao: str, texto: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._icone = QLabel()
        self._icone.setFixedSize(20, 20)
        self._texto = QLabel()
        self._texto.setWordWrap(True)
        self._layout = QHBoxLayout(self)
        self._layout.setContentsMargins(0, 2, 0, 2)
        self._layout.setSpacing(8)
        self._layout.addWidget(self._icone, alignment=Qt.AlignmentFlag.AlignTop)
        self._layout.addWidget(self._texto, 1)
        self.definir(situacao, texto)

    def definir(self, situacao: str, texto: str) -> None:
        nome, cor = self.ICONES.get(situacao, self.ICONES["pendente"])
        self._icone.setPixmap(tema.icone(nome, cor).pixmap(18, 18))
        self._texto.setText(texto)
        self.situacao = situacao

    @property
    def texto(self) -> str:
        return self._texto.text()

    def adicionar_botao(self, botao: QPushButton) -> None:
        self._layout.addWidget(botao, alignment=Qt.AlignmentFlag.AlignTop)


# ===================================================================== diálogo
class DialogoCamera(QDialog):
    def __init__(
        self,
        pai: QWidget | None,
        config: Config,
        servico: ServicoCameras,
        limites: LimitesLeitura,
        camera: Camera | None = None,
    ) -> None:
        super().__init__(pai)
        self.config = config
        self.servico = servico
        self.limites = limites
        self.original = camera
        self.camera_salva: Camera | None = None
        self._codigo_manual = camera is not None
        self._leitor_teste: LeitorVideo | None = None
        self._ultimo_numero_previa = -1
        self._primeiro_quadro = True
        self._fps_atualizado_em = 0.0
        self._linha_video: LinhaResultado | None = None
        self._tentou_salvar = False
        self._erros: dict[str, QLabel] = {}
        self._campos_erro: dict[str, QWidget] = {}
        self._modelos = config.modelos_url()

        self._timer_previa = QTimer(self)
        self._timer_previa.setInterval(100)  # prévia a 10 quadros por segundo
        self._timer_previa.timeout.connect(self._atualizar_previa)

        self.setWindowTitle("Editar câmera" if camera else "Adicionar câmera")
        self.setModal(True)
        self.resize(1040, 760)
        self.setMinimumSize(900, 560)

        self._montar()
        self._preencher(camera)
        self._conectar()
        self._atualizar_visibilidade()

    # ================================================================ montagem
    def _campo(self, chave: str, titulo: str, widget: QWidget, dica: str = "") -> QWidget:
        """Bloco "título / campo / erro" (e uma dica opcional)."""
        bloco = QWidget()
        layout = QVBoxLayout(bloco)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(5)
        titulo_rotulo = rotulo(titulo)
        titulo_rotulo.setStyleSheet("font-weight: 600;")
        layout.addWidget(titulo_rotulo)
        layout.addWidget(widget)
        if dica:
            layout.addWidget(rotulo(dica, "dica", quebra=True))
        erro = rotulo("", "erro", quebra=True)
        erro.hide()
        layout.addWidget(erro)
        self._erros[chave] = erro
        self._campos_erro[chave] = widget
        return bloco

    @staticmethod
    def _lista_numeros(minimo: int, maximo: int) -> QComboBox:
        """Lista para escolher um número pequeno (canais, webcam)."""
        lista = QComboBox()
        for numero in range(minimo, maximo + 1):
            lista.addItem(str(numero), numero)
        return lista

    @staticmethod
    def _secao(titulo: str, *linhas: QWidget) -> QWidget:
        cartao = Cartao()
        layout = QVBoxLayout(cartao)
        layout.setContentsMargins(18, 16, 18, 18)
        layout.setSpacing(14)
        layout.addWidget(rotulo(titulo.upper(), "secao"))
        for linha in linhas:
            layout.addWidget(linha)
        return cartao

    @staticmethod
    def _lado_a_lado(*widgets: QWidget, pesos: tuple[int, ...] | None = None) -> QWidget:
        container = QWidget()
        layout = QHBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(14)
        for indice, widget in enumerate(widgets):
            layout.addWidget(widget, (pesos or (1,) * len(widgets))[indice], Qt.AlignmentFlag.AlignTop)
        return container

    def _montar(self) -> None:
        # ---------- identificação
        self.nome = QLineEdit()
        self.nome.setMaxLength(NOME_MAX)
        self.nome.setPlaceholderText("Ex.: Frente da casa")
        self.codigo = QLineEdit()
        self.codigo.setMaxLength(CODIGO_MAX)
        self.codigo.setPlaceholderText("FRENTE")
        identificacao = self._secao(
            "Identificação",
            self._lado_a_lado(
                self._campo("nome", "Nome", self.nome, "Único. Também nomeia a pasta dos eventos."),
                self._campo("codigo", "Código para a placa", self.codigo, "Até 10 letras/números. Vai nos alertas da BitDogLab."),
                pesos=(3, 2),
            ),
        )

        # ---------- fonte do vídeo
        self.fonte = GrupoSegmentos(
            [
                (TipoFonte.RTSP.value, "Câmera IP (RTSP)", "Câmera de verdade na rede"),
                (TipoFonte.ARQUIVO.value, "Arquivo de vídeo", "Vídeo .mp4 em loop, para testes"),
                (TipoFonte.WEBCAM.value, "Webcam", "Webcam do computador, para testes"),
            ]
        )
        self._fonte_atual = TipoFonte.RTSP

        self.ip = QLineEdit()
        self.ip.setPlaceholderText("192.168.0.50")
        self.porta = CampoNumero(1, 65535)
        self.usuario = QLineEdit()
        self.senha = CampoSenha()
        self.senha.setPlaceholderText("opcional")
        self._bloco_rtsp = QWidget()
        rtsp = QVBoxLayout(self._bloco_rtsp)
        rtsp.setContentsMargins(0, 0, 0, 0)
        rtsp.setSpacing(12)
        rtsp.addWidget(
            self._lado_a_lado(
                self._campo("ip", "IP na rede local", self.ip),
                self._campo("porta", "Porta RTSP", self.porta),
                pesos=(3, 1),
            )
        )
        rtsp.addWidget(
            self._lado_a_lado(
                self._campo("usuario", "Usuário", self.usuario),
                self._campo("senha", "Senha", self.senha, "Deixe vazio se a câmera não tem senha."),
            )
        )

        self.caminho = QComboBox()
        self.caminho.setEditable(True)
        self.caminho.lineEdit().setPlaceholderText("videos/exemplo.mp4")
        for item in self.config.obter_ou("fontes_teste", []):
            if item.get("tipo") == "arquivo":
                self.caminho.addItem(str(item.get("caminho", "")))
        procurar = QPushButton("Procurar…")
        procurar.clicked.connect(self._procurar_arquivo)
        self._bloco_arquivo = self._campo(
            "caminho",
            "Arquivo de vídeo",
            self._lado_a_lado(self.caminho, procurar, pesos=(1, 0)),
            "Toca em loop, como se fosse uma câmera. Caminho relativo = dentro da pasta pc/.",
        )
        self.webcam = self._lista_numeros(0, 9)
        self._bloco_webcam = self._campo("webcam", "Número da webcam", self.webcam, "0 = primeira webcam do computador.")

        fonte = self._secao("Fonte do vídeo", self.fonte, self._bloco_rtsp, self._bloco_arquivo, self._bloco_webcam)

        # ---------- imagem e lentes
        self.canais = self._lista_numeros(1, int(self.config.obter("cadastro.max_canais")))
        self.lentes = QComboBox()
        for disposicao, texto in TEXTOS_LENTES.items():
            self.lentes.addItem(texto, disposicao)
        self._grade_nomes = QGridLayout()
        self._grade_nomes.setHorizontalSpacing(14)
        self._grade_nomes.setVerticalSpacing(8)
        self._nomes_vistas: list[QLineEdit] = []
        nomes = QWidget()
        nomes.setLayout(self._grade_nomes)
        self._bloco_nomes = self._campo("nomes", "Nomes das vistas (opcional)", nomes)
        self._bloco_canais = self._campo(
            "canais", "Canais (fluxos RTSP)", self.canais, "Use mais de 1 só se cada lente tiver seu próprio fluxo."
        )
        imagem = self._secao(
            "Imagem",
            self._lado_a_lado(
                self._bloco_canais,
                self._campo(
                    "lentes", "Lentes dentro da imagem", self.lentes,
                    "Câmeras de duas lentes costumam mandar as duas imagens juntas: o app corta ao meio.",
                ),
                pesos=(1, 2),
            ),
            self._bloco_nomes,
        )

        # ---------- PTZ
        self.ptz = Interruptor("A câmera tem motor (PTZ)")
        self.porta_onvif = CampoNumero(1, 65535)
        self.lente_ptz = QComboBox()
        self.lente_ptz.addItem("Lente 1", 1)
        self.lente_ptz.addItem("Lente 2", 2)
        self._bloco_lente_ptz = self._campo("lente_ptz", "Lente que o motor move", self.lente_ptz)
        self._detalhes_ptz = self._lado_a_lado(
            self._campo("porta_onvif", "Porta ONVIF", self.porta_onvif), self._bloco_lente_ptz
        )
        ptz = self._secao("Motor (PTZ)", self.ptz, self._detalhes_ptz)

        # ---------- opções
        self.deteccao = Interruptor("Detecção de objetos ativada")
        self.gravar = Interruptor("Gravar eventos (foto e vídeo)")
        opcoes = self._secao("Opções", self.deteccao, self.gravar)

        # ---------- avançado
        self.modelo = QComboBox()
        for chave, (nome, _url) in self._modelos.items():
            self.modelo.addItem(nome, chave)
        self.modelo.addItem("Personalizado", PERSONALIZADO)
        self.modelo_url = QLineEdit()
        conteudo_avancado = QWidget()
        avancado_layout = QVBoxLayout(conteudo_avancado)
        avancado_layout.setContentsMargins(0, 0, 0, 0)
        avancado_layout.setSpacing(12)
        avancado_layout.addWidget(self._campo("modelo", "Modelo pronto", self.modelo))
        avancado_layout.addWidget(
            self._campo(
                "modelo_url", "Modelo de URL RTSP", self.modelo_url,
                "Variáveis: {ip} {porta} {usuario} {senha} {canal} {stream} (0 = principal, 1 = substream).",
            )
        )
        self._avancado = SecaoRecolhivel("Avançado: modelo de URL RTSP", conteudo_avancado)
        avancado = Cartao()
        QVBoxLayout(avancado).addWidget(self._avancado)
        avancado.layout().setContentsMargins(12, 10, 12, 12)

        # ---------- coluna do formulário (rolável)
        formulario = QWidget()
        coluna = QVBoxLayout(formulario)
        coluna.setContentsMargins(20, 20, 12, 20)
        coluna.setSpacing(14)
        for secao in (identificacao, fonte, imagem, ptz, opcoes, avancado):
            coluna.addWidget(secao)
        coluna.addStretch(1)
        rolagem = QScrollArea()
        rolagem.setWidgetResizable(True)
        rolagem.setWidget(formulario)
        rolagem.setFrameShape(QFrame.Shape.NoFrame)

        # ---------- painel de teste
        self.previa = PreviaVideo()
        self.botao_testar = QPushButton("Testar conexão")
        self.botao_testar.setIcon(tema.icone("mdi6.connection", "texto"))
        self.botao_testar.clicked.connect(self._testar)
        self._resultados = QVBoxLayout()
        self._resultados.setSpacing(6)
        painel = Cartao()
        painel.setFixedWidth(400)
        painel_layout = QVBoxLayout(painel)
        painel_layout.setContentsMargins(18, 16, 18, 18)
        painel_layout.setSpacing(12)
        painel_layout.addWidget(rotulo("TESTAR CONEXÃO", "secao"))
        painel_layout.addWidget(self.previa, 1)
        painel_layout.addWidget(self.botao_testar)
        painel_layout.addLayout(self._resultados)
        painel_layout.addStretch(0)

        corpo = QHBoxLayout()
        corpo.setContentsMargins(0, 0, 20, 0)
        corpo.setSpacing(8)
        corpo.addWidget(rolagem, 1)
        lado = QVBoxLayout()
        lado.setContentsMargins(0, 20, 0, 20)
        lado.addWidget(painel)
        corpo.addLayout(lado)

        # ---------- rodapé
        self.mensagem_geral = rotulo("", "erro", quebra=True)
        self.botao_cancelar = QPushButton("Cancelar")
        self.botao_cancelar.clicked.connect(self.reject)
        self.botao_salvar = QPushButton("Salvar câmera" if not self.original else "Salvar alterações")
        self.botao_salvar.setProperty("variante", "primario")
        self.botao_salvar.setDefault(True)
        self.botao_salvar.clicked.connect(self._salvar)
        rodape = QFrame()
        rodape.setObjectName("rodapeDialogo")
        rodape_layout = QHBoxLayout(rodape)
        rodape_layout.setContentsMargins(20, 12, 20, 12)
        rodape_layout.addWidget(self.mensagem_geral, 1)
        rodape_layout.addWidget(self.botao_cancelar)
        rodape_layout.addWidget(self.botao_salvar)

        raiz = QVBoxLayout(self)
        raiz.setContentsMargins(0, 0, 0, 0)
        raiz.setSpacing(0)
        raiz.addLayout(corpo, 1)
        raiz.addWidget(rodape)

    # ================================================================ preenchimento
    def _preencher(self, camera: Camera | None) -> None:
        cadastro = lambda chave: self.config.obter(f"cadastro.{chave}")  # noqa: E731
        if camera is None:
            camera = Camera(
                nome="",
                codigo="",
                fonte=TipoFonte.RTSP,
                ip="",
                porta_rtsp=int(cadastro("porta_rtsp")),
                usuario=str(cadastro("usuario")),
                modelo_url=self._modelos.get(str(cadastro("modelo_url")), next(iter(self._modelos.values())))[1],
                porta_onvif=int(cadastro("porta_onvif")),
                canais=int(cadastro("canais")),
            )
            senha = ""
        else:
            try:
                senha = self.servico.obter_senha(camera.id or 0)
            except ErroCofre as erro:
                senha = ""
                self.mensagem_geral.setText(f"Não foi possível ler a senha guardada: {erro}")

        self.nome.setText(camera.nome)
        self.codigo.setText(camera.codigo)
        self._fonte_atual = camera.fonte
        self.fonte.definir(camera.fonte.value)
        self.ip.setText(camera.ip)
        self.porta.setValue(camera.porta_rtsp)
        self.usuario.setText(camera.usuario)
        self.senha.setText(senha)
        if camera.fonte is TipoFonte.ARQUIVO:
            self.caminho.setCurrentText(camera.caminho)
        elif camera.fonte is TipoFonte.WEBCAM:
            self.webcam.setCurrentIndex(max(0, self.webcam.findData(int(camera.caminho or 0))))
        self.canais.setCurrentIndex(max(0, self.canais.findData(camera.canais)))
        self.lentes.setCurrentIndex(self.lentes.findData(camera.lentes))
        self._recriar_nomes_vistas(camera.nomes_vistas)
        self.ptz.setChecked(camera.tem_ptz)
        self.porta_onvif.setValue(camera.porta_onvif)
        self.lente_ptz.setCurrentIndex(max(0, self.lente_ptz.findData(camera.lente_ptz)))
        self.deteccao.setChecked(camera.deteccao)
        self.gravar.setChecked(camera.gravar)
        self.modelo_url.setText(camera.modelo_url)
        self._sincronizar_combo_modelo()

    def _conectar(self) -> None:
        self.nome.textEdited.connect(self._nome_editado)
        self.codigo.textEdited.connect(self._codigo_editado)
        self.fonte.escolhido.connect(self._fonte_escolhida)
        self.lentes.currentIndexChanged.connect(lambda _i: self._lentes_mudaram())
        self.canais.currentIndexChanged.connect(lambda _i: self._recriar_nomes_vistas(self._nomes_digitados()))
        for campo_porta in (self.porta, self.porta_onvif):
            campo_porta.textEdited.connect(lambda _t: self._revalidar())
        self.ptz.toggled.connect(lambda _v: self._atualizar_visibilidade())
        self.modelo.currentIndexChanged.connect(self._modelo_escolhido)
        self.modelo_url.textEdited.connect(lambda _t: self._sincronizar_combo_modelo())
        for campo in (self.ip, self.usuario, self.modelo_url):
            campo.textEdited.connect(lambda _t: self._revalidar())
        self.caminho.currentTextChanged.connect(lambda _t: self._revalidar())

    # ================================================================ reações
    def _nome_editado(self, texto: str) -> None:
        if not self._codigo_manual:
            ignorar = self.original.id if self.original else None
            self.codigo.setText(gerar_codigo(texto, self.servico.banco.codigos_em_uso(ignorar)) if texto.strip() else "")
        self._revalidar()

    def _codigo_editado(self, texto: str) -> None:
        self._codigo_manual = True
        maiusculo = texto.upper()
        if maiusculo != texto:
            posicao = self.codigo.cursorPosition()
            self.codigo.setText(maiusculo)
            self.codigo.setCursorPosition(posicao)
        self._revalidar()

    def _fonte_escolhida(self, valor: str) -> None:
        self._fonte_atual = TipoFonte(valor)
        self._parar_teste()
        self._atualizar_visibilidade()
        self._revalidar()

    def _lentes_mudaram(self) -> None:
        self.previa.definir_disposicao(self._disposicao())
        self._recriar_nomes_vistas(self._nomes_digitados())
        self._atualizar_visibilidade()

    def _modelo_escolhido(self, _indice: int) -> None:
        chave = self.modelo.currentData()
        if chave != PERSONALIZADO and chave in self._modelos:
            self.modelo_url.setText(self._modelos[chave][1])
            self._revalidar()

    def _sincronizar_combo_modelo(self) -> None:
        """Se o texto bate com um modelo pronto, mostra o nome dele; senão, "Personalizado"."""
        texto = self.modelo_url.text().strip()
        chave = next((k for k, (_n, url) in self._modelos.items() if url == texto), PERSONALIZADO)
        self.modelo.blockSignals(True)
        self.modelo.setCurrentIndex(self.modelo.findData(chave))
        self.modelo.blockSignals(False)

    def _disposicao(self) -> DisposicaoLentes:
        return DisposicaoLentes(self.lentes.currentData())

    def _canais(self) -> int:
        return int(self.canais.currentData())

    def _atualizar_visibilidade(self) -> None:
        rtsp = self._fonte_atual is TipoFonte.RTSP
        self._bloco_rtsp.setVisible(rtsp)
        self._bloco_arquivo.setVisible(self._fonte_atual is TipoFonte.ARQUIVO)
        self._bloco_webcam.setVisible(self._fonte_atual is TipoFonte.WEBCAM)
        self._bloco_canais.setEnabled(rtsp)
        if not rtsp:
            self.canais.setCurrentIndex(0)
        self._detalhes_ptz.setVisible(self.ptz.isChecked())
        self._bloco_lente_ptz.setVisible(self._disposicao().quantidade > 1)
        self._avancado.setEnabled(rtsp)
        self._bloco_nomes.setVisible(len(self._nomes_vistas) > 1)

    def _nomes_digitados(self) -> dict[int, str]:
        return {i + 1: campo.text() for i, campo in enumerate(self._nomes_vistas) if campo.text().strip()}

    def _recriar_nomes_vistas(self, nomes: dict[int, str]) -> None:
        """Um campo de nome para cada vista (canais x lentes)."""
        for campo in self._nomes_vistas:
            campo.deleteLater()
        self._nomes_vistas = []
        while self._grade_nomes.count():
            item = self._grade_nomes.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        provisoria = Camera("x", "X", self._fonte_atual, "", 1, "", "", 1, canais=self._canais(), lentes=self._disposicao())
        for indice, vista in enumerate(provisoria.vistas()):
            campo = QLineEdit(nomes.get(vista.numero, ""))
            campo.setPlaceholderText(provisoria.nome_vista(vista).removeprefix("x · "))
            campo.setMaxLength(24)
            self._grade_nomes.addWidget(campo, indice // 2, indice % 2)
            self._nomes_vistas.append(campo)
        self._bloco_nomes.setVisible(len(self._nomes_vistas) > 1)

    def _procurar_arquivo(self) -> None:
        inicial = str(self.config.resolver_caminho("videos"))
        caminho, _filtro = QFileDialog.getOpenFileName(
            self, "Escolher vídeo", inicial, "Vídeos (*.mp4 *.avi *.mkv *.mov);;Todos os arquivos (*)"
        )
        if caminho:
            self.caminho.setCurrentText(caminho)

    # ================================================================ validação
    def _caminho_resolvido(self) -> Path:
        return self.config.resolver_caminho(self.caminho.currentText().strip())

    def _coletar_erros(self) -> dict[str, str]:
        erros: dict[str, str] = {}
        ignorar = self.original.id if self.original else None
        nome = normalizar_nome(self.nome.text())
        if erro := validar_nome(nome):
            erros["nome"] = erro
        elif self.servico.banco.nome_em_uso(nome, ignorar_id=ignorar):
            erros["nome"] = "Já existe uma câmera com esse nome."
        codigo = self.codigo.text().strip()
        if erro := validar_codigo(codigo):
            erros["codigo"] = erro
        elif codigo in self.servico.banco.codigos_em_uso(ignorar):
            erros["codigo"] = "Esse código já é usado por outra câmera."
        if self._fonte_atual is TipoFonte.RTSP:
            if erro := validar_ip(self.ip.text()):
                erros["ip"] = erro
            if erro := validar_porta(self.porta.value()):
                erros["porta"] = erro
            if self.ptz.isChecked() and (erro := validar_porta(self.porta_onvif.value())):
                erros["porta_onvif"] = erro
            if erro := validar_modelo_url(self.modelo_url.text()):
                erros["modelo_url"] = erro
                self._avancado.definir_aberta(True)
        elif self._fonte_atual is TipoFonte.ARQUIVO:
            if not self.caminho.currentText().strip():
                erros["caminho"] = "Escolha um arquivo de vídeo."
            elif not self._caminho_resolvido().is_file():
                erros["caminho"] = f"Arquivo não encontrado: {self._caminho_resolvido()}"
        return erros

    def _mostrar_erros(self, erros: dict[str, str]) -> None:
        for chave, rotulo_erro in self._erros.items():
            mensagem = erros.get(chave, "")
            rotulo_erro.setText(mensagem)
            rotulo_erro.setVisible(bool(mensagem))
            campo = self._campos_erro.get(chave)
            if isinstance(campo, (QLineEdit, QComboBox)):
                campo.setProperty("erro", "true" if mensagem else "false")
                repolir(campo)

    def _revalidar(self) -> None:
        """Depois da primeira tentativa de salvar, os erros se atualizam enquanto você digita."""
        if self._tentou_salvar:
            self._mostrar_erros(self._coletar_erros())

    def camera_dos_campos(self) -> Camera:
        """Monta a ``Camera`` com o que está digitado (sem validar)."""
        base = self.original
        fonte = self._fonte_atual
        if fonte is TipoFonte.ARQUIVO:
            caminho = self.caminho.currentText().strip()
        elif fonte is TipoFonte.WEBCAM:
            caminho = str(self.webcam.currentData())
        else:
            caminho = ""
        dados = {
            "nome": normalizar_nome(self.nome.text()),
            "codigo": self.codigo.text().strip(),
            "fonte": fonte,
            "ip": self.ip.text().strip() if fonte is TipoFonte.RTSP else "",
            "porta_rtsp": self.porta.value(),
            "usuario": self.usuario.text().strip(),
            "modelo_url": self.modelo_url.text().strip(),
            "porta_onvif": self.porta_onvif.value(),
            "canais": self._canais() if fonte is TipoFonte.RTSP else 1,
            "lentes": self._disposicao(),
            "caminho": caminho,
            "tem_ptz": self.ptz.isChecked(),
            "lente_ptz": int(self.lente_ptz.currentData()) if self._disposicao().quantidade > 1 else 1,
            "deteccao": self.deteccao.isChecked(),
            "gravar": self.gravar.isChecked(),
            "nomes_vistas": {k: v.strip() for k, v in self._nomes_digitados().items()},
        }
        return replace(base, **dados) if base else Camera(**dados)  # type: ignore[arg-type]

    # ================================================================ salvar
    def _salvar(self) -> None:
        self._tentou_salvar = True
        erros = self._coletar_erros()
        self._mostrar_erros(erros)
        if erros:
            self.mensagem_geral.setText("Corrija os campos destacados.")
            primeiro = next(iter(erros))
            self._campos_erro[primeiro].setFocus()
            return
        camera = self.camera_dos_campos()
        senha = self.senha.text() if camera.fonte is TipoFonte.RTSP else ""
        try:
            if self.original:
                self.camera_salva = self.servico.editar(camera, senha)
            else:
                self.camera_salva = self.servico.cadastrar(camera, senha)
        except (ErroBanco, ErroCofre, OSError) as erro:
            log.warning("Falha ao salvar a câmera: %s", erro)
            self.mensagem_geral.setText(str(erro))
            return
        self.accept()

    # ================================================================ teste de conexão
    def _limpar_resultados(self) -> None:
        while self._resultados.count():
            item = self._resultados.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

    def _resultado(self, situacao: str, texto: str) -> LinhaResultado:
        linha = LinhaResultado(situacao, texto)
        self._resultados.addWidget(linha)
        return linha

    def resultados(self) -> list[LinhaResultado]:
        """Linhas de resultado atuais (útil nos testes automáticos)."""
        return [
            self._resultados.itemAt(i).widget()  # type: ignore[misc]
            for i in range(self._resultados.count())
            if isinstance(self._resultados.itemAt(i).widget(), LinhaResultado)
        ]

    def _testar(self) -> None:
        self._parar_teste()
        self._limpar_resultados()
        self.previa.definir_imagem(None)
        erros = {k: v for k, v in self._coletar_erros().items() if k in ("ip", "porta", "modelo_url", "caminho")}
        self._mostrar_erros(erros)
        if erros:
            self._resultado("erro", "Preencha corretamente os campos da fonte do vídeo.")
            return
        camera = self.camera_dos_campos()
        self.previa.definir_texto("Conectando…")
        if camera.fonte is TipoFonte.ARQUIVO:
            self._abrir_previa(str(self._caminho_resolvido()))
        elif camera.fonte is TipoFonte.WEBCAM:
            self._abrir_previa(int(camera.caminho or 0))
        else:
            senha = self.senha.text()
            url = camera.url(1, TipoStream.SUBSTREAM, senha)
            timeout = float(self.config.obter("diagnostico.timeout_s"))
            linha = self._resultado("pendente", "Verificando a câmera…")
            executar(
                lambda: sondar_rtsp(url, camera.usuario, senha, timeout),
                lambda r: self._sonda_concluida(linha, r, url),
                lambda e: linha.definir("erro", f"Erro inesperado: {e}"),
                dono=self,
            )
            if camera.tem_ptz:
                linha_onvif = self._resultado("pendente", "Testando o ONVIF (motor)…")
                executar(
                    lambda: testar_onvif(camera.ip, camera.porta_onvif, camera.usuario, senha, timeout),
                    lambda r: self._onvif_concluido(linha_onvif, r),
                    lambda e: linha_onvif.definir("erro", f"ONVIF: {e}"),
                    dono=self,
                )

    def _sonda_concluida(self, linha: LinhaResultado, resultado: ResultadoSonda, url: str) -> None:
        if not resultado.ok:
            linha.definir("erro", resultado.mensagem[:1].upper() + resultado.mensagem[1:])
            self.previa.definir_texto("Sem imagem.")
            return
        linha.definir("ok", f"A câmera respondeu em {resultado.tempo_ms:.0f} ms")
        self._abrir_previa(url)

    def _onvif_concluido(self, linha: LinhaResultado, r: ResultadoOnvif) -> None:
        if not r.ok:
            linha.definir("erro", f"ONVIF: {r.mensagem}")
        elif r.tem_ptz:
            linha.definir("ok", f"Motor (PTZ) por ONVIF: {r.detalhes_ptz}")
        else:
            linha.definir("aviso", "ONVIF funciona, mas a câmera não anuncia motor (PTZ).")

    def _abrir_previa(self, fonte: str | int) -> None:
        self._linha_video = self._resultado("pendente", "Abrindo o vídeo (substream)…")
        self._ultimo_numero_previa = -1
        self._primeiro_quadro = True
        self._leitor_teste = LeitorVideo("teste-conexao", fonte, self.limites, arquivo_em_loop=True).iniciar()
        self._timer_previa.start()

    def _atualizar_previa(self) -> None:
        leitor = self._leitor_teste
        if leitor is None or self._linha_video is None:
            return
        if leitor.estado is EstadoLeitor.OFFLINE:
            self._linha_video.definir("erro", f"O vídeo não abriu: {leitor.mensagem}")
            self.previa.definir_texto("Sem imagem.")
            self._parar_teste()
            return
        quadro = leitor.ultimo_quadro()
        if quadro is None or quadro.numero == self._ultimo_numero_previa:
            return
        self._ultimo_numero_previa = quadro.numero
        tamanho = self.previa.tamanho_util()
        imagem = converter_para_qimage(quadro.imagem, tamanho.width(), tamanho.height())
        imagem.setDevicePixelRatio(self.previa.devicePixelRatioF())
        self.previa.definir_imagem(imagem)
        altura, largura = quadro.imagem.shape[:2]
        agora = time.monotonic()
        if self._primeiro_quadro or agora - self._fps_atualizado_em > 1.0:
            fps = f", {leitor.fps:.1f} FPS" if leitor.fps else ""
            self._linha_video.definir("ok", f"Vídeo ao vivo: {largura}x{altura}{fps}")
            self._fps_atualizado_em = agora
        if self._primeiro_quadro:
            self._primeiro_quadro = False
            self._sugerir_lentes(largura, altura)

    def _sugerir_lentes(self, largura: int, altura: int) -> None:
        """640x720 = duas imagens 16:9 empilhadas; 1280x360 = lado a lado."""
        if self._disposicao() is not DisposicaoLentes.UNICA:
            return
        proporcao = largura / altura
        if abs(proporcao - (16 / 9) / 2) < 0.08:
            sugestao, texto = DisposicaoLentes.VERTICAL, "empilhadas (em cima e embaixo)"
        elif abs(proporcao - (16 / 9) * 2) < 0.25:
            sugestao, texto = DisposicaoLentes.HORIZONTAL, "lado a lado"
        else:
            return
        linha = self._resultado("aviso", f"A imagem ({largura}x{altura}) parece ter duas lentes {texto}.")
        botao = QPushButton("Dividir")
        botao.setProperty("variante", "primario")

        def aplicar() -> None:
            self.lentes.setCurrentIndex(self.lentes.findData(sugestao))
            linha.definir("ok", f"Imagem dividida em duas lentes {texto}.")
            botao.hide()

        botao.clicked.connect(aplicar)
        linha.adicionar_botao(botao)

    def _parar_teste(self) -> None:
        self._timer_previa.stop()
        if self._leitor_teste is not None:
            self._leitor_teste.parar()
            self._leitor_teste = None

    # ================================================================ fechamento
    def done(self, resultado: int) -> None:  # noqa: D102
        self._parar_teste()
        super().done(resultado)

    def closeEvent(self, evento: QCloseEvent) -> None:  # noqa: N802
        self._parar_teste()
        super().closeEvent(evento)
