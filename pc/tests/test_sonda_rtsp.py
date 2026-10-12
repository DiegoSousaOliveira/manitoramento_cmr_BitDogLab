"""Sonda RTSP e teste de portas, contra um servidor RTSP falso rodando no próprio PC."""

from __future__ import annotations

import hashlib
import re
import socket
import threading
import time
from collections.abc import Callable, Iterator

import pytest

from camera.rede import testar_porta
from camera.sonda_rtsp import sondar_rtsp
from tests.conftest import porta_livre

SDP_H264 = "v=0\r\nm=video 0 RTP/AVP 96\r\na=rtpmap:96 H264/90000\r\n"

#: Recebe o texto do pedido e devolve a resposta (bytes) ou None para fechar sem responder.
Responder = Callable[[str], bytes | None]


def resposta(status: str, cabecalhos: dict[str, str] | None = None, corpo: str = "", cseq: int = 1) -> bytes:
    linhas = [f"RTSP/1.0 {status}", f"CSeq: {cseq}", "Server: CameraFalsa/1.0"]
    linhas += [f"{k}: {v}" for k, v in (cabecalhos or {}).items()]
    if corpo:
        linhas.append(f"Content-Length: {len(corpo.encode())}")
    return ("\r\n".join(linhas) + "\r\n\r\n" + corpo).encode()


class ServidorRtspFalso:
    """Servidor TCP mínimo que responde a cada conexão com a função ``responder``."""

    def __init__(self, responder: Responder) -> None:
        self.responder = responder
        self.pedidos: list[str] = []
        self._socket = socket.socket()
        self._socket.bind(("127.0.0.1", 0))
        self._socket.listen()
        self._socket.settimeout(0.2)
        self.porta = int(self._socket.getsockname()[1])
        self._parar = threading.Event()
        self._thread = threading.Thread(target=self._atender, daemon=True)
        self._thread.start()

    def _atender(self) -> None:
        while not self._parar.is_set():
            try:
                conexao, _ = self._socket.accept()
            except TimeoutError:
                continue
            with conexao:
                conexao.settimeout(2)
                dados = b""
                while b"\r\n\r\n" not in dados:
                    pedaco = conexao.recv(4096)
                    if not pedaco:
                        break
                    dados += pedaco
                if not dados:
                    continue  # só abriu e fechou (teste de porta)
                pedido = dados.decode("utf-8")
                self.pedidos.append(pedido)
                saida = self.responder(pedido)
                if saida:
                    conexao.sendall(saida)

    def fechar(self) -> None:
        self._parar.set()
        self._thread.join()
        self._socket.close()

    def url(self, caminho: str = "/user=admin&password=&channel=1&stream=1.sdp") -> str:
        return f"rtsp://127.0.0.1:{self.porta}{caminho}"


@pytest.fixture
def servidor() -> Iterator[Callable[[Responder], ServidorRtspFalso]]:
    criados: list[ServidorRtspFalso] = []

    def criar(responder: Responder) -> ServidorRtspFalso:
        s = ServidorRtspFalso(responder)
        criados.append(s)
        return s

    yield criar
    for s in criados:
        s.fechar()


def _md5(texto: str) -> str:
    return hashlib.md5(texto.encode()).hexdigest()  # noqa: S324


def digest(usuario_ok: str, senha_ok: str) -> Responder:
    """Câmera que exige Digest: 401 sem credencial; 200 só com a resposta correta."""
    realm, nonce = "IPC", "abc123"

    def responder(pedido: str) -> bytes:
        cseq = int(re.search(r"CSeq: (\d+)", pedido).group(1))  # type: ignore[union-attr]
        auth = re.search(r"Authorization: Digest (.*)", pedido)
        if auth:
            p = dict(re.findall(r'(\w+)="([^"]*)"', auth.group(1)))
            metodo, uri = pedido.split(" ")[0], p["uri"]
            esperado = _md5(f"{_md5(f'{usuario_ok}:{realm}:{senha_ok}')}:{nonce}:{_md5(f'{metodo}:{uri}')}")
            if p["username"] == usuario_ok and p["response"] == esperado:
                return resposta("200 OK", {"Content-Type": "application/sdp"}, SDP_H264, cseq)
        return resposta("401 Unauthorized", {"WWW-Authenticate": f'Digest realm="{realm}", nonce="{nonce}"'}, cseq=cseq)

    return responder


