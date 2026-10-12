"""PTZ: mover a câmera (pan = girar para os lados, tilt = inclinar para cima/baixo).

Peças:

- ``ControladorPTZ``: o que todo controle sabe fazer — ``mover(x, y)`` (velocidades de
  -1 a 1; x > 0 direita, y > 0 cima), ``parar()``, ``ir_preset(n)`` e ``salvar_preset(n)``.
- ``PTZOnvif``: a câmera de verdade, por ONVIF (``ContinuousMove`` proporcional ao
  joystick e ``Stop``). A XM232 do Diego anuncia "movimento contínuo" (diagnóstico da Fase 0).
- ``PTZFalso``: não move nada — registra no log e guarda a direção (a tela mostra o
  indicador). Usado nas câmeras de teste (arquivo/webcam) e quando o ONVIF falha.
- ``ServicoPTZ``: uma thread ("ptz") que fala com as câmeras. Comandos ONVIF demoram
  centenas de ms; aqui eles nunca travam a tela. Regras de segurança da especificação:
  no máximo ``max_comandos_s`` comandos por segundo e, se a câmera estiver andando sem
  receber comando por ``timeout_joystick_s``, ela PARA (um "solta" perdido não deixa a
  câmera girando para sempre). Quem segura um botão/joystick repete o comando (E5).
- Quando a câmera para depois de se mover, quem assinou (``assinar_vista_movida``) é
  avisado: a IA esquece o "fundo" (MOG2) e a regra de "parado" recomeça o aquecimento da
  lente que girou — senão a cena inteira viraria movimento e carros parados, alerta. O
  aviso espera ``estabilizar_s`` (o vídeo chega atrasado) e, numa posição salva, também
  ``tempo_preset_s`` (o motor ainda está indo); se a câmera voltar a andar, é cancelado.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from camera.diagnostico import conectar_onvif, explicar_erro_onvif
from core.config import Config
from core.modelos import Camera, TipoFonte

log = logging.getLogger(__name__)
Chave = tuple[int, int]

#: Quantas posições (presets) a tela oferece.
PRESETS = (1, 2, 3, 4)


class ErroPTZ(Exception):
    """Falha ao falar com o motor da câmera (mensagem pronta para o usuário)."""


class ControladorPTZ(Protocol):
    tipo: str  # "onvif" ou "falso"
    descricao: str

    def mover(self, x: float, y: float) -> None: ...
    def parar(self) -> None: ...
    def ir_preset(self, numero: int) -> None: ...
    def salvar_preset(self, numero: int) -> None: ...


# ===================================================================== controladores
class PTZFalso:
    """Não move nada: registra no log e guarda a última direção (para o indicador e os testes)."""

    tipo = "falso"

    def __init__(self, nome: str = "") -> None:
        self.descricao = "PTZ simulado (sem motor)"
        self._nome = nome
        self.velocidade = (0.0, 0.0)
        self.comandos: list[tuple[str, Any]] = []
        self.presets: dict[int, tuple[float, float]] = {}

    def mover(self, x: float, y: float) -> None:
        if self.velocidade == (0.0, 0.0):
            log.info("PTZ simulado [%s]: começou a mover (x=%.2f, y=%.2f)", self._nome, x, y)
        self.velocidade = (x, y)
        self.comandos.append(("mover", (x, y)))

    def parar(self) -> None:
        log.info("PTZ simulado [%s]: parou", self._nome)
        self.velocidade = (0.0, 0.0)
        self.comandos.append(("parar", None))

    def ir_preset(self, numero: int) -> None:
        log.info("PTZ simulado [%s]: ir para a posição %d", self._nome, numero)
        self.comandos.append(("preset", numero))

    def salvar_preset(self, numero: int) -> None:
        log.info("PTZ simulado [%s]: posição %d salva", self._nome, numero)
        self.presets[numero] = self.velocidade
        self.comandos.append(("salvar_preset", numero))


class PTZOnvif:
    """Motor da câmera por ONVIF (serviço PTZ + o perfil de vídeo que tem PTZ)."""

    tipo = "onvif"

    def __init__(self, servico_ptz: Any, token_perfil: str, descricao: str) -> None:
        self._ptz = servico_ptz
        self._token = token_perfil
        self.descricao = descricao

    @classmethod
    def conectar(cls, ip: str, porta: int, usuario: str, senha: str, timeout_s: float) -> PTZOnvif:
        """Conecta e acha o perfil com PTZ. Levanta ``ErroPTZ`` com a explicação."""
        try:
            camera = conectar_onvif(ip, porta, usuario, senha, timeout_s)
            perfis = camera.create_media_service().GetProfiles()
            perfil = next((p for p in perfis if getattr(p, "PTZConfiguration", None) is not None), None)
            if perfil is None:
                raise ErroPTZ("a câmera não tem perfil ONVIF com PTZ")
            servico = camera.create_ptz_service()
        except ErroPTZ:
            raise
        except ImportError:
            raise ErroPTZ("a biblioteca onvif-zeep não está instalada") from None
        except Exception as erro:  # noqa: BLE001 (a biblioteca lança exceções de vários tipos)
            raise ErroPTZ(explicar_erro_onvif(erro)) from None
        return cls(servico, str(perfil.token), f"ONVIF {ip}:{porta}")

    def _chamar(self, nome: str, pedido: dict[str, Any]) -> Any:
        try:
            return getattr(self._ptz, nome)(pedido)
        except Exception as erro:  # noqa: BLE001
            raise ErroPTZ(explicar_erro_onvif(erro)) from None

    def mover(self, x: float, y: float) -> None:
        self._chamar("ContinuousMove", {"ProfileToken": self._token, "Velocity": {"PanTilt": {"x": x, "y": y}}})

    def parar(self) -> None:
        self._chamar("Stop", {"ProfileToken": self._token, "PanTilt": True, "Zoom": True})

    def ir_preset(self, numero: int) -> None:
        """Procura a posição pelo NOME ("Sentinela 2") ou pelo número; o token é o da câmera."""
        presets = self._chamar("GetPresets", {"ProfileToken": self._token}) or []
        nome = f"Sentinela {numero}"
        preset = next((p for p in presets if str(getattr(p, "Name", "")) == nome), None) or next(
            (p for p in presets if str(getattr(p, "token", "")) == str(numero)), None
        )
        if preset is None:
            raise ErroPTZ(f"a posição {numero} ainda não foi salva nesta câmera")
        self._chamar("GotoPreset", {"ProfileToken": self._token, "PresetToken": str(preset.token)})

    def salvar_preset(self, numero: int) -> None:
        """Tenta salvar com o número; se a câmera recusar, salva sem número (ela escolhe) com o nome."""
        nome = f"Sentinela {numero}"
        try:
            self._chamar("SetPreset", {"ProfileToken": self._token, "PresetToken": str(numero), "PresetName": nome})
        except ErroPTZ:
            self._chamar("SetPreset", {"ProfileToken": self._token, "PresetName": nome})


# ===================================================================== serviço
@dataclass(frozen=True)
class EstadoPTZ:
    """Para a tela (indicador e aviso)."""

    tipo: str  # "onvif", "falso" ou "conectando"
    descricao: str
    x: float = 0.0  # velocidade pedida agora (o indicador mostra)
    y: float = 0.0
    movendo: bool = False
    aviso: str = ""  # ex.: "ONVIF falhou (...): usando PTZ simulado"


@dataclass
class _CameraPTZ:
    camera: Camera
    controlador: ControladorPTZ | None = None
    aviso: str = ""
    desejado: tuple[float, float] = (0.0, 0.0)
    enviado: tuple[float, float] = (0.0, 0.0)
    ultimo_pedido: float = 0.0  # último comando de quem controla (joystick, tela, teclado)
    ultimo_envio: float = -1e9  # último comando mandado à câmera
    falhas_parar: int = 0
    pendentes: list[tuple[str, int]] = field(default_factory=list)  # presets
    avisar_em: float | None = None  # quando avisar que a cena mudou (a câmera parou)


ConectarPTZ = Callable[[Camera, str, float], ControladorPTZ]


class ServicoPTZ:
    """Thread que move as câmeras com PTZ, com limite de comandos e parada de segurança."""

    def __init__(
        self,
        config: Config,
        obter_senha: Callable[[int], str],
        conectar: ConectarPTZ | None = None,
    ) -> None:
        self.config = config
        self._obter_senha = obter_senha
        self._conectar = conectar or _conectar_padrao
        self._ler_ajustes()
        self._fila: queue.Queue[tuple[str, Any]] = queue.Queue()
        self._cameras: dict[int, _CameraPTZ] = {}  # só a thread "ptz" altera
        self._estados: dict[int, EstadoPTZ] = {}
        self._trava = threading.Lock()
        self._assinantes: list[Callable[[Chave], None]] = []
        self._parar_evento = threading.Event()
        self._thread = threading.Thread(target=self._executar, name="ptz", daemon=True)

    # ================================================================ controle (qualquer thread)
    def iniciar(self) -> ServicoPTZ:
        self._thread.start()
        return self

    def parar(self, esperar_s: float = 3.0) -> None:
        """Encerra a thread — antes, PARA as câmeras que estiverem andando."""
        self._parar_evento.set()
        if self._thread.is_alive():
            self._thread.join(esperar_s)

    def _ler_ajustes(self) -> None:
        def ajuste(nome: str) -> Any:
            return self.config.obter(f"ptz.{nome}")

        self._timeout_comando = float(ajuste("timeout_joystick_s"))
        self._intervalo = 1.0 / max(1.0, float(ajuste("max_comandos_s")))
        self._reenviar = float(ajuste("reenviar_movimento_s"))
        self._estabilizar = float(ajuste("estabilizar_s"))
        self._tempo_preset = float(ajuste("tempo_preset_s"))
        self._timeout_onvif = float(ajuste("timeout_onvif_s"))
        self.velocidade_botoes = float(ajuste("velocidade_botoes"))
        self._inverter = (bool(ajuste("inverter_x")), bool(ajuste("inverter_y")))

    def recarregar_ajustes(self) -> None:
        """Os ajustes de PTZ mudaram no config.yaml (vale na hora)."""
        self._fila.put(("ajustes", None))

    def definir_cameras(self, cameras: list[Camera]) -> None:
        self._fila.put(("cameras", [c for c in cameras if c.tem_ptz and c.id is not None]))

    def mover(self, camera_id: int | None, x: float, y: float) -> None:
        """Velocidade -1..1 (x > 0 direita, y > 0 cima). Quem segura deve repetir (< 3 s)."""
        if camera_id is not None:
            self._fila.put(("mover", (camera_id, x, y, time.monotonic())))

    def parar_camera(self, camera_id: int | None) -> None:
        if camera_id is not None:
            self._fila.put(("mover", (camera_id, 0.0, 0.0, time.monotonic())))

    def parar_outras(self, camera_id: int | None) -> None:
        """O joystick passou para outra câmera: as demais param."""
        self._fila.put(("parar_outras", camera_id))

    def ir_preset(self, camera_id: int | None, numero: int) -> None:
        if camera_id is not None:
            self._fila.put(("preset", (camera_id, "ir", numero)))

    def salvar_preset(self, camera_id: int | None, numero: int) -> None:
        if camera_id is not None:
            self._fila.put(("preset", (camera_id, "salvar", numero)))

    def reconectar(self, camera_id: int) -> None:
        """Tenta o ONVIF de novo (ex.: depois de corrigir a senha ou a porta)."""
        self._fila.put(("reconectar", camera_id))

    def estado(self, camera_id: int | None) -> EstadoPTZ | None:
        with self._trava:
            return self._estados.get(camera_id) if camera_id is not None else None

    def assinar_vista_movida(self, funcao: Callable[[Chave], None]) -> None:
        """``funcao(chave da vista)`` quando a câmera PARA depois de girar (thread "ptz")."""
        self._assinantes.append(funcao)

    # ================================================================ thread "ptz"
    def _executar(self) -> None:
        while not self._parar_evento.is_set():
            try:
                item: tuple[str, Any] | None = self._fila.get(timeout=0.02)
            except queue.Empty:
                item = None
            while item is not None:
                try:
                    self._tratar(*item)
                except Exception:  # noqa: BLE001 (um comando ruim não pode parar o PTZ)
                    log.exception("Falha no PTZ (%s)", item[0])
                try:
                    item = self._fila.get_nowait()
                except queue.Empty:
                    item = None
            agora = time.monotonic()
            for estado in list(self._cameras.values()):
                self._aplicar(estado, agora)
        for estado in self._cameras.values():  # fechando o app: ninguém fica girando
            if estado.enviado != (0.0, 0.0) and estado.controlador is not None:
                try:
                    estado.controlador.parar()
                except Exception:  # noqa: BLE001
                    log.warning("Não foi possível parar a câmera %s ao sair", estado.camera.nome)

    def _tratar(self, tipo: str, dado: Any) -> None:
        if tipo == "cameras":
            self._aplicar_cameras(dado)
        elif tipo == "ajustes":
            self._ler_ajustes()
        elif tipo == "mover":
            camera_id, x, y, quando = dado
            estado = self._cameras.get(camera_id)
            if estado is not None:
                estado.desejado = self._normalizar(x, y)
                estado.ultimo_pedido = quando
                if estado.desejado != (0.0, 0.0):
                    estado.avisar_em = None  # voltou a andar: a cena ainda vai mudar
                self._publicar(estado)
        elif tipo == "parar_outras":
            for camera_id, estado in self._cameras.items():
                if camera_id != dado and estado.desejado != (0.0, 0.0):
                    estado.desejado = (0.0, 0.0)
        elif tipo == "preset":
            camera_id, acao, numero = dado
            if (estado := self._cameras.get(camera_id)) is not None:
                estado.pendentes.append((acao, int(numero)))
        elif tipo == "reconectar":
            if (estado := self._cameras.get(dado)) is not None:
                self._ligar(estado)

    @staticmethod
    def _normalizar(x: float, y: float) -> tuple[float, float]:
        x, y = max(-1.0, min(1.0, float(x))), max(-1.0, min(1.0, float(y)))
        return round(x, 2) + 0.0, round(y, 2) + 0.0  # + 0.0: -0.0 vira 0.0

    def _para_camera(self, camera: Camera, x: float, y: float) -> tuple[float, float]:
        """Câmera instalada "ao contrário": inverte só o que vai para o motor (não o indicador).

        ``camera.ajustes["ptz.inverter_x"]`` (tela de Ajustes → Câmeras) vale mais que o padrão.
        """
        inverter_x = bool(camera.ajustes.get("ptz.inverter_x", self._inverter[0]))
        inverter_y = bool(camera.ajustes.get("ptz.inverter_y", self._inverter[1]))
        return (-x if inverter_x else x) + 0.0, (-y if inverter_y else y) + 0.0

    def _aplicar_cameras(self, cameras: list[Camera]) -> None:
        novas = {c.id: c for c in cameras if c.id is not None}
        for camera_id in list(self._cameras):
            antiga = self._cameras[camera_id]
            nova = novas.get(camera_id)
            if nova is None or _assinatura(nova) != _assinatura(antiga.camera):
                self._desligar(antiga)
                del self._cameras[camera_id]
                with self._trava:
                    self._estados.pop(camera_id, None)
            else:
                antiga.camera = nova  # nome/lente mudaram: não precisa reconectar
        for camera_id, camera in novas.items():
            if camera_id not in self._cameras:
                estado = _CameraPTZ(camera)
                self._cameras[camera_id] = estado
                self._ligar(estado)

    def _ligar(self, estado: _CameraPTZ) -> None:
        camera = estado.camera
        with self._trava:
            self._estados[camera.id or 0] = EstadoPTZ("conectando", "conectando ao motor…")
        try:
            estado.controlador = self._conectar(camera, self._obter_senha(camera.id or 0), self._timeout_onvif)
            estado.aviso = ""
            log.info("PTZ de %s: %s", camera.nome, estado.controlador.descricao)
        except ErroPTZ as erro:
            estado.controlador = PTZFalso(camera.nome)
            estado.aviso = f"ONVIF falhou ({erro}): usando PTZ simulado"
            log.warning("PTZ de %s: %s", camera.nome, estado.aviso)
        self._publicar(estado)

    def _desligar(self, estado: _CameraPTZ) -> None:
        if estado.enviado != (0.0, 0.0) and estado.controlador is not None:
            try:
                estado.controlador.parar()
            except Exception:  # noqa: BLE001
                log.warning("Não foi possível parar a câmera %s", estado.camera.nome)

    def _aplicar(self, estado: _CameraPTZ, agora: float) -> None:
        """Manda à câmera o que foi pedido, respeitando o limite e a parada de segurança."""
        controlador = estado.controlador
        if controlador is None:
            return
        if estado.avisar_em is not None and agora >= estado.avisar_em:
            estado.avisar_em = None
            self._avisar_movida(estado)
        if estado.desejado != (0.0, 0.0) and agora - estado.ultimo_pedido > self._timeout_comando:
            log.warning("PTZ de %s: sem comando há %.0f s — parando (segurança)", estado.camera.nome, self._timeout_comando)
            estado.desejado = (0.0, 0.0)
            self._publicar(estado)
        if agora - estado.ultimo_envio < self._intervalo:
            return  # no máximo max_comandos_s por segundo
        if estado.pendentes:
            acao, numero = estado.pendentes.pop(0)
            estado.ultimo_envio = agora
            try:
                if acao == "ir":
                    controlador.ir_preset(numero)
                else:
                    controlador.salvar_preset(numero)
                estado.aviso = "" if controlador.tipo == "onvif" else estado.aviso
                if acao == "ir":  # a cena vai mudar: avisa quando o motor chegar e o vídeo estabilizar
                    estado.avisar_em = agora + self._tempo_preset + self._estabilizar
            except ErroPTZ as erro:
                estado.aviso = f"posição {numero}: {erro}"
                log.warning("PTZ de %s: %s", estado.camera.nome, estado.aviso)
            self._publicar(estado)
            return
        repetir = estado.desejado != (0.0, 0.0) and agora - estado.ultimo_envio >= self._reenviar
        if estado.desejado == estado.enviado and not repetir:
            return
        alvo = estado.desejado
        estado.ultimo_envio = agora
        try:
            if alvo == (0.0, 0.0):
                controlador.parar()
            else:
                controlador.mover(*self._para_camera(estado.camera, *alvo))
        except ErroPTZ as erro:
            estado.aviso = f"o motor não respondeu: {erro}"
            log.warning("PTZ de %s: %s", estado.camera.nome, estado.aviso)
            if alvo == (0.0, 0.0):
                estado.falhas_parar += 1
                if estado.falhas_parar < 5:
                    estado.ultimo_envio = agora + 0.5  # tenta parar de novo daqui a pouco
                    self._publicar(estado)
                    return
            estado.falhas_parar = 0
            estado.enviado = alvo
            self._publicar(estado)
            return
        parou = alvo == (0.0, 0.0) and estado.enviado != (0.0, 0.0)
        estado.enviado, estado.falhas_parar = alvo, 0
        if controlador.tipo == "onvif":
            estado.aviso = ""
        self._publicar(estado)
        if parou:
            estado.avisar_em = agora + self._estabilizar

    def _avisar_movida(self, estado: _CameraPTZ) -> None:
        camera = estado.camera
        for vista in camera.vistas():
            if vista.lente == camera.lente_ptz or len(camera.vistas()) == 1:
                for funcao in list(self._assinantes):
                    try:
                        funcao(vista.chave)
                    except Exception:  # noqa: BLE001
                        log.exception("Assinante do PTZ falhou")

    def _publicar(self, estado: _CameraPTZ) -> None:
        controlador = estado.controlador
        novo = EstadoPTZ(
            tipo=controlador.tipo if controlador else "conectando",
            descricao=controlador.descricao if controlador else "conectando ao motor…",
            x=estado.desejado[0],
            y=estado.desejado[1],
            movendo=estado.desejado != (0.0, 0.0),
            aviso=estado.aviso,
        )
        with self._trava:
            self._estados[estado.camera.id or 0] = novo


def _assinatura(camera: Camera) -> tuple[Any, ...]:
    """O que exige reconectar o motor se mudar."""
    return (camera.fonte, camera.ip, camera.porta_onvif, camera.usuario)


def _conectar_padrao(camera: Camera, senha: str, timeout_s: float) -> ControladorPTZ:
    """Câmera RTSP: ONVIF de verdade. Câmeras de teste (arquivo/webcam): PTZ simulado."""
    if camera.fonte is not TipoFonte.RTSP:
        return PTZFalso(camera.nome)
    return PTZOnvif.conectar(camera.ip, camera.porta_onvif, camera.usuario, senha, timeout_s)
