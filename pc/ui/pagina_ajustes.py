"""Tela de Ajustes (Fase 8): tudo o que dá para configurar sem abrir o config.yaml.

Seções (lista à esquerda): Aparência, Inteligência artificial, Gravação de eventos,
Alertas, Placa BitDogLab, PTZ, Câmeras e Sistema.

- Os valores **padrão** ficam no ``config.yaml`` (gravado sem perder os comentários).
- Em **Câmeras**, cada câmera pode ter os próprios valores (``camera.ajustes``, no banco);
  campo vazio = usa o padrão (que aparece como dica dentro do campo).
- Mudou algo? Aparece embaixo a barra "Alterações não salvas" (Descartar / Salvar).
  Salvar grava e **aplica na hora** (sinal ``salvo``) — só a pasta de dados exige reiniciar.
- A seção da placa tem o próprio botão ("Salvar e conectar"), porque ela conecta na hora.
"""

from __future__ import annotations

import logging
import platform
from collections.abc import Callable
from pathlib import Path
from typing import Any

import cv2
from PySide6 import __version__ as versao_pyside
from PySide6.QtCore import Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QShowEvent
from PySide6.QtWidgets import (
    QButtonGroup,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QStackedWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from core.config import Config, ErroConfig
from core.modelos import Camera
from core.servico_cameras import ServicoCameras
from ui import tema
from ui.componentes import rotulo
from ui.formulario import Campo, EditorAjuste, TabelaClasses
from ui.tarefas import executar

log = logging.getLogger(__name__)

SECOES: list[tuple[str, str, str]] = [
    ("aparencia", "mdi6.palette-outline", "Aparência"),
    ("ia", "mdi6.brain", "Inteligência artificial"),
    ("eventos", "mdi6.record-rec", "Gravação de eventos"),
    ("alertas", "mdi6.alarm-light-outline", "Alertas"),
    ("placa", "mdi6.chip", "Placa BitDogLab"),
    ("ptz", "mdi6.camera-control", "PTZ (motor)"),
    ("cameras", "mdi6.cctv", "Câmeras"),
    ("sistema", "mdi6.cog-outline", "Sistema"),
]

AREAS = (("quadro", "Imagem inteira"), ("zona", "Só dentro da zona"))
MODELOS_CONHECIDOS = (
    ("yolo11n.pt", "YOLO11n — rápido (recomendado)"),
    ("yolo11s.pt", "YOLO11s — mais preciso, mais pesado"),
    ("yolov8n.pt", "YOLOv8n — o reserva"),
)

# ---------------------------------------------------------------- campos (padrões, no config.yaml)
CAMPOS_APARENCIA = [
    Campo("interface.tema", "Tema", "escolha", opcoes=(("escuro", "Escuro"), ("claro", "Claro"))),
    Campo("interface.layout_mosaico", "Layout do mosaico", "escolha",
          opcoes=(("auto", "Automático"), ("1", "1 câmera"), ("2x2", "2×2"), ("3x3", "3×3"), ("4x4", "4×4"))),
    Campo("interface.fps_mosaico", "Atualizações do mosaico", "inteiro", 1, 30, sufixo="por segundo",
          dica="Menos = mais leve para o PC. 12 é bem fluido."),
    Campo("interface.fps_tela_camera", "Atualizações da Tela da câmera", "inteiro", 5, 30, sufixo="por segundo"),
    Campo("interface.mostrar_caixas_mosaico", "Caixas de detecção no mosaico", "booleano"),
    Campo("interface.suavizar_ms", "Suavizar o vídeo ao vivo", "inteiro", 0, 1000, sufixo="ms de atraso",
          dica="O Wi-Fi entrega os quadros em rajadas (engasgos). Um pequeno atraso deixa o vídeo fluido. "
               "0 = desligado (mais imediato, menos fluido)."),
]
CAMPOS_IA_DEPOIS_DO_MODELO = [
    Campo("ia.dispositivo", "Onde a IA roda", "escolha",
          opcoes=(("auto", "Automático (placa de vídeo, se houver)"), ("cuda", "Placa de vídeo NVIDIA (CUDA)"),
                  ("cpu", "Processador (CPU)"))),
    Campo("ia.fonte", "Imagem analisada", "escolha",
          opcoes=(("principal", "Alta resolução (enxerga mais longe)"), ("substream", "Substream (mais leve)")),
          dica="Alta resolução mantém o stream HD aberto o tempo todo (mais Wi-Fi e um pouco de CPU)."),
    Campo("ia.ia_fps", "Análises por segundo", "inteiro", 1, 15, sufixo="por câmera/lente"),
    Campo("ia.conf_minima", "Confiança mínima", "decimal", 10, 90, sufixo="%", escala=100,
          dica="Abaixo disso o objeto vira DESCONHECIDO."),
    Campo("ia.ia_so_com_movimento", "Só analisar quando houver movimento", "booleano",
          dica="Economiza GPU/CPU; objetos parados continuam marcados."),
    Campo("ia.movimento_area_minima", "Tamanho mínimo do movimento", "decimal", 0.1, 10, sufixo="% da imagem",
          escala=100, dica="Maior = ignora folhas e sombras pequenas."),
    Campo("treino.coletar", "Guardar imagens difíceis para o treino (Fase 9)", "booleano"),
    Campo("treino.limite_gb", "Espaço máximo das imagens de treino", "decimal", 0.1, 100, sufixo="GB"),
    Campo("treino.automatico", "Aprender sozinho (aprendizado contínuo)", "booleano",
          dica="Todo dia, no horário abaixo, se houver imagens novas: o professor rotula, a IA treina e faz a prova."),
    Campo("treino.horario", "Horário do treino", "escolha", opcoes=tuple((f"{h:02d}:00", f"{h:02d}:00") for h in range(24))),
    Campo("treino.trocar_automatico", "Usar o modelo novo sozinho quando ele for melhor", "booleano",
          dica="Desligado: o modelo aprovado espera você escolher “Usar este” na página Treino."),
    Campo("treino.modelo_professor", "Professor (rotula as imagens)", "escolha",
          opcoes=(("yolo11x.pt", "YOLO11x — o mais preciso (recomendado)"), ("yolo11l.pt", "YOLO11l — um pouco mais leve"),
                  ("yolo11m.pt", "YOLO11m — mais leve"))),
    Campo("treino.min_novas", "Imagens novas para treinar", "inteiro", 10, 5000,
          dica="Menos que isto, a rodada automática espera juntar mais."),
    Campo("treino.memoria_minima_gb", "Memória livre para começar o treino", "decimal", 1, 64, sufixo="GB",
          dica="Menos que isto (RAM + arquivo de paginação), o treino espera: as câmeras têm prioridade."),
    Campo("treino.memoria_critica_gb", "Parar o treino se a memória livre cair abaixo de", "decimal", 0.5, 16,
          sufixo="GB"),
]
CAMPOS_EVENTOS = [
    Campo("eventos.area", "Onde o movimento abre evento", "escolha", opcoes=AREAS),
    Campo("eventos.pre_evento_s", "Gravar antes do início", "decimal", 0, 15, sufixo="s"),
    Campo("eventos.pos_evento_s", "Gravar depois do fim", "decimal", 0, 15, sufixo="s"),
    Campo("eventos.fim_evento_s", "Fim do evento após", "decimal", 1, 30, sufixo="s sem atividade"),
    Campo("eventos.max_evento_s", "Duração máxima de um evento", "decimal", 10, 600, sufixo="s"),
    Campo("eventos.qualidade_clipe", "Qualidade do vídeo", "inteiro", 18, 32,
          dica="18 = melhor e maior; 28 = menor. 23 é um bom meio-termo."),
    Campo("eventos.retencao_dias", "Guardar eventos por", "inteiro", 1, 365, sufixo="dias"),
    Campo("eventos.limite_disco_gb", "Espaço máximo dos eventos", "decimal", 1, 2000, sufixo="GB",
          dica="Passou disso, os eventos mais antigos são apagados primeiro."),
]
CAMPOS_ALERTAS = [
    Campo("parado.cooldown_s", "Esperar para alertar de novo o mesmo objeto", "decimal", 10, 3600, sufixo="s"),
    Campo("parado.aquecimento_s", "Ignorar o que já está parado ao ligar", "decimal", 0, 300, sufixo="s",
          dica="Ex.: o carro do vizinho estacionado. Vale até ele se mover."),
    Campo("parado.limite_movimento", "Quanto pode se mexer e ainda ser “parado”", "decimal", 1, 20,
          sufixo="% da largura", escala=100),
    Campo("parado.tolerancia_sumico_s", "Pode sumir (alguém passou na frente) por", "decimal", 0, 10, sufixo="s"),
    Campo("parado.limpar_apos_sumico_s", "Alerta some depois de o objeto sumir por", "decimal", 1, 60, sufixo="s"),
    Campo("parado.passou_desconhecido", "DESCONHECIDO também gera “PASSOU”", "booleano"),
    Campo("parado.sem_zona", "Lente sem zona desenhada", "escolha",
          opcoes=(("ignorar", "Não gera alertas (recomendado)"), ("imagem_inteira", "Vigia a imagem inteira")),
          dica="A zona de uma lente vale SÓ para ela. Vale também para “Só dentro da zona” dos eventos."),
]
CAMPOS_PTZ = [
    Campo("ptz.velocidade_botoes", "Velocidade das setas (tela e teclado)", "decimal", 10, 100, sufixo="%", escala=100),
    Campo("ptz.inverter_x", "Inverter esquerda/direita", "booleano"),
    Campo("ptz.inverter_y", "Inverter cima/baixo", "booleano"),
    Campo("ptz.timeout_joystick_s", "Parar se ficar sem comando por", "decimal", 1, 10, sufixo="s",
          dica="Segurança: um “soltei o joystick” perdido não deixa a câmera girando."),
]
CAMPOS_SISTEMA = [
    Campo("geral.log_nivel", "Detalhe do log", "escolha",
          opcoes=(("INFO", "Normal"), ("DEBUG", "Detalhado (para investigar problemas)"), ("WARNING", "Só avisos"),
                  ("ERROR", "Só erros"))),
    Campo("geral.pasta_dados", "Pasta de dados", "texto", reiniciar=True,
          dica="Banco, eventos, fotos e logs. Mudar só vale depois de reiniciar o app; os dados atuais ficam onde estão."),
]

# ---------------------------------------------------------------- campos por câmera (camera.ajustes)
CAMPOS_CAMERA = [
    Campo("eventos.area", "Onde o movimento abre evento", "escolha", opcoes=AREAS),
    Campo("ia.ia_fps", "Análises por segundo", "inteiro", 1, 15),
    Campo("ia.conf_minima", "Confiança mínima", "decimal", 10, 90, sufixo="%", escala=100),
    Campo("ia.ia_so_com_movimento", "Só analisar com movimento", "booleano"),
    Campo("ia.movimento_area_minima", "Tamanho mínimo do movimento", "decimal", 0.1, 10, sufixo="% da imagem",
          escala=100),
    Campo("parado.cooldown_s", "Esperar para alertar de novo", "decimal", 10, 3600, sufixo="s"),
    Campo("parado.aquecimento_s", "Ignorar o que já está parado ao ligar", "decimal", 0, 300, sufixo="s"),
]
CAMPOS_CAMERA_PTZ = [
    Campo("ptz.inverter_x", "Inverter esquerda/direita", "booleano"),
    Campo("ptz.inverter_y", "Inverter cima/baixo", "booleano"),
]
#: Chaves de ``camera.ajustes`` que esta tela controla (as outras são preservadas).
CHAVES_CAMERA = {c.chave for c in CAMPOS_CAMERA + CAMPOS_CAMERA_PTZ} | {"parado.tempo_parado", "parado.nivel",
                                                                     "parado.alertar"}
#: Classes que a IA conhece (a ordem da tela).
CLASSES_TELA = ("PESSOA", "BICICLETA", "CARRO", "MOTO", "ONIBUS", "CAMINHAO", "GATO", "CACHORRO", "DESCONHECIDO")


def tamanho_pasta(pasta: Path) -> int:
    return sum(p.stat().st_size for p in pasta.rglob("*") if p.is_file()) if pasta.exists() else 0


class PaginaAjustes(QWidget):
    salvo = Signal(object, object)  # chaves do config alteradas (set[str]), ids de câmeras alteradas (set[int])
    editar_camera = Signal(int)
    limpar_eventos = Signal()

    def __init__(
        self,
        config: Config,
        servico: ServicoCameras,
        notificar: Callable[..., object],
        secao_placa: QWidget,
        extra_ia: QWidget | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._config = config
        self._servico = servico
        self._notificar = notificar
        self._editores: list[EditorAjuste] = []  # padrões (config.yaml)
        self._camera: Camera | None = None
        self._editores_camera: list[EditorAjuste] = []  # da câmera escolhida
        self._tabela_camera: TabelaClasses | None = None

        # ---------- navegação à esquerda
        self._grupo = QButtonGroup(self)
        self._botoes: dict[str, QToolButton] = {}
        navegacao = QVBoxLayout()
        navegacao.setContentsMargins(16, 20, 8, 16)
        navegacao.setSpacing(2)
        navegacao.addWidget(rotulo("AJUSTES", "secao"))
        navegacao.addSpacing(6)
        for nome, icone, texto in SECOES:
            botao = QToolButton()
            botao.setObjectName("itemAjustes")
            botao.setText(texto)
            botao.setProperty("icone", icone)
            botao.setCheckable(True)
            botao.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
            botao.setMinimumWidth(210)
            botao.setCursor(Qt.CursorShape.PointingHandCursor)
            botao.clicked.connect(lambda _c=False, n=nome: self.mostrar_secao(n))
            self._grupo.addButton(botao)
            self._botoes[nome] = botao
            navegacao.addWidget(botao)
        navegacao.addStretch(1)

        # ---------- barra "alterações não salvas"
        self.barra = QFrame()
        self.barra.setObjectName("barraSalvar")
        self.texto_barra = rotulo("Você tem alterações não salvas.", "suave")
        self.erro = rotulo("", "erro", quebra=True)
        self.botao_descartar = QPushButton("Descartar")
        self.botao_descartar.setProperty("variante", "fantasma")
        self.botao_descartar.clicked.connect(self.descartar)
        self.botao_salvar = QPushButton("Salvar")
        self.botao_salvar.setProperty("variante", "primario")
        self.botao_salvar.clicked.connect(self.salvar)
        linha = QHBoxLayout(self.barra)
        linha.setContentsMargins(24, 10, 24, 10)
        linha.setSpacing(12)
        linha.addWidget(self.texto_barra)
        linha.addWidget(self.erro, 1)
        linha.addWidget(self.botao_descartar)
        linha.addWidget(self.botao_salvar)
        self.barra.hide()

        # ---------- páginas
        self.pilha = QStackedWidget()
        self._indices: dict[str, int] = {}
        self._montar_aparencia()
        self._montar_ia(extra_ia)
        self._montar_eventos()
        self._montar_alertas()
        self._montar_placa(secao_placa)
        self._montar_ptz()
        self._montar_cameras()
        self._montar_sistema()

        direita = QVBoxLayout()
        direita.setContentsMargins(0, 0, 0, 0)
        direita.setSpacing(0)
        direita.addWidget(self.pilha, 1)
        direita.addWidget(self.barra)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addLayout(navegacao)
        layout.addLayout(direita, 1)

        self._aplicar_icones()
        tema.sinais.tema_mudou.connect(self._aplicar_icones)
        self.mostrar_secao("aparencia")

    # ================================================================ montagem
    def _pagina(self, nome: str, titulo: str, descricao: str) -> QVBoxLayout:
        conteudo = QWidget()
        layout = QVBoxLayout(conteudo)
        layout.setContentsMargins(24, 22, 32, 28)
        layout.setSpacing(12)
        layout.addWidget(rotulo(titulo, "titulo"))
        if descricao:
            layout.addWidget(rotulo(descricao, "suave", quebra=True))
        layout.addSpacing(6)
        rolagem = QScrollArea()
        rolagem.setWidget(conteudo)
        rolagem.setWidgetResizable(True)
        rolagem.setFrameShape(QFrame.Shape.NoFrame)
        self._indices[nome] = self.pilha.addWidget(rolagem)
        return layout

    @staticmethod
    def _formulario(layout: QVBoxLayout) -> QFormLayout:
        formulario = QFormLayout()
        formulario.setHorizontalSpacing(18)
        formulario.setVerticalSpacing(12)
        formulario.setLabelAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        layout.addLayout(formulario)
        return formulario

    def _linha(self, formulario: QFormLayout, editor: EditorAjuste) -> EditorAjuste:
        campo = editor.campo
        texto = campo.rotulo + ("  ↻" if campo.reiniciar else "")
        etiqueta = QLabel(texto)
        etiqueta.setMinimumWidth(260)
        if campo.reiniciar:
            etiqueta.setToolTip("Só vale depois de reiniciar o app")
        if campo.dica:
            coluna = QWidget()
            interno = QVBoxLayout(coluna)
            interno.setContentsMargins(0, 0, 0, 0)
            interno.setSpacing(3)
            interno.addWidget(editor)
            interno.addWidget(rotulo(campo.dica, "dica", quebra=True))
            formulario.addRow(etiqueta, coluna)
        else:
            formulario.addRow(etiqueta, editor)
        return editor

    def _campo(self, formulario: QFormLayout, campo: Campo) -> EditorAjuste:
        editor = EditorAjuste(campo, self._config.obter(campo.chave))
        editor.mudou.connect(self._atualizar_barra)
        self._editores.append(editor)
        return self._linha(formulario, editor)

    def _montar_aparencia(self) -> None:
        layout = self._pagina("aparencia", "Aparência", "Como o app se apresenta. Tudo vale na hora.")
        formulario = self._formulario(layout)
        for campo in CAMPOS_APARENCIA:
            self._campo(formulario, campo)
        layout.addStretch(1)

    def _montar_ia(self, extra: QWidget | None) -> None:
        layout = self._pagina(
            "ia", "Inteligência artificial",
            "O detector de objetos (YOLO). Trocar o modelo ou o dispositivo não exige reiniciar: a IA "
            "continua com o modelo atual até o novo ficar pronto.",
        )
        formulario = self._formulario(layout)
        pasta = self._config.resolver_caminho(str(self._config.obter("ia.pasta_modelos")))
        opcoes = list(MODELOS_CONHECIDOS)
        conhecidos = {valor for valor, _ in opcoes}
        for arquivo in sorted(pasta.glob("*.pt")) if pasta.exists() else []:
            if arquivo.name not in conhecidos:
                opcoes.append((arquivo.name, f"{arquivo.name} (na pasta de modelos)"))
        self._campo(formulario, Campo("ia.modelo", "Modelo", "escolha", opcoes=tuple(opcoes),
                                      dica="Modelos novos são baixados na primeira vez (precisa de internet)."))
        for campo in CAMPOS_IA_DEPOIS_DO_MODELO:
            self._campo(formulario, campo)
        layout.addSpacing(10)
        layout.addWidget(rotulo("OBJETOS QUE A IA IDENTIFICA", "secao"))
        layout.addWidget(rotulo("Desmarcados somem da tela, dos eventos e dos alertas. DESCONHECIDO = manchas de "
                                "movimento sem objeto reconhecido (folhas, sombras, chuva...).", "dica", quebra=True))
        objetos = self._formulario(layout)
        for classe in CLASSES_TELA:
            self._campo(objetos, Campo(f"ia.identificar.{classe}", classe, "booleano"))
        if extra is not None:
            layout.addSpacing(10)
            layout.addWidget(rotulo("STATUS E EXPORTAÇÃO", "secao"))
            layout.addWidget(extra)
        layout.addStretch(1)

    def _montar_eventos(self) -> None:
        layout = self._pagina(
            "eventos", "Gravação de eventos",
            "Quando um evento começa e termina, quanto vídeo guardar e por quanto tempo. "
            "Cada câmera pode ter a própria área (seção Câmeras).",
        )
        formulario = self._formulario(layout)
        for campo in CAMPOS_EVENTOS:
            self._campo(formulario, campo)
        layout.addSpacing(10)
        layout.addWidget(rotulo("ESPAÇO EM DISCO", "secao"))
        self.uso_disco = rotulo("Calculando…", "suave")
        botao_limpar = QPushButton("Limpar eventos antigos agora")
        botao_limpar.setToolTip("Apaga já os eventos mais velhos que o limite de dias ou que passam do espaço máximo")
        botao_limpar.clicked.connect(self.limpar_eventos)
        linha = QHBoxLayout()
        linha.addWidget(self.uso_disco)
        linha.addStretch(1)
        linha.addWidget(botao_limpar)
        layout.addLayout(linha)
        layout.addStretch(1)

    def _montar_alertas(self) -> None:
        layout = self._pagina(
            "alertas", "Alertas (objeto parado na zona)",
            "Valores padrão de todas as câmeras. Em Câmeras, cada uma pode ter os seus.",
        )
        formulario = self._formulario(layout)
        for campo in CAMPOS_ALERTAS:
            self._campo(formulario, campo)
        layout.addSpacing(10)
        layout.addWidget(rotulo("POR CLASSE: ALERTA, TEMPO PARADO E NÍVEL", "secao"))
        tempos = dict(self._config.obter("parado.tempo_parado"))
        niveis = dict(self._config.obter("parado.nivel"))
        alertar = dict(self._config.obter_ou("parado.alertar", {}))
        self.tabela_classes = TabelaClasses(tempos, niveis, alertar=alertar)
        for editor in self.tabela_classes.editores:
            editor.mudou.connect(self._atualizar_barra)
            self._editores.append(editor)
        layout.addWidget(self.tabela_classes)
        layout.addStretch(1)

    def _montar_placa(self, secao: QWidget) -> None:
        layout = self._pagina(
            "placa", "Placa BitDogLab (dispositivo de alerta)",
            "A placa mostra o IP dela no display: digite aqui e clique em “Salvar e conectar”.",
        )
        layout.addWidget(secao)
        layout.addStretch(1)

    def _montar_ptz(self) -> None:
        layout = self._pagina("ptz", "PTZ (motor das câmeras)", "Padrões; a inversão também pode ser por câmera.")
        formulario = self._formulario(layout)
        for campo in CAMPOS_PTZ:
            self._campo(formulario, campo)
        layout.addStretch(1)

    def _montar_cameras(self) -> None:
        layout = self._pagina(
            "cameras", "Ajustes por câmera",
            "Deixe um campo vazio (ou em “Padrão”) para usar o valor geral. Nome, IP, senha e lentes "
            "ficam em “Editar câmera”.",
        )
        self.escolha_camera = QComboBox()
        self.escolha_camera.setMinimumWidth(280)
        self.escolha_camera.activated.connect(self._camera_escolhida)
        self.botao_editar_camera = QPushButton("Editar câmera…")
        self.botao_editar_camera.clicked.connect(
            lambda: self.editar_camera.emit(self._camera.id) if self._camera and self._camera.id else None
        )
        self.botao_padroes = QPushButton("Voltar tudo ao padrão")
        self.botao_padroes.clicked.connect(self._camera_padroes)
        topo = QHBoxLayout()
        topo.addWidget(self.escolha_camera)
        topo.addWidget(self.botao_editar_camera)
        topo.addStretch(1)
        topo.addWidget(self.botao_padroes)
        layout.addLayout(topo)
        self._area_camera = QVBoxLayout()
        self._area_camera.setSpacing(12)
        layout.addLayout(self._area_camera)
        self.sem_cameras = rotulo("Nenhuma câmera cadastrada ainda.", "suave")
        layout.addWidget(self.sem_cameras)
        layout.addStretch(1)
        self.recarregar_cameras()

    def _montar_sistema(self) -> None:
        layout = self._pagina("sistema", "Sistema", "")
        formulario = self._formulario(layout)
        self._campo(formulario, CAMPOS_SISTEMA[0])
        self.editor_pasta = self._campo(formulario, CAMPOS_SISTEMA[1])
        escolher = QPushButton("Escolher pasta…")
        escolher.clicked.connect(self._escolher_pasta)
        formulario.addRow("", escolher)

        layout.addSpacing(10)
        layout.addWidget(rotulo("PASTAS", "secao"))
        for texto, pasta in (("Dados", self._config.pasta_dados), ("Logs", self._config.pasta_logs)):
            linha = QHBoxLayout()
            linha.addWidget(rotulo(f"{texto}: {pasta}", "suave"))
            linha.addStretch(1)
            abrir = QPushButton("Abrir")
            abrir.clicked.connect(lambda _c=False, p=pasta: QDesktopServices.openUrl(QUrl.fromLocalFile(str(p))))
            linha.addWidget(abrir)
            layout.addLayout(linha)
        layout.addSpacing(10)
        layout.addWidget(rotulo("SOBRE", "secao"))
        layout.addWidget(rotulo(
            f"Sentinela Casa · Python {platform.python_version()} · PySide6 {versao_pyside} · "
            f"OpenCV {cv2.__version__} · {platform.system()} {platform.release()}",
            "suave", quebra=True,
        ))
        layout.addStretch(1)

    # ================================================================ câmeras
    def recarregar_cameras(self) -> None:
        """Lista de câmeras (chamada ao mostrar a página e quando o cadastro muda)."""
        cameras = self._servico.listar()
        atual = self._camera.id if self._camera else None
        self.escolha_camera.blockSignals(True)
        self.escolha_camera.clear()
        for camera in cameras:
            self.escolha_camera.addItem(camera.nome, camera.id)
        self.escolha_camera.blockSignals(False)
        tem = bool(cameras)
        self.sem_cameras.setVisible(not tem)
        for widget in (self.escolha_camera, self.botao_editar_camera, self.botao_padroes):
            widget.setVisible(tem)
        if not tem:
            self._mostrar_camera(None)
            return
        escolhida = next((c for c in cameras if c.id == atual), cameras[0])
        self.escolha_camera.setCurrentIndex(self.escolha_camera.findData(escolhida.id))
        if not self._camera_alterada():
            self._mostrar_camera(escolhida)

    def escolher_camera(self, camera_id: int) -> None:
        indice = self.escolha_camera.findData(camera_id)
        if indice >= 0:
            self.escolha_camera.setCurrentIndex(indice)
            self._camera_escolhida(indice)

    def _camera_escolhida(self, _indice: int) -> None:
        camera_id = self.escolha_camera.currentData()
        if self._camera is not None and camera_id == self._camera.id:
            return
        if self._camera_alterada():
            resposta = QMessageBox.question(
                self, "Ajustes da câmera",
                f"Salvar as alterações de “{self._camera.nome if self._camera else ''}” antes de trocar?",
                QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Discard | QMessageBox.StandardButton.Cancel,
            )
            if resposta == QMessageBox.StandardButton.Cancel or (
                resposta == QMessageBox.StandardButton.Save and not self.salvar()
            ):
                self.escolha_camera.setCurrentIndex(self.escolha_camera.findData(self._camera.id if self._camera else None))
                return
        self._mostrar_camera(self._servico.obter(camera_id) if camera_id is not None else None)

    def _mostrar_camera(self, camera: Camera | None) -> None:
        """Monta o formulário da câmera escolhida (valores próprios + padrões como dica)."""
        while self._area_camera.count():
            item = self._area_camera.takeAt(0)
            if item.widget():
                _descartar(item.widget())
            elif item.layout():
                _apagar_layout(item.layout())
        self._editores_camera = []
        self._tabela_camera = None
        self._camera = camera
        if camera is None:
            self._atualizar_barra()
            return
        cartao = QWidget()
        layout = QVBoxLayout(cartao)
        layout.setContentsMargins(0, 6, 0, 0)
        layout.setSpacing(12)
        formulario = self._formulario(layout)
        for chave, texto, valor in (("camera.deteccao", "Detecção de objetos (IA)", camera.deteccao),
                                    ("camera.gravar", "Gravar eventos (foto e vídeo)", camera.gravar)):
            self._editor_camera(formulario, EditorAjuste(Campo(chave, texto, "booleano"), valor))
        campos = CAMPOS_CAMERA + (CAMPOS_CAMERA_PTZ if camera.tem_ptz else [])
        for campo in campos:
            padrao = self._config.obter(campo.chave)
            editor = EditorAjuste(campo, camera.ajustes.get(campo.chave), padrao, opcional=True)
            self._editor_camera(formulario, editor)
        layout.addWidget(rotulo("POR CLASSE: ALERTA, TEMPO PARADO E NÍVEL (vazio = padrão)", "secao"))
        self._tabela_camera = TabelaClasses(
            dict(camera.ajustes.get("parado.tempo_parado", {})), dict(camera.ajustes.get("parado.nivel", {})),
            dict(self._config.obter("parado.tempo_parado")), dict(self._config.obter("parado.nivel")), opcional=True,
            alertar=dict(camera.ajustes.get("parado.alertar", {})),
            padroes_alertar=dict(self._config.obter_ou("parado.alertar", {})),
        )
        for editor in self._tabela_camera.editores:
            editor.mudou.connect(self._atualizar_barra)
            self._editores_camera.append(editor)
        layout.addWidget(self._tabela_camera)
        self._area_camera.addWidget(cartao)
        self._atualizar_barra()

    def _editor_camera(self, formulario: QFormLayout, editor: EditorAjuste) -> None:
        editor.mudou.connect(self._atualizar_barra)
        self._editores_camera.append(editor)
        self._linha(formulario, editor)

    def _camera_alterada(self) -> bool:
        return any(e.alterado for e in self._editores_camera)

    def _camera_padroes(self) -> None:
        for editor in self._editores_camera:
            if editor.opcional:
                editor.definir(None)

    def _ajustes_da_camera(self, camera: Camera) -> tuple[bool, bool, dict[str, Any]]:
        ajustes = {k: v for k, v in camera.ajustes.items() if k not in CHAVES_CAMERA}  # chaves de outras telas ficam
        deteccao, gravar = camera.deteccao, camera.gravar
        for editor in self._editores_camera:
            chave, valor = editor.campo.chave, editor.valor()
            if chave == "camera.deteccao":
                deteccao = bool(valor)
            elif chave == "camera.gravar":
                gravar = bool(valor)
            elif editor.opcional and "." in chave and not chave.startswith(("parado.tempo_parado.", "parado.nivel.",
                                                                              "parado.alertar.")) and valor is not None:
                ajustes[chave] = valor
        if self._tabela_camera is not None:
            tempos, niveis, alertar = self._tabela_camera.valores()
            if tempos:
                ajustes["parado.tempo_parado"] = tempos
            if niveis:
                ajustes["parado.nivel"] = niveis
            if alertar:
                ajustes["parado.alertar"] = alertar
        return deteccao, gravar, ajustes

    def editor(self, chave: str) -> EditorAjuste:
        """O campo de um padrão (ex.: ``"interface.fps_mosaico"``)."""
        return next(e for e in self._editores if e.campo.chave == chave)

    def editor_camera(self, chave: str) -> EditorAjuste:
        """O campo da câmera escolhida (ex.: ``"parado.cooldown_s"``, ``"camera.deteccao"``)."""
        return next(e for e in self._editores_camera if e.campo.chave == chave)

    # ================================================================ salvar / descartar
    @property
    def tem_alteracoes(self) -> bool:
        return any(e.alterado for e in self._editores) or self._camera_alterada()

    def _todos(self) -> list[EditorAjuste]:
        return self._editores + self._editores_camera

    def _atualizar_barra(self) -> None:
        erros = [e for editor in self._todos() if (e := editor.erro())]
        self.erro.setText(erros[0] if erros else "")
        self.botao_salvar.setEnabled(not erros)
        self.barra.setVisible(self.tem_alteracoes or bool(erros))

    def salvar(self) -> bool:
        """Grava config.yaml e/ou a câmera e avisa (``salvo``) para aplicar na hora."""
        erros = [e for editor in self._todos() if (e := editor.erro())]
        if erros:
            self.erro.setText(erros[0])
            return False
        chaves: set[str] = set()
        reiniciar: list[str] = []
        try:
            for editor in self._editores:
                if editor.alterado:
                    self._config.definir(editor.campo.chave, editor.valor())
                    chaves.add(editor.campo.chave)
                    if editor.campo.reiniciar:
                        reiniciar.append(editor.campo.rotulo)
            if chaves:
                self._config.salvar()
        except (OSError, ErroConfig) as erro:
            self.erro.setText(f"Não foi possível salvar o config.yaml: {erro}")
            return False
        cameras: set[int] = set()
        if self._camera is not None and self._camera.id is not None and self._camera_alterada():
            deteccao, gravar, ajustes = self._ajustes_da_camera(self._camera)
            try:
                self._camera = self._servico.salvar_ajustes(self._camera.id, deteccao, gravar, ajustes)
            except Exception as erro:  # noqa: BLE001 (banco travado, câmera removida...)
                self.erro.setText(f"Não foi possível salvar a câmera: {erro}")
                return False
            cameras.add(self._camera.id)
        for editor in self._todos():
            editor.confirmar()
        self._atualizar_barra()
        log.info("Ajustes salvos: %s%s", ", ".join(sorted(chaves)) or "-",
                 f" · câmeras {sorted(cameras)}" if cameras else "")
        self.salvo.emit(chaves, cameras)
        detalhe = "Já estão valendo."
        if reiniciar:
            detalhe = f"Reinicie o app para valer: {', '.join(reiniciar)}."
        self._notificar("Ajustes salvos", detalhe, "aviso" if reiniciar else "sucesso")
        return True

    def descartar(self) -> None:
        for editor in self._todos():
            editor.descartar()
        self._atualizar_barra()

    # ================================================================ outros
    def mostrar_secao(self, nome: str) -> None:
        self._botoes[nome].setChecked(True)
        self.pilha.setCurrentIndex(self._indices[nome])
        if nome == "eventos":
            self._calcular_uso()

    @property
    def secao_atual(self) -> str:
        return next(n for n, i in self._indices.items() if i == self.pilha.currentIndex())

    def showEvent(self, evento: QShowEvent) -> None:  # noqa: N802
        """Valores podem ter mudado por fora (ex.: layout escolhido na barra superior)."""
        super().showEvent(evento)
        for editor in self._editores:
            editor.recarregar(self._config.obter(editor.campo.chave))
        self.recarregar_cameras()
        self._atualizar_barra()

    def _calcular_uso(self) -> None:
        pasta = self._config.pasta_dados

        def medir() -> str:
            eventos, treino = tamanho_pasta(pasta / "eventos"), tamanho_pasta(pasta / "treino")
            limite = float(self._config.obter("eventos.limite_disco_gb"))
            return (f"Eventos: {eventos / 1024**3:.2f} GB de {limite:g} GB · "
                    f"imagens de treino: {treino / 1024**2:.0f} MB")

        executar(medir, self.uso_disco.setText, lambda erro: self.uso_disco.setText(f"Não foi possível medir: {erro}"),
                 dono=self)

    def _escolher_pasta(self) -> None:
        pasta = QFileDialog.getExistingDirectory(self, "Pasta de dados", str(self._config.pasta_dados))
        if pasta:
            self.editor_pasta.definir(pasta)

    def _aplicar_icones(self, _paleta: object = None) -> None:
        for botao in self._botoes.values():
            botao.setIcon(tema.icone(str(botao.property("icone")), "texto_suave", cor_ativa="destaque"))


def _descartar(widget: QWidget) -> None:
    """Esconde JÁ e apaga depois: o ``deleteLater`` só age quando o Qt volta ao laço de eventos,
    e até lá o formulário antigo continuaria visível por cima do novo."""
    widget.hide()
    widget.deleteLater()


def _apagar_layout(layout: Any) -> None:
    while layout.count():
        item = layout.takeAt(0)
        if item.widget():
            _descartar(item.widget())
        elif item.layout():
            _apagar_layout(item.layout())
