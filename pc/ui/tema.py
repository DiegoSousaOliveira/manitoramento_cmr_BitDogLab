"""Tema visual: paleta de cores, fontes, ícones e folha de estilos (QSS).

Para mudar as cores do app inteiro, edite só ``ESCURO`` e ``CLARO`` abaixo.

Como funciona:
- A **paleta** é um conjunto de cores com nomes de PAPEL ("destaque", "alerta"...),
  não de cor ("azul"). Assim o tema claro só troca os valores.
- O **QSS** é o "CSS do Qt": gerado a partir da paleta em ``gerar_qss``.
  Os widgets escolhem variações com propriedades, ex.:
  ``botao.setProperty("variante", "primario")`` -> ``QPushButton[variante="primario"]``.
- Widgets desenhados à mão (blocos de vídeo, interruptores) leem ``paleta_atual()``
  na hora de pintar, e escutam ``sinais.tema_mudou`` para trocar ícones.
- Vermelho (``alerta``) é RESERVADO para alertas de verdade.
"""

from __future__ import annotations

from dataclasses import dataclass, fields

import qtawesome as qta
from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QColor, QFont, QFontDatabase, QIcon, QPalette
from PySide6.QtWidgets import QApplication

from core.modelos import CORES_CLASSES  # noqa: F401 (tema.CORES_CLASSES: cores por classe, iguais nos dois temas)


@dataclass(frozen=True)
class Paleta:
    nome: str
    fundo: str  # fundo da janela
    lateral: str  # barra lateral
    superficie: str  # cartões e diálogos
    superficie_alta: str  # campos, hover
    borda: str
    texto: str
    texto_suave: str
    texto_fraco: str
    destaque: str  # cor de ação (azul-ciano)
    destaque_texto: str  # texto sobre o destaque
    alerta: str  # vermelho — só para alertas
    sucesso: str  # verde — online
    aviso: str  # amarelo — conectando
    offline: str  # cinza — offline
    video_fundo: str  # fundo do bloco de vídeo sem imagem


ESCURO = Paleta(
    nome="escuro",
    fundo="#0F1115",
    lateral="#0A0C10",
    superficie="#171A21",
    superficie_alta="#1F242D",
    borda="#2A303B",
    texto="#E8EAF0",
    texto_suave="#A3AAB8",
    texto_fraco="#6B7385",
    destaque="#22C3E6",
    destaque_texto="#04161C",
    alerta="#EF4444",
    sucesso="#22C55E",
    aviso="#F5B300",
    offline="#6B7385",
    video_fundo="#07080B",
)

CLARO = Paleta(
    nome="claro",
    fundo="#F2F4F7",
    lateral="#FFFFFF",
    superficie="#FFFFFF",
    superficie_alta="#EDF0F4",
    borda="#D9DEE6",
    texto="#151922",
    texto_suave="#4B5465",
    texto_fraco="#8A93A3",
    destaque="#0A9CC0",
    destaque_texto="#FFFFFF",
    alerta="#DC2626",
    sucesso="#16A34A",
    aviso="#D08700",
    offline="#9AA2B1",
    video_fundo="#14171D",
)

TEMAS: dict[str, Paleta] = {"escuro": ESCURO, "claro": CLARO}


#: Fontes preferidas, na ordem. A primeira instalada é usada.
FONTES = ("Inter", "Inter Variable", "Segoe UI Variable Text", "Segoe UI", "Ubuntu", "Cantarell", "Noto Sans")
RAIO = 12  # cantos dos cartões e blocos
ESPACO = 12  # espaçamento padrão entre elementos


class _SinaisTema(QObject):
    tema_mudou = Signal(object)  # Paleta


sinais = _SinaisTema()
_atual: Paleta = ESCURO


def paleta_atual() -> Paleta:
    return _atual


def cor(nome: str, alfa: float = 1.0) -> QColor:
    """Cor da paleta atual como ``QColor`` (com transparência opcional, 0 a 1)."""
    c = QColor(getattr(_atual, nome))
    c.setAlphaF(alfa)
    return c


def icone(nome: str, cor_nome: str = "texto_suave", cor_ativa: str | None = None) -> QIcon:
    """Ícone do qtawesome (ex.: ``"mdi6.cctv"``) colorido com a paleta atual."""
    opcoes: dict[str, str] = {"color": getattr(_atual, cor_nome)}
    if cor_ativa:
        opcoes["color_active"] = getattr(_atual, cor_ativa)
        opcoes["color_on"] = getattr(_atual, cor_ativa)
    return qta.icon(nome, **opcoes)


