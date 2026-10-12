"""Gera vídeos sintéticos para cadastrar "câmeras falsas" (fonte: arquivo).

Uso (de dentro da pasta ``pc/``):

    python tools/gerar_video_teste.py                  # videos/exemplo.mp4 (1280x720)
    python tools/gerar_video_teste.py --duas-lentes    # videos/duas_lentes.mp4 (640x720, como a iCSee XM232)
    python tools/gerar_video_teste.py --pessoas        # videos/pessoas.mp4 (para testar a IA)

Os dois primeiros são formas geométricas se movendo, com relógio — servem para
testar o mosaico, a reconexão e a divisão de lentes. O ``--pessoas`` passeia a
"câmera" sobre as fotos de exemplo do Ultralytics (pessoas e um ônibus), para a
IA ter o que reconhecer. Um clipe real (ex.: gravado pelo app iCSee) também serve.
"""

from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path

import cv2
import numpy as np

PASTA_PC = Path(__file__).resolve().parents[1]


def _cena(largura: int, altura: int, t: float, matiz: int, titulo: str) -> np.ndarray:
    """Um quadro: céu em gradiente, "rua", um "carro" indo e voltando e uma bola quicando."""
    y = np.linspace(0, 1, altura, dtype=np.float32)[:, None]
    base = np.array([40 + matiz, 30, 20], dtype=np.float32)  # BGR
    topo = np.array([90 + matiz, 70, 45], dtype=np.float32)
    quadro = (topo * (1 - y) + base * y)[:, None, :].repeat(largura, axis=1).reshape(altura, largura, 3)
    quadro = quadro.astype(np.uint8)

    rua_y = int(altura * 0.62)
    cv2.rectangle(quadro, (0, rua_y), (largura, altura), (48, 48, 52), -1)
    for x in range(-80, largura, 120):
        deslocado = int(x + (t * 90) % 120)
        cv2.rectangle(quadro, (deslocado, rua_y + int(altura * 0.17)), (deslocado + 60, rua_y + int(altura * 0.19)), (200, 200, 200), -1)

    ida = (math.sin(t * 0.6) + 1) / 2
    carro_x = int(ida * (largura - largura * 0.22))
    carro_l, carro_a = int(largura * 0.22), int(altura * 0.12)
    carro_y = rua_y + int(altura * 0.03)
    cv2.rectangle(quadro, (carro_x, carro_y), (carro_x + carro_l, carro_y + carro_a), (60, 80, 220), -1)
    cv2.rectangle(quadro, (carro_x + carro_l // 5, carro_y - carro_a // 2), (carro_x + carro_l * 4 // 5, carro_y), (70, 95, 235), -1)

    bola_x = int(largura * (0.15 + 0.7 * (math.sin(t * 1.3) + 1) / 2))
    bola_y = int(rua_y - abs(math.sin(t * 3.1)) * altura * 0.35) - 12
    cv2.circle(quadro, (bola_x, bola_y), max(6, altura // 30), (60, 200, 250), -1)

    escala = max(0.5, largura / 1280)
    cv2.putText(quadro, titulo, (int(16 * escala), int(36 * escala)), cv2.FONT_HERSHEY_SIMPLEX, 0.9 * escala, (255, 255, 255), 2, cv2.LINE_AA)
    relogio = f"{int(t // 60):02d}:{t % 60:05.2f}"
    cv2.putText(quadro, relogio, (largura - int(190 * escala), int(36 * escala)), cv2.FONT_HERSHEY_SIMPLEX, 0.9 * escala, (255, 255, 255), 2, cv2.LINE_AA)
    return quadro


def gerar_pessoas(destino: Path, segundos: float, fps: float) -> Path:
    """Passeia uma janela 16:9 sobre bus.jpg e zidane.jpg (fotos que vêm com o Ultralytics)."""
    from ultralytics.utils import ASSETS

    destino.parent.mkdir(parents=True, exist_ok=True)
    largura, altura = 1280, 720
    fotos = [cv2.imread(str(ASSETS / nome)) for nome in ("zidane.jpg", "bus.jpg")]
    gravador = cv2.VideoWriter(str(destino), cv2.VideoWriter_fourcc(*"mp4v"), fps, (largura, altura))
    total = int(segundos * fps)
    for i in range(total):
        t = i / total
        foto = fotos[0] if t < 0.5 else fotos[1]
        fase = (t % 0.5) * 2  # 0 -> 1 dentro de cada foto
        escala = max(largura / foto.shape[1], altura / foto.shape[0]) * (1.25 - 0.2 * math.sin(fase * math.pi))
        grande = cv2.resize(foto, None, fx=escala, fy=escala, interpolation=cv2.INTER_LINEAR)
        sobra_x, sobra_y = grande.shape[1] - largura, grande.shape[0] - altura
        x, y = int(sobra_x * fase), int(sobra_y * (0.5 + 0.5 * math.sin(fase * 2 * math.pi)) / 1)
        quadro = grande[min(y, sobra_y) : min(y, sobra_y) + altura, x : x + largura]
        gravador.write(np.ascontiguousarray(quadro))
    gravador.release()
    return destino


def gerar(destino: Path, duas_lentes: bool, segundos: float, fps: float) -> Path:
    destino.parent.mkdir(parents=True, exist_ok=True)
    largura, altura = (640, 720) if duas_lentes else (1280, 720)
    gravador = cv2.VideoWriter(str(destino), cv2.VideoWriter_fourcc(*"mp4v"), fps, (largura, altura))
    if not gravador.isOpened():
        raise OSError(f"Não foi possível criar {destino}")
    for i in range(int(segundos * fps)):
        t = i / fps
        if duas_lentes:  # duas imagens 640x360 empilhadas, como a iCSee XM232
            cima = _cena(640, 360, t, 0, "LENTE 1 - RUA")
            baixo = _cena(640, 360, t + 7.3, 60, "LENTE 2 - CALCADA")
            quadro = np.vstack([cima, baixo])
        else:
            quadro = _cena(largura, altura, t, 0, "VIDEO DE TESTE")
        gravador.write(quadro)
    gravador.release()
    return destino


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Gera vídeos sintéticos para câmeras falsas.")
    parser.add_argument("--duas-lentes", action="store_true", help="640x720 com duas lentes empilhadas")
    parser.add_argument("--pessoas", action="store_true", help="fotos com pessoas e ônibus em movimento (IA)")
    parser.add_argument("--segundos", type=float, default=20.0)
    parser.add_argument("--fps", type=float, default=15.0)
    parser.add_argument("--saida", help="arquivo de saída (padrão: videos/exemplo.mp4 ou videos/duas_lentes.mp4)")
    args = parser.parse_args(argv)
    if args.pessoas:
        destino = Path(args.saida) if args.saida else PASTA_PC / "videos/pessoas.mp4"
        caminho = gerar_pessoas(destino, args.segundos, args.fps)
    else:
        padrao = "videos/duas_lentes.mp4" if args.duas_lentes else "videos/exemplo.mp4"
        destino = Path(args.saida) if args.saida else PASTA_PC / padrao
        caminho = gerar(destino, args.duas_lentes, args.segundos, args.fps)
    sys.stdout.write(f"Vídeo gerado: {caminho}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
