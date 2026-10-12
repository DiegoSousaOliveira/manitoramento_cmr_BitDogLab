"""Ajustes → Placa BitDogLab: onde o usuário cadastra o IP e as portas da placa.

Como a placa e o PC se encontram (decisão de 11/10/2026): a placa mostra o próprio IP no
display; a pessoa digita esse IP aqui e clica em "Salvar e conectar". O PC passa a mandar
PING para ``<IP>:<porta da placa>`` e a placa responde para o endereço de onde o PING veio.
Sem IP, o PC espera a placa se apresentar com OLA (precisa do IP do PC no firmware).

O status embaixo é atualizado a cada segundo, sem reiniciar nada. O jeito do buzzer (uma vez ou
contínuo) vale na hora: o app manda ``SOM`` para a placa. Embaixo fica o manual da placa.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import QComboBox, QFormLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton, QVBoxLayout, QWidget

from core.config import Config, ErroConfig
from core.validacao import validar_ip_placa, validar_portas_placa
from link.bitdog import EstadoPlaca, LinkBitDog, ip_desta_maquina
from ui.componentes import CampoNumero, repolir, rotulo

log = logging.getLogger(__name__)

BUZZER_OPCOES = (
    ("uma_vez", "Toca uma vez, quando o alerta aparece"),
    ("continuo", "Continua tocando até o alerta acabar (o objeto sai da zona ou some)"),
)

MANUAL = """
<p><b>Ligar.</b> Ligue a placa <b>sem mexer no joystick</b> (ela mede a posição de repouso nessa hora). Ela entra no
Wi-Fi (só <b>2,4 GHz</b>) e mostra <b>AGUARDANDO PC</b> com o <b>IP</b> dela: digite esse IP acima e clique em
“Salvar e conectar”. Em até 2 s aparece <b>MONITORANDO</b> e o LED da Pico W acende.</p>

<p><b>Display.</b> MONITORANDO: hora, câmera do joystick, último objeto que passou e o IP.
ALERTA: a câmera, o objeto em letras grandes e <b>PARADO 12s</b>. “SEM SOM” = mudo, modo NOITE ou alerta calado.</p>

<p><b>Matriz de LEDs.</b> O desenho do objeto. <span style="color:#EF4444">Vermelho piscando = ALTO</span>,
<span style="color:#F5B300">amarelo = MÉDIO</span>, <span style="color:#22C55E">verde = BAIXO</span>,
<span style="color:#3B82F6">azul = DESCONHECIDO</span>.</p>

<p><b>Buzzer.</b> ALTO = 3 bipes rápidos; MÉDIO = 1 bipe longo; BAIXO = sem som. Com “continua tocando”, o padrão
se repete até o objeto sair da zona ou sumir da imagem.</p>

<p><b>Botão A.</b> <b>1 toque</b>: liga o <b>mudo</b> (nenhum som); outro toque volta ao normal.
<b>2 toques rápidos</b>: cala só o <b>alerta atual</b> (ele continua no display até o objeto sair; um alerta novo
toca de novo).<br>
<b>Botão B.</b> Modo NORMAL / NOITE (NOITE = sem som; o app mostra o modo na barra lateral).<br>
<b>Joystick.</b> Move a câmera com motor. <b>Apertar a alavanca</b> = próxima câmera com motor.</p>

