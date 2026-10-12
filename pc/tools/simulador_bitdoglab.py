"""Simulador da placa BitDogLab (Tkinter, só biblioteca padrão do Python).

Faz EXATAMENTE o que a placa vai fazer (``docs/protocolo.md``): escuta UDP na 5006, fala
com o PC na 5005, mostra o "OLED", a "matriz 5x5" e o "buzzer", e tem joystick e botões.
É um arquivo só, sem importar nada do projeto: dá para copiar para outro computador da
rede e testar pelo Wi-Fi, como a placa de verdade. A parte "placa" (``PlacaSimulada``)
não depende do Tkinter — é a mesma lógica que o firmware em C deve seguir.

Como a placa e o PC se encontram: o display mostra "AGUARDANDO PC" com o IP e a porta
desta máquina. Digite esse IP no app (Ajustes → Placa BitDogLab) e clique em "Salvar e
conectar": o app manda PING para cá e o simulador responde para o endereço de onde o
PING veio (IP e porta). Ninguém precisa saber o IP do PC de antemão.

Uso (de dentro da pasta ``pc/``, ou em qualquer pasta, em outro computador)::

    python tools/simulador_bitdoglab.py
    python tools/simulador_bitdoglab.py --ip-pc 192.168.1.101   # opcional: também manda OLA ao PC

Teclas: setas ou W/A/S/D = joystick · Espaço (ou 1) = botão A: 1 toque liga/desliga o MUDO,
2 toques rápidos calam o alerta atual · B (ou 2) = modo NORMAL/NOITE · J (ou 3) = próxima câmera.
A tecla "A" fica com o joystick (esquerda), por isso o botão A usa o Espaço.
"""

from __future__ import annotations

import argparse
import math
import re
import socket
import time
from dataclasses import dataclass, field

# ===================================================================== protocolo (lado da placa)
TAMANHO_MAXIMO = 64
CLASSES = ("PESSOA", "BICICLETA", "CARRO", "MOTO", "ONIBUS", "CAMINHAO", "GATO", "CACHORRO", "DESCONHECIDO")
NIVEIS = ("BAIXO", "MEDIO", "ALTO")
ZONA_MORTA = 0.12  # E6: dentro dela o joystick vale 0.00
REPETIR_JOY_S = 1.0  # E5: joystick fora do centro é repetido a cada 1 s
MIN_INTERVALO_JOY_S = 0.05  # no máximo 20 JOY por segundo
REPETIR_OLA_S = 5.0  # E8: OLA a cada 5 s até o PC responder
EXPIRAR_ALERTA_S = 6.0  # E2: sem reenvio por 6 s, o alerta sai da tela
SEM_PC_S = 6.0  # sem PING por este tempo: volta a mostrar "AGUARDANDO PC" com o IP
INTERVALO_DUPLO_S = 0.4  # E14: dois toques no A dentro deste tempo = "clique duplo"
REPETIR_TOQUE_S = {"ALTO": 1.5, "MEDIO": 2.5}  # E13: buzzer contínuo repete o padrão neste ritmo

_CODIGO = re.compile(r"^(?=.*[A-Z0-9])[A-Z0-9_-]{1,10}$")
_INTEIRO = re.compile(r"^\d+$")
_CONF = re.compile(r"^[01]\.\d{2}$")
_HORA = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")

#: Ícones 5x5 da matriz de LEDs ("#" = aceso).
ICONES: dict[str, tuple[str, ...]] = {
    "PESSOA": ("..#..", ".###.", "..#..", ".#.#.", ".#.#."),
    "BICICLETA": (".....", "..#..", ".#.#.", "#.#.#", ".#.#."),
    "CARRO": (".....", ".###.", "#####", "#####", ".#.#."),
    "MOTO": (".....", "...##", ".###.", "#.#.#", ".#.#."),
    "ONIBUS": ("#####", "#.#.#", "#####", "#####", ".#.#."),
    "CAMINHAO": ("###..", "####.", "#####", "#####", ".#.#."),
    "GATO": ("#...#", "##.##", "#####", ".###.", "..#.."),
    "CACHORRO": ("#....", "###.#", ".####", ".#..#", ".#..#"),
    "DESCONHECIDO": (".###.", "#...#", "..##.", ".....", "..#.."),
}
CORES_NIVEL = {"ALTO": "#EF4444", "MEDIO": "#F5B300", "BAIXO": "#22C55E"}
COR_DESCONHECIDO = "#3B82F6"