# ----------------------------------------------------------------- sonda
def test_200_ok_com_codec(servidor) -> None:  # noqa: ANN001
    s = servidor(lambda pedido: resposta("200 OK", {"Content-Type": "application/sdp"}, SDP_H264))
    r = sondar_rtsp(s.url(), timeout_s=2)
    assert r.ok and r.status == 200
    assert r.codecs == ["H264"]
    assert r.servidor == "CameraFalsa/1.0"
    assert s.pedidos[0].startswith(f"DESCRIBE {s.url()} RTSP/1.0\r\n")


def test_digest_com_senha_certa(servidor) -> None:  # noqa: ANN001
    s = servidor(digest("admin", "1234"))
    r = sondar_rtsp(s.url(), "admin", "1234", timeout_s=2)
    assert r.ok and r.autenticacao == "Digest"
    assert len(s.pedidos) == 2  # tentou sem senha, recebeu o desafio e respondeu


def test_digest_com_senha_errada(servidor) -> None:  # noqa: ANN001
    s = servidor(digest("admin", "1234"))
    r = sondar_rtsp(s.url(), "admin", "errada", timeout_s=2)
    assert not r.ok and r.status == 401
    assert "senha incorretos" in r.mensagem


def test_login_na_url_tem_prioridade_e_nao_vai_na_linha_do_pedido(servidor) -> None:  # noqa: ANN001
    s = servidor(digest("joao", "segredo"))
    url = f"rtsp://joao:segredo@127.0.0.1:{s.porta}/onvif1"
    r = sondar_rtsp(url, "admin", "outra", timeout_s=2)
    assert r.ok
    assert all("segredo" not in pedido for pedido in s.pedidos)  # a senha não viaja aberta


def test_basic(servidor) -> None:  # noqa: ANN001
    def responder(pedido: str) -> bytes:
        if "Authorization: Basic YWRtaW46MTIz" in pedido:  # admin:123 em base64
            return resposta("200 OK", corpo=SDP_H264)
        return resposta("401 Unauthorized", {"WWW-Authenticate": 'Basic realm="x"'})

    r = sondar_rtsp(servidor(responder).url(), "admin", "123", timeout_s=2)
    assert r.ok and r.autenticacao == "Basic"


@pytest.mark.parametrize(
    ("status", "trecho"),
    [("404 Not Found", "canal"), ("400 Bad Request", "URL"), ("503 Service Unavailable", "limite de conexões")],
)
def test_erros_explicados(servidor, status: str, trecho: str) -> None:  # noqa: ANN001
    r = sondar_rtsp(servidor(lambda pedido: resposta(status)).url(), timeout_s=2)
    assert not r.ok and trecho in r.mensagem


def test_porta_que_nao_e_rtsp(servidor) -> None:  # noqa: ANN001
    s = servidor(lambda pedido: b"HTTP/1.1 400 Bad Request\r\n\r\n")
    assert "não é um servidor RTSP" in sondar_rtsp(s.url(), timeout_s=2).mensagem


def test_camera_fecha_a_conexao_sem_responder(servidor) -> None:  # noqa: ANN001
    assert "fechou a conexão" in sondar_rtsp(servidor(lambda pedido: None).url(), timeout_s=2).mensagem


def test_camera_aceita_mas_nao_responde(servidor) -> None:  # noqa: ANN001
    def lento(pedido: str) -> None:
        time.sleep(1.5)

    r = sondar_rtsp(servidor(lento).url(), timeout_s=0.5)
    assert "não respondeu ao comando RTSP" in r.mensagem


def test_porta_fechada() -> None:
    r = sondar_rtsp(f"rtsp://127.0.0.1:{porta_livre()}/x", timeout_s=5)
    assert not r.ok and "fechada" in r.mensagem


@pytest.mark.parametrize("url", ["http://1.2.3.4/x", "rtsp:///semhost", "rtsp://1.2.3.4:porta/x"])
def test_url_invalida(url: str) -> None:
    assert "URL inválida" in sondar_rtsp(url, timeout_s=1).mensagem


# ----------------------------------------------------------------- portas
def test_porta_aberta(servidor) -> None:  # noqa: ANN001
    r = testar_porta("127.0.0.1", servidor(lambda pedido: None).porta, 2)
    assert r.aberta and r.host_respondeu


def test_porta_fechada_mostra_que_o_aparelho_respondeu() -> None:
    r = testar_porta("127.0.0.1", porta_livre(), 5)
    assert not r.aberta and r.host_respondeu
    assert "fechada" in r.mensagem