def escolher_fonte(tamanho_pt: float = 10) -> QFont:
    instaladas = set(QFontDatabase.families())
    familia = next((f for f in FONTES if f in instaladas), QApplication.font().family())
    fonte = QFont(familia)
    fonte.setPointSizeF(tamanho_pt)
    fonte.setHintingPreference(QFont.HintingPreference.PreferNoHinting)
    return fonte


def aplicar_tema(app: QApplication, nome: str) -> Paleta:
    """Aplica estilo Fusion + paleta + QSS e avisa os widgets que desenham sozinhos."""
    global _atual
    _atual = TEMAS.get(nome, ESCURO)
    app.setStyle("Fusion")  # estilo igual no Windows e no Linux, e que respeita a paleta
    app.setFont(escolher_fonte())
    app.setPalette(_qpalette(_atual))
    app.setStyleSheet(gerar_qss(_atual))
    sinais.tema_mudou.emit(_atual)
    return _atual


def _qpalette(p: Paleta) -> QPalette:
    q = QPalette()
    papel = QPalette.ColorRole
    q.setColor(papel.Window, QColor(p.fundo))
    q.setColor(papel.WindowText, QColor(p.texto))
    q.setColor(papel.Base, QColor(p.superficie_alta))
    q.setColor(papel.AlternateBase, QColor(p.superficie))
    q.setColor(papel.Text, QColor(p.texto))
    q.setColor(papel.Button, QColor(p.superficie_alta))
    q.setColor(papel.ButtonText, QColor(p.texto))
    q.setColor(papel.Highlight, QColor(p.destaque))
    q.setColor(papel.HighlightedText, QColor(p.destaque_texto))
    q.setColor(papel.ToolTipBase, QColor(p.superficie_alta))
    q.setColor(papel.ToolTipText, QColor(p.texto))
    q.setColor(papel.PlaceholderText, QColor(p.texto_fraco))
    q.setColor(papel.Link, QColor(p.destaque))
    for grupo_cor in (papel.Text, papel.ButtonText, papel.WindowText):
        q.setColor(QPalette.ColorGroup.Disabled, grupo_cor, QColor(p.texto_fraco))
    return q


def _rgba(hexa: str, alfa: float) -> str:
    c = QColor(hexa)
    return f"rgba({c.red()}, {c.green()}, {c.blue()}, {alfa})"


