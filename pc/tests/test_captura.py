"""Configuração do OpenCV/FFmpeg: as opções precisam chegar de fato ao FFmpeg.

Regressão de um bug real: no Windows, opções gravadas só em ``os.environ`` não chegam
ao plugin FFmpeg do OpenCV (ele usa outro runtime C). Os testes abaixo conferem o
efeito do lado de fora: o que o FFmpeg pede a um servidor RTSP e o que ele escreve no terminal.
"""

from __future__ import annotations

import os
import re
import socket
import subprocess
import sys
import textwrap
import threading
from collections.abc import Iterator

import cv2
import pytest

from camera.captura import abrir_captura, configurar_opencv
from core.config import PASTA_PC

_SDP = (
    "v=0\r\no=- 0 0 IN IP4 127.0.0.1\r\ns=teste\r\nc=IN IP4 0.0.0.0\r\nt=0 0\r\n"
    "m=video 0 RTP/AVP 96\r\na=rtpmap:96 H264/90000\r\na=control:trackID=0\r\n"
)


class ServidorSetup:
    """Servidor RTSP que responde ao DESCRIBE e anota o cabeçalho Transport do SETUP."""

    def __init__(self) -> None:
        self.transportes: list[str] = []
        self._socket = socket.socket()
        self._socket.bind(("127.0.0.1", 0))
        self._socket.listen()
        self.porta = int(self._socket.getsockname()[1])
        threading.Thread(target=self._atender, daemon=True).start()

    def _atender(self) -> None:
        try:
            conexao, _ = self._socket.accept()
        except OSError:
            return
        with conexao:
            dados = b""
            while True:
                pedaco = conexao.recv(4096)
                if not pedaco:
                    return
                dados += pedaco
                while b"\r\n\r\n" in dados:
                    pedido, dados = dados.split(b"\r\n\r\n", 1)
                    texto = pedido.decode()
                    metodo = texto.split(" ")[0]
                    cseq = re.search(r"CSeq: *(\d+)", texto).group(1)  # type: ignore[union-attr]
                    if metodo == "DESCRIBE":
                        resposta = (
                            f"RTSP/1.0 200 OK\r\nCSeq: {cseq}\r\nContent-Type: application/sdp\r\n"
                            f"Content-Base: rtsp://127.0.0.1:{self.porta}/x/\r\nContent-Length: {len(_SDP)}\r\n\r\n{_SDP}"
                        )
                    elif metodo == "SETUP":
                        self.transportes.append(re.search(r"Transport: *(.*)", texto).group(1).strip())  # type: ignore[union-attr]
                        resposta = f"RTSP/1.0 461 Unsupported Transport\r\nCSeq: {cseq}\r\n\r\n"
                    else:
                        resposta = f"RTSP/1.0 200 OK\r\nCSeq: {cseq}\r\nPublic: DESCRIBE, SETUP, PLAY\r\n\r\n"
                    conexao.sendall(resposta.encode())

    def fechar(self) -> None:
        self._socket.close()


@pytest.fixture
def restaurar_tcp() -> Iterator[None]:
    yield
    configurar_opencv("tcp")


@pytest.mark.parametrize(("transporte", "esperado"), [("udp", "RTP/AVP/UDP"), ("tcp", "RTP/AVP/TCP")])
def test_transporte_rtsp_chega_ao_ffmpeg(transporte: str, esperado: str, restaurar_tcp: None) -> None:
    configurar_opencv(transporte)
    servidor = ServidorSetup()
    try:
        abrir_captura(f"rtsp://127.0.0.1:{servidor.porta}/x", 3).release()
    finally:
        servidor.fechar()
    assert servidor.transportes, "o FFmpeg nem chegou ao SETUP"
    assert servidor.transportes[0].startswith(esperado)


def test_transporte_invalido() -> None:
    with pytest.raises(ValueError):
        configurar_opencv("http")


def test_avisos_do_ffmpeg_nao_aparecem_no_terminal() -> None:
    """Câmera que aceita a conexão e trava: o OpenCV avisaria "Stream timeout triggered".

    Roda em outro processo porque o plugin lê o nível de log uma única vez.
    """
    script = textwrap.dedent(
        """
        import socket, threading
        from camera.captura import abrir_captura, configurar_opencv
        configurar_opencv("tcp")
        servidor = socket.socket(); servidor.bind(("127.0.0.1", 0)); servidor.listen()
        conexoes = []
        threading.Thread(target=lambda: conexoes.append(servidor.accept()), daemon=True).start()
        captura = abrir_captura(f"rtsp://127.0.0.1:{servidor.getsockname()[1]}/x", 1.0)
        print("aberto" if captura.isOpened() else "fechado")
        """
    )
    # Ambiente limpo: sem isso o filho herdaria as variáveis OPENCV_* que outros testes
    # já definiram neste processo e o teste passaria mesmo com o bug.
    ambiente = {k: v for k, v in os.environ.items() if not k.upper().startswith("OPENCV_")}
    resultado = subprocess.run(
        [sys.executable, "-c", script], cwd=PASTA_PC, env=ambiente,
        capture_output=True, text=True, timeout=60, check=False,
    )
    assert resultado.stdout.strip() == "fechado"
    assert "WARN" not in resultado.stderr, resultado.stderr
    assert "timeout" not in resultado.stderr.lower(), resultado.stderr


def test_abrir_arquivo_com_limites_separados(video_teste) -> None:  # noqa: ANN001
    captura = abrir_captura(str(video_teste), abertura_s=2, leitura_s=1)
    try:
        ok, quadro = captura.read()
        assert ok and quadro.shape == (48, 64, 3)
    finally:
        captura.release()
    assert cv2.utils.logging.getLogLevel() is not None