class ErroMensagem(ValueError):
    """Mensagem do PC fora do formato (é ignorada e aparece no log)."""


def ip_desta_maquina() -> str:
    """IP desta máquina na rede local — o que a placa mostra no display.

    O ``connect`` de um socket UDP não envia nada: só escolhe a interface de rede.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sonda:
        try:
            sonda.connect(("10.255.255.255", 1))
            return str(sonda.getsockname()[0])
        except OSError:
            return "127.0.0.1"


def _ler(dados: bytes) -> list[str]:
    """Valida o datagrama (regra E11) e devolve os campos."""
    if len(dados) > TAMANHO_MAXIMO:
        raise ErroMensagem(f"{len(dados)} bytes (máximo {TAMANHO_MAXIMO})")
    try:
        texto = dados.decode("ascii")
    except UnicodeDecodeError:
        raise ErroMensagem("bytes fora do ASCII") from None
    texto = texto.replace("\r", "")
    if texto.endswith("\n"):
        texto = texto[:-1]
    if not texto or any(not 0x20 <= ord(c) <= 0x7E for c in texto):
        raise ErroMensagem("vazia ou com caracteres de controle")
    return texto.split(";")


def _inteiro(texto: str, minimo: int, maximo: int, nome: str) -> int:
    if not _INTEIRO.match(texto) or not minimo <= int(texto) <= maximo:
        raise ErroMensagem(f"{nome} inválido: {texto!r}")
    return int(texto)


def _codigo(texto: str, aceita_nenhuma: bool = False) -> str:
    if aceita_nenhuma and texto == "-":
        return texto
    if not _CODIGO.match(texto):
        raise ErroMensagem(f"código de câmera inválido: {texto!r}")
    return texto


@dataclass
class AlertaExibido:
    cam: str
    classe: str
    segundos: int
    confianca: float
    id: int
    nivel: str
    recebido: float  # time.monotonic() do último ALERTA (novo ou reenvio)


@dataclass
class Saida:
    """O que a placa faz ao receber uma mensagem."""

    enviar: list[str] = field(default_factory=list)
    buzzer: str | None = None  # "ALTO" (3 bipes) ou "MEDIO" (1 longo)


class PlacaSimulada:
    """A lógica da placa, sem tela e sem rede (testável e igual à do firmware)."""

    def __init__(self, ip_local: str = "", porta: int = 5006) -> None:
        self.ip_local = ip_local  # o display mostra para a pessoa cadastrar no app
        self.porta = porta
        self.conhece_pc = True  # False: ainda não sabe o endereço do PC (não manda OLA)
        self.modo = "NORMAL"
        self.hora = "--:--"
        self.cam = "-"
        self.passou = ""
        self.alerta: AlertaExibido | None = None
        self.buzzer_silenciado = False  # o alerta ATUAL foi calado (2 toques no A); um alerta novo toca
        self.mudo = False  # 1 toque no A: nenhum som até tocar no A de novo (só na placa)
        self.som_continuo = False  # E13: SOM;CONTINUO = toca até o alerta acabar
        self.pc_respondeu = False
        self._ultimo_ola = -math.inf
        self.joy_enviado = (0.0, 0.0)
        self._joy_pendente: tuple[float, float] | None = None
        self._ultimo_joy = -math.inf
        self.ultimo_ping: float | None = None

    # ------------------------------------------------------------ ligar e tempo
    def ligar(self, agora: float) -> list[str]:
        """Ao ligar (E8): OLA e, logo depois, o modo atual."""
        self._ultimo_ola = agora
        return ["OLA", f"MODO;{self.modo}"]

    def pc_encontrado(self) -> list[str]:
        """A placa passou a conhecer um PC (endereço novo): conta o modo atual a ele (E8)."""
        self.conhece_pc = True
        return [f"MODO;{self.modo}"]

    def tique(self, agora: float) -> list[str]:
        """Chamar várias vezes por segundo: OLA repetido, alerta expirado, joystick mantido."""
        saida: list[str] = []
        if self.conhece_pc and not self.pc_respondeu and agora - self._ultimo_ola >= REPETIR_OLA_S:
            saida += self.ligar(agora)
        if self.alerta is not None and agora - self.alerta.recebido > EXPIRAR_ALERTA_S:
            self.alerta = None  # E2: o PC parou de reenviar (silenciado ou LIMPAR perdido)
        if self._joy_pendente is not None and agora - self._ultimo_joy >= MIN_INTERVALO_JOY_S:
            saida.append(self._enviar_joy(self._joy_pendente, agora))
        elif self.joy_enviado != (0.0, 0.0) and agora - self._ultimo_joy >= REPETIR_JOY_S:
            saida.append(self._enviar_joy(self.joy_enviado, agora))  # E5
        return saida

    # ------------------------------------------------------------ mensagens do PC
    def receber(self, dados: bytes, agora: float) -> Saida:
        campos = _ler(dados)
        comando, args = campos[0], campos[1:]
        esperado = {"ALERTA": 6, "LIMPAR": 2, "PASSOU": 2, "CAM": 1, "PING": 1, "HORA": 1, "SOM": 1}.get(comando)
        if esperado is None:
            raise ErroMensagem(f"comando desconhecido: {comando!r}")
        if len(args) != esperado:
            raise ErroMensagem(f"{comando} precisa de {esperado} campo(s), veio {len(args)}")
        self.pc_respondeu = True
        saida = Saida()
        if comando == "ALERTA":
            cam, classe, seg, conf, id_, nivel = args
            if classe not in CLASSES or nivel not in NIVEIS or not _CONF.match(conf) or float(conf) > 1:
                raise ErroMensagem(f"ALERTA com classe/nível/confiança inválidos: {args}")
            novo = AlertaExibido(_codigo(cam), classe, _inteiro(seg, 0, 99999, "segundos"), float(conf),
                                 _inteiro(id_, 1, 999999, "id"), nivel, agora)
            mesmo = self.alerta is not None and (self.alerta.cam, self.alerta.id) == (novo.cam, novo.id)
            self.alerta = novo
            if not mesmo:  # E1: reenvio só atualiza a tela; alerta novo toca o buzzer
                self.buzzer_silenciado = False
                if self.modo == "NORMAL" and not self.mudo and nivel in ("ALTO", "MEDIO"):
                    saida.buzzer = nivel
        elif comando == "LIMPAR":
            cam, id_ = _codigo(args[0]), _inteiro(args[1], 1, 999999, "id")
            if self.alerta is not None and (self.alerta.cam, self.alerta.id) == (cam, id_):
                self.alerta = None  # E3: LIMPAR de outro objeto (ou repetido) é ignorado
        elif comando == "PASSOU":
            if args[1] not in CLASSES:
                raise ErroMensagem(f"classe inválida: {args[1]!r}")
            self.passou = f"{args[1]} {_codigo(args[0])}"
        elif comando == "CAM":
            self.cam = _codigo(args[0], aceita_nenhuma=True)
        elif comando == "SOM":
            if args[0] not in ("UMA", "CONTINUO"):
                raise ErroMensagem(f"SOM inválido: {args[0]!r}")
            self.som_continuo = args[0] == "CONTINUO"
        elif comando == "PING":
            seq = _inteiro(args[0], 0, 65535, "seq")
            self.ultimo_ping = agora
            saida.enviar.append(f"PONG;{seq}")
        else:  # HORA
            if not _HORA.match(args[0]):
                raise ErroMensagem(f"hora inválida: {args[0]!r}")
            self.hora = args[0]
        return saida

    # ------------------------------------------------------------ botões e joystick
    def apertar(self, botao: str) -> list[str]:
        """``"A"`` = 1 toque no A, ``"AA"`` = 2 toques rápidos no A (quem distingue é quem lê o botão)."""
        if botao == "A":  # E14: liga/desliga o MUDO — só na placa (o PC continua mandando tudo)
            self.mudo = not self.mudo
            return []
        if botao == "AA":  # E14: cala o alerta ATUAL (o próximo alerta toca) e avisa o PC
            self.buzzer_silenciado = True
            return ["BTN;A"]
        if botao == "B":
            self.modo = "NOITE" if self.modo == "NORMAL" else "NORMAL"
            return ["BTN;B", f"MODO;{self.modo}"]
        if botao == "J":
            return ["BTN;J"]
        raise ValueError(f"botão desconhecido: {botao}")

    def mover_joystick(self, x: float, y: float, agora: float) -> list[str]:
        """Posição do joystick (-1..1). Envia só quando muda (E6), até 20 por segundo."""
        valor = (self._eixo(x), self._eixo(y))
        if valor == self.joy_enviado:
            self._joy_pendente = None
            return []
        if agora - self._ultimo_joy < MIN_INTERVALO_JOY_S:
            self._joy_pendente = valor  # muito rápido: vai no próximo tique
            return []
        return [self._enviar_joy(valor, agora)]

    @staticmethod
    def _eixo(valor: float) -> float:
        valor = max(-1.0, min(1.0, valor))
        return 0.0 if abs(valor) < ZONA_MORTA else round(valor, 2)

    def _enviar_joy(self, valor: tuple[float, float], agora: float) -> str:
        self.joy_enviado, self._joy_pendente, self._ultimo_joy = valor, None, agora
        return f"JOY;{valor[0]:.2f};{valor[1]:.2f}"

    # ------------------------------------------------------------ o que aparece na placa
    @property
    def som_permitido(self) -> bool:
        """O buzzer pode tocar agora? (não está no mudo, nem no modo NOITE, nem com o alerta calado)"""
        return not self.mudo and not self.buzzer_silenciado and self.modo == "NORMAL"

    def toque_continuo(self) -> str | None:
        """E13: com SOM;CONTINUO, o padrão ("ALTO"/"MEDIO") que deve continuar tocando; ou None."""
        if not self.som_continuo or self.alerta is None or not self.som_permitido:
            return None
        return self.alerta.nivel if self.alerta.nivel in ("ALTO", "MEDIO") else None

    def pc_conectado(self, agora: float) -> bool:
        """O PC mandou PING nos últimos ``SEM_PC_S`` segundos."""
        return self.ultimo_ping is not None and agora - self.ultimo_ping <= SEM_PC_S

    def linhas_oled(self, agora: float) -> list[tuple[str, bool]]:
        """(texto, grande?) — espera, repouso ou alerta, como no OLED 128x64."""
        if self.alerta is None and not self.pc_conectado(agora):
            return [  # a pessoa lê o IP aqui e digita no app
                ("AGUARDANDO PC", True),
                (f"IP {self.ip_local or '?'}", False),
                (f"PORTA {self.porta}", False),
                ("Cadastre no app", False),
            ]
        if self.alerta is None:
            linhas = [
                ("MONITORANDO", True),
                (f"HORA {self.hora}", False),
                (f"CAM {self.cam}", False),
                (f"PASSOU {self.passou}" if self.passou else "PASSOU -", False),
                (f"IP {self.ip_local}", False),
            ]
            return linhas + ([("MUDO (toque no A)", False)] if self.mudo else [])
        segundos = self.alerta.segundos + int(agora - self.alerta.recebido)
        return [
            (f"ALERTA {self.alerta.cam}", False),
            (self.alerta.classe, True),
            (f"PARADO {segundos}s", True),
            (f"{self.alerta.nivel} #{self.alerta.id}" + ("" if self.som_permitido else "  SEM SOM"), False),
        ]

    def matriz(self, agora: float) -> list[list[str | None]]:
        """Cores dos 25 LEDs (None = apagado). ALTO pisca."""
        apagada: list[list[str | None]] = [[None] * 5 for _ in range(5)]
        if self.alerta is None:
            return apagada
        alerta = self.alerta
        cor = COR_DESCONHECIDO if alerta.classe == "DESCONHECIDO" else CORES_NIVEL[alerta.nivel]
        if alerta.nivel == "ALTO" and int(agora * 4) % 2:
            return apagada  # pisca 2x por segundo
        icone = ICONES.get(alerta.classe, ICONES["DESCONHECIDO"])
        return [[cor if c == "#" else None for c in linha] for linha in icone]


# ===================================================================== rede (sem tela)
@dataclass
class Ocorrencia:
    tipo: str  # "recebida", "enviada", "invalida", "buzzer", "aviso"
    texto: str


class ConexaoPlaca:
    """Socket UDP + ``PlacaSimulada``. A janela chama ``processar`` várias vezes por segundo.

    ``ip_pc`` é opcional: sem ele, a placa espera o PING do PC e aprende o endereço dele
    (IP **e porta** de origem — responder à porta de origem funciona até com roteador no meio).
    """

    def __init__(self, ip_pc: str | None = None, porta_pc: int = 5005, porta_placa: int = 5006,
                 placa: PlacaSimulada | None = None, endereco: str = "") -> None:
        self.destino: tuple[str, int] | None = (ip_pc, porta_pc) if ip_pc else None
        self.porta_pc = porta_pc
        self.placa = placa or PlacaSimulada()
        self.placa.porta = porta_placa
        self.placa.ip_local = self.placa.ip_local or (endereco if endereco not in ("", "0.0.0.0") else ip_desta_maquina())
        self.placa.conhece_pc = self.destino is not None
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        if hasattr(socket, "SIO_UDP_CONNRESET"):
            self.socket.ioctl(socket.SIO_UDP_CONNRESET, False)  # type: ignore[attr-defined]
        self.socket.bind((endereco, porta_placa))  # "" = todas as interfaces (Wi-Fi)
        self.socket.setblocking(False)
        self.ocorrencias: list[Ocorrencia] = []

    @property
    def ip_pc(self) -> str | None:
        return self.destino[0] if self.destino else None

    def iniciar(self) -> list[Ocorrencia]:
        if self.destino is not None:  # já sabe onde está o PC: se apresenta (E8)
            self.enviar(self.placa.ligar(time.monotonic()))
        return self._tirar()

    def processar(self, agora: float | None = None) -> list[Ocorrencia]:
        agora = time.monotonic() if agora is None else agora
        while True:
            try:
                dados, origem = self.socket.recvfrom(1024)
            except (BlockingIOError, ConnectionResetError):
                break
            except OSError:
                break
            texto = dados.decode("ascii", "replace").strip()
            try:
                saida = self.placa.receber(dados, agora)
            except ErroMensagem as erro:
                self.ocorrencias.append(Ocorrencia("invalida", f"{texto!r} de {origem[0]}: {erro}"))
                continue
            if self.destino != origem:  # E7: responde para o endereço (IP e porta) de onde o PC falou
                self.ocorrencias.append(Ocorrencia("aviso", f"PC encontrado em {origem[0]}:{origem[1]}"))
                self.destino = (origem[0], int(origem[1]))
                self.enviar(self.placa.pc_encontrado())
            self.ocorrencias.append(Ocorrencia("recebida", texto))
            if saida.buzzer:
                self.ocorrencias.append(Ocorrencia("buzzer", saida.buzzer))
            self.enviar(saida.enviar)
        self.enviar(self.placa.tique(agora))
        return self._tirar()

    def enviar(self, mensagens: list[str]) -> None:
        for mensagem in mensagens:
            if self.destino is None:
                self.ocorrencias.append(Ocorrencia("aviso", f"{mensagem} não enviada: o PC ainda não se conectou"))
                continue
            try:
                self.socket.sendto((mensagem + "\n").encode("ascii"), self.destino)
                self.ocorrencias.append(Ocorrencia("enviada", mensagem))
            except OSError as erro:
                self.ocorrencias.append(Ocorrencia("invalida", f"falha ao enviar {mensagem}: {erro}"))

    def fechar(self) -> None:
        self.socket.close()

    def _tirar(self) -> list[Ocorrencia]:
        saida, self.ocorrencias = self.ocorrencias, []
        return saida


# ===================================================================== janela (Tkinter)
FUNDO = "#14171C"
SUPERFICIE = "#1E232B"
TEXTO = "#E6E9EF"
SUAVE = "#8B93A1"
DESTAQUE = "#22C3E6"
ESCALA_OLED = 4  # 128x64 ampliado 4x


class SimuladorApp:
    """A janela: OLED, matriz, buzzer, joystick, botões e log."""

    def __init__(self, raiz, conexao: ConexaoPlaca) -> None:  # noqa: ANN001 (tk.Tk)
        import tkinter as tk

        self.tk = tk
        self.raiz = raiz
        self.conexao = conexao
        self.placa = conexao.placa
        self._teclas: set[str] = set()
        self._bipes: list[str] = []  # "after" pendentes do buzzer (cancelados ao silenciar)
        self._buzzer_ate = 0.0
        self._proximo_continuo = 0.0  # E13: quando repetir o padrão do buzzer contínuo
        self._clique_a: str | None = None  # 1º toque no A esperando para ver se vem o 2º
        raiz.title("Simulador BitDogLab")
        raiz.configure(bg=FUNDO)
        raiz.resizable(False, False)

        topo = tk.Frame(raiz, bg=FUNDO)
        topo.pack(padx=16, pady=(14, 6), fill="x")
        tk.Label(topo, text="BitDogLab (simulada)", bg=FUNDO, fg=TEXTO, font=("Segoe UI", 14, "bold")).pack(side="left")
        self.status = tk.Label(topo, text="", bg=FUNDO, fg=SUAVE, font=("Segoe UI", 9))
        self.status.pack(side="right")

        corpo = tk.Frame(raiz, bg=FUNDO)
        corpo.pack(padx=16, pady=6)
        esquerda = tk.Frame(corpo, bg=FUNDO)
        esquerda.grid(row=0, column=0, sticky="n")
        direita = tk.Frame(corpo, bg=FUNDO)
        direita.grid(row=0, column=1, sticky="n", padx=(16, 0))

        self._titulo(esquerda, "OLED 128x64")
        self.oled = tk.Canvas(esquerda, width=128 * ESCALA_OLED, height=64 * ESCALA_OLED, bg="black",
                              highlightthickness=2, highlightbackground="#2A303A")
        self.oled.pack()

        linha = tk.Frame(esquerda, bg=FUNDO)
        linha.pack(fill="x", pady=(12, 0))
        bloco_matriz = tk.Frame(linha, bg=FUNDO)
        bloco_matriz.pack(side="left")
        self._titulo(bloco_matriz, "Matriz 5x5")
        self.matriz = tk.Canvas(bloco_matriz, width=5 * 30 + 6, height=5 * 30 + 6, bg="#0B0D10", highlightthickness=0)
        self.matriz.pack()
        self._leds = [
            [self.matriz.create_rectangle(4 + c * 30, 4 + r * 30, 4 + c * 30 + 26, 4 + r * 30 + 26,
                                          fill="#1A1E25", outline="") for c in range(5)]
            for r in range(5)
        ]
        bloco_buzzer = tk.Frame(linha, bg=FUNDO)
        bloco_buzzer.pack(side="left", padx=(18, 0), anchor="n")
        self._titulo(bloco_buzzer, "Buzzer e modo")
        self.buzzer = tk.Label(bloco_buzzer, text="BUZZER OFF", width=16, bg=SUPERFICIE, fg=SUAVE,
                               font=("Segoe UI", 11, "bold"), pady=8)
        self.buzzer.pack(anchor="w")
        self.modo = tk.Label(bloco_buzzer, text="", bg=FUNDO, fg=TEXTO, font=("Segoe UI", 10))
        self.modo.pack(anchor="w", pady=(8, 0))
        botoes = tk.Frame(bloco_buzzer, bg=FUNDO)
        botoes.pack(anchor="w", pady=(12, 0))
        acoes = (("A · 1x mudo\n2x calar (Espaço)", self.tocar_a), ("B · modo\n(tecla B)", lambda: self.apertar("B")),
                 ("J · câmera\n(tecla J)", lambda: self.apertar("J")))
        for texto, acao in acoes:
            tk.Button(botoes, text=texto, width=13, bg=SUPERFICIE, fg=TEXTO, activebackground="#2A303A",
                      activeforeground=TEXTO, relief="flat", command=acao).pack(side="left", padx=(0, 6))

        self._titulo(direita, "Joystick (arraste ou setas/WASD)")
        self.pad = tk.Canvas(direita, width=200, height=200, bg=SUPERFICIE, highlightthickness=0)
        self.pad.pack()
        self.pad.create_oval(10, 10, 190, 190, outline="#3A4250", width=2)
        zona = ZONA_MORTA * 90
        self.pad.create_oval(100 - zona, 100 - zona, 100 + zona, 100 + zona, outline="#2F3540", dash=(3, 3))
        self.botao_joy = self.pad.create_oval(82, 82, 118, 118, fill=DESTAQUE, outline="")
        self.texto_joy = tk.Label(direita, text="JOY 0.00 ; 0.00", bg=FUNDO, fg=SUAVE, font=("Consolas", 10))
        self.texto_joy.pack(pady=(6, 0))
        self.pad.bind("<B1-Motion>", self._arrastar)
        self.pad.bind("<Button-1>", self._arrastar)
        self.pad.bind("<ButtonRelease-1>", lambda _e: self._mover(0.0, 0.0))

        self._titulo(direita, "Mensagens")
        self.log = tk.Text(direita, width=60, height=14, bg="#0B0D10", fg=TEXTO, font=("Consolas", 9),
                           relief="flat", state="disabled")
        self.log.pack()
        for tipo, cor in (("recebida", "#7DD3FC"), ("enviada", "#86EFAC"), ("invalida", "#FCA5A5"),
                          ("buzzer", "#FCD34D"), ("aviso", "#C4B5FD")):
            self.log.tag_configure(tipo, foreground=cor)

        raiz.bind("<KeyPress>", self._tecla)
        raiz.bind("<KeyRelease>", self._soltar_tecla)
        raiz.protocol("WM_DELETE_WINDOW", self.fechar)
        self._registrar(self.conexao.iniciar())
        self._laco()

    def _titulo(self, pai, texto: str) -> None:  # noqa: ANN001
        self.tk.Label(pai, text=texto.upper(), bg=FUNDO, fg=SUAVE, font=("Segoe UI", 8, "bold")).pack(
            anchor="w", pady=(6, 4))

    # ------------------------------------------------------------ laço
    def _laco(self) -> None:
        self._registrar(self.conexao.processar())
        toque = self.placa.toque_continuo()
        if toque and time.monotonic() >= self._proximo_continuo:
            self._tocar(toque)  # E13: buzzer contínuo — repete até o alerta acabar (ou ser calado)
        self.desenhar()
        self.raiz.after(30, self._laco)

    def _registrar(self, ocorrencias: list[Ocorrencia]) -> None:
        for o in ocorrencias:
            if o.tipo == "buzzer":
                self._tocar(o.texto)
            if o.tipo == "recebida" and (o.texto.startswith("PING") or o.texto.startswith("PONG")):
                continue  # o PING a cada 2 s encheria o log
            if o.tipo == "enviada" and o.texto.startswith("PONG"):
                continue
            seta = {"recebida": "←", "enviada": "→", "invalida": "✕", "buzzer": "♪", "aviso": "•"}[o.tipo]
            self.log.configure(state="normal")
            self.log.insert("end", f"{time.strftime('%H:%M:%S')} {seta} {o.texto}\n", o.tipo)
            if int(self.log.index("end-1c").split(".")[0]) > 400:
                self.log.delete("1.0", "50.0")
            self.log.see("end")
            self.log.configure(state="disabled")

    def desenhar(self) -> None:
        agora = time.monotonic()
        self.oled.delete("all")
        y = 10
        for texto, grande in self.placa.linhas_oled(agora):
            tamanho = 26 if grande else 15
            self.oled.create_text(12, y, text=texto, anchor="nw", fill="white", font=("Consolas", tamanho, "bold"))
            y += tamanho + 26 if grande else tamanho + 18
        for r, linha in enumerate(self.placa.matriz(agora)):
            for c, cor in enumerate(linha):
                self.matriz.itemconfigure(self._leds[r][c], fill=cor or "#1A1E25")
        tocando = agora < self._buzzer_ate and self.placa.som_permitido
        self.buzzer.configure(text="BUZZER ON" if tocando else "BUZZER OFF",
                              bg="#EF4444" if tocando else SUPERFICIE, fg="white" if tocando else SUAVE)
        self.modo.configure(text=f"Modo {self.placa.modo}" + (" (sem som)" if self.placa.modo == "NOITE" else "")
                            + (" · MUDO" if self.placa.mudo else "")
                            + (" · som contínuo" if self.placa.som_continuo else " · som 1 vez"))
        ping = self.placa.ultimo_ping
        if self.conexao.destino is None or ping is None:
            conexao = "aguardando o PING do app…"
        else:
            ip, porta = self.conexao.destino
            conexao = f"PC {ip}:{porta} · " + ("conectado" if self.placa.pc_conectado(agora) else "sem PING há 6 s")
        self.status.configure(text=f"Eu: {self.placa.ip_local}:{self.placa.porta} · {conexao}")

    # ------------------------------------------------------------ buzzer
    def _tocar(self, nivel: str) -> None:
        for pendente in self._bipes:
            self.raiz.after_cancel(pendente)
        self._bipes = []
        self._proximo_continuo = time.monotonic() + REPETIR_TOQUE_S.get(nivel, 2.0)
        if nivel == "ALTO":  # 3 bipes rápidos
            self._buzzer_ate = time.monotonic() + 0.5
            self._bipes = [self.raiz.after(atraso, self._bipe) for atraso in (0, 160, 320)]
        else:  # MEDIO: 1 bipe longo
            self._buzzer_ate = time.monotonic() + 0.9
            self._bipes = [self.raiz.after(0, self._bipe)]

    def _bipe(self) -> None:
        if self.placa.som_permitido:
            self.raiz.bell()

    # ------------------------------------------------------------ botões e joystick
    def tocar_a(self) -> None:
        """E14: 1 toque = mudo liga/desliga; 2 toques em até 0,4 s = calar o alerta atual."""
        if self._clique_a is not None:
            self.raiz.after_cancel(self._clique_a)
            self._clique_a = None
            self.apertar("AA")
        else:
            self._clique_a = self.raiz.after(int(INTERVALO_DUPLO_S * 1000), self._toque_simples_a)

    def _toque_simples_a(self) -> None:
        self._clique_a = None
        self.apertar("A")

    def apertar(self, botao: str) -> None:
        self.conexao.enviar(self.placa.apertar(botao))
        if not self.placa.som_permitido:  # mudo, calado ou NOITE: para o bipe na hora
            for pendente in self._bipes:
                self.raiz.after_cancel(pendente)
            self._bipes = []
        if botao in ("A", "AA"):
            texto = {"A": "mudo " + ("LIGADO" if self.placa.mudo else "desligado"), "AA": "alerta atual calado"}[botao]
            self.conexao.ocorrencias.append(Ocorrencia("aviso", f"botão A: {texto}"))
        self._registrar(self.conexao._tirar())  # noqa: SLF001

    def _mover(self, x: float, y: float) -> None:
        self.conexao.enviar(self.placa.mover_joystick(x, y, time.monotonic()))
        self._registrar(self.conexao._tirar())  # noqa: SLF001
        cx, cy = 100 + x * 82, 100 - y * 82
        self.pad.coords(self.botao_joy, cx - 18, cy - 18, cx + 18, cy + 18)
        self.texto_joy.configure(text=f"JOY {self.placa._eixo(x):.2f} ; {self.placa._eixo(y):.2f}")  # noqa: SLF001

    def _arrastar(self, evento) -> None:  # noqa: ANN001
        x, y = (evento.x - 100) / 82, (100 - evento.y) / 82
        distancia = math.hypot(x, y)
        if distancia > 1:
            x, y = x / distancia, y / distancia
        self._mover(x, y)

    TECLAS_JOY = {"Left": "esq", "a": "esq", "Right": "dir", "d": "dir", "Up": "cima", "w": "cima",
                  "Down": "baixo", "s": "baixo"}

    def _tecla(self, evento) -> None:  # noqa: ANN001
        tecla = evento.keysym if evento.keysym in self.TECLAS_JOY else evento.keysym.lower()
        if tecla in self.TECLAS_JOY:
            self._teclas.add(self.TECLAS_JOY[tecla])
            self._joy_das_teclas()
        elif tecla in ("space", "1"):
            self.tocar_a()
        elif tecla in ("b", "2"):
            self.apertar("B")
        elif tecla in ("j", "3"):
            self.apertar("J")

    def _soltar_tecla(self, evento) -> None:  # noqa: ANN001
        tecla = evento.keysym if evento.keysym in self.TECLAS_JOY else evento.keysym.lower()
        if tecla in self.TECLAS_JOY:
            self._teclas.discard(self.TECLAS_JOY[tecla])
            self._joy_das_teclas()

    def _joy_das_teclas(self) -> None:
        x = ("dir" in self._teclas) - ("esq" in self._teclas)
        y = ("cima" in self._teclas) - ("baixo" in self._teclas)
        self._mover(float(x), float(y))

    def fechar(self) -> None:
        self.conexao.fechar()
        self.raiz.destroy()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Simulador da placa BitDogLab (UDP). Cadastre no app o IP que aparece no display."
    )
    parser.add_argument("--ip-pc", default=None,
                        help="opcional: IP do computador com o app (a placa manda OLA para ele ao ligar)")
    parser.add_argument("--porta-pc", type=int, default=5005, help="porta em que o app escuta (padrão: 5005)")
    parser.add_argument("--porta-placa", type=int, default=5006, help="porta em que a placa escuta (padrão: 5006)")
    args = parser.parse_args()
    try:
        conexao = ConexaoPlaca(args.ip_pc, args.porta_pc, args.porta_placa)
    except OSError as erro:
        print(f"Não foi possível usar a porta UDP {args.porta_placa}: {erro}")
        print("Outro simulador (ou a placa) já está usando essa porta neste computador?")
        return 1
    import tkinter as tk

    print(f"Simulador da BitDogLab: IP {conexao.placa.ip_local}, porta {args.porta_placa}.")
    print("No app: Ajustes > Placa BitDogLab > digite esse IP e clique em 'Salvar e conectar'.")
    raiz = tk.Tk()
    SimuladorApp(raiz, conexao)
    raiz.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
