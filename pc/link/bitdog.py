"""Comunicação com a placa BitDogLab por UDP (protocolo FIXO em ``docs/protocolo.md``).

Uma thread ("placa") faz tudo:

- escuta a porta ``placa.porta_pc`` (5005) e interpreta o que a placa manda;
- **o PC puxa a conversa**: o IP da placa é cadastrado no app (Ajustes → Placa BitDogLab;
  a placa mostra o IP dela no display) e o PC manda PING para ``<IP>:<porta_placa>``. A placa
  responde para o endereço de onde o PING veio — funciona até com um roteador no meio (E7).
  Sem IP cadastrado, o PC aprende o IP pelo 1º pacote da placa (OLA). Com IP cadastrado,
  pacotes de outros endereços são ignorados (ninguém da rede "finge" ser a placa);
- ``reconfigurar`` troca IP e portas na hora, sem reiniciar o app;
- PING a cada ``intervalo_ping_s``; HORA a cada ``intervalo_hora_s``; "conectada" = algum
  PONG nos últimos ``timeout_pong_s`` (E9). Ao conectar (e quando a placa diz OLA), o PC
  manda HORA, CAM, SOM e o alerta ativo;
- alertas (assinante do ``ServicoAlertas``): envia o de MAIOR nível (empate: o mais
  recente), reenvia a cada ``reenvio_alerta_s`` com os segundos atualizados (E1, E4),
  LIMPAR duas vezes (E3) e PASSOU;
- buzzer: ``placa.buzzer`` (Ajustes → Placa) vira ``SOM;UMA`` ou ``SOM;CONTINUO`` (E13);
- botões: A (2 cliques na placa) = o alerta atual foi silenciado NA PLACA — o PC continua a
  reenviá-lo, para ele seguir na tela da placa até o objeto sair (E14); J troca a câmera do
  joystick e responde CAM (E10);
- JOY e botões vão para quem assinou (a Fase 7 move a câmera com eles).

Mensagens inválidas são ignoradas e registradas no log — nunca derrubam o app.
"""

from __future__ import annotations

import logging
import queue
import socket
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from core.config import Config
from core.modelos import Camera
from link import protocolo
from link.protocolo import Botao, ErroProtocolo, Joy, Modo, Ola, Pong
from vision.dwell import Aviso, TipoAviso

log = logging.getLogger(__name__)

#: E3: o LIMPAR é repetido este tempo depois (UDP pode perder o primeiro).
ATRASO_LIMPAR_REPETIDO_S = 1.0
#: Intervalo da espera no socket (a thread confere os relógios entre uma leitura e outra).
ESPERA_SOCKET_S = 0.05
#: No máximo um aviso de "mensagem inválida" no log por este tempo (evita encher o log).
INTERVALO_LOG_INVALIDA_S = 5.0
#: HORA/CAM/alerta enviados há menos que isto não são repetidos ao conectar (o OLA já mandou).
INTERVALO_CONTEXTO_S = 1.0

ChaveAlerta = tuple[str, int]  # (código da câmera, id do objeto)


