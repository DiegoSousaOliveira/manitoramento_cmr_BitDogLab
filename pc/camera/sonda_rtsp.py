"""Sonda RTSP: conversa diretamente com a câmera para descobrir POR QUE um stream não abre.

O OpenCV só diz "abriu" ou "não abriu". Para dar mensagens claras (senha errada?
URL inválida? câmera ocupada?) enviamos nós mesmos o primeiro comando do RTSP,
o ``DESCRIBE``, e lemos o código de resposta — igual ao HTTP:

    C->S: DESCRIBE rtsp://192.168.0.50:554/user=admin&password=&channel=1&stream=1.sdp RTSP/1.0
          CSeq: 1
    S->C: RTSP/1.0 200 OK          (ou 401 Unauthorized, 404 Not Found...)
          Content-Type: application/sdp
          ...descrição do vídeo (SDP), de onde tiramos o codec (H264/H265)...

Se a câmera pedir autenticação (401) e houver usuário/senha, respondemos ao
desafio Basic ou Digest (o mesmo que o VLC/FFmpeg fazem) e tentamos de novo.
"""

from __future__ import annotations

import base64
import hashlib
import re
import secrets
import socket
import time
from dataclasses import dataclass, field
from urllib.parse import unquote, urlsplit, urlunsplit

from camera.rede import descrever_erro_conexao

_TAMANHO_MAX_CABECALHO = 64 * 1024
_TAMANHO_MAX_CORPO = 64 * 1024
_PARAMETRO_AUTH = re.compile(r'(\w+)\s*=\s*(?:"([^"]*)"|([^,\s]+))')
_CODEC_SDP = re.compile(r"^a=rtpmap:\d+\s+([A-Za-z0-9-]+)/", re.MULTILINE)

#: Significado dos códigos de resposta mais comuns, em português.
MENSAGENS_STATUS: dict[int, str] = {
    400: "a câmera recusou a URL (requisição inválida): confira o modelo de URL",
    401: "usuário ou senha incorretos",
    403: "acesso negado: esse usuário não tem permissão para ver o vídeo",
    404: "caminho não encontrado: canal ou modelo de URL inválido para esta câmera",
    405: "a câmera não aceita esse comando RTSP",
    453: "a câmera está sem banda/recursos para mais um stream",
    454: "sessão não encontrada",
    500: "erro interno na câmera (reiniciá-la costuma resolver)",
    503: "câmera ocupada: limite de conexões simultâneas atingido (feche o app iCSee/VLC e tente de novo)",
}


class _NaoEhRtsp(Exception):
    """A porta respondeu, mas não com o protocolo RTSP."""


@dataclass
class RespostaRtsp:
    status: int
    motivo: str
    cabecalhos: dict[str, list[str]]
    corpo: str

    def cabecalho(self, nome: str) -> str:
        valores = self.cabecalhos.get(nome.lower())
        return valores[0] if valores else ""


@dataclass
class ResultadoSonda:
    ok: bool
    mensagem: str
    status: int | None = None
    codecs: list[str] = field(default_factory=list)  # do SDP, ex.: ["H264"]
    servidor: str = ""  # cabeçalho "Server", ajuda a identificar o fabricante
    autenticacao: str = ""  # "", "Basic" ou "Digest"
    tempo_ms: float = 0.0  # tempo da última requisição (conectar + resposta)


def sondar_rtsp(url: str, usuario: str = "", senha: str = "", timeout_s: float = 5.0) -> ResultadoSonda:
    """Envia ``DESCRIBE`` para a URL e interpreta a resposta.

    Se a URL tiver ``usuario:senha@``, esses dados têm prioridade sobre os parâmetros.
    """
    try:
        partes = urlsplit(url)
        porta = partes.port or 554
    except ValueError:
        return ResultadoSonda(False, "URL inválida (porta fora do formato)")
    if partes.scheme.lower() != "rtsp" or not partes.hostname:
        return ResultadoSonda(False, "URL inválida: deve começar com rtsp:// e conter o IP")

    host = partes.hostname
    if partes.username is not None:
        usuario, senha = unquote(partes.username), unquote(partes.password or "")
    netloc = f"{host}:{partes.port}" if partes.port else host
    url_requisicao = urlunsplit((partes.scheme, netloc, partes.path or "/", partes.query, ""))

    try:
        inicio = time.perf_counter()
        resposta = _requisitar(host, porta, "DESCRIBE", url_requisicao, 1, {}, timeout_s)
        autenticacao = ""
        if resposta.status == 401 and (usuario or senha):
            desafios = resposta.cabecalhos.get("www-authenticate", [])
            cabecalho, autenticacao = _responder_desafio(desafios, "DESCRIBE", url_requisicao, usuario, senha)
            if cabecalho:
                # Nova conexão: várias câmeras fecham o socket depois de um 401.
                inicio = time.perf_counter()
                resposta = _requisitar(
                    host, porta, "DESCRIBE", url_requisicao, 2, {"Authorization": cabecalho}, timeout_s
                )
        tempo_ms = (time.perf_counter() - inicio) * 1000
    except _NaoEhRtsp:
        return ResultadoSonda(False, f"a porta {porta} respondeu, mas não é um servidor RTSP")
    except _ConexaoFechada:
        return ResultadoSonda(
            False,
            "a câmera fechou a conexão sem responder "
            "(senha errada em alguns modelos, ou limite de conexões atingido)",
        )
    except _SemRespostaRtsp:
        return ResultadoSonda(False, f"a porta {porta} aceitou a conexão, mas não respondeu ao comando RTSP")
    except OSError as erro:
        return ResultadoSonda(False, descrever_erro_conexao(erro, host, porta))

    servidor = resposta.cabecalho("server")
    if resposta.status == 200:
        codecs = list(dict.fromkeys(c.upper() for c in _CODEC_SDP.findall(resposta.corpo)))
        detalhe = f"codec {'/'.join(codecs)}" if codecs else "sem descrição de codec"
        return ResultadoSonda(
            True, f"RTSP respondeu 200 OK em {tempo_ms:.0f} ms ({detalhe})", 200, codecs, servidor, autenticacao, tempo_ms
        )

    explicacao = MENSAGENS_STATUS.get(resposta.status, "resposta inesperada da câmera")
    return ResultadoSonda(
        False,
        f"RTSP {resposta.status} {resposta.motivo}: {explicacao}",
        resposta.status,
        servidor=servidor,
        autenticacao=autenticacao,
        tempo_ms=tempo_ms,
    )


