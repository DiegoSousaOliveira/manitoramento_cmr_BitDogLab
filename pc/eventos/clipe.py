"""Gravação do clipe MP4: H.264 via PyAV (FFmpeg) e, se não der, OpenCV ``mp4v``.

- **H.264** é o formato que qualquer reprodutor abre e comprime muito bem.
  Usamos o ``libx264`` no modo ``veryfast`` (leve para a CPU).
- **Reserva**: se o PyAV ou o H.264 falharem, gravamos com o OpenCV no formato
  ``mp4v`` (arquivo maior; o player do app abre igual).
- **Taxa constante**: o clipe é gravado sempre a ``fps`` quadros por segundo. Assim o
  segundo N do vídeo é exatamente o instante N do evento — e as caixas do
  ``deteccoes.json`` aparecem no lugar certo no player.
- H.264 exige largura e altura PARES: a imagem é ajustada se preciso.
"""

from __future__ import annotations

import logging
from pathlib import Path

import cv2
import numpy as np

log = logging.getLogger(__name__)


class ErroClipe(Exception):
    pass


class EscritorClipe:
    """Escreve quadros BGR num MP4. Use ``escrever()`` e, no fim, ``fechar()``."""

    def __init__(self, caminho: Path, largura: int, altura: int, fps: float, qualidade: int) -> None:
        self.caminho = caminho
        self.largura = max(2, largura - largura % 2)
        self.altura = max(2, altura - altura % 2)
        self.fps = fps
        self.quadros = 0
        self.formato = ""
        self._av_container = None
        self._av_stream = None
        self._cv_writer: cv2.VideoWriter | None = None
        try:
            self._abrir_pyav(qualidade)
        except Exception as erro:  # noqa: BLE001 (PyAV ausente ou sem H.264: cai para o OpenCV)
            log.warning("H.264 (PyAV) indisponível, gravando em mp4v: %s", erro)
            self._abrir_opencv()

    def _abrir_pyav(self, qualidade: int) -> None:
        import av

        # "+faststart": o índice vai para o começo do arquivo (o player abre sem ler tudo).
        # No PyAV 19 essa opção só pode ser passada na abertura.
        container = av.open(str(self.caminho), mode="w", options={"movflags": "+faststart"})
        try:
            stream = container.add_stream("libx264", rate=round(self.fps))
            stream.width, stream.height = self.largura, self.altura
            stream.pix_fmt = "yuv420p"
            stream.options = {"preset": "veryfast", "crf": str(qualidade)}
        except Exception:
            container.close()
            raise
        self._av_container, self._av_stream = container, stream
        self.formato = "h264"

    def _abrir_opencv(self) -> None:
        writer = cv2.VideoWriter(
            str(self.caminho), cv2.CAP_FFMPEG, cv2.VideoWriter_fourcc(*"mp4v"), self.fps, (self.largura, self.altura)
        )
        if not writer.isOpened():
            raise ErroClipe(f"Não foi possível criar o clipe {self.caminho}")
        self._cv_writer = writer
        self.formato = "mp4v"

    def escrever(self, imagem_bgr: np.ndarray) -> None:
        altura, largura = imagem_bgr.shape[:2]
        if (largura, altura) != (self.largura, self.altura):
            imagem_bgr = cv2.resize(imagem_bgr, (self.largura, self.altura), interpolation=cv2.INTER_AREA)
        if self._av_stream is not None:
            import av

            quadro = av.VideoFrame.from_ndarray(np.ascontiguousarray(imagem_bgr), format="bgr24")
            for pacote in self._av_stream.encode(quadro):
                self._av_container.mux(pacote)  # type: ignore[union-attr]
        elif self._cv_writer is not None:
            self._cv_writer.write(imagem_bgr)
        self.quadros += 1

    def fechar(self) -> None:
        if self._av_stream is not None:
            for pacote in self._av_stream.encode():  # esvazia o que o codificador ainda segura
                self._av_container.mux(pacote)  # type: ignore[union-attr]
            self._av_container.close()  # type: ignore[union-attr]
            self._av_stream = None
        if self._cv_writer is not None:
            self._cv_writer.release()
            self._cv_writer = None

    @property
    def duracao_s(self) -> float:
        return self.quadros / self.fps if self.fps else 0.0
