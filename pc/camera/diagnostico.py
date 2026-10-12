"""Diagnóstico de UMA câmera informada pelo usuário (nunca faz varredura de rede).

Etapas:
1. Portas TCP (por padrão 554 RTSP, 8899 ONVIF e 34567 DVRIP/Xiongmai).
2. Substream: sonda RTSP (mensagem de erro precisa) + abertura com OpenCV
   (resolução, FPS, codec e um quadro de amostra).
3. Stream principal: idem.
4. ONVIF: fabricante, perfis de vídeo e suporte a PTZ (motor).
5. Conclusões em português, com o que fazer em seguida.

Usado por ``tools/testar_camera.py`` e pelo botão "Testar conexão" do cadastro.
As funções são bloqueantes (podem levar dezenas de segundos): na interface,
chame-as fora da thread da interface.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np

from camera.captura import abrir_captura, codec_da_captura
from camera.rede import ResultadoPorta, testar_porta
from camera.sonda_rtsp import ResultadoSonda, sondar_rtsp
from core.modelos import TipoStream
from core.url_rtsp import mascarar_url, montar_url, variaveis_do_modelo

log = logging.getLogger(__name__)

#: Para que serve cada porta conhecida (aparece no relatório).
DESCRICAO_PORTAS: dict[int, str] = {
    554: "RTSP (vídeo)",
    8899: "ONVIF (controle/PTZ)",
    34567: "DVRIP (protocolo Xiongmai/iCSee)",
    80: "HTTP (página web)",
}

def descrever_porta(porta: int, parametros: ParametrosDiagnostico | None = None) -> str:
    """Para que serve a porta, considerando as portas configuradas na câmera."""
    if parametros is not None:
        if porta == parametros.porta_rtsp:
            return DESCRICAO_PORTAS[554]
        if porta == parametros.porta_onvif:
            return DESCRICAO_PORTAS[8899]
    return DESCRICAO_PORTAS.get(porta, "")


#: Recebe (etapa, resultado). ``resultado`` é ``None`` quando a etapa está começando.
Progresso = Callable[[str, object | None], None]


@dataclass
class ParametrosDiagnostico:
    ip: str
    usuario: str
    senha: str = field(repr=False)  # nunca aparece em repr()/logs
    canal: int
    porta_rtsp: int
    porta_onvif: int
    modelo_url: str
    portas: list[int]
    timeout_s: float  # testes de rede: portas, sonda RTSP e ONVIF
    medir_fps_s: float
    video_abertura_s: float  # limite para abrir o vídeo (captura.timeout_abertura_s)
    video_leitura_s: float  # limite para cada quadro (captura.timeout_leitura_s)
    testar_principal: bool = True
    testar_onvif: bool = True
    #: Testa esta URL (ou arquivo de vídeo) diretamente, em vez de montar pelo modelo.
    url_direta: str | None = None

    def url(self, stream: TipoStream) -> str:
        if self.url_direta:
            return self.url_direta
        return montar_url(
            self.modelo_url,
            ip=self.ip,
            porta=self.porta_rtsp,
            usuario=self.usuario,
            senha=self.senha,
            canal=self.canal,
            stream=int(stream),
        )


@dataclass
class ResultadoStream:
    nome: str  # "substream" ou "principal"
    fonte: str  # URL já mascarada (ou caminho do arquivo)
    ok: bool
    mensagem: str
    largura: int = 0
    altura: int = 0
    fps_declarado: float = 0.0
    fps_medido: float = 0.0
    codec: str = ""
    tempo_abertura_s: float = 0.0
    quadros: int = 0
    quadro: np.ndarray | None = field(default=None, repr=False)  # amostra para miniatura


@dataclass
class PerfilOnvif:
    nome: str
    token: str
    largura: int = 0
    altura: int = 0
    codec: str = ""
    fps: float = 0.0
    url: str = ""  # já mascarada
    ptz: bool = False


@dataclass
class ResultadoOnvif:
    ok: bool
    mensagem: str
    fabricante: str = ""
    modelo: str = ""
    firmware: str = ""
    perfis: list[PerfilOnvif] = field(default_factory=list)
    tem_ptz: bool = False
    detalhes_ptz: str = ""


@dataclass
class Diagnostico:
    parametros: ParametrosDiagnostico
    portas: list[ResultadoPorta] = field(default_factory=list)
    sonda_sub: ResultadoSonda | None = None
    sub: ResultadoStream | None = None
    sonda_principal: ResultadoSonda | None = None
    principal: ResultadoStream | None = None
    onvif: ResultadoOnvif | None = None
    conclusoes: list[str] = field(default_factory=list)
    cancelado: bool = False

    @property
    def ok(self) -> bool:
        """Sucesso = o substream (usado no mosaico e na IA) abriu."""
        return bool(self.sub and self.sub.ok)


# ==================================================================== etapas
def testar_stream(
    fonte: str | int, nome: str, abertura_s: float, leitura_s: float, medir_fps_s: float
) -> ResultadoStream:
    """Abre a fonte com OpenCV, lê um quadro e mede o FPS real por ``medir_fps_s`` segundos."""
    eh_arquivo = isinstance(fonte, str) and "://" not in fonte
    rotulo = mascarar_url(fonte) if isinstance(fonte, str) else f"webcam {fonte}"
    if eh_arquivo and not Path(fonte).is_file():
        return ResultadoStream(nome, rotulo, False, f"arquivo não encontrado: {fonte}")

    inicio = time.perf_counter()
    captura = abrir_captura(fonte, abertura_s, leitura_s)
    try:
        if not captura.isOpened():
            decorrido = time.perf_counter() - inicio
            mensagem = "o OpenCV não conseguiu abrir o vídeo"
            if decorrido >= abertura_s * 0.95:
                mensagem += f" em {abertura_s:.0f} s (limite esgotado: rede lenta?)"
            return ResultadoStream(nome, rotulo, False, mensagem, tempo_abertura_s=decorrido)
        aberto_em = time.perf_counter() - inicio
        ok, quadro = captura.read()
        abertura = time.perf_counter() - inicio
        if not ok or quadro is None:
            return ResultadoStream(
                nome, rotulo, False,
                f"a conexão abriu em {aberto_em:.1f} s, mas nenhum quadro completo chegou em "
                f"{leitura_s:.0f} s (rede lenta para esta resolução?)",
                tempo_abertura_s=abertura,
            )
        altura, largura = quadro.shape[:2]
        fps_declarado = float(captura.get(cv2.CAP_PROP_FPS) or 0.0)
        if not 0 < fps_declarado <= 240:
            fps_declarado = 0.0  # algumas câmeras informam valores sem sentido
        codec = codec_da_captura(captura)

        quadros, fps_medido = 1, 0.0
        if not eh_arquivo and medir_fps_s > 0:
            quadro, lidos, fps_medido = _medir_fps(captura, medir_fps_s, quadro)
            quadros += lidos
    except cv2.error as erro:
        return ResultadoStream(nome, rotulo, False, f"erro do OpenCV: {erro}")
    finally:
        captura.release()

    partes = [f"{largura}x{altura}"]
    if fps_medido:
        partes.append(f"{fps_medido:.1f} FPS medido")
    if fps_declarado:
        partes.append(f"{fps_declarado:.0f} FPS declarado")
    if codec:
        partes.append(f"codec {codec}")
    mensagem = f"vídeo aberto em {abertura:.1f} s: " + ", ".join(partes)
    return ResultadoStream(
        nome, rotulo, True, mensagem, largura, altura, fps_declarado, fps_medido, codec, abertura, quadros, quadro
    )


def _medir_fps(captura: cv2.VideoCapture, duracao_s: float, ultimo: np.ndarray) -> tuple[np.ndarray, int, float]:
    """Lê quadros por ``duracao_s`` e calcula o FPS.

    O primeiro terço do tempo é ignorado no cálculo: logo após abrir, o FFmpeg
    entrega de uma vez os quadros que estavam no buffer, o que inflaria o FPS.
    """
    instantes: list[float] = []
    inicio = time.perf_counter()
    while time.perf_counter() - inicio < duracao_s:
        ok, quadro = captura.read()
        if not ok:
            break
        ultimo = quadro
        instantes.append(time.perf_counter())
    corte = inicio + duracao_s / 3
    validos = [t for t in instantes if t >= corte]
    fps = (len(validos) - 1) / (validos[-1] - validos[0]) if len(validos) > 2 and validos[-1] > validos[0] else 0.0
    return ultimo, len(instantes), fps


def testar_onvif(ip: str, porta: int, usuario: str, senha: str, timeout_s: float) -> ResultadoOnvif:
    """Conecta por ONVIF e lista informações, perfis de vídeo e suporte a PTZ."""
    porta_ok = testar_porta(ip, porta, timeout_s)
    if not porta_ok.aberta:
        return ResultadoOnvif(False, f"ONVIF indisponível: {porta_ok.mensagem}")
    try:
        camera = conectar_onvif(ip, porta, usuario, senha, timeout_s)
    except ImportError:
        return ResultadoOnvif(False, "a biblioteca onvif-zeep não está instalada (pip install onvif-zeep)")
    except Exception as erro:  # noqa: BLE001 (a biblioteca lança exceções de vários tipos)
        log.debug("Falha ao conectar no ONVIF", exc_info=True)
        return ResultadoOnvif(False, explicar_erro_onvif(erro))

    resultado = ResultadoOnvif(True, "ONVIF respondeu")
    try:
        info = camera.devicemgmt.GetDeviceInformation()
        resultado.fabricante = str(info.Manufacturer or "")
        resultado.modelo = str(info.Model or "")
        resultado.firmware = str(info.FirmwareVersion or "")
    except Exception as erro:  # noqa: BLE001
        return ResultadoOnvif(False, explicar_erro_onvif(erro))

    try:
        media = camera.create_media_service()
        for perfil in media.GetProfiles():
            resultado.perfis.append(_ler_perfil(media, perfil))
    except Exception as erro:  # noqa: BLE001
        resultado.mensagem = f"ONVIF respondeu, mas não listou os perfis: {explicar_erro_onvif(erro)}"

    resultado.tem_ptz, resultado.detalhes_ptz = _verificar_ptz(camera, resultado.perfis)
    return resultado


def conectar_onvif(ip: str, porta: int, usuario: str, senha: str, timeout_s: float):  # noqa: ANN202
    from onvif import ONVIFCamera
    from zeep.transports import Transport

    def criar(ajustar_relogio: bool):  # noqa: ANN202
        transporte = Transport(timeout=timeout_s, operation_timeout=timeout_s)
        return ONVIFCamera(ip, porta, usuario, senha, transport=transporte, adjust_time=ajustar_relogio)

    try:
        # adjust_time: compensa a diferença de relógio entre PC e câmera, que
        # faz a autenticação ONVIF falhar em câmeras com hora errada.
        return criar(True)
    except (AttributeError, TypeError, ValueError):
        # A câmera não informou a hora UTC; seguimos sem compensar o relógio.
        return criar(False)


def _ler_perfil(media, perfil) -> PerfilOnvif:  # noqa: ANN001
    dados = PerfilOnvif(nome=str(perfil.Name), token=str(perfil.token))
    codificador = getattr(perfil, "VideoEncoderConfiguration", None)
    if codificador is not None:
        if codificador.Resolution is not None:
            dados.largura, dados.altura = int(codificador.Resolution.Width), int(codificador.Resolution.Height)
        dados.codec = str(codificador.Encoding or "")
        if getattr(codificador, "RateControl", None) is not None:
            fps = float(codificador.RateControl.FrameRateLimit or 0)
            dados.fps = fps if 0 < fps <= 240 else 0.0  # o perfil JPEG das Xiongmai informa -3600
    dados.ptz = getattr(perfil, "PTZConfiguration", None) is not None
    try:
        uri = media.GetStreamUri(
            {
                "StreamSetup": {"Stream": "RTP-Unicast", "Transport": {"Protocol": "RTSP"}},
                "ProfileToken": perfil.token,
            }
        )
        dados.url = mascarar_url(str(uri.Uri))
    except Exception:  # noqa: BLE001
        log.debug("GetStreamUri falhou para o perfil %s", dados.nome, exc_info=True)
    return dados


def _verificar_ptz(camera, perfis: list[PerfilOnvif]) -> tuple[bool, str]:  # noqa: ANN001
    try:
        capacidades = camera.devicemgmt.GetCapabilities({"Category": "All"})
        anunciado = getattr(capacidades, "PTZ", None) is not None
    except Exception:  # noqa: BLE001
        anunciado = False
    if not anunciado and not any(p.ptz for p in perfis):
        return False, "a câmera não anuncia PTZ por ONVIF"
    try:
        nos = camera.create_ptz_service().GetNodes()
        continuo = any(
            getattr(getattr(no, "SupportedPTZSpaces", None), "ContinuousPanTiltVelocitySpace", None)
            for no in nos
        )
        detalhe = f"{len(nos)} nó(s) PTZ" + (", movimento contínuo suportado" if continuo else "")
        return True, detalhe
    except Exception as erro:  # noqa: BLE001
        return True, f"PTZ anunciado, mas o serviço não respondeu: {explicar_erro_onvif(erro)}"


def explicar_erro_onvif(erro: BaseException) -> str:
    texto = str(erro).lower()
    if any(p in texto for p in ("not authorized", "notauthorized", "unauthorized", "authority failure", "401")):
        return "usuário ou senha incorretos para o ONVIF"
    if "timed out" in texto or "timeout" in texto:
        return "a câmera não respondeu ao ONVIF dentro do tempo limite"
    if "refused" in texto:
        return "porta ONVIF fechada"
    if "connection" in texto:
        return "falha de conexão com o serviço ONVIF"
    resumo = str(erro).strip().splitlines()[0][:160] if str(erro).strip() else type(erro).__name__
    return f"erro no ONVIF: {resumo}"


# ==================================================================== orquestração
def diagnosticar(
    parametros: ParametrosDiagnostico,
    ao_progresso: Progresso | None = None,
    cancelar: threading.Event | None = None,
) -> Diagnostico:
    """Executa todas as etapas e devolve o relatório completo.

    ``ao_progresso`` é chamado no início (resultado ``None``) e no fim de cada etapa;
    ``cancelar`` permite interromper entre etapas (botão "Cancelar" da interface).
    """
    avisar: Progresso = ao_progresso or (lambda etapa, resultado: None)
    diag = Diagnostico(parametros)
    p = parametros

    def cancelado() -> bool:
        diag.cancelado = bool(cancelar and cancelar.is_set())
        return diag.cancelado

    # 1) Portas ---------------------------------------------------------------
    # As portas da própria câmera sempre entram, mesmo se não estiverem na lista
    # do config. Os testes rodam em paralelo: cada "porta fechada" pode levar ~2 s
    # no Windows, que repete a tentativa antes de desistir.
    usa_rede = not p.url_direta or "://" in p.url_direta
    if usa_rede and p.ip:
        if cancelado():
            return diag
        lista = list(dict.fromkeys([p.porta_rtsp, p.porta_onvif, *p.portas]))
        for porta in lista:
            avisar(f"porta:{porta}", None)
        with ThreadPoolExecutor(max_workers=len(lista), thread_name_prefix="porta") as executor:
            resultados = list(executor.map(lambda porta: testar_porta(p.ip, porta, p.timeout_s), lista))
        for resultado in resultados:
            diag.portas.append(resultado)
            avisar(f"porta:{resultado.porta}", resultado)

    # Se a porta já se mostrou inacessível, não adianta esperar o OpenCV/ONVIF
    # estourarem o tempo limite: pulamos a etapa e apontamos para a causa.
    portas = {r.porta: r for r in diag.portas}
    porta_rtsp = portas.get(p.porta_rtsp) if not p.url_direta else None
    porta_onvif = portas.get(p.porta_onvif)

    # 2 e 3) Streams --------------------------------------------------------
    try:
        streams = [(TipoStream.SUBSTREAM, "substream")]
        if p.testar_principal and not p.url_direta:
            if "stream" in variaveis_do_modelo(p.modelo_url):
                streams.append((TipoStream.PRINCIPAL, "principal"))
        for tipo, nome in streams:
            url = p.url(tipo)
            if cancelado():
                return diag
            if porta_rtsp is not None and not porta_rtsp.aberta:
                resultado_stream = ResultadoStream(
                    nome, mascarar_url(url), False, f"não testado: a porta RTSP {p.porta_rtsp} não está acessível"
                )
                setattr(diag, "sub" if nome == "substream" else "principal", resultado_stream)
                avisar(f"stream:{nome}", resultado_stream)
                continue
            if url.lower().startswith("rtsp://"):
                avisar(f"rtsp:{nome}", None)
                sonda = sondar_rtsp(url, p.usuario, p.senha, p.timeout_s)
                setattr(diag, "sonda_sub" if nome == "substream" else "sonda_principal", sonda)
                avisar(f"rtsp:{nome}", sonda)
                if cancelado():
                    return diag
            avisar(f"stream:{nome}", None)
            resultado_stream = testar_stream(url, nome, p.video_abertura_s, p.video_leitura_s, p.medir_fps_s)
            setattr(diag, "sub" if nome == "substream" else "principal", resultado_stream)
            avisar(f"stream:{nome}", resultado_stream)
    except ValueError as erro:  # modelo de URL inválido
        diag.sub = ResultadoStream("substream", "", False, str(erro))
        avisar("stream:substream", diag.sub)

    # 4) ONVIF ---------------------------------------------------------------
    if p.testar_onvif and usa_rede and p.ip and not cancelado():
        avisar("onvif", None)
        if porta_onvif is not None and not porta_onvif.aberta:
            diag.onvif = ResultadoOnvif(False, f"não testado: a porta ONVIF {p.porta_onvif} não está acessível")
        else:
            diag.onvif = testar_onvif(p.ip, p.porta_onvif, p.usuario, p.senha, p.timeout_s)
        avisar("onvif", diag.onvif)

    diag.conclusoes = concluir(diag)
    return diag


def concluir(diag: Diagnostico) -> list[str]:
    """Transforma os resultados em frases curtas sobre o que fazer."""
    p = diag.parametros
    frases: list[str] = []
    portas = {r.porta: r for r in diag.portas}

    if diag.portas and not any(r.host_respondeu for r in diag.portas):
        # Nada respondeu: as demais etapas falharam pela mesma causa.
        return [
            f"{p.ip} não respondeu em nenhuma porta: confira se a câmera está ligada, se o IP "
            "está certo (veja no app iCSee ou no roteador) e se o PC está na mesma rede."
        ]
    rtsp = portas.get(p.porta_rtsp)
    porta_rtsp_fechada = bool(rtsp and not rtsp.aberta and rtsp.host_respondeu)
    if porta_rtsp_fechada:
        frases.append(
            f"O aparelho em {p.ip} respondeu, mas a porta RTSP {p.porta_rtsp} está fechada: confira a porta RTSP."
        )

    sonda = diag.sonda_sub
    explicou_falha = porta_rtsp_fechada
    if sonda and sonda.status == 401:
        explicou_falha = True
        if p.senha:
            frases.append("A câmera recusou o login: confira usuário e senha (os mesmos do app iCSee).")
        else:
            frases.append("A câmera pede senha: informe a senha (a mesma do app iCSee).")
    elif sonda and sonda.status == 404:
        explicou_falha = True
        frases.append(f"O canal {p.canal} ou o modelo de URL não existe nesta câmera: tente o canal 0 ou o modelo ONVIF.")
    elif sonda and sonda.status == 503:
        explicou_falha = True
        frases.append("A câmera está no limite de conexões: feche o app iCSee/VLC e tente de novo.")

    if diag.sub and diag.sub.ok:
        frases.append(
            f"Substream OK ({diag.sub.largura}x{diag.sub.altura}). No cadastro use: porta {p.porta_rtsp}, "
            f"canal {p.canal} e o mesmo modelo de URL."
        )
        if sonda and not sonda.ok and sonda.status is None:
            frases.append("(A sonda RTSP falhou, mas o vídeo abriu — pode ignorar a falha da sonda.)")
    elif sonda and sonda.ok:
        frases.append(
            "A câmera aceitou a URL, mas o vídeo não abriu no OpenCV: tente transporte UDP "
            "(captura.transporte_rtsp no config.yaml) ou reinicie a câmera."
        )
    elif diag.sub and not explicou_falha:
        motivo = sonda.mensagem if sonda else diag.sub.mensagem
        frases.append(f"O vídeo não abriu ({motivo}): confira IP, porta, canal e modelo de URL, e reinicie a câmera.")

    principal = diag.principal
    if principal and not principal.ok and not porta_rtsp_fechada and diag.sub and diag.sub.ok:
        frases.append(
            "O stream principal (resolução máxima) não entregou imagem a tempo. Causas comuns: Wi-Fi "
            "fraco para essa resolução ou limite de tempo curto. Tente aumentar captura.timeout_abertura_s "
            "no config.yaml, aproximar a câmera do roteador ou reduzir a taxa de bits do stream principal "
            "no app iCSee. O mosaico e a IA usam o substream, que funcionou."
        )
    if principal and principal.ok:
        if principal.codec.lower() in ("hevc", "h265", "hev1", "hvc1"):
            frases.append(
                "O stream principal está em H.265 (HEVC): decodificar exige mais CPU. "
                "Se pesar, mude para H.264 no app iCSee."
            )
        if diag.sub and diag.sub.ok and (principal.largura, principal.altura) == (diag.sub.largura, diag.sub.altura):
            frases.append("Principal e substream têm a mesma resolução: confira se o canal/stream está certo.")

    # Latência: a PRIMEIRA resposta de uma câmera Wi-Fi costuma ser lenta (ela "acorda"
    # o rádio ou o codificador); só é problema de rede se até a resposta mais rápida for lenta.
    tempos = [r.tempo_ms for r in diag.portas if r.host_respondeu]
    tempos += [s.tempo_ms for s in (diag.sonda_sub, diag.sonda_principal) if s and s.status]
    if tempos and min(tempos) > 1000:
        frases.append(
            f"A câmera está lenta para responder (a resposta mais rápida levou {min(tempos) / 1000:.1f} s): "
            "sinal de Wi-Fi fraco ou rede congestionada."
        )
    elif tempos and max(tempos) > 1000:
        frases.append(
            f"A primeira resposta da câmera levou {max(tempos) / 1000:.1f} s e as seguintes foram rápidas: "
            "normal em câmeras Wi-Fi que economizam energia."
        )

    dvrip = portas.get(34567)
    if dvrip and dvrip.aberta:
        frases.append("Porta 34567 aberta: câmera Xiongmai/iCSee confirmada (PTZ via DVRIP também é possível).")

    onvif = diag.onvif
    if onvif:
        if onvif.ok and onvif.tem_ptz:
            frases.append(f"PTZ por ONVIF disponível na porta {p.porta_onvif}: marque 'possui motor' no cadastro.")
        elif onvif.ok:
            frases.append("ONVIF funciona, mas sem PTZ: câmera fixa (ou motor só pelo app iCSee).")
        else:
            frases.append(f"ONVIF não funcionou ({onvif.mensagem}): sem ele, o PTZ por ONVIF fica indisponível.")
    return frases