<p><b>Problemas?</b> Display em “WI-FI FALHOU”: confira o nome e a senha em <code>bitDogLab/wifi_config.h</code>
(rede de 2,4 GHz, fora da “rede de convidados”). Em AGUARDANDO PC com o IP certo: o firewall do Windows precisa
permitir o Python em “Redes privadas”.</p>
"""


def texto_status(estado: EstadoPlaca | None) -> tuple[str, str]:
    """(texto, papel do rótulo) para a linha de status da placa."""
    if estado is None:
        return "A comunicação com a placa está desligada neste app.", "erro"
    if estado.erro:
        return f"⚠ {estado.erro}. Escolha outra porta do PC e salve.", "erro"
    if estado.conectada:
        partes = [f"✓ Conectada · {estado.ip}:{estado.porta_placa}"]
        if estado.modo:
            partes.append(f"modo {estado.modo}")
        if estado.resposta_ms is not None:
            partes.append(f"resposta {estado.resposta_ms:.0f} ms")
        return " · ".join(partes), "ok"
    if estado.configurada:
        tempo = f" (sem resposta há {estado.sem_resposta_s:.0f} s)" if estado.sem_resposta_s else ""
        return (
            f"Procurando a placa em {estado.ip}:{estado.porta_placa}…{tempo} Confira o IP no display "
            "da placa, se ela está ligada e no mesmo Wi-Fi, e se o firewall do Windows permite o app.",
            "suave",
        )
    return "Digite o IP que aparece no display da placa e clique em “Salvar e conectar”.", "suave"


class SecaoPlaca(QWidget):
    salva = Signal()

    def __init__(self, config: Config, link: LinkBitDog | None, notificar: Callable[..., object],
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._config = config
        self._link = link
        self._notificar = notificar

        self.ip = QLineEdit(str(config.obter("placa.ip") or ""))
        self.ip.setPlaceholderText("ex.: 192.168.1.60 — aparece no display da placa")
        self.ip.setMaximumWidth(300)
        self.ip.returnPressed.connect(self.salvar)
        self.porta_placa = CampoNumero(1, 65535)
        self.porta_placa.setValue(int(config.obter("placa.porta_placa")))
        self.porta_placa.setMaximumWidth(110)
        self.porta_pc = CampoNumero(1, 65535)
        self.porta_pc.setValue(int(config.obter("placa.porta_pc")))
        self.porta_pc.setMaximumWidth(110)
        self.porta_pc.setToolTip("Porta em que ESTE PC escuta a placa (padrão 5005)")

        formulario = QFormLayout()
        formulario.setHorizontalSpacing(14)
        formulario.setVerticalSpacing(8)
        formulario.addRow("IP da placa", self.ip)
        formulario.addRow("Porta da placa", self.porta_placa)
        formulario.addRow("Porta deste PC", self.porta_pc)

        self.buzzer = QComboBox()
        for valor, texto in BUZZER_OPCOES:
            self.buzzer.addItem(texto, valor)
        atual = str(config.obter_ou("placa.buzzer", "uma_vez"))
        self.buzzer.setCurrentIndex(max(0, self.buzzer.findData(atual)))
        self.buzzer.currentIndexChanged.connect(self._buzzer_mudou)
        formulario.addRow("Buzzer da placa", self.buzzer)

        self.botao_salvar = QPushButton("Salvar e conectar")
        self.botao_salvar.setProperty("variante", "primario")
        self.botao_salvar.clicked.connect(self.salvar)
        botoes = QHBoxLayout()
        botoes.addWidget(self.botao_salvar)
        botoes.addStretch(1)

        self.erro = rotulo("", "erro", quebra=True)
        self.erro.hide()
        self.status = rotulo("", "suave", quebra=True)
        self.dica = rotulo(
            f"Este PC na rede: {ip_desta_maquina()}. A placa (ou o simulador) mostra o IP dela no display; "
            "ela precisa estar no mesmo Wi-Fi que este PC — ou num lugar que o PC consiga alcançar.",
            "dica",
            quebra=True,
        )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)
        layout.addLayout(formulario)
        layout.addLayout(botoes)
        layout.addWidget(self.erro)
        layout.addWidget(self.status)
        layout.addWidget(self.dica)
        layout.addSpacing(8)
        layout.addWidget(rotulo("MANUAL DA PLACA", "secao"))
        self.manual = QLabel(MANUAL)
        self.manual.setTextFormat(Qt.TextFormat.RichText)
        self.manual.setWordWrap(True)
        self.manual.setProperty("papel", "suave")
        layout.addWidget(self.manual)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self.atualizar_status)
        self._timer.start(1000)
        self.atualizar_status()

    def atualizar_status(self) -> None:
        texto, papel = texto_status(self._link.estado() if self._link is not None else None)
        if (texto, papel) != (self.status.text(), self.status.property("papel")):
            self.status.setText(texto)
            self.status.setProperty("papel", papel)
            repolir(self.status)

    def _buzzer_mudou(self) -> None:
        """Vale na hora: grava no config.yaml e o app manda ``SOM`` para a placa."""
        valor = str(self.buzzer.currentData())
        try:
            self._config.definir("placa.buzzer", valor)
            self._config.salvar()
        except (OSError, ErroConfig) as erro:
            self.erro.setText(f"Não foi possível salvar no config.yaml: {erro}")
            self.erro.show()
            return
        if self._link is not None:
            self._link.definir_som(valor == "continuo")
        log.info("Buzzer da placa: %s", valor)
        self._notificar("Buzzer da placa", self.buzzer.currentText(), "info")

    def salvar(self) -> bool:
        """Valida, grava no config.yaml e reconecta na hora. Devolve True se salvou."""
        ip = self.ip.text().strip()
        problema = validar_ip_placa(ip) or validar_portas_placa(self.porta_placa.text(), self.porta_pc.text())
        if problema:
            self.erro.setText(problema)
            self.erro.show()
            return False
        self.erro.hide()
        porta_placa, porta_pc = self.porta_placa.value(), self.porta_pc.value()
        try:
            self._config.definir("placa.ip", ip)
            self._config.definir("placa.porta_placa", porta_placa)
            self._config.definir("placa.porta_pc", porta_pc)
            self._config.salvar()
        except (OSError, ErroConfig) as erro:
            self.erro.setText(f"Não foi possível salvar no config.yaml: {erro}")
            self.erro.show()
            return False
        if self._link is not None:
            self._link.reconfigurar(ip or None, porta_placa, porta_pc)
        log.info("Placa BitDogLab cadastrada: %s:%d (PC escuta na %d)", ip or "(descobrir pelo OLA)", porta_placa, porta_pc)
        self._notificar(
            "Placa salva",
            f"Conectando a {ip}:{porta_placa}…" if ip else "Sem IP: o app espera a placa se apresentar (OLA).",
            "info",
        )
        self.atualizar_status()
        self.salva.emit()
        return True