def gerar_qss(p: Paleta) -> str:
    """Folha de estilos do app inteiro, a partir da paleta."""
    v = {f.name: getattr(p, f.name) for f in fields(p)}
    v["destaque_hover"] = QColor(p.destaque).lighter(112).name()
    v["destaque_suave"] = _rgba(p.destaque, 0.14)
    v["destaque_borda"] = _rgba(p.destaque, 0.55)
    v["alerta_hover"] = QColor(p.alerta).lighter(112).name()
    v["raio"] = RAIO
    return """
QMainWindow, QDialog {{ background: {fundo}; }}
QWidget {{ color: {texto}; }}
QToolTip {{ background: {superficie_alta}; color: {texto}; border: 1px solid {borda};
            border-radius: 6px; padding: 6px 8px; }}

/* ---------- estrutura ---------- */
#barraLateral {{ background: {lateral}; border-right: 1px solid {borda}; }}
#barraSuperior {{ background: {fundo}; border-bottom: 1px solid {borda}; }}
#cartao {{ background: {superficie}; border: 1px solid {borda}; border-radius: {raio}px; }}
#rodapeDialogo {{ background: {superficie}; border-top: 1px solid {borda}; }}

/* ---------- textos ---------- */
QLabel[papel="titulo"] {{ font-size: 19px; font-weight: 600; }}
QLabel[papel="titulo-grande"] {{ font-size: 26px; font-weight: 600; }}
QLabel[papel="suave"] {{ color: {texto_suave}; }}
QLabel[papel="fraco"] {{ color: {texto_fraco}; font-size: 12px; }}
QLabel[papel="relogio"] {{ color: {texto_suave}; font-size: 15px; font-weight: 500; }}
QLabel[papel="secao"] {{ color: {texto_fraco}; font-size: 11px; font-weight: 600; }}
QLabel[papel="erro"] {{ color: {alerta}; font-size: 12px; }}
QLabel[papel="ok"] {{ color: {sucesso}; }}
QLabel[papel="dica"] {{ color: {texto_suave}; font-size: 12px; }}

/* ---------- barra lateral ---------- */
QToolButton#itemLateral {{ border: none; border-radius: 10px; padding: 8px; background: transparent;
                            color: {texto_fraco}; }}
QToolButton#itemLateral:hover {{ background: {superficie_alta}; color: {texto_suave}; }}
QToolButton#itemLateral:checked {{ background: {destaque_suave}; color: {destaque}; font-weight: 600; }}

/* ---------- tela de Ajustes ---------- */
QToolButton#itemAjustes {{ border: none; border-radius: 8px; padding: 8px 12px; background: transparent;
                           color: {texto_suave}; font-weight: 500; }}
QToolButton#itemAjustes:hover {{ background: {superficie_alta}; color: {texto}; }}
QToolButton#itemAjustes:checked {{ background: {destaque_suave}; color: {destaque}; font-weight: 600; }}
#barraSalvar {{ background: {superficie}; border-top: 1px solid {borda}; }}

/* ---------- botões ---------- */
QPushButton {{ background: {superficie_alta}; border: 1px solid {borda}; border-radius: 8px;
               padding: 7px 14px; font-weight: 500; }}
QPushButton:hover {{ border-color: {destaque_borda}; }}
QPushButton:pressed {{ background: {borda}; }}
QPushButton:disabled {{ color: {texto_fraco}; border-color: {borda}; }}
QPushButton[variante="primario"] {{ background: {destaque}; color: {destaque_texto}; border: none; font-weight: 600; }}
QPushButton[variante="primario"]:hover {{ background: {destaque_hover}; }}
QPushButton[variante="primario"]:disabled {{ background: {borda}; color: {texto_fraco}; }}
QPushButton[variante="perigo"] {{ background: {alerta}; color: white; border: none; font-weight: 600; }}
QPushButton[variante="perigo"]:hover {{ background: {alerta_hover}; }}
QPushButton[variante="fantasma"] {{ background: transparent; border: none; }}
QPushButton[variante="fantasma"]:hover {{ background: {superficie_alta}; }}
QToolButton[papel="segmento"] {{ background: transparent; border: none; border-radius: 6px; padding: 5px 9px;
                                 color: {texto_suave}; font-weight: 500; }}
QToolButton[papel="segmento"]:hover {{ color: {texto}; }}
QToolButton[papel="segmento"]:checked {{ background: {superficie}; color: {texto}; }}
#grupoSegmentos {{ background: {superficie_alta}; border: 1px solid {borda}; border-radius: 8px; }}
QToolButton[papel="icone"] {{ background: transparent; border: none; border-radius: 6px; padding: 4px; }}
QToolButton[papel="icone"]:hover {{ background: {superficie_alta}; }}

/* ---------- campos ---------- */
QLineEdit, QSpinBox, QComboBox, QPlainTextEdit {{
    background: {fundo}; border: 1px solid {borda}; border-radius: 8px; padding: 6px 10px;
    selection-background-color: {destaque}; selection-color: {destaque_texto}; }}
QLineEdit:focus, QSpinBox:focus, QComboBox:focus, QPlainTextEdit:focus {{ border-color: {destaque}; }}
QLineEdit[erro="true"], QSpinBox[erro="true"], QComboBox[erro="true"] {{ border-color: {alerta}; }}
QLineEdit:disabled, QSpinBox:disabled, QComboBox:disabled {{ color: {texto_fraco}; }}
QComboBox QAbstractItemView {{ background: {superficie}; border: 1px solid {borda}; border-radius: 8px;
                               selection-background-color: {superficie_alta}; selection-color: {texto};
                               outline: none; padding: 4px; }}
QRadioButton {{ spacing: 6px; }}

/* ---------- menus ---------- */
QMenu {{ background: {superficie}; border: 1px solid {borda}; border-radius: 10px; padding: 6px; }}
QMenu::item {{ padding: 7px 18px 7px 10px; border-radius: 6px; }}
QMenu::item:selected {{ background: {superficie_alta}; }}
QMenu::item:disabled {{ color: {texto_fraco}; }}
QMenu::separator {{ height: 1px; background: {borda}; margin: 5px 6px; }}
QMenu::icon {{ padding-left: 6px; }}

/* ---------- barras de rolagem finas ---------- */
QScrollArea {{ background: transparent; border: none; }}
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: {borda}; border-radius: 4px; min-height: 30px; }}
QScrollBar::handle:vertical:hover {{ background: {texto_fraco}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}

QMessageBox QLabel {{ min-width: 280px; }}
""".format(**v)
