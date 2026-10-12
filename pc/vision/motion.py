"""Detecção de movimento por subtração de fundo (MOG2).

Como funciona (para quem está começando):
- O MOG2 observa os quadros e aprende, pixel a pixel, como é o "fundo" da cena
  (rua, muro, calçada). Tudo que foge desse padrão vira "primeiro plano" (branco
  na máscara): algo se mexendo.
- Trabalhamos numa versão reduzida da imagem (ex.: 320 px de largura): é muito
  mais rápido e o ruído fino some.
- A máscara passa por "abertura" (apaga pontinhos isolados) e "fechamento" (junta
  pedaços do mesmo objeto). Cada mancha grande o bastante vira uma região.
- Sombras: o MOG2 marca sombras com cinza (127); nós ignoramos — só o branco (255) conta.
- Nos primeiros quadros o MOG2 ainda está aprendendo o fundo e marcaria tudo como
  movimento; por isso existe um "aquecimento" em que nenhuma região é devolvida.

Usos: regiões com movimento sem objeto reconhecido viram DESCONHECIDO (Fase 3),
o início de eventos (Fase 4) e a opção "só rodar a IA com movimento".
"""

from __future__ import annotations

from dataclasses import dataclass, field

import cv2
import numpy as np

Caixa = tuple[float, float, float, float]  # x1, y1, x2, y2 normalizados


@dataclass(frozen=True)
class ResultadoMovimento:
    regioes: list[Caixa] = field(default_factory=list)  # manchas grandes o bastante
    mascara: np.ndarray | None = None  # imagem reduzida, 0 = parado, 255 = movimento
    fracao: float = 0.0  # fração do quadro em movimento (0 a 1)
    aquecendo: bool = False

    @property
    def tem_movimento(self) -> bool:
        return bool(self.regioes)


@dataclass(frozen=True)
class AjustesMovimento:
    largura_trabalho: int  # largura da imagem reduzida (px)
    historico: int  # quantos quadros o MOG2 usa para aprender o fundo
    limiar: float  # varThreshold do MOG2: maior = menos sensível
    area_minima: float  # fração do quadro para uma mancha contar como movimento
    aquecimento_quadros: int  # quadros iniciais ignorados (aprendendo o fundo)


class DetectorMovimento:
    """Um por vista: cada câmera/lente tem seu próprio "fundo"."""

    def __init__(self, ajustes: AjustesMovimento) -> None:
        self.ajustes = ajustes
        self._mog2 = cv2.createBackgroundSubtractorMOG2(
            history=ajustes.historico, varThreshold=ajustes.limiar, detectShadows=True
        )
        self._nucleo_abrir = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
        self._nucleo_fechar = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (9, 9))
        self._quadros = 0

    def reiniciar(self) -> None:
        """Esquece o fundo e volta ao aquecimento (ex.: a câmera girou: a cena inteira mudou)."""
        self._mog2 = cv2.createBackgroundSubtractorMOG2(
            history=self.ajustes.historico, varThreshold=self.ajustes.limiar, detectShadows=True
        )
        self._quadros = 0

    def processar(self, imagem_bgr: np.ndarray) -> ResultadoMovimento:
        altura, largura = imagem_bgr.shape[:2]
        escala = self.ajustes.largura_trabalho / largura
        pequena = cv2.resize(
            imagem_bgr,
            (self.ajustes.largura_trabalho, max(1, round(altura * escala))),
            interpolation=cv2.INTER_AREA,
        )
        pequena = cv2.GaussianBlur(pequena, (5, 5), 0)
        bruta = self._mog2.apply(pequena)
        self._quadros += 1

        _, mascara = cv2.threshold(bruta, 200, 255, cv2.THRESH_BINARY)  # descarta sombras (127)
        mascara = cv2.morphologyEx(mascara, cv2.MORPH_OPEN, self._nucleo_abrir)
        mascara = cv2.morphologyEx(mascara, cv2.MORPH_CLOSE, self._nucleo_fechar)

        if self._quadros <= self.ajustes.aquecimento_quadros:
            return ResultadoMovimento(mascara=np.zeros_like(mascara), aquecendo=True)

        alt_p, larg_p = mascara.shape[:2]
        area_total = float(alt_p * larg_p)
        contornos, _ = cv2.findContours(mascara, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        regioes: list[Caixa] = []
        for contorno in contornos:
            if cv2.contourArea(contorno) < self.ajustes.area_minima * area_total:
                continue
            x, y, w, h = cv2.boundingRect(contorno)
            regioes.append((x / larg_p, y / alt_p, (x + w) / larg_p, (y + h) / alt_p))
        fracao = float(np.count_nonzero(mascara)) / area_total
        return ResultadoMovimento(regioes=regioes, mascara=mascara, fracao=fracao)


def interseccao_sobre_area(a: Caixa, b: Caixa) -> float:
    """Quanto da caixa ``a`` está coberto pela caixa ``b`` (0 a 1)."""
    largura = min(a[2], b[2]) - max(a[0], b[0])
    altura = min(a[3], b[3]) - max(a[1], b[1])
    if largura <= 0 or altura <= 0:
        return 0.0
    area_a = (a[2] - a[0]) * (a[3] - a[1])
    return (largura * altura) / area_a if area_a > 0 else 0.0