# ==================================================================== detalhes
class _ConexaoFechada(Exception):
    pass


class _SemRespostaRtsp(Exception):
    pass


def _requisitar(
    host: str, porta: int, metodo: str, url: str, cseq: int, extras: dict[str, str], timeout_s: float
) -> RespostaRtsp:
    linhas = [f"{metodo} {url} RTSP/1.0", f"CSeq: {cseq}", "User-Agent: SentinelaCasa", "Accept: application/sdp"]
    linhas += [f"{nome}: {valor}" for nome, valor in extras.items()]
    pedido = ("\r\n".join(linhas) + "\r\n\r\n").encode("utf-8")

    with socket.create_connection((host, porta), timeout=timeout_s) as conexao:
        conexao.settimeout(timeout_s)
        conexao.sendall(pedido)
        try:
            return _ler_resposta(conexao)
        except TimeoutError as erro:
            raise _SemRespostaRtsp from erro
        except (ConnectionResetError, ConnectionAbortedError) as erro:
            raise _ConexaoFechada from erro


def _ler_resposta(conexao: socket.socket) -> RespostaRtsp:
    dados = b""
    while b"\r\n\r\n" not in dados:
        pedaco = conexao.recv(4096)
        if not pedaco:
            if not dados:
                raise _ConexaoFechada
            break
        dados += pedaco
        if len(dados) > _TAMANHO_MAX_CABECALHO:
            raise _NaoEhRtsp
    cabeca, _, corpo = dados.partition(b"\r\n\r\n")
    linhas = cabeca.decode("latin-1").split("\r\n")

    partes = linhas[0].split(" ", 2)
    if len(partes) < 2 or not partes[0].startswith("RTSP/") or not partes[1].isdigit():
        raise _NaoEhRtsp
    status, motivo = int(partes[1]), (partes[2] if len(partes) > 2 else "")

    cabecalhos: dict[str, list[str]] = {}
    for linha in linhas[1:]:
        nome, separador, valor = linha.partition(":")
        if separador:
            cabecalhos.setdefault(nome.strip().lower(), []).append(valor.strip())

    try:
        tamanho = min(int(cabecalhos.get("content-length", ["0"])[0]), _TAMANHO_MAX_CORPO)
    except ValueError:
        tamanho = 0
    while len(corpo) < tamanho:
        try:
            pedaco = conexao.recv(4096)
        except TimeoutError:
            break
        if not pedaco:
            break
        corpo += pedaco
    return RespostaRtsp(status, motivo, cabecalhos, corpo.decode("utf-8", errors="replace"))


def _responder_desafio(
    desafios: list[str], metodo: str, uri: str, usuario: str, senha: str
) -> tuple[str, str]:
    """Monta o cabeçalho ``Authorization``. Prefere Digest (a senha não viaja aberta)."""
    ordenados = sorted(desafios, key=lambda d: not d.lower().startswith("digest"))
    for desafio in ordenados:
        tipo, _, resto = desafio.partition(" ")
        if tipo.lower() == "digest":
            return _digest(dict(_parametros(resto)), metodo, uri, usuario, senha), "Digest"
        if tipo.lower() == "basic":
            credencial = base64.b64encode(f"{usuario}:{senha}".encode()).decode("ascii")
            return f"Basic {credencial}", "Basic"
    return "", ""


def _parametros(texto: str) -> list[tuple[str, str]]:
    """``realm="x", nonce="y"`` -> ``[("realm", "x"), ("nonce", "y")]``."""
    return [
        (m.group(1).lower(), m.group(2) if m.group(2) is not None else m.group(3))
        for m in _PARAMETRO_AUTH.finditer(texto)
    ]


def _md5(texto: str) -> str:
    return hashlib.md5(texto.encode("utf-8")).hexdigest()  # noqa: S324 (exigido pelo RFC 2617)


def _digest(p: dict[str, str], metodo: str, uri: str, usuario: str, senha: str) -> str:
    """Resposta Digest (RFC 2617), como as câmeras IP esperam."""
    realm, nonce = p.get("realm", ""), p.get("nonce", "")
    ha1 = _md5(f"{usuario}:{realm}:{senha}")
    ha2 = _md5(f"{metodo}:{uri}")
    campos = [f'username="{usuario}"', f'realm="{realm}"', f'nonce="{nonce}"', f'uri="{uri}"']
    if "auth" in [q.strip() for q in p.get("qop", "").split(",")]:
        nc, cnonce = "00000001", secrets.token_hex(8)
        resposta = _md5(f"{ha1}:{nonce}:{nc}:{cnonce}:auth:{ha2}")
        campos += ["qop=auth", f"nc={nc}", f'cnonce="{cnonce}"']
    else:
        resposta = _md5(f"{ha1}:{nonce}:{ha2}")
    campos.append(f'response="{resposta}"')
    if "opaque" in p:
        campos.append(f'opaque="{p["opaque"]}"')
    return "Digest " + ", ".join(campos)