def ip_desta_maquina() -> str:
    """IP deste computador na rede local (o da interface que leva ao roteador).

    O ``connect`` de um socket UDP não envia nada: só escolhe a interface de rede.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sonda:
        try:
            sonda.connect(("10.255.255.255", 1))
            return str(sonda.getsockname()[0])
        except OSError:
            return "127.0.0.1"


@dataclass(frozen=True)
class EstadoPlaca:
    """Foto instantânea para a interface (barra lateral)."""

    conectada: bool
    ip: str | None
    porta_pc: int
    modo: str | None = None
    resposta_ms: float | None = None  # tempo do PING até o PONG
    sem_resposta_s: float | None = None  # há quanto tempo não chega PONG
    alerta: str = ""  # alerta exibido na placa agora (ex.: "PESSOA em FRENTE")
    camera: str = protocolo.SEM_CAMERA  # código da câmera do joystick
    erro: str = ""  # ex.: porta 5005 em uso
    porta_placa: int = 5006
    configurada: bool = False  # True = IP cadastrado no app (senão, aprende pelo OLA)


@dataclass
class _AlertaPlaca:
    aviso: Aviso
    codigo: str
    enviado: bool = False


class LinkBitDog:
    """Serviço de comunicação com a placa (uma thread, um socket UDP)."""

    def __init__(self, config: Config, endereco: str = "") -> None:
        """``endereco``: interface onde escutar ("" = todas, necessário para a placa no Wi-Fi;
        os testes usam "127.0.0.1" para não acionar o firewall)."""

        def ajuste(nome: str) -> object:
            return config.obter(f"placa.{nome}")

        self.config = config
        self._endereco = endereco
        self._ip_config = str(ajuste("ip") or "").strip() or None
        self.porta_pc = int(ajuste("porta_pc"))  # type: ignore[arg-type]
        self.porta_placa = int(ajuste("porta_placa"))  # type: ignore[arg-type]
        self._intervalo_ping = float(ajuste("intervalo_ping_s"))  # type: ignore[arg-type]
        self._intervalo_hora = float(ajuste("intervalo_hora_s"))  # type: ignore[arg-type]
        self._timeout_pong = float(ajuste("timeout_pong_s"))  # type: ignore[arg-type]
        self._reenvio_alerta = float(ajuste("reenvio_alerta_s"))  # type: ignore[arg-type]
        self._som_continuo = str(config.obter_ou("placa.buzzer", "uma_vez")) == "continuo"

        self._fila: queue.Queue[tuple[str, object]] = queue.Queue()
        self._parar = threading.Event()
        self._thread = threading.Thread(target=self._executar, name="placa", daemon=True)
        self._socket: socket.socket | None = None
        self._trava = threading.Lock()

        # Estado (só a thread "placa" altera; ``estado()`` lê com a trava)
        self._ip_aprendido: str | None = None
        self._seq = 0
        self._pings: dict[int, float] = {}
        self._ultimo_pong: float | None = None
        self._resposta_ms: float | None = None
        self._modo: str | None = None
        self._erro = ""
        self._alertas: dict[ChaveAlerta, _AlertaPlaca] = {}
        self._exibido: ChaveAlerta | None = None
        self._ultimo_envio_alerta = 0.0
        self._limpar_pendentes: list[tuple[float, str]] = []
        self._cameras: list[Camera] = []
        self._camera_joystick: int | None = None
        self._ultimo_log_invalida = 0.0
        self._invalidas_caladas = 0
        self._proximo_ping = 0.0
        self._ultimo_contexto = -INTERVALO_CONTEXTO_S

        self._assinantes_joy: list[Callable[[float, float], None]] = []
        self._assinantes_botao: list[Callable[[str], None]] = []
        self._assinantes_camera: list[Callable[[int | None], None]] = []

    # ================================================================ controle (qualquer thread)
    def iniciar(self) -> LinkBitDog:
        self._thread.start()
        return self

    def parar(self, esperar_s: float = 2.0) -> None:
        self._parar.set()
        if self._thread.is_alive():
            self._thread.join(esperar_s)

    def receber_aviso(self, aviso: Aviso, camera: Camera) -> None:
        """Assinante do ``ServicoAlertas`` (ALERTA, LIMPAR, PASSOU) — só enfileira."""
        self._fila.put(("aviso", (aviso, camera)))

    def definir_cameras(self, cameras: list[Camera]) -> None:
        self._fila.put(("cameras", list(cameras)))

    def reconfigurar(self, ip: str | None, porta_placa: int, porta_pc: int) -> None:
        """Novo IP/portas da placa (tela de Ajustes). Vale na hora: o PC já manda um PING."""
        self._fila.put(("reconfigurar", (ip.strip() if ip else None, int(porta_placa), int(porta_pc))))

    def definir_som(self, continuo: bool) -> None:
        """Como o buzzer da placa toca (Ajustes → Placa). Vale na hora: o PC já manda ``SOM``."""
        self._fila.put(("som", bool(continuo)))

    def definir_camera_joystick(self, camera_id: int | None) -> None:
        """A câmera aberta/selecionada na interface (só vale se ela tiver PTZ)."""
        self._fila.put(("joystick", camera_id))

    def assinar_joystick(self, funcao: Callable[[float, float], None]) -> None:
        """``funcao(x, y)`` a cada JOY da placa (thread "placa")."""
        self._assinantes_joy.append(funcao)

    def assinar_botao(self, funcao: Callable[[str], None]) -> None:
        """``funcao("A" | "B" | "J")`` a cada botão apertado na placa (thread "placa")."""
        self._assinantes_botao.append(funcao)

    def assinar_camera(self, funcao: Callable[[int | None], None]) -> None:
        """``funcao(id da câmera ou None)`` quando a câmera do joystick muda (thread "placa")."""
        self._assinantes_camera.append(funcao)

    @property
    def camera_joystick(self) -> int | None:
        return self._camera_joystick

    def estado(self) -> EstadoPlaca:
        agora = time.monotonic()
        with self._trava:
            conectada = self._ultimo_pong is not None and agora - self._ultimo_pong <= self._timeout_pong
            exibido = self._alertas.get(self._exibido) if self._exibido else None
            camera = self._camera_por_id(self._camera_joystick)
            return EstadoPlaca(
                conectada=conectada,
                ip=self._ip_config or self._ip_aprendido,
                porta_pc=self.porta_pc,
                modo=self._modo,
                resposta_ms=self._resposta_ms if conectada else None,
                sem_resposta_s=None if self._ultimo_pong is None else agora - self._ultimo_pong,
                alerta=f"{exibido.aviso.classe} em {exibido.codigo}" if exibido else "",
                camera=camera.codigo if camera else protocolo.SEM_CAMERA,
                erro=self._erro,
                porta_placa=self.porta_placa,
                configurada=self._ip_config is not None,
            )

    # ================================================================ thread "placa"
    def _executar(self) -> None:
        self._socket = self._tentar_abrir(self.porta_pc)
        proxima_hora = time.monotonic() + self._intervalo_hora
        conectada = False
        while not self._parar.is_set():
            if self._socket is None:  # porta ocupada: espera uma nova configuração (Ajustes)
                self._parar.wait(ESPERA_SOCKET_S)
                self._tratar_fila()
                continue
            self._ler_socket()
            self._tratar_fila()
            agora = time.monotonic()
            if agora >= self._proximo_ping:
                self._proximo_ping = agora + self._intervalo_ping
                self._enviar_ping(agora)
            if agora >= proxima_hora:
                proxima_hora = agora + self._intervalo_hora
                self._enviar(protocolo.hora(datetime.now()))
            if self._exibido is not None and agora - self._ultimo_envio_alerta >= self._reenvio_alerta:
                self._enviar_alerta(self._exibido, agora)  # E1: reenvio com os segundos atualizados
            for quando, mensagem in [p for p in self._limpar_pendentes if p[0] <= agora]:
                self._limpar_pendentes.remove((quando, mensagem))
                self._enviar(mensagem)
            agora_conectada = self.estado().conectada
            if agora_conectada != conectada:
                conectada = agora_conectada
                log.info("Placa BitDogLab %s", "conectada" if conectada else "desconectada (sem PONG)")
                if conectada and agora - self._ultimo_contexto > INTERVALO_CONTEXTO_S:
                    self._enviar_contexto(agora)  # "ao conectar": HORA, CAM e o alerta ativo
        if self._socket is not None:
            self._socket.close()

    def _tentar_abrir(self, porta: int) -> socket.socket | None:
        try:
            sock = self._abrir_socket(porta)
        except OSError as erro:
            with self._trava:
                self._erro = f"não foi possível usar a porta UDP {porta}: {erro}"
            log.error("Placa BitDogLab: %s", self._erro)
            return None
        with self._trava:
            self._erro = ""
        log.info(
            "Comunicação com a placa: escutando UDP %d; placa em %s:%d",
            porta, self._ip_config or "(aprende pelo 1º pacote)", self.porta_placa,
        )
        return sock

    def _aplicar_configuracao(self, ip: str | None, porta_placa: int, porta_pc: int) -> None:
        """Troca IP/portas sem reiniciar. A conexão recomeça do zero (o PING sai na hora)."""
        if porta_pc != self.porta_pc or self._socket is None:
            novo = self._tentar_abrir(porta_pc)
            if novo is None:
                return  # porta ocupada: o erro aparece na tela de Ajustes
            if self._socket is not None:
                self._socket.close()
            self._socket = novo
        with self._trava:
            self.porta_pc, self.porta_placa = porta_pc, porta_placa
            self._ip_config = ip
            self._ip_aprendido = None
            self._ultimo_pong = self._resposta_ms = self._modo = None
        self._pings.clear()
        self._proximo_ping = 0.0
        log.info("Placa BitDogLab reconfigurada: %s:%d (o PC escuta na %d)", ip or "(aprende pelo OLA)", porta_placa, porta_pc)

    def _enviar_contexto(self, agora: float) -> None:
        """HORA, câmera do joystick, jeito do buzzer e o alerta ativo (ao conectar ou no OLA)."""
        self._ultimo_contexto = agora
        self._enviar(protocolo.hora(datetime.now()))
        self._enviar(protocolo.camera(self._codigo_joystick()))
        self._enviar(protocolo.som(self._som_continuo))
        if self._exibido is not None:
            self._enviar_alerta(self._exibido, agora)

    def _abrir_socket(self, porta: int) -> socket.socket:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            if hasattr(socket, "SIO_UDP_CONNRESET"):
                # Windows: sem isto, enviar para uma porta fechada faz o PRÓXIMO recvfrom falhar.
                sock.ioctl(socket.SIO_UDP_CONNRESET, False)  # type: ignore[attr-defined]
            sock.bind((self._endereco, porta))
        except OSError:
            sock.close()
            raise
        sock.settimeout(ESPERA_SOCKET_S)
        return sock

    def _ler_socket(self) -> None:
        assert self._socket is not None
        try:
            dados, (ip, _porta) = self._socket.recvfrom(1024)
        except (TimeoutError, BlockingIOError, ConnectionResetError):
            return
        except OSError as erro:
            log.debug("Leitura UDP falhou: %s", erro)
            return
        self._receber(dados, ip)

    def _tratar_fila(self) -> None:
        while True:
            try:
                tipo, dado = self._fila.get_nowait()
            except queue.Empty:
                return
            try:
                if tipo == "aviso":
                    aviso, camera = dado  # type: ignore[misc]
                    self._aviso(aviso, camera)
                elif tipo == "cameras":
                    self._aplicar_cameras(dado)  # type: ignore[arg-type]
                elif tipo == "joystick":
                    self._trocar_camera(dado if dado in self._ids_ptz() else self._camera_joystick)  # type: ignore[arg-type]
                elif tipo == "reconfigurar":
                    self._aplicar_configuracao(*dado)  # type: ignore[misc]
                elif tipo == "som":
                    self._som_continuo = bool(dado)
                    self._enviar(protocolo.som(self._som_continuo))
            except Exception:  # noqa: BLE001 (um problema não pode parar a comunicação)
                log.exception("Falha ao tratar %s", tipo)

    # ---------------------------------------------------------------- recebendo
    def _receber(self, dados: bytes, ip: str) -> None:
        if self._ip_config is not None and ip != self._ip_config:
            self._registrar_invalida(f"pacote de {ip} ignorado (a placa configurada é {self._ip_config})", dados)
            return
        try:
            mensagem = protocolo.interpretar(dados)
        except ErroProtocolo as erro:
            self._registrar_invalida(str(erro), dados)
            return
        log.debug("placa → PC: %s", dados.decode("ascii").strip())
        agora = time.monotonic()
        if self._ip_config is None and ip != self._ip_aprendido:
            log.info("Placa BitDogLab encontrada em %s", ip)
            with self._trava:
                self._ip_aprendido = ip
        if isinstance(mensagem, Pong):
            enviado = self._pings.pop(mensagem.seq, None)
            with self._trava:
                self._ultimo_pong = agora
                if enviado is not None:
                    self._resposta_ms = (agora - enviado) * 1000
        elif isinstance(mensagem, Ola):
            log.info("Placa BitDogLab disse OLA (ligou ou reconectou)")
            self._enviar_contexto(agora)  # regra 2: HORA, CAM e o alerta ativo
            self._enviar_ping(agora)
        elif isinstance(mensagem, Modo):
            if mensagem.nome != self._modo:
                log.info("Placa BitDogLab em modo %s", mensagem.nome)
            with self._trava:
                self._modo = mensagem.nome
        elif isinstance(mensagem, Botao):
            log.info("Placa BitDogLab: botão %s", mensagem.nome)
            if mensagem.nome == "A" and self._exibido is not None:
                # E14: quem cala é a PLACA (2 cliques no A). O PC continua reenviando o alerta, para ela
                # seguir mostrando-o até o objeto sair (antes o PC parava e o alerta sumia em 6 s).
                log.info("Alerta silenciado na placa: %s #%d", *self._exibido)
            elif mensagem.nome == "J":
                self._proxima_camera()
            self._avisar(self._assinantes_botao, mensagem.nome)
        elif isinstance(mensagem, Joy):
            self._avisar(self._assinantes_joy, mensagem.x, mensagem.y)

    def _registrar_invalida(self, motivo: str, dados: bytes) -> None:
        agora = time.monotonic()
        if agora - self._ultimo_log_invalida < INTERVALO_LOG_INVALIDA_S:
            self._invalidas_caladas += 1
            return
        extra = f" (+{self._invalidas_caladas} ignoradas antes)" if self._invalidas_caladas else ""
        log.warning("Mensagem da placa ignorada: %s — %r%s", motivo, dados[:80], extra)
        self._ultimo_log_invalida, self._invalidas_caladas = agora, 0

    # ---------------------------------------------------------------- alertas
    def _aviso(self, aviso: Aviso, camera: Camera) -> None:
        chave = (camera.codigo, aviso.id_rastreio)
        agora = time.monotonic()
        if aviso.tipo is TipoAviso.ALERTA:
            with self._trava:
                self._alertas[chave] = _AlertaPlaca(aviso, camera.codigo)
            self._escolher_alerta(agora)
        elif aviso.tipo is TipoAviso.LIMPAR:
            with self._trava:
                alerta = self._alertas.pop(chave, None)
            if alerta is not None and alerta.enviado:
                mensagem = protocolo.limpar(camera.codigo, aviso.id_rastreio)
                self._enviar(mensagem)
                self._limpar_pendentes.append((agora + ATRASO_LIMPAR_REPETIDO_S, mensagem))  # E3
            if chave == self._exibido:
                with self._trava:
                    self._exibido = None
            self._escolher_alerta(agora)
        else:
            self._enviar(protocolo.passou(camera.codigo, aviso.classe))

    def _escolher_alerta(self, agora: float) -> None:
        """E4: entre os ativos, o de maior nível (empate: o mais recente)."""
        escolhido = max(self._alertas.items(), key=lambda par: (par[1].aviso.nivel, par[1].aviso.instante),
                        default=None)
        chave = escolhido[0] if escolhido else None
        if chave != self._exibido:
            with self._trava:
                self._exibido = chave
            if chave is not None:
                self._enviar_alerta(chave, agora)  # mudou: envia na hora

    def _enviar_alerta(self, chave: ChaveAlerta, agora: float) -> None:
        alerta = self._alertas.get(chave)
        if alerta is None:
            return
        aviso = alerta.aviso
        segundos = aviso.segundos + max(0.0, agora - aviso.instante)  # parado há mais tempo agora
        self._enviar(
            protocolo.alerta(alerta.codigo, aviso.classe, segundos, aviso.confianca, aviso.id_rastreio, aviso.nivel)
        )
        alerta.enviado = True
        self._ultimo_envio_alerta = agora

    # ---------------------------------------------------------------- câmera do joystick
    def _ids_ptz(self) -> list[int]:
        return [c.id for c in self._cameras if c.tem_ptz and c.id is not None]

    def _camera_por_id(self, camera_id: int | None) -> Camera | None:
        return next((c for c in self._cameras if c.id == camera_id), None)

    def _codigo_joystick(self) -> str | None:
        camera = self._camera_por_id(self._camera_joystick)
        return camera.codigo if camera else None

    def _aplicar_cameras(self, cameras: list[Camera]) -> None:
        with self._trava:
            self._cameras = sorted(cameras, key=lambda c: (c.ordem, c.id or 0))
        ids = self._ids_ptz()
        atual = self._camera_joystick if self._camera_joystick in ids else (ids[0] if ids else None)
        self._trocar_camera(atual, forcar=False)

    def _trocar_camera(self, camera_id: int | None, forcar: bool = False) -> None:
        if camera_id == self._camera_joystick and not forcar:
            return
        with self._trava:
            self._camera_joystick = camera_id
        self._enviar(protocolo.camera(self._codigo_joystick()))
        self._avisar(self._assinantes_camera, camera_id)

    def _proxima_camera(self) -> None:
        """BTN;J: a próxima câmera com PTZ (em ciclo); sem nenhuma, responde CAM;- (E10)."""
        ids = self._ids_ptz()
        if not ids:
            self._trocar_camera(None, forcar=True)
            return
        indice = ids.index(self._camera_joystick) + 1 if self._camera_joystick in ids else 0
        self._trocar_camera(ids[indice % len(ids)], forcar=True)

    # ---------------------------------------------------------------- enviando
    def _enviar_ping(self, agora: float) -> None:
        seq = self._seq
        self._seq = (self._seq + 1) % (protocolo.SEQ_MAXIMO + 1)
        self._pings[seq] = agora
        for antigo in [s for s, t in self._pings.items() if agora - t > self._timeout_pong]:
            del self._pings[antigo]
        self._enviar(protocolo.ping(seq))

    def _enviar(self, mensagem: str) -> None:
        ip = self._ip_config or self._ip_aprendido
        if ip is None or self._socket is None:
            return  # ainda não sabemos onde a placa está (ela se apresenta com OLA)
        try:
            self._socket.sendto(mensagem.encode("ascii"), (ip, self.porta_placa))
            log.debug("PC → placa: %s", mensagem.strip())
        except OSError as erro:
            log.debug("Envio para a placa falhou (%s): %s", ip, erro)

    @staticmethod
    def _avisar(assinantes: list[Callable[..., None]], *argumentos: object) -> None:
        for funcao in list(assinantes):
            try:
                funcao(*argumentos)
            except Exception:  # noqa: BLE001
                log.exception("Assinante da placa falhou")
