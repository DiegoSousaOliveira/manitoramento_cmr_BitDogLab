"""Janela principal: barra lateral + barra superior + páginas (mosaico, eventos, ajustes).

Navegação:
- clique num bloco do mosaico: abre a Tela da câmera (alta resolução);
- duplo clique no bloco (ou no vídeo da Tela da câmera): tela cheia da câmera;
- F11: tela cheia do app (com as barras);
- Esc: sai da tela cheia; na Tela da câmera, volta ao mosaico.

Alertas (Fase 5): cada ALERTA vira um toast vermelho com a miniatura do objeto;
clicar abre o evento (ou a câmera ao vivo, se o evento ainda está sendo gravado).

Placa BitDogLab (Fase 6): status na barra lateral (atualizado a cada segundo), avisos
quando conecta/desconecta, troca de modo ou silencia um alerta. A câmera aberta (ou a
última selecionada no mosaico) com PTZ vira a câmera do joystick da placa.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from PySide6.QtCore import QEvent, QObject, QPoint, Qt, QTime, QTimer, QUrl, Signal
from PySide6.QtGui import QCloseEvent, QDesktopServices, QIcon, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QCheckBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QStackedWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from camera.gerenciador import GerenciadorCameras
from camera.ptz import ServicoPTZ
from treino.historico import Rodada
from treino.servico import ServicoTreino
from core.banco import ErroBanco
from core.config import Config, ErroConfig
from core.logs import definir_nivel
from core.modelos import Camera, Evento, Vista
from core.servico_cameras import ServicoCameras
from ui import grade, tema
from ui.ajustes_placa import SecaoPlaca
from ui.pagina_ajustes import PaginaAjustes
from ui.pagina_treino import PaginaTreino
from ui.barra_lateral import BarraLateral
from ui.componentes import GrupoSegmentos, rotulo
from ui.dialogo_camera import DialogoCamera
from ui.distribuidor import DistribuidorQuadros, converter_para_qimage
from eventos.alertas import ServicoAlertas
from eventos.gravador import ServicoEventos
from link.bitdog import EstadoPlaca, LinkBitDog
from ui.mosaico import Mosaico
from ui.pagina_eventos import PaginaEventos
from ui.tarefas import executar
from ui.tela_camera import TelaCamera
from vision.detector import EstadoIA, ServicoDeteccao, exportar_modelo
from vision.dwell import Aviso, TipoAviso
from ui.toast import Notificador

log = logging.getLogger(__name__)
Chave = tuple[int, int]

NOMES_PAGINAS = {
    "cameras": "Câmeras", "eventos": "Eventos", "treino": "Treino da IA", "config": "Ajustes", "camera": "Câmera",
}
#: Classes com nome feminino ("PESSOA parada", mas "CARRO parado").
CLASSES_FEMININAS = {"PESSOA", "BICICLETA", "MOTO"}
DURACAO_TOAST_ALERTA_MS = 15000
TEXTOS_LAYOUT = {"1": "1", "2x2": "2×2", "3x3": "3×3", "4x4": "4×4", "auto": "Auto"}


class PaginaVazia(QWidget):
    """Página centralizada com ícone, título, texto e (opcionalmente) um botão."""

    def __init__(self, icone: str, titulo: str, texto: str, botao: QPushButton | None = None) -> None:
        super().__init__()
        self._nome_icone = icone
        self._icone = QLabel()
        self._icone.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout = QVBoxLayout(self)
        layout.addStretch(1)
        layout.addWidget(self._icone)
        layout.addSpacing(8)
        titulo_rotulo = rotulo(titulo, "titulo-grande")
        titulo_rotulo.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(titulo_rotulo)
        texto_rotulo = rotulo(texto, "suave", quebra=True)
        texto_rotulo.setAlignment(Qt.AlignmentFlag.AlignCenter)
        texto_rotulo.setFixedWidth(500)
        # Texto com quebra de linha + "alignment" no layout = altura errada (limitação do Qt);
        # por isso centralizamos com espaços elásticos dos dois lados.
        linha = QHBoxLayout()
        linha.addStretch(1)
        linha.addWidget(texto_rotulo)
        linha.addStretch(1)
        layout.addLayout(linha)
        if botao is not None:
            layout.addSpacing(14)
            layout.addWidget(botao, alignment=Qt.AlignmentFlag.AlignHCenter)
        layout.addStretch(2)
        self._aplicar_icone()
        # Método (e não lambda): o Qt desliga sozinho quando o widget é destruído.
        tema.sinais.tema_mudou.connect(self._aplicar_icone)

    def _aplicar_icone(self, _paleta: object = None) -> None:
        self._icone.setPixmap(tema.icone(self._nome_icone, "destaque").pixmap(64, 64))


class _PonteEventos(QObject):
    """Leva os avisos do serviço de eventos (thread "eventos") para a thread da interface.

    Emitir um sinal de outra thread é seguro: o Qt entrega aos MÉTODOS conectados
    na thread de quem os recebe.
    """

    gravado = Signal(object, object)  # Evento, Camera
    removidos = Signal(object)  # list[int]


class _PonteAlertas(QObject):
    """Leva ALERTA/LIMPAR/PASSOU da thread "alertas" para a thread da interface."""

    aviso = Signal(object, object)  # Aviso, Camera


class _PonteTreino(QObject):
    """Leva os avisos do treino (thread que lê o processo de treino) para a interface."""

    aviso = Signal(object)  # dict


class _PonteLink(QObject):
    """Leva os botões e a troca de câmera da placa (thread "placa") para a interface."""

    botao = Signal(str)
    camera = Signal(object)  # id da câmera do joystick (ou None)


def descrever_placa(estado: EstadoPlaca) -> str:
    """Texto da dica da placa na barra lateral."""
    if estado.erro:
        return f"Placa BitDogLab: comunicação indisponível — {estado.erro}"
    if estado.conectada:
        partes = [f"BitDogLab conectada · {estado.ip}"]
        if estado.modo:
            partes.append(f"modo {estado.modo}")
        if estado.resposta_ms is not None:
            partes.append(f"resposta {estado.resposta_ms:.0f} ms")
        texto = " · ".join(partes) + f"\nJoystick: câmera {estado.camera}"
        return texto + (f"\nNa placa agora: {estado.alerta}" if estado.alerta else "")
    if estado.ip is None:
        return "BitDogLab desconectada · clique para cadastrar o IP da placa (aparece no display dela)"
    if estado.sem_resposta_s is None:
        return f"BitDogLab desconectada · sem resposta de {estado.ip}:{estado.porta_placa}"
    return f"BitDogLab desconectada · sem resposta de {estado.ip}:{estado.porta_placa} há {estado.sem_resposta_s:.0f} s"


class JanelaPrincipal(QMainWindow):
    def __init__(
        self,
        config: Config,
        servico: ServicoCameras,
        gerenciador: GerenciadorCameras,
        deteccao: ServicoDeteccao | None = None,
        eventos: ServicoEventos | None = None,
        alertas: ServicoAlertas | None = None,
        link: LinkBitDog | None = None,
        ptz: ServicoPTZ | None = None,
        treino: ServicoTreino | None = None,
    ) -> None:
        super().__init__()
        self.ptz = ptz
        self.treino = treino
        self._ponte_treino = _PonteTreino(self)
        self._ponte_treino.aviso.connect(self._aviso_treino)
        if treino is not None:
            treino.assinar(self._ponte_treino.aviso.emit)
        self.config = config
        self.servico = servico
        self.gerenciador = gerenciador
        self.deteccao = deteccao
        self.eventos = eventos
        self.alertas = alertas
        self.link = link
        self._placa_conectada: bool | None = None
        self._modo_placa: str | None = None
        self._ponte = _PonteEventos(self)
        self._ponte.gravado.connect(self._evento_gravado)
        self._ponte.removidos.connect(self._eventos_removidos)
        if eventos is not None:
            eventos.assinar_gravacao(self._ponte.gravado.emit)
            eventos.assinar_remocao(self._ponte.removidos.emit)
        self._ponte_alertas = _PonteAlertas(self)
        self._ponte_alertas.aviso.connect(self._aviso)
        if alertas is not None:
            alertas.assinar(self._ponte_alertas.aviso.emit)
        self._ponte_link = _PonteLink(self)
        self._ponte_link.botao.connect(self._botao_da_placa)
        self._ponte_link.camera.connect(self._camera_do_joystick)
        if link is not None:
            link.assinar_botao(self._ponte_link.botao.emit)
            link.assinar_camera(self._ponte_link.camera.emit)
        self._ia_avisada = False
        self._cameras: dict[int, Camera] = {}
        self._camera_tela_cheia = False  # Tela da câmera sem barras, em tela cheia
        self._estava_em_tela_cheia = False

        self.setWindowTitle("Sentinela Casa")
        self.setMinimumSize(980, 620)
        self.resize(1440, 880)

        self.distribuidor = DistribuidorQuadros(
            gerenciador.leitor_da_vista,
            float(config.obter("interface.fps_mosaico")),
            deteccao.resultado_da_vista if deteccao else None,
            alertas.situacao_da_vista if alertas else None,
        )
        self.mosaico = Mosaico(
            self.distribuidor,
            str(config.obter("interface.layout_mosaico")),
            bool(config.obter("interface.mostrar_caixas_mosaico")),
        )
        self.distribuidor.quadro_pronto.connect(self.mosaico.receber_quadro)
        self.distribuidor.deteccoes_prontas.connect(self.mosaico.receber_deteccoes)
        self.distribuidor.parado_pronto.connect(self.mosaico.receber_parado)
        self.distribuidor.estado_mudou.connect(self.mosaico.receber_estado)
        # ATENÇÃO: sinais vindos de outra thread devem ir para MÉTODOS de objetos da interface.
        # Uma lambda rodaria na thread do distribuidor e mexeria em widgets fora da thread da interface.
        self.distribuidor.estado_mudou.connect(self._estado_de_vista_mudou)

        self._montar()
        self.notificador = Notificador(self)
        self._atalhos()
        self._aplicar_icones()
        tema.sinais.tema_mudou.connect(self._aplicar_icones)

        self.distribuidor.iniciar()
        self.tela_camera.iniciar()
        self.recarregar_cameras()
        self.ir_para("cameras")

    # ================================================================ montagem
    def _montar(self) -> None:
        self.barra_lateral = BarraLateral()
        self.barra_lateral.pagina_escolhida.connect(self.ir_para)
        self.barra_lateral.placa_clicada.connect(self.abrir_ajustes_placa)

        # ---------- barra superior
        self.titulo = rotulo("Câmeras", "titulo")
        self.resumo = rotulo("", "fraco")
        bloco_titulo = QVBoxLayout()
        bloco_titulo.setSpacing(0)
        bloco_titulo.addWidget(self.titulo)
        bloco_titulo.addWidget(self.resumo)

        self.pagina_anterior = QToolButton()
        self.proxima_pagina = QToolButton()
        self.texto_pagina = rotulo("", "suave")
        for botao, delta in ((self.pagina_anterior, -1), (self.proxima_pagina, 1)):
            botao.setProperty("papel", "icone")
            botao.setCursor(Qt.CursorShape.PointingHandCursor)
            botao.clicked.connect(lambda _c=False, d=delta: self.mosaico.mudar_pagina(d))
        self._navegacao = QWidget()
        navegacao = QHBoxLayout(self._navegacao)
        navegacao.setContentsMargins(0, 0, 0, 0)
        navegacao.setSpacing(2)
        navegacao.addWidget(self.pagina_anterior)
        navegacao.addWidget(self.texto_pagina)
        navegacao.addWidget(self.proxima_pagina)
        self.mosaico.paginas_mudaram.connect(self._paginas_mudaram)

        self.seletor_layout = GrupoSegmentos([(v, TEXTOS_LAYOUT[v], f"Layout {TEXTOS_LAYOUT[v]}") for v in grade.LAYOUTS])
        self.seletor_layout.definir(self.mosaico.layout_atual)
        self.seletor_layout.escolhido.connect(self.definir_layout)

        self.relogio = rotulo("", "relogio")
        self._timer_relogio = QTimer(self)
        self._timer_relogio.timeout.connect(self._tique_relogio)
        self._timer_relogio.start(1000)
        self._tique_relogio()
        self._timer_ia = QTimer(self)
        self._timer_ia.timeout.connect(self._atualizar_status_ia)
        self._timer_ia.start(2000)
        self._timer_placa = QTimer(self)
        self._timer_placa.timeout.connect(self._atualizar_placa)
        self._timer_placa.start(1000)

        self.botao_adicionar = QPushButton("Adicionar câmera")
        self.botao_adicionar.setProperty("variante", "primario")
        self.botao_adicionar.setCursor(Qt.CursorShape.PointingHandCursor)
        self.botao_adicionar.clicked.connect(self.adicionar_camera)

        self.barra_superior = QFrame()
        self.barra_superior.setObjectName("barraSuperior")
        superior = QHBoxLayout(self.barra_superior)
        superior.setContentsMargins(24, 12, 20, 12)
        superior.setSpacing(14)
        superior.addLayout(bloco_titulo)
        superior.addStretch(1)
        superior.addWidget(self._navegacao)
        superior.addWidget(self.seletor_layout)
        superior.addSpacing(6)
        superior.addWidget(self.relogio)
        superior.addSpacing(6)
        superior.addWidget(self.botao_adicionar)

        # ---------- páginas
        botao_primeira = QPushButton("Cadastrar a primeira câmera")
        botao_primeira.setProperty("variante", "primario")
        botao_primeira.setCursor(Qt.CursorShape.PointingHandCursor)
        botao_primeira.clicked.connect(self.adicionar_camera)
        self.boas_vindas = PaginaVazia(
            "mdi6.cctv",
            "Bem-vindo ao Sentinela Casa",
            "Cadastre uma câmera iCSee pelo IP — ou um vídeo .mp4 / a webcam para testar — "
            "e acompanhe tudo ao vivo por aqui.",
            botao_primeira,
        )
        self.paginas_cameras = QStackedWidget()
        self.paginas_cameras.addWidget(self.boas_vindas)
        self.paginas_cameras.addWidget(self.mosaico)

        self.paginas = QStackedWidget()
        self._indice_pagina = {
            "cameras": self.paginas.addWidget(self.paginas_cameras),
            "eventos": self.paginas.addWidget(self._criar_pagina_eventos()),
            "treino": self.paginas.addWidget(self._criar_pagina_treino()),
            "config": self.paginas.addWidget(self._pagina_ajustes()),
        }
        self.tela_camera = TelaCamera(
            self.config, self.servico, self.gerenciador, notificar=self._notificar, deteccao=self.deteccao,
            alertas=self.alertas, ptz=self.ptz,
        )
        self._indice_pagina["camera"] = self.paginas.addWidget(self.tela_camera)
        self.tela_camera.voltar.connect(self._voltar_ao_mosaico)
        self.tela_camera.alternar_tela_cheia.connect(self.alternar_tela_cheia_camera)
        self.tela_camera.info_mudou.connect(self._info_da_camera)
        self.tela_camera.evento_excluido.connect(self._evento_excluido)
        self.tela_camera.zona_salva.connect(self._zona_salva)
        self.pagina_eventos.evento_excluido.connect(self._evento_excluido)

        coluna = QVBoxLayout()
        coluna.setContentsMargins(0, 0, 0, 0)
        coluna.setSpacing(0)
        coluna.addWidget(self.barra_superior)
        coluna.addWidget(self.paginas, 1)
        central = QWidget()
        raiz = QHBoxLayout(central)
        raiz.setContentsMargins(0, 0, 0, 0)
        raiz.setSpacing(0)
        raiz.addWidget(self.barra_lateral)
        raiz.addLayout(coluna, 1)
        self.setCentralWidget(central)

        # Clique abre a Tela da câmera já no "aperto" do botão; num duplo clique, o segundo
        # clique cai no vídeo da Tela da câmera, que entra em tela cheia.
        self.mosaico.vista_clicada.connect(self.abrir_camera)
        self.mosaico.menu_vista.connect(self.menu_da_vista)
        self.mosaico.reconectar.connect(lambda chave: self.gerenciador.reconectar(chave[0]))

    def _criar_pagina_eventos(self) -> QWidget:
        self.pagina_eventos = PaginaEventos(self.servico, notificar=self._notificar)
        return self.pagina_eventos

    def _evento_gravado(self, evento: Evento, camera: Camera) -> None:
        """Evento novo terminou: atualiza as listas em tempo real."""
        self.pagina_eventos.painel.evento_gravado(evento, camera)
        if self.tela_camera.camera is not None and self.tela_camera.camera.id == evento.camera_id:
            self.tela_camera.painel.evento_gravado(evento, camera)

    def _evento_excluido(self, evento_id: int) -> None:
        """Excluído pelo usuário numa das telas: some das duas listas."""
        self._eventos_removidos([evento_id])

    def _eventos_removidos(self, ids: list[int]) -> None:
        self.pagina_eventos.painel.eventos_removidos(ids)
        self.tela_camera.painel.eventos_removidos(ids)

    # ================================================================ alertas
    def _aviso(self, aviso: Aviso, camera: Camera) -> None:
        """Aviso do serviço de alertas (já na thread da interface)."""
        if aviso.tipo is not TipoAviso.ALERTA:
            return  # LIMPAR/PASSOU: a borda se apaga sozinha; a placa (Fase 6) também recebe
        vista = self.vista(aviso.chave)
        onde = camera.nome_vista(vista) if vista else camera.nome
        parado = "parada" if aviso.classe in CLASSES_FEMININAS else "parado"
        miniatura = converter_para_qimage(aviso.miniatura, 176, 100) if aviso.miniatura is not None else None
        self.notificador.mostrar(
            f"{aviso.classe} {parado} na zona",
            f"{onde} · há {aviso.segundos:.0f} s · nível {aviso.nivel.name} — clique para ver",
            "alerta",
            miniatura=miniatura,
            ao_clicar=lambda a=aviso: self.abrir_alerta(a),
            duracao_ms=DURACAO_TOAST_ALERTA_MS,
        )

    def evento_do_alerta(self, aviso: Aviso) -> Evento | None:
        """O evento gravado que contém o momento do alerta (None se ainda está gravando)."""
        if aviso.quando is None:
            return None
        folga = timedelta(seconds=2)
        for evento in self.servico.banco.listar_eventos(camera_id=aviso.chave[0], so_alertas=True, limite=50):
            fim = evento.fim or evento.inicio
            if evento.vista == aviso.chave[1] and evento.inicio - folga <= aviso.quando <= fim + folga:
                return evento
        return None

    def abrir_alerta(self, aviso: Aviso) -> None:
        """Clique no toast: abre o evento; se ele ainda está sendo gravado, a câmera ao vivo."""
        evento = self.evento_do_alerta(aviso)
        if evento is not None:
            self.ir_para("eventos")
            self.pagina_eventos.mostrar_evento(evento)
            return
        if aviso.chave[0] not in self._cameras:
            return
        self.abrir_camera(aviso.chave)
        if self.eventos is not None and self.eventos.em_andamento(aviso.chave):
            self.notificador.mostrar(
                "Ao vivo", "O evento deste alerta ainda está sendo gravado; ele aparece na lista quando terminar.",
                "info",
            )

    # ================================================================ placa BitDogLab
    def _atualizar_placa(self) -> None:
        """A cada segundo: status na barra lateral e avisos quando algo muda."""
        if self.link is None:
            self.barra_lateral.definir_placa(False, "Comunicação com a placa desligada")
            return
        estado = self.link.estado()
        self.barra_lateral.definir_placa(estado.conectada, descrever_placa(estado))
        anterior, self._placa_conectada = self._placa_conectada, estado.conectada
        if anterior is not None and anterior != estado.conectada:  # mudou (a 1ª leitura não avisa)
            if estado.conectada:
                self.notificador.mostrar("Placa BitDogLab conectada", f"{estado.ip}", "sucesso")
            else:
                self.notificador.mostrar(
                    "Placa BitDogLab desconectada", "Ela parou de responder (sem PONG).", "aviso"
                )
        if estado.modo is not None and self._modo_placa is not None and estado.modo != self._modo_placa:
            texto = "sem som (buzzer desligado)" if estado.modo == "NOITE" else "com som"
            self.notificador.mostrar(f"Placa em modo {estado.modo}", texto, "info")
        self._modo_placa = estado.modo or self._modo_placa

    def abrir_ajustes_placa(self) -> None:
        """Clique na placa da barra lateral: Ajustes → Placa, com o cursor no IP."""
        self.ir_para("config")
        self.pagina_ajustes.mostrar_secao("placa")
        self.secao_placa.ip.setFocus()

    def _botao_da_placa(self, botao: str) -> None:
        if botao == "A":
            self.notificador.mostrar("Alerta silenciado na placa",
                                     "Botão A (2 cliques): o buzzer calou; o alerta segue na placa até o objeto sair.",
                                     "info")

    def _camera_do_joystick(self, camera_id: object) -> None:
        """A placa trocou a câmera do joystick (botão J)."""
        camera = self._cameras.get(camera_id) if isinstance(camera_id, int) else None
        if camera is None:
            self.notificador.mostrar("Joystick da placa", "Nenhuma câmera com PTZ cadastrada.", "info")
            return
        vistas = camera.vistas()
        vista = next((v for v in vistas if v.numero == camera.lente_ptz), vistas[0])
        self.mosaico.selecionar(vista.chave)
        self.notificador.mostrar("Joystick da placa", f"Agora controla: {camera.nome}", "info")

    def _usar_no_joystick(self, camera_id: int) -> None:
        """Câmera aberta/selecionada na interface: se tiver PTZ, vira a do joystick."""
        camera = self._cameras.get(camera_id)
        if self.link is not None and camera is not None and camera.tem_ptz:
            self.link.definir_camera_joystick(camera_id)

    def _zona_salva(self, _camera_id: int) -> None:
        """A zona mudou no banco: eventos e alertas releem."""
        cameras = list(self._cameras.values())
        if self.eventos is not None:
            self.eventos.definir_cameras(cameras)
        if self.alertas is not None:
            self.alertas.definir_cameras(cameras)

    # ================================================================ treino (Fase 9)
    def _criar_pagina_treino(self) -> QWidget:
        if self.treino is None:
            self.pagina_treino = None
            return PaginaVazia("mdi6.school-outline", "Treino da IA", "O aprendizado contínuo está desligado neste app.")
        self.pagina_treino = PaginaTreino(self.config, self.treino, notificar=self._notificar)
        self.pagina_treino.usar_modelo.connect(self.usar_modelo)
        return self.pagina_treino

    def _aviso_treino(self, aviso: dict) -> None:
        """Aviso do treino (já na thread da interface). No fim, troca o modelo se ele foi aprovado."""
        if self.pagina_treino is not None:
            self.pagina_treino.receber_aviso(aviso)
        if not aviso.get("fim"):
            return
        rodada = Rodada.de_dict(aviso.get("rodada") or {"inicio": ""})
        if rodada.promovido and rodada.modelo_novo:
            atual, novo = rodada.metricas_atual, rodada.metricas_novo
            nota = f"nota {atual.map:.3f} → {novo.map:.3f}" if atual and novo else ""
            if bool(self.config.obter("treino.trocar_automatico")):
                self.usar_modelo(rodada.modelo_novo, avisar=False)
                self.notificador.mostrar("A IA aprendeu com as suas câmeras",
                                         f"Modelo novo em uso ({nota}). Dá para voltar em Treino.", "sucesso",
                                         duracao_ms=12000)
            else:
                self.notificador.mostrar("Modelo novo aprovado", f"{nota}. Escolha “Usar este” em Treino.", "info",
                                         duracao_ms=12000)
        elif rodada.erro:
            self.notificador.mostrar("O treino não terminou", rodada.motivo, "aviso", duracao_ms=10000)
        elif rodada.treinou or aviso.get("manual"):
            self.notificador.mostrar("Rodada de treino", rodada.motivo or "terminou", "info", duracao_ms=10000)

    def usar_modelo(self, arquivo: str, avisar: bool = True) -> None:
        """Troca o modelo em uso (gravado no config.yaml) e a IA recarrega — sem reiniciar o app."""
        try:
            self.config.definir("ia.modelo", arquivo)
            self.config.salvar()
        except (OSError, ErroConfig) as erro:
            self.notificador.mostrar("Não foi possível trocar o modelo", str(erro), "erro")
            return
        log.info("Modelo de IA em uso: %s", arquivo)
        if self.deteccao is not None:
            self.deteccao.recarregar_modelo()
        if self.pagina_treino is not None:
            self.pagina_treino.atualizar()
        if avisar:
            self.notificador.mostrar("Modelo trocado", f"{arquivo} — a IA continua funcionando enquanto troca.", "sucesso")

    def _pagina_ajustes(self) -> QWidget:
        """Tela de Ajustes completa (``ui/pagina_ajustes.py``) + status da IA e exportação do modelo."""
        self.status_ia = rotulo("", "suave", quebra=True)
        self.botoes_exportar: list[QPushButton] = []
        botoes = QHBoxLayout()
        for formato, texto in (("openvino", "Exportar para OpenVINO"), ("onnx", "Exportar para ONNX")):
            botao = QPushButton(texto)
            botao.setToolTip(
                "Deixa a IA mais rápida em computadores SEM placa de vídeo NVIDIA.\n"
                "Leva alguns minutos e pode instalar pacotes extras (onnx/openvino)."
            )
            botao.clicked.connect(lambda _c=False, f=formato: self._exportar_modelo(f))
            self.botoes_exportar.append(botao)
            botoes.addWidget(botao)
        botoes.addStretch(1)
        extra_ia = QWidget()
        coluna = QVBoxLayout(extra_ia)
        coluna.setContentsMargins(0, 0, 0, 0)
        coluna.setSpacing(8)
        coluna.addWidget(self.status_ia)
        coluna.addLayout(botoes)
        coluna.addWidget(rotulo(
            "Com placa de vídeo NVIDIA a exportação não é necessária. Sem ela, exporte e escolha "
            "“Processador (CPU)” em “Onde a IA roda”: o modelo exportado passa a ser usado.",
            "dica", quebra=True,
        ))
        self.secao_placa = SecaoPlaca(self.config, self.link, notificar=self._notificar)
        self.pagina_ajustes = PaginaAjustes(self.config, self.servico, self._notificar, self.secao_placa, extra_ia)
        self.pagina_ajustes.salvo.connect(self._ajustes_salvos)
        self.pagina_ajustes.editar_camera.connect(self.editar_camera)
        self.pagina_ajustes.limpar_eventos.connect(self._limpar_eventos_agora)
        return self.pagina_ajustes

    def _ajustes_salvos(self, chaves: set[str], cameras: set[int]) -> None:
        """Aplica NA HORA o que foi salvo na tela de Ajustes (sem reiniciar o app)."""
        c = self.config

        def mudou(*prefixos: str) -> bool:
            return any(k.startswith(prefixos) for k in chaves)

        if "interface.tema" in chaves:
            from PySide6.QtWidgets import QApplication

            tema.aplicar_tema(QApplication.instance(), str(c.obter("interface.tema")))  # type: ignore[arg-type]
        if "interface.layout_mosaico" in chaves:
            layout = str(c.obter("interface.layout_mosaico"))
            self.mosaico.definir_layout(layout)
            self.seletor_layout.definir(layout)
        if "interface.fps_mosaico" in chaves:
            self.distribuidor.definir_fps(float(c.obter("interface.fps_mosaico")))
        if "interface.fps_tela_camera" in chaves:
            self.tela_camera.distribuidor.definir_fps(float(c.obter("interface.fps_tela_camera")))
        if "interface.suavizar_ms" in chaves:
            self.gerenciador.definir_suavizacao()
        if "interface.mostrar_caixas_mosaico" in chaves:
            self.mosaico.definir_mostrar_caixas(bool(c.obter("interface.mostrar_caixas_mosaico")))
        if "geral.log_nivel" in chaves:
            definir_nivel(str(c.obter("geral.log_nivel")))
        if self.deteccao is not None and chaves & {"ia.modelo", "ia.dispositivo"}:
            self.deteccao.recarregar_modelo()  # a IA continua com o modelo atual até o novo ficar pronto
        if self.eventos is not None and mudou("eventos.", "treino.", "parado.sem_zona", "ia.identificar"):
            self.eventos.recarregar_ajustes()
        if self.ptz is not None and mudou("ptz."):
            self.ptz.recarregar_ajustes()
        if cameras or mudou("ia.", "parado.", "eventos.area"):
            self.recarregar_cameras()  # IA, alertas e eventos releem os ajustes (gerais e por câmera)

    def _limpar_eventos_agora(self) -> None:
        if self.eventos is None:
            return
        eventos = self.eventos

        def terminou(ids: object) -> None:
            quantos = len(ids) if isinstance(ids, list) else 0
            texto = f"{quantos} evento(s) antigo(s) apagado(s)." if quantos else "Nada para apagar: tudo dentro dos limites."
            self.notificador.mostrar("Limpeza de eventos", texto, "sucesso")

        executar(eventos.aplicar_retencao, terminou,
                 lambda erro: self.notificador.mostrar("Falha na limpeza", str(erro), "erro"), dono=self)

    def _atalhos(self) -> None:
        QShortcut(QKeySequence(Qt.Key.Key_F11), self, activated=self.alternar_tela_cheia)
        QShortcut(QKeySequence(Qt.Key.Key_Escape), self, activated=self._esc)

    def _aplicar_icones(self, _paleta: object = None) -> None:
        self.setWindowIcon(tema.icone("mdi6.shield-home-outline", "destaque"))
        self.botao_adicionar.setIcon(tema.icone("mdi6.plus", "destaque_texto"))
        self.pagina_anterior.setIcon(tema.icone("mdi6.chevron-left", "texto_suave"))
        self.proxima_pagina.setIcon(tema.icone("mdi6.chevron-right", "texto_suave"))

    # ================================================================ navegação
    def _notificar(self, *args: object, **kwargs: object) -> object:
        return self.notificador.mostrar(*args, **kwargs)  # type: ignore[arg-type]

    def ir_para(self, pagina: str) -> None:
        nos_ajustes = self.paginas.currentIndex() == self._indice_pagina.get("config", -1)
        if pagina != "config" and nos_ajustes and self.pagina_ajustes.tem_alteracoes:
            resposta = QMessageBox.question(
                self, "Ajustes", "Salvar as alterações antes de sair dos Ajustes?",
                QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Discard | QMessageBox.StandardButton.Cancel,
            )
            if resposta == QMessageBox.StandardButton.Cancel or (
                resposta == QMessageBox.StandardButton.Save and not self.pagina_ajustes.salvar()
            ):
                self.barra_lateral.definir_pagina("config")
                return
            if resposta == QMessageBox.StandardButton.Discard:
                self.pagina_ajustes.descartar()
        if pagina != "eventos":
            self.pagina_eventos.fechar_visualizador()  # solta o vídeo do evento
        if pagina != "camera" and self.tela_camera.aberta:
            if self._camera_tela_cheia:
                self.alternar_tela_cheia_camera()
            self.tela_camera.fechar()  # sair da Tela da câmera ENCERRA o stream principal
        self.paginas.setCurrentIndex(self._indice_pagina[pagina])
        self.barra_lateral.definir_pagina(pagina)
        self.titulo.setText(NOMES_PAGINAS[pagina])
        no_mosaico = pagina == "cameras"
        self.seletor_layout.setVisible(no_mosaico and bool(self._cameras))
        self._navegacao.setVisible(no_mosaico and self.mosaico.total_paginas > 1)
        self._atualizar_resumo()

    def _paginas_mudaram(self, pagina: int, total: int) -> None:
        self.texto_pagina.setText(f"{pagina + 1}/{total}")
        self.pagina_anterior.setEnabled(pagina > 0)
        self.proxima_pagina.setEnabled(pagina < total - 1)
        no_mosaico = self.paginas.currentIndex() == self._indice_pagina.get("cameras", 0)
        self._navegacao.setVisible(no_mosaico and total > 1)

    def _tique_relogio(self) -> None:
        self.relogio.setText(QTime.currentTime().toString("HH:mm:ss"))

    def _exportar_modelo(self, formato: str) -> None:
        for botao in self.botoes_exportar:
            botao.setEnabled(False)
        self.notificador.mostrar(f"Exportando para {formato.upper()}…", "Isso leva alguns minutos.", "info")

        def terminou(caminho: object) -> None:
            for botao in self.botoes_exportar:
                botao.setEnabled(True)
            self.notificador.mostrar(
                "Modelo exportado",
                f"{caminho}\nPara usá-lo, escolha “Processador (CPU)” em “Onde a IA roda” e salve.",
                "sucesso",
                duracao_ms=10000,
            )

        def falhou(erro: BaseException) -> None:
            for botao in self.botoes_exportar:
                botao.setEnabled(True)
            self.notificador.mostrar("Falha ao exportar", str(erro)[:300], "erro", duracao_ms=12000)

        executar(lambda: exportar_modelo(self.config, formato), terminou, falhou, dono=self)

    def _atualizar_status_ia(self) -> None:
        """Rodapé da barra lateral: IA carregando / pronta (GPU ou CPU) / com erro."""
        if hasattr(self, "status_ia") and self.deteccao is not None:
            texto = {
                EstadoIA.CARREGANDO: "Carregando o modelo…",
                EstadoIA.PRONTO: f"Pronta: {self.deteccao.descricao} · "
                f"{self.deteccao.inferencias_por_s:.1f} imagens/s analisadas",
                EstadoIA.ERRO: f"Indisponível: {self.deteccao.mensagem}",
                EstadoIA.PARADO: "Parada",
            }[self.deteccao.estado]
            self.status_ia.setText(texto)
        if self.deteccao is None:
            self.barra_lateral.definir_ia("IA –", "Detecção desligada", "texto_fraco")
            return
        estado = self.deteccao.estado
        if estado is EstadoIA.CARREGANDO:
            self.barra_lateral.definir_ia("IA …", "Carregando o modelo de IA (na 1ª vez, baixa ~6 MB)…", "aviso")
        elif estado is EstadoIA.PRONTO:
            onde = "GPU" if "GPU" in self.deteccao.descricao else "CPU"
            fonte = "imagem HD" if self.gerenciador.ia_usa_principal else "substream"
            dica = (
                f"{self.deteccao.descricao} · analisando a {fonte}\n"
                f"{self.deteccao.inferencias_por_s:.1f} imagens/s analisadas"
                f"\núltimo lote: {self.deteccao.tempo_lote_ms:.0f} ms"
            )
            self.barra_lateral.definir_ia(f"IA {onde}", dica, "sucesso")
        else:
            self.barra_lateral.definir_ia("IA ✕", f"IA indisponível: {self.deteccao.mensagem}", "alerta")
            if not self._ia_avisada and estado is EstadoIA.ERRO:
                self._ia_avisada = True
                self.notificador.mostrar("IA indisponível", self.deteccao.mensagem, "erro", duracao_ms=12000)

    def _estado_de_vista_mudou(self, _chave: object, _estado: str, _mensagem: str, _segundos: int) -> None:
        self._atualizar_resumo()

    def _info_da_camera(self, texto: str) -> None:
        if self.paginas.currentIndex() == self._indice_pagina["camera"]:
            self.resumo.setText(texto)

    def _atualizar_resumo(self) -> None:
        if self.paginas.currentIndex() != self._indice_pagina.get("cameras", 0) or not self._cameras:
            self.resumo.setText("")
            return
        estados = self.mosaico.estados()
        online = sum(1 for e in estados.values() if e == "online")
        n = len(self._cameras)
        self.resumo.setText(f"{n} câmera{'s' if n != 1 else ''} · {online} de {len(estados)} vistas online")

    # ================================================================ câmeras
    def recarregar_cameras(self) -> None:
        """Lê o banco, liga/desliga leitores e refaz o mosaico."""
        cameras = self.servico.listar()
        self._cameras = {c.id or 0: c for c in cameras}
        self.gerenciador.sincronizar(cameras)
        if self.deteccao is not None:
            self.deteccao.definir_cameras(cameras)
        if self.eventos is not None:
            self.eventos.definir_cameras(cameras)  # depois do gerenciador: os leitores já existem
        if self.alertas is not None:
            self.alertas.definir_cameras(cameras)
        if self.link is not None:
            self.link.definir_cameras(cameras)
        if self.ptz is not None:
            self.ptz.definir_cameras(cameras)
        self.pagina_eventos.definir_cameras(cameras)
        self.pagina_ajustes.recarregar_cameras()
        itens = [(vista, camera.nome_vista(vista)) for camera in cameras for vista in camera.vistas()]
        self.mosaico.definir_vistas(itens)
        self.paginas_cameras.setCurrentWidget(self.mosaico if cameras else self.boas_vindas)
        if self.paginas.currentIndex() == self._indice_pagina["cameras"]:
            self.seletor_layout.setVisible(bool(cameras))
        aberta = self.tela_camera.camera
        if aberta is not None and aberta.id not in self._cameras:
            self._voltar_ao_mosaico()  # a câmera aberta foi removida
        self._atualizar_resumo()

    # ================================================================ Tela da câmera
    def abrir_camera(self, chave: Chave) -> None:
        """Abre a Tela da câmera com a vista clicada em destaque."""
        camera = self._cameras.get(chave[0])
        if camera is None:
            return
        self.mosaico.selecionar(chave)  # última selecionada
        self._usar_no_joystick(camera.id or 0)
        self.tela_camera.abrir(camera, chave[1])
        self.ir_para("camera")
        self.barra_lateral.definir_pagina("cameras")
        self.titulo.setText(camera.nome)
        self.resumo.setText(self.tela_camera.info or "Conectando…")

    def _voltar_ao_mosaico(self) -> None:
        self.ir_para("cameras")

    def alternar_tela_cheia_camera(self) -> None:
        """Só o vídeo, sem barras, em tela cheia. De novo (ou Esc): volta."""
        entrar = not self._camera_tela_cheia
        self._camera_tela_cheia = entrar
        self.barra_lateral.setVisible(not entrar)
        self.barra_superior.setVisible(not entrar)
        self.tela_camera.definir_modo_tela_cheia(entrar)
        if entrar:
            self._estava_em_tela_cheia = self.isFullScreen()
            self.showFullScreen()
        elif not self._estava_em_tela_cheia:
            self.showNormal()

    def _limites(self):  # noqa: ANN202
        return self.gerenciador.limites()

    def adicionar_camera(self) -> None:
        dialogo = DialogoCamera(self, self.config, self.servico, self._limites())
        if dialogo.exec() and dialogo.camera_salva:
            self.ir_para("cameras")
            self.recarregar_cameras()
            self.notificador.mostrar("Câmera cadastrada", dialogo.camera_salva.nome, "sucesso")

    def editar_camera(self, camera_id: int) -> None:
        camera = self.servico.obter(camera_id)
        if camera is None:
            return
        dialogo = DialogoCamera(self, self.config, self.servico, self._limites(), camera)
        if dialogo.exec() and dialogo.camera_salva:
            self.recarregar_cameras()
            self.notificador.mostrar("Alterações salvas", dialogo.camera_salva.nome, "sucesso")

    def remover_camera(self, camera_id: int) -> None:
        camera = self.servico.obter(camera_id)
        if camera is None:
            return
        eventos, tamanho = self.servico.resumo_arquivos(camera)
        caixa = QMessageBox(self)
        caixa.setWindowTitle("Remover câmera")
        caixa.setIcon(QMessageBox.Icon.Warning)
        caixa.setText(f"Remover a câmera “{camera.nome}”?")
        caixa.setInformativeText("O cadastro, as zonas e a senha guardada serão apagados.")
        apagar = QCheckBox(
            f"Apagar também os eventos e fotos gravados ({eventos} evento(s), {tamanho / 1_048_576:.1f} MB)"
        )
        caixa.setCheckBox(apagar)
        botao_remover = caixa.addButton("Remover", QMessageBox.ButtonRole.DestructiveRole)
        botao_remover.setProperty("variante", "perigo")
        caixa.addButton("Cancelar", QMessageBox.ButtonRole.RejectRole)
        caixa.exec()
        if caixa.clickedButton() is not botao_remover:
            return
        try:
            self.servico.remover(camera_id, apagar_arquivos=apagar.isChecked())
        except (ErroBanco, OSError) as erro:
            QMessageBox.critical(self, "Remover câmera", f"Não foi possível remover: {erro}")
            return
        self.recarregar_cameras()
        self.notificador.mostrar("Câmera removida", camera.nome, "info")

    def alternar_deteccao(self, camera_id: int) -> None:
        camera = self.servico.definir_deteccao(camera_id, not self._cameras[camera_id].deteccao)
        self.recarregar_cameras()  # o serviço de IA passa a ignorar (ou acompanhar) as vistas dela
        estado = "ativada" if camera.deteccao else "desativada"
        self.notificador.mostrar(f"Detecção {estado}", camera.nome, "info")

    def tirar_foto(self, chave: Chave) -> None:
        camera = self._cameras.get(chave[0])
        vista = next((v for v in camera.vistas() if v.chave == chave), None) if camera else None
        leitor = self.gerenciador.leitor_da_vista(vista) if vista else None
        quadro = leitor.ultimo_quadro() if leitor else None
        if camera is None or vista is None or quadro is None:
            self.notificador.mostrar("Sem imagem para fotografar", "A câmera ainda não está online.", "aviso")
            return
        imagem = vista.recortar(quadro.imagem)
        try:
            caminho = self.servico.salvar_foto(camera, vista, imagem)
        except OSError as erro:
            self.notificador.mostrar("Não foi possível salvar a foto", str(erro), "erro")
            return
        self.notificador.mostrar(
            "Foto salva",
            f"{camera.nome_vista(vista)} — clique para abrir a pasta",
            "sucesso",
            miniatura=converter_para_qimage(imagem, 176, 100),
            ao_clicar=lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(caminho.parent))),
        )

    def menu_da_vista(self, chave: Chave, posicao: QPoint) -> None:
        camera = self._cameras.get(chave[0])
        if camera is None:
            return
        self.mosaico.selecionar(chave)
        self._usar_no_joystick(chave[0])
        menu = QMenu(self)
        menu.addAction(tema.icone("mdi6.pencil-outline"), "Editar câmera", lambda: self.editar_camera(chave[0]))
        menu.addAction(tema.icone("mdi6.camera-outline"), "Tirar foto", lambda: self.tirar_foto(chave))
        texto_deteccao = "Desativar detecção" if camera.deteccao else "Ativar detecção"
        menu.addAction(tema.icone("mdi6.motion-sensor"), texto_deteccao, lambda: self.alternar_deteccao(chave[0]))
        menu.addAction(tema.icone("mdi6.refresh"), "Reconectar", lambda: self.gerenciador.reconectar(chave[0]))
        menu.addSeparator()
        menu.addAction(tema.icone("mdi6.delete-outline"), "Remover câmera", lambda: self.remover_camera(chave[0]))
        menu.exec(posicao)

    # ================================================================ layout e tema
    def definir_layout(self, layout: str) -> None:
        self.mosaico.definir_layout(layout)
        self._salvar_ajuste("interface.layout_mosaico", layout)

    def definir_tema(self, nome: str) -> None:
        from PySide6.QtWidgets import QApplication

        tema.aplicar_tema(QApplication.instance(), nome)  # type: ignore[arg-type]
        self._salvar_ajuste("interface.tema", nome)

    def _salvar_ajuste(self, chave: str, valor: object) -> None:
        try:
            self.config.definir(chave, valor)
            self.config.salvar()
        except (OSError, ErroConfig) as erro:
            log.warning("Não foi possível salvar %s: %s", chave, erro)

    # ================================================================ tela cheia
    def alternar_tela_cheia(self) -> None:
        if self.isFullScreen():
            self.showNormal()
        else:
            self.showFullScreen()

    def _esc(self) -> None:
        if self.tela_camera.editando_zona:
            self.tela_camera.cancelar_zona()
        elif self._camera_tela_cheia:
            self.alternar_tela_cheia_camera()
        elif self.isFullScreen():
            self.showNormal()
        elif self.tela_camera.aberta and self.tela_camera.vendo_evento:
            self.tela_camera.voltar_ao_vivo()
        elif self.tela_camera.aberta:
            self._voltar_ao_mosaico()

    # ================================================================ ciclo de vida
    def changeEvent(self, evento: QEvent) -> None:  # noqa: N802
        if evento.type() == QEvent.Type.WindowStateChange:
            self.mosaico.definir_minimizado(self.isMinimized())  # minimizada: nada de converter imagens
        super().changeEvent(evento)

    def closeEvent(self, evento: QCloseEvent) -> None:  # noqa: N802
        self.tela_camera.parar()
        self.pagina_eventos.visualizador.encerrar()
        self.distribuidor.parar()
        super().closeEvent(evento)

    def vista(self, chave: Chave) -> Vista | None:
        camera = self._cameras.get(chave[0])
        return next((v for v in camera.vistas() if v.chave == chave), None) if camera else None

    @staticmethod
    def icone_app() -> QIcon:
        return tema.icone("mdi6.shield-home-outline", "destaque")
