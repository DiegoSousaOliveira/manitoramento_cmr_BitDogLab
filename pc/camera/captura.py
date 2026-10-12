"""Abertura de fontes de vídeo com OpenCV (RTSP, arquivo ou webcam).

Centralizado aqui para que o diagnóstico e as threads de leitura (Fase 1)
abram as câmeras exatamente do mesmo jeito.
"""

from __future__ import annotations

import ctypes
import os
import sys

import cv2
import numpy as np


def definir_variavel_ffmpeg(nome: str, valor: str, sobrescrever: bool = True) -> None:
    """Define uma variável de ambiente de forma que o FFmpeg do OpenCV consiga lê-la.

    Pegadinha do Windows (verificada com um servidor RTSP de teste): o FFmpeg do
    OpenCV fica num plugin separado (``opencv_videoio_ffmpeg*.dll``) compilado com o
    runtime C antigo (``msvcrt.dll``), que tem SUA PRÓPRIA cópia das variáveis de
    ambiente. O que o Python grava em ``os.environ`` vai para outro runtime (o UCRT)
    e o plugin nunca vê. Por isso, no Windows, gravamos também direto no ``msvcrt``.
    No Linux o FFmpeg está embutido no OpenCV e ``os.environ`` basta.

    Com ``sobrescrever=False``, um valor já definido no terminal (antes de abrir o
    Python) é respeitado.
    """
    if not sobrescrever and nome in os.environ:
        return
    os.environ[nome] = valor
    if sys.platform == "win32":
        ctypes.cdll.msvcrt._putenv(f"{nome}={valor}".encode("ascii"))


def configurar_opencv(transporte_rtsp: str = "tcp") -> None:
    """Ajustes globais do OpenCV/FFmpeg. Chame uma vez, no início, antes de abrir câmeras.

    - Transporte RTSP: o OpenCV usa o FFmpeg por baixo, e o FFmpeg lê suas opções
      da variável ``OPENCV_FFMPEG_CAPTURE_OPTIONS``. TCP é o padrão aqui: no Wi-Fi,
      o UDP perde pacotes e a imagem fica cheia de "borrões" cinzas.
    - Silencia os avisos que o OpenCV e o FFmpeg escrevem direto no terminal
      (ex.: "Stream timeout triggered"): nós já tratamos esses casos e registramos
      mensagens melhores no nosso log.
    """
    if transporte_rtsp not in ("tcp", "udp"):
        raise ValueError(f"Transporte RTSP inválido: {transporte_rtsp!r} (use tcp ou udp)")
    definir_variavel_ffmpeg("OPENCV_FFMPEG_CAPTURE_OPTIONS", f"rtsp_transport;{transporte_rtsp}")
    definir_variavel_ffmpeg("OPENCV_FFMPEG_LOGLEVEL", "-8", sobrescrever=False)  # -8 = silêncio total
    definir_variavel_ffmpeg("OPENCV_LOG_LEVEL", "ERROR", sobrescrever=False)
    cv2.utils.logging.setLogLevel(cv2.utils.logging.LOG_LEVEL_ERROR)


def abrir_captura(fonte: str | int, abertura_s: float, leitura_s: float | None = None) -> cv2.VideoCapture:
    """Abre uma URL RTSP, um arquivo de vídeo ou uma webcam (índice inteiro).

    - ``abertura_s``: tempo máximo para conectar E descobrir o formato do vídeo.
      O FFmpeg espera o primeiro quadro-chave (imagem completa), que em alta
      resolução pelo Wi-Fi pode demorar vários segundos.
    - ``leitura_s``: tempo máximo de cada leitura de quadro (padrão: igual à abertura).

    Atenção: se o tempo estourar no MEIO de um pacote, a conexão RTSP/TCP fica
    dessincronizada e só chega "lixo" depois (erros "error while decoding MB").
    Por isso os limites não devem ser apertados demais.
    """
    if isinstance(fonte, int):
        if sys.platform == "win32":
            api = cv2.CAP_DSHOW  # abre webcams bem mais rápido que o padrão (MSMF)
        elif sys.platform.startswith("linux"):
            api = cv2.CAP_V4L2
        else:
            api = cv2.CAP_ANY
        return cv2.VideoCapture(fonte, api)
    parametros = [
        cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, int(abertura_s * 1000),
        cv2.CAP_PROP_READ_TIMEOUT_MSEC, int((leitura_s or abertura_s) * 1000),
    ]
    return cv2.VideoCapture(fonte, cv2.CAP_FFMPEG, parametros)


def codec_da_captura(captura: cv2.VideoCapture) -> str:
    """Nome do codec (ex.: ``h264``, ``hevc``) a partir do código FOURCC do OpenCV."""
    fourcc = int(captura.get(cv2.CAP_PROP_FOURCC))
    texto = "".join(chr((fourcc >> (8 * i)) & 0xFF) for i in range(4)).replace("\x00", "").strip()
    return texto if texto.isprintable() else ""


def salvar_jpg(caminho: os.PathLike[str] | str, quadro: np.ndarray, qualidade: int = 90) -> None:
    """Grava um JPG aceitando acentos no caminho.

    ``cv2.imwrite`` falha no Windows com caminhos não-ASCII (ex.: "Câmera"),
    então codificamos na memória e gravamos com o NumPy.
    """
    ok, dados = cv2.imencode(".jpg", quadro, [cv2.IMWRITE_JPEG_QUALITY, qualidade])
    if not ok:
        raise OSError(f"Não foi possível codificar a imagem JPG para {caminho}")
    dados.tofile(str(caminho))
