"""Página "Treino": o aprendizado contínuo da IA com as imagens das suas câmeras.

Mostra o que está acontecendo (imagens esperando o professor, prontas, para revisar), o
modelo em uso, o liga/desliga do "aprender sozinho", o botão "Treinar agora", o progresso
da rodada e o HISTÓRICO — cada rodada com a nota antes e depois e por que o modelo novo foi
aceito ou descartado. "Usar este" volta (ou avança) para qualquer modelo aprovado.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime
from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QShowEvent
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from core.config import Config, ErroConfig
from treino.dados import REVISAR, ROTULADAS
from treino.historico import Rodada
from treino.servico import ServicoTreino
from ui.componentes import Interruptor, rotulo
from ui.revisor import DialogoRevisao
from ui.tarefas import executar

log = logging.getLogger(__name__)
MODELO_ORIGINAL = "yolo11n.pt"


def _data(texto: str) -> str:
    try:
        return datetime.fromisoformat(texto).strftime("%d/%m/%Y %H:%M")
    except ValueError:
        return texto


def resumo_rodada(rodada: Rodada) -> str:
    """Uma linha em português: "nota 0.412 → 0.455 · 230 imagens · aprovado"."""
    partes = []
    atual, novo = rodada.metricas_atual, rodada.metricas_novo
    if atual is not None and novo is not None:
        partes.append(f"nota {atual.map:.3f} → {novo.map:.3f}")
    if rodada.imagens_treino:
        partes.append(f"{rodada.imagens_treino} imagens de treino · {rodada.imagens_validacao} de validação")
    if rodada.duracao_s:
        partes.append(f"{rodada.duracao_s / 60:.0f} min")
    return " · ".join(partes)


class _Cartao(QFrame):
    def __init__(self, titulo: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("cartao")
        self.valor = QLabel("–")
        self.valor.setStyleSheet("font-size: 26px; font-weight: 600;")
        self.detalhe = rotulo("", "dica", quebra=True)
        self.layout_ = QVBoxLayout(self)
        self.layout_.setContentsMargins(16, 14, 16, 14)
        self.layout_.setSpacing(4)
        self.layout_.addWidget(rotulo(titulo.upper(), "secao"))
        self.layout_.addWidget(self.valor)
        self.layout_.addWidget(self.detalhe)
        self.layout_.addStretch(1)


class PaginaTreino(QWidget):
    usar_modelo = Signal(str)  # arquivo em pc/modelos: a janela troca e a IA recarrega (sem reiniciar)

    def __init__(self, config: Config, treino: ServicoTreino, notificar: Callable[..., object],
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._config = config
        self._treino = treino
        self._notificar = notificar
        self._modelos_na_pasta: set[str] = set()

        explicacao = rotulo(
            "A IA aprende com as imagens das SUAS câmeras. As imagens difíceis são guardadas sozinhas; "
            "um modelo PROFESSOR, bem maior e mais preciso, marca os objetos nelas (as que ele não tem "
            "certeza vêm para a sua revisão). De madrugada a IA treina com elas e faz uma prova em imagens "
            "separadas, que ela nunca viu: o modelo novo só passa a ser usado se tirar nota MAIOR que o "
            "atual. Dá para voltar a qualquer modelo anterior com um clique.",
            "suave", quebra=True,
        )

        self.cartao_pendentes = _Cartao("Esperando o professor")
        self.cartao_prontas = _Cartao("Prontas para o treino")
        self.cartao_revisar = _Cartao("Para você revisar")
        self.botao_revisar = QPushButton("Revisar")
        self.botao_revisar.clicked.connect(self.revisar)
        self.cartao_revisar.layout_.addWidget(self.botao_revisar, alignment=Qt.AlignmentFlag.AlignLeft)
        self.cartao_modelo = _Cartao("Modelo em uso")
        cartoes = QGridLayout()
        cartoes.setSpacing(12)
        for coluna, cartao in enumerate((self.cartao_pendentes, self.cartao_prontas, self.cartao_revisar,
                                         self.cartao_modelo)):
            cartoes.addWidget(cartao, 0, coluna)

        self.automatico = Interruptor()
        self.automatico.setChecked(bool(config.obter("treino.automatico")))
        self.automatico.toggled.connect(self._definir_automatico)
        self.texto_automatico = rotulo("", "")
        self.botao_treinar = QPushButton("Treinar agora")
        self.botao_treinar.setProperty("variante", "primario")
        self.botao_treinar.setToolTip("Roda uma rodada já, mesmo com poucas imagens novas")
        self.botao_treinar.clicked.connect(self.treinar_agora)
        self.botao_cancelar = QPushButton("Cancelar")
        self.botao_cancelar.clicked.connect(self._treino.cancelar)
        controles = QHBoxLayout()
        controles.setSpacing(10)
        controles.addWidget(self.automatico)
        controles.addWidget(self.texto_automatico)
        controles.addStretch(1)
        controles.addWidget(self.botao_cancelar)
        controles.addWidget(self.botao_treinar)

        self.etapa = rotulo("", "suave")
        self.barra = QProgressBar()
        self.barra.setTextVisible(False)
        self.barra.setFixedHeight(8)
        self.ultima = rotulo("", "suave", quebra=True)

        self._lista = QVBoxLayout()
        self._lista.setSpacing(8)

        conteudo = QWidget()
        layout = QVBoxLayout(conteudo)
        layout.setContentsMargins(32, 24, 32, 28)
        layout.setSpacing(12)
        layout.addWidget(rotulo("Aprendizado contínuo", "titulo"))
        layout.addWidget(explicacao)
        layout.addSpacing(4)
        layout.addLayout(cartoes)
        layout.addSpacing(6)
        layout.addLayout(controles)
        layout.addWidget(self.etapa)
        layout.addWidget(self.barra)
        layout.addWidget(self.ultima)
        layout.addSpacing(8)
        layout.addWidget(rotulo("HISTÓRICO DE MODELOS", "secao"))
        layout.addLayout(self._lista)
        layout.addStretch(1)
        rolagem = QScrollArea()
        rolagem.setWidget(conteudo)
        rolagem.setWidgetResizable(True)
        rolagem.setFrameShape(QFrame.Shape.NoFrame)
        externo = QVBoxLayout(self)
        externo.setContentsMargins(0, 0, 0, 0)
        externo.addWidget(rolagem)

        self.mostrar_progresso({})
        self.atualizar()

    # ================================================================ dados
    def atualizar(self) -> None:
        """Contagens, modelo em uso e histórico (contar lê os arquivos: roda em segundo plano)."""
        self.texto_automatico.setText(f"Aprender sozinho todo dia às {self._config.obter('treino.horario')}")

        def contar() -> dict[str, Any]:
            conjunto = self._treino.conjunto()
            contagem = conjunto.contar()
            treino = validacao = 0
            for amostra in conjunto.amostras(ROTULADAS):
                if amostra.meta().get("lado") == "validacao":
                    validacao += 1
                else:
                    treino += 1
            pasta = self._config.resolver_caminho(str(self._config.obter("ia.pasta_modelos")))
            modelos = {p.name for p in pasta.glob("*.pt")} if pasta.exists() else set()
            return {"contagem": contagem, "treino": treino, "validacao": validacao, "modelos": modelos}

        executar(contar, self._mostrar_contagem, lambda erro: log.warning("Contagem do treino: %s", erro), dono=self)
        self._mostrar_historico()

    def _mostrar_contagem(self, dados: object) -> None:
        assert isinstance(dados, dict)
        contagem = dados["contagem"]
        self._modelos_na_pasta = set(dados["modelos"])
        self.cartao_pendentes.valor.setText(str(contagem["pendentes"]))
        self.cartao_pendentes.detalhe.setText("guardadas pelas câmeras desde a última rodada")
        self.cartao_prontas.valor.setText(str(contagem[ROTULADAS]))
        self.cartao_prontas.detalhe.setText(f"{dados['treino']} para treinar · {dados['validacao']} para a prova")
        self.cartao_revisar.valor.setText(str(contagem[REVISAR]))
        self.cartao_revisar.detalhe.setText("o professor ficou em dúvida (opcional)")
        self.botao_revisar.setEnabled(contagem[REVISAR] > 0)
        self._mostrar_historico()

    def _mostrar_historico(self) -> None:
        em_uso = str(self._config.obter("ia.modelo"))
        rodadas = self._treino.historico()
        de_uso = next((r for r in reversed(rodadas) if r.modelo_novo == em_uso), None)
        self.cartao_modelo.valor.setText(em_uso.removesuffix(".pt"))
        if de_uso is not None and de_uso.metricas_novo is not None:
            self.cartao_modelo.detalhe.setText(f"treinado em {_data(de_uso.inicio)} · nota {de_uso.metricas_novo.map:.3f}")
        elif em_uso == MODELO_ORIGINAL:
            self.cartao_modelo.detalhe.setText("o modelo original (ainda sem treino com as suas câmeras)")
        else:
            self.cartao_modelo.detalhe.setText("")

        while self._lista.count():
            item = self._lista.takeAt(0)
            if item.widget():
                item.widget().hide()
                item.widget().deleteLater()
        for rodada in reversed(rodadas):
            self._lista.addWidget(self._linha_rodada(rodada, em_uso))
        self._lista.addWidget(self._linha_modelo(MODELO_ORIGINAL, "Modelo original (YOLO11n, sem treino)", em_uso))
        ultima = rodadas[-1] if rodadas else None
        self.ultima.setText(f"Última rodada: {_data(ultima.inicio)} — {ultima.motivo}" if ultima else
                            "Nenhuma rodada de treino ainda.")

    def _linha_rodada(self, rodada: Rodada, em_uso: str) -> QWidget:
        if rodada.promovido and rodada.modelo_novo:
            titulo = f"✓ {_data(rodada.inicio)} — aprovado: {rodada.modelo_novo.removesuffix('.pt')}"
            papel = "ok"
        elif rodada.erro:
            titulo, papel = f"✕ {_data(rodada.inicio)} — {rodada.motivo}", "erro"
        else:
            titulo, papel = f"– {_data(rodada.inicio)} — descartado: {rodada.motivo}", "suave"
        return self._linha_modelo(rodada.modelo_novo if rodada.promovido else "", titulo, em_uso, papel,
                                  resumo_rodada(rodada))

    def _linha_modelo(self, arquivo: str, titulo: str, em_uso: str, papel: str = "", detalhe: str = "") -> QWidget:
        linha = QFrame()
        linha.setObjectName("cartao")
        externo = QHBoxLayout(linha)
        externo.setContentsMargins(14, 10, 14, 10)
        textos = QWidget()
        coluna = QVBoxLayout(textos)
        coluna.setContentsMargins(0, 0, 0, 0)
        coluna.setSpacing(2)
        coluna.addWidget(rotulo(titulo, papel or None, quebra=True))
        if detalhe:
            coluna.addWidget(rotulo(detalhe, "dica"))
        externo.addWidget(textos, 1)
        if arquivo:
            if arquivo == em_uso:
                externo.addWidget(rotulo("EM USO", "ok"))
            elif arquivo in self._modelos_na_pasta or arquivo == MODELO_ORIGINAL:
                botao = QPushButton("Usar este")
                botao.clicked.connect(lambda _c=False, a=arquivo: self.usar_modelo.emit(a))
                externo.addWidget(botao)
            else:
                externo.addWidget(rotulo("apagado (limite de modelos guardados)", "dica"))
        return linha

    # ================================================================ treino
    def treinar_agora(self) -> None:
        if (motivo := self._treino.falta_memoria()) is not None:
            self._notificar("Não dá para treinar agora", motivo[0].upper() + motivo[1:] + ".", "aviso")
            return
        if self._treino.treinar_agora():
            self._notificar("Treino iniciado", "A IA continua funcionando enquanto treina.", "info")
        else:
            self._notificar("Já há um treino rodando", "Espere terminar ou cancele.", "aviso")
        self.mostrar_progresso({"etapa": "iniciando", "texto": "Preparando o treino…"})

    def receber_aviso(self, aviso: dict[str, Any]) -> None:
        """Aviso do processo de treino (já na thread da interface)."""
        if aviso.get("fim"):
            self.mostrar_progresso({})
            self.atualizar()
        else:
            self.mostrar_progresso(aviso)

    def mostrar_progresso(self, aviso: dict[str, Any]) -> None:
        rodando = bool(aviso) or self._treino.estado().rodando
        self.botao_treinar.setEnabled(not rodando)
        self.botao_cancelar.setVisible(rodando)
        self.barra.setVisible(rodando)
        self.etapa.setVisible(rodando)
        if not rodando:
            return
        texto = str(aviso.get("texto") or self._treino.estado().texto)
        self.etapa.setText(texto)
        total, feito = int(aviso.get("total", 0) or 0), int(aviso.get("feito", 0) or 0)
        if total > 0:
            self.barra.setRange(0, total)
            self.barra.setValue(feito)
        else:
            self.barra.setRange(0, 0)  # "ocupado" (animação), sem porcentagem

    def revisar(self) -> None:
        dialogo = DialogoRevisao(self._treino.conjunto(), self)
        dialogo.exec()
        if dialogo.aceitas or dialogo.descartadas:
            self._notificar("Revisão salva", f"{dialogo.aceitas} aceita(s), {dialogo.descartadas} descartada(s).",
                            "sucesso")
        self.atualizar()

    def _definir_automatico(self, ligado: bool) -> None:
        try:
            self._config.definir("treino.automatico", ligado)
            self._config.salvar()
        except (OSError, ErroConfig) as erro:
            self._notificar("Não foi possível salvar", str(erro), "erro")

    def showEvent(self, evento: QShowEvent) -> None:  # noqa: N802
        super().showEvent(evento)
        self.automatico.blockSignals(True)
        self.automatico.setChecked(bool(self._config.obter("treino.automatico")))
        self.automatico.blockSignals(False)
        self.mostrar_progresso({})
        self.atualizar()
