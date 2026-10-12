"""Buffer circular de quadros: os últimos N segundos de vídeo de uma câmera.

Para que serve: um evento começa quando algo se mexe, mas o clipe precisa
mostrar também os segundos ANTES disso (``pre_evento_s``). Como não dá para
voltar no tempo, guardamos sempre os últimos segundos.

Custo: o OpenCV entrega um array NOVO a cada quadro (verificado), então o
buffer só guarda referências — nada é copiado. Memória ~ resolução x FPS x
segundos (substream 640x720, 12 FPS, 8 s ≈ 130 MB por câmera).
"""

from __future__ import annotations

import threading
from collections import deque

from camera.stream import Quadro


class BufferQuadros:
    """Guarda os quadros dos últimos ``segundos`` (seguro para várias threads)."""

    def __init__(self, segundos: float) -> None:
        self.segundos = segundos
        self._quadros: deque[Quadro] = deque()
        self._trava = threading.Lock()

    def adicionar(self, quadro: Quadro) -> None:
        """Chamado pelo leitor a cada quadro (precisa ser rápido)."""
        with self._trava:
            self._quadros.append(quadro)
            while self._quadros and quadro.instante - self._quadros[0].instante > self.segundos:
                self._quadros.popleft()

    def ate(self, instante: float) -> Quadro | None:
        """O quadro mais recente capturado até ``instante`` (ou ``None`` se não houver)."""
        with self._trava:
            for quadro in reversed(self._quadros):
                if quadro.instante <= instante:
                    return quadro
        return None

    def mais_antigo(self) -> float | None:
        with self._trava:
            return self._quadros[0].instante if self._quadros else None

    def __len__(self) -> int:
        with self._trava:
            return len(self._quadros)
