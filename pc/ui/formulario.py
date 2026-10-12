"""Peças dos formulários da tela de Ajustes.

- ``Campo``: descreve um ajuste (chave no config.yaml ou em ``camera.ajustes``, rótulo,
  tipo, faixa permitida, unidade...).
- ``EditorAjuste``: o widget de um campo. Sabe mostrar o valor, validar, dizer se mudou
  e desfazer. No modo **"por câmera"** (``padrao`` informado), vazio significa "usar o
  padrão" — o valor padrão aparece como dica dentro do campo.
- ``TabelaClasses``: tempos de "parado" e níveis de alerta de cada classe (PESSOA, CARRO...).

``escala`` serve para mostrar frações como porcentagem: 0.05 guardado = "5" na tela com
``escala=100`` e sufixo "%".
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from PySide6.QtCore import Signal
from PySide6.QtGui import QIntValidator
from PySide6.QtWidgets import QComboBox, QGridLayout, QHBoxLayout, QLabel, QLineEdit, QWidget

from core.modelos import CLASSES, CORES_CLASSES
from ui.componentes import Interruptor, repolir, rotulo

#: Para o modo "por câmera": nenhum valor próprio (usa o padrão).
PADRAO = None


@dataclass(frozen=True)
class Campo:
    chave: str
    rotulo: str
    tipo: str  # "inteiro" | "decimal" | "booleano" | "escolha" | "texto"
    minimo: float = 0.0
    maximo: float = 0.0
    opcoes: tuple[tuple[str, str], ...] = ()  # (valor, texto) para "escolha"
    dica: str = ""
    sufixo: str = ""
    escala: float = 1.0
    reiniciar: bool = False  # só vale depois de reiniciar o app


def _formatar(numero: float) -> str:
    return f"{numero:.6g}"


class EditorAjuste(QWidget):
    mudou = Signal()

    def __init__(self, campo: Campo, valor: Any, padrao: Any = None, opcional: bool = False,
                 parent: QWidget | None = None) -> None:
        """``opcional=True``: modo "por câmera" — ``valor`` None = usar ``padrao``."""
        super().__init__(parent)
        self.campo = campo
        self.opcional = opcional
        self._padrao = padrao
        self._original: Any = None
        self._entrada: QLineEdit | None = None
        self._combo: QComboBox | None = None
        self._interruptor: Interruptor | None = None

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        if campo.tipo in ("inteiro", "decimal", "texto"):
            self._entrada = QLineEdit()
            if campo.tipo == "inteiro":
                self._entrada.setValidator(QIntValidator(-10**9, 10**9, self._entrada))
                self._entrada.setMaximumWidth(110)
            elif campo.tipo == "decimal":
                # Sem QDoubleValidator: ele segue o idioma do Windows e recusaria "0.5" (quer "0,5").
                # Quem valida é ``erro()``, que aceita vírgula ou ponto.
                self._entrada.setMaximumWidth(110)
            if opcional:
                self._entrada.setPlaceholderText(f"padrão: {self._texto_numero(padrao)}")
            self._entrada.textChanged.connect(self._ao_mudar)
            layout.addWidget(self._entrada)
        elif campo.tipo == "booleano" and not opcional:
            self._interruptor = Interruptor()
            self._interruptor.toggled.connect(self._ao_mudar)
            layout.addWidget(self._interruptor)
        else:  # escolha (ou booleano por câmera: "Padrão / Sim / Não")
            self._combo = QComboBox()
            opcoes = campo.opcoes if campo.tipo == "escolha" else ((True, "Sim"), (False, "Não"))
            if opcional:
                texto_padrao = next((t for v, t in opcoes if v == padrao), str(padrao))
                self._combo.addItem(f"Padrão ({texto_padrao})", PADRAO)
            for valor_opcao, texto in opcoes:
                self._combo.addItem(texto, valor_opcao)
            self._combo.currentIndexChanged.connect(self._ao_mudar)
            layout.addWidget(self._combo)
        if campo.sufixo:
            layout.addWidget(rotulo(campo.sufixo, "suave"))
        layout.addStretch(1)
        self.definir(valor)
        self._original = self.valor()

    # ------------------------------------------------------------ valor
    def _texto_numero(self, valor: Any) -> str:
        if valor is None or valor == "":
            return ""
        if self.campo.tipo == "texto":
            return str(valor)
        return _formatar(float(valor) * self.campo.escala)

    def definir(self, valor: Any) -> None:
        if self._entrada is not None:
            self._entrada.setText(self._texto_numero(valor))
        elif self._interruptor is not None:
            self._interruptor.setChecked(bool(valor))
        elif self._combo is not None:
            indice = self._combo.findData(valor)
            if indice < 0 and valor is not None:  # valor que não está na lista (ex.: modelo treinado)
                self._combo.addItem(str(valor), valor)
                indice = self._combo.count() - 1
            self._combo.setCurrentIndex(max(0, indice))

    def valor(self) -> Any:
        """O valor como será guardado (None = padrão, no modo por câmera)."""
        if self._interruptor is not None:
            return self._interruptor.isChecked()
        if self._combo is not None:
            return self._combo.currentData()
        assert self._entrada is not None
        texto = self._entrada.text().strip().replace(",", ".")
        if self.campo.tipo == "texto":
            return texto
        if not texto:
            return PADRAO if self.opcional else None
        try:
            numero = round(float(texto) / self.campo.escala, 6)
        except ValueError:
            return None
        if self.campo.tipo == "inteiro" or numero.is_integer():
            return round(numero)  # 8 e não 8.0: o config.yaml fica mais fácil de ler
        return numero

    def erro(self) -> str | None:
        """Mensagem em português se o valor digitado não serve."""
        if self._entrada is None or self.campo.tipo == "texto":
            return None
        texto = self._entrada.text().strip().replace(",", ".")
        if not texto:
            return None if self.opcional else f"{self.campo.rotulo}: preencha um valor."
        try:
            mostrado = float(texto)
        except ValueError:
            return f"{self.campo.rotulo}: “{texto}” não é um número."
        if not self.campo.minimo <= mostrado <= self.campo.maximo:
            sufixo = f" {self.campo.sufixo}" if self.campo.sufixo else ""
            return (f"{self.campo.rotulo}: use um valor entre {_formatar(self.campo.minimo)} e "
                    f"{_formatar(self.campo.maximo)}{sufixo}.")
        return None

    @property
    def alterado(self) -> bool:
        return self.valor() != self._original

    def confirmar(self) -> None:
        """Depois de salvar: o valor atual vira o "original"."""
        self._original = self.valor()
        self._ao_mudar()

    def descartar(self) -> None:
        self.definir(self._original)

    def recarregar(self, valor: Any) -> None:
        """O valor mudou por fora (ex.: layout escolhido na barra superior): só se não foi editado."""
        if not self.alterado:
            self.definir(valor)
            self._original = self.valor()

    def _ao_mudar(self, *_args: object) -> None:
        if self._entrada is not None:
            self._entrada.setProperty("erro", "true" if self.erro() else "false")
            repolir(self._entrada)
        self.mudou.emit()


class TabelaClasses(QWidget):
    """Uma linha por classe: alerta ligado?, segundos parado para alertar e o nível do alerta."""

    mudou = Signal()

    def __init__(self, tempos: dict[str, Any], niveis: dict[str, Any], padroes_tempo: dict[str, Any] | None = None,
                 padroes_nivel: dict[str, Any] | None = None, opcional: bool = False,
                 parent: QWidget | None = None, alertar: dict[str, Any] | None = None,
                 padroes_alertar: dict[str, Any] | None = None) -> None:
        super().__init__(parent)
        self.editores: list[EditorAjuste] = []
        grade = QGridLayout(self)
        grade.setContentsMargins(0, 0, 0, 0)
        grade.setHorizontalSpacing(14)
        grade.setVerticalSpacing(6)
        grade.addWidget(rotulo("CLASSE", "secao"), 0, 0)
        grade.addWidget(rotulo("ALERTA", "secao"), 0, 1)
        grade.addWidget(rotulo("PARADO POR", "secao"), 0, 2)
        grade.addWidget(rotulo("NÍVEL DO ALERTA", "secao"), 0, 3)
        niveis_opcoes = (("ALTO", "Alto (buzzer: 3 bipes)"), ("MEDIO", "Médio (1 bipe longo)"), ("BAIXO", "Baixo (sem som)"))
        for linha, classe in enumerate(CLASSES, start=1):
            cor = CORES_CLASSES.get(classe, "#94A3B8")
            nome = QLabel(f"<span style='color:{cor}; font-size:14px'>●</span>&nbsp;{classe}")
            tempo = EditorAjuste(
                Campo(f"parado.tempo_parado.{classe}", f"Parado por ({classe})", "decimal", 1, 3600, sufixo="s"),
                tempos.get(classe), (padroes_tempo or {}).get(classe), opcional,
            )
            nivel = EditorAjuste(
                Campo(f"parado.nivel.{classe}", f"Nível ({classe})", "escolha", opcoes=niveis_opcoes),
                niveis.get(classe), (padroes_nivel or {}).get(classe), opcional,
            )
            # Falta no config = alerta ligado (como era antes de existir esta coluna).
            padrao_alerta = (padroes_alertar or {}).get(classe, True) if opcional else None
            valor_alerta = (alertar or {}).get(classe, None if opcional else True)
            alerta = EditorAjuste(
                Campo(f"parado.alertar.{classe}", f"Alerta ({classe})", "booleano",
                      dica="Desligado: a classe aparece na tela, mas nunca gera alerta nem PASSOU."),
                valor_alerta, padrao_alerta, opcional,
            )
            for editor in (alerta, tempo, nivel):
                editor.mudou.connect(self.mudou)
                self.editores.append(editor)
            grade.addWidget(nome, linha, 0)
            grade.addWidget(alerta, linha, 1)
            grade.addWidget(tempo, linha, 2)
            grade.addWidget(nivel, linha, 3)
        grade.setColumnStretch(4, 1)

    def tempos_e_niveis(self) -> tuple[dict[str, Any], dict[str, Any]]:
        """{classe: valor} — no modo por câmera, só as classes com valor próprio."""
        tempos, niveis, _alertar = self.valores()
        return tempos, niveis

    def valores(self) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        """(tempos, níveis, alertar) — no modo por câmera, só as classes com valor próprio."""
        grupos: dict[str, dict[str, Any]] = {"tempo_parado": {}, "nivel": {}, "alertar": {}}
        for editor in self.editores:
            _secao, grupo, classe = editor.campo.chave.split(".")
            valor = editor.valor()
            if valor is not None:
                grupos[grupo][classe] = valor
        return grupos["tempo_parado"], grupos["nivel"], grupos["alertar"]
