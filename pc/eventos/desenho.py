"""Desenho das caixas de detecção numa imagem (para a ``foto.jpg`` do evento), com OpenCV.

A interface desenha as caixas com Qt; aqui é a mesma ideia, gravada na imagem.
"""

from __future__ import annotations

import cv2
import numpy as np

from core.modelos import CORES_CLASSES, Deteccao


def cor_bgr(hexa: str) -> tuple[int, int, int]:
    """``"#FF8A3D"`` -> (61, 138, 255) — o OpenCV usa a ordem azul, verde, vermelho."""
    hexa = hexa.lstrip("#")
    r, g, b = (int(hexa[i : i + 2], 16) for i in (0, 2, 4))
    return b, g, r


def desenhar_caixas(imagem: np.ndarray, deteccoes: tuple[Deteccao, ...] | list[Deteccao]) -> np.ndarray:
    """Devolve uma CÓPIA da imagem com as caixas e rótulos ("PESSOA #7 0.87")."""
    saida = imagem.copy()
    altura, largura = saida.shape[:2]
    escala = max(0.5, largura / 1280)
    espessura = max(1, round(2 * escala))
    for d in deteccoes:
        if d.ausente_s > 0:
            continue  # a foto mostra só o que a IA viu naquele quadro
        cor = cor_bgr(CORES_CLASSES.get(d.classe, "#94A3B8"))
        x1, y1, x2, y2 = (round(v) for v in (d.caixa[0] * largura, d.caixa[1] * altura, d.caixa[2] * largura, d.caixa[3] * altura))
        cv2.rectangle(saida, (x1, y1), (x2, y2), cor, espessura, cv2.LINE_8)  # traço nítido
        rotulo = d.classe + (f" #{d.id_rastreio}" if d.id_rastreio is not None else "")
        if d.origem == "ia":
            rotulo += f" {d.confianca:.2f}"
        (tw, th), base = cv2.getTextSize(rotulo, cv2.FONT_HERSHEY_SIMPLEX, 0.5 * escala, espessura)
        topo = y1 - th - base - 4 if y1 - th - base - 4 >= 0 else y1
        cv2.rectangle(saida, (x1, topo), (x1 + tw + 6, topo + th + base + 4), cor, -1)
        luminancia = 0.299 * cor[2] + 0.587 * cor[1] + 0.114 * cor[0]
        texto = (20, 18, 15) if luminancia > 150 else (255, 255, 255)
        cv2.putText(saida, rotulo, (x1 + 3, topo + th + 2), cv2.FONT_HERSHEY_SIMPLEX, 0.5 * escala, texto, espessura, cv2.LINE_AA)
    return saida


#: Vermelho dos alertas (o mesmo ``alerta`` do tema escuro).
VERMELHO_ALERTA = "#EF4444"


def recorte_do_objeto(
    imagem: np.ndarray, caixa: tuple[float, float, float, float], largura: int = 320, altura: int = 180
) -> np.ndarray:
    """Miniatura ``largura`` x ``altura`` centrada no objeto, com a caixa em vermelho.

    Num toast pequeno, a imagem inteira deixaria a pessoa do tamanho de um ponto; por
    isso recortamos em volta dela (2,5x o tamanho da caixa, no mínimo 1/4 da imagem),
    sem sair das bordas e sem distorcer.
    """
    alt_img, larg_img = imagem.shape[:2]
    x1, y1, x2, y2 = caixa[0] * larg_img, caixa[1] * alt_img, caixa[2] * larg_img, caixa[3] * alt_img
    proporcao = largura / altura
    larg_rec = max((x2 - x1) * 2.5, (y2 - y1) * 2.5 * proporcao, larg_img / 4)
    larg_rec = min(larg_rec, larg_img, alt_img * proporcao)
    alt_rec = larg_rec / proporcao
    cx, cy = (x1 + x2) / 2, (y1 + y2) / 2
    esq = round(min(max(cx - larg_rec / 2, 0), larg_img - larg_rec))
    topo = round(min(max(cy - alt_rec / 2, 0), alt_img - alt_rec))
    recorte = imagem[topo : topo + max(1, round(alt_rec)), esq : esq + max(1, round(larg_rec))]
    escala = largura / recorte.shape[1]
    saida = cv2.resize(recorte, (largura, altura), interpolation=cv2.INTER_AREA if escala < 1 else cv2.INTER_LINEAR)
    escala_y = altura / recorte.shape[0]
    p1 = (round((x1 - esq) * escala), round((y1 - topo) * escala_y))
    p2 = (round((x2 - esq) * escala), round((y2 - topo) * escala_y))
    cv2.rectangle(saida, p1, p2, cor_bgr(VERMELHO_ALERTA), 2, cv2.LINE_AA)
    return saida
