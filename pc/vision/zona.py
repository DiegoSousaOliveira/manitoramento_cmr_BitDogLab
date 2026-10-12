"""Zona de vigilância: polígono em coordenadas normalizadas (0 a 1).

Sem zona definida (``None``), vale o quadro inteiro. Usada pela regra de "parado
na zona" (``vision/dwell.py``), pelos eventos (``eventos.area: zona``) e pelo editor
de zona da Tela da câmera (que usa ``indice_insercao`` e ``validar_zona``).
"""

from __future__ import annotations

import math

Ponto = tuple[float, float]
Poligono = list[Ponto]

#: Área mínima de uma zona (fração da imagem): menor que isso é quase uma linha.
AREA_MINIMA = 0.0005


def ponto_no_poligono(ponto: Ponto, poligono: Poligono | None) -> bool:
    """O ponto está dentro do polígono? (algoritmo do "raio": conta quantas bordas cruza)."""
    if not poligono or len(poligono) < 3:
        return True  # sem zona = quadro inteiro
    x, y = ponto
    dentro = False
    j = len(poligono) - 1
    for i, (xi, yi) in enumerate(poligono):
        xj, yj = poligono[j]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi) + xi:
            dentro = not dentro
        j = i
    return dentro


def centro(caixa: tuple[float, float, float, float]) -> Ponto:
    return (caixa[0] + caixa[2]) / 2, (caixa[1] + caixa[3]) / 2


def distancia(a: Ponto, b: Ponto, proporcao: float = 1.0) -> float:
    """Distância medida em LARGURAS da imagem.

    As coordenadas são normalizadas: x vai de 0 a 1 na largura e y de 0 a 1 na
    altura. Numa imagem 16:9, andar 0,1 em y é andar bem menos pixels que 0,1 em x;
    ``proporcao`` = altura / largura corrige isso (y vira "larguras").
    """
    return math.hypot(a[0] - b[0], (a[1] - b[1]) * proporcao)


def indice_insercao(poligono: Poligono, ponto: Ponto, proporcao: float = 1.0) -> int:
    """Em que posição do polígono um ponto novo deve entrar.

    Com menos de 3 pontos, vai no fim. Depois, entra na aresta que MENOS aumenta o
    contorno — é o que a pessoa espera ao clicar perto de um lado da zona, e evita
    que as linhas se cruzem.
    """
    n = len(poligono)
    if n < 3:
        return n
    melhor, indice = math.inf, n
    for i in range(n):
        a, b = poligono[i], poligono[(i + 1) % n]
        custo = distancia(a, ponto, proporcao) + distancia(ponto, b, proporcao) - distancia(a, b, proporcao)
        if custo < melhor:
            melhor, indice = custo, i + 1
    return indice


def area_poligono(poligono: Poligono) -> float:
    """Área (fração da imagem) pela fórmula do "cadarço" (shoelace)."""
    soma = 0.0
    for i, (x1, y1) in enumerate(poligono):
        x2, y2 = poligono[(i + 1) % len(poligono)]
        soma += x1 * y2 - x2 * y1
    return abs(soma) / 2


def _cruzam(p1: Ponto, p2: Ponto, p3: Ponto, p4: Ponto) -> bool:
    """O segmento p1-p2 cruza o segmento p3-p4 (cruzamento "de verdade", em X)?"""

    def lado(a: Ponto, b: Ponto, c: Ponto) -> float:
        return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])

    return lado(p3, p4, p1) * lado(p3, p4, p2) < 0 and lado(p1, p2, p3) * lado(p1, p2, p4) < 0


def poligono_simples(poligono: Poligono) -> bool:
    """True se nenhuma aresta cruza outra (zona em forma de "laço" confunde o dentro/fora)."""
    n = len(poligono)
    for i in range(n):
        a1, a2 = poligono[i], poligono[(i + 1) % n]
        for j in range(i + 2, n):
            if i == 0 and j == n - 1:
                continue  # arestas vizinhas (a última encosta na primeira)
            if _cruzam(a1, a2, poligono[j], poligono[(j + 1) % n]):
                return False
    return True


def validar_zona(poligono: Poligono | None) -> str | None:
    """Mensagem em português se a zona não serve; ``None`` se está ok (vazia = quadro inteiro)."""
    if not poligono:
        return None
    if len(poligono) < 3:
        return "A zona precisa de pelo menos 3 pontos (ou nenhum, para vigiar o quadro inteiro)."
    if any(not (0.0 <= x <= 1.0 and 0.0 <= y <= 1.0) for x, y in poligono):
        return "Todos os pontos precisam ficar dentro da imagem."
    if not poligono_simples(poligono):
        return "As linhas da zona se cruzam. Arraste os pontos para desfazer o cruzamento."
    if area_poligono(poligono) < AREA_MINIMA:
        return "A zona está pequena demais. Afaste os pontos."
    return None
