"""Cálculo da grade do mosaico (matemática pura, sem Qt — fácil de testar).

Os blocos são sempre 16:9. Dado o espaço disponível, escolhemos o maior tamanho
que cabe e centralizamos a grade; a última linha incompleta também fica centralizada.
"""

from __future__ import annotations

import math

#: Layouts do seletor da barra superior.
LAYOUTS: tuple[str, ...] = ("1", "2x2", "3x3", "4x4", "auto")
PROPORCAO = 16 / 9

Retangulo = tuple[int, int, int, int]  # x, y, largura, altura


def dimensoes_grade(quantidade: int, layout: str) -> tuple[int, int]:
    """(colunas, linhas) da grade.

    ``auto`` escolhe a grade mais "quadrada" que cabe todos: 2 -> 2x1, 3 e 4 -> 2x2,
    5 e 6 -> 3x2, 7 a 9 -> 3x3...
    """
    if layout == "auto":
        if quantidade <= 1:
            return 1, 1
        colunas = math.ceil(math.sqrt(quantidade))
        return colunas, math.ceil(quantidade / colunas)
    lado = {"1": 1, "2x2": 2, "3x3": 3, "4x4": 4}.get(layout)
    if lado is None:
        raise ValueError(f"Layout desconhecido: {layout!r} (use {', '.join(LAYOUTS)})")
    return lado, lado


def melhor_grade(quantidade: int, largura: int, altura: int, espaco: int) -> tuple[int, int]:
    """(colunas, linhas) que deixam os blocos 16:9 MAIORES nesta área.

    Ex.: 2 vistas numa janela larga ficam lado a lado; numa tela em pé, uma sobre a outra.
    """
    if quantidade <= 1 or largura <= 0 or altura <= 0:
        return 1, 1
    melhor, maior = (1, quantidade), 0.0
    for colunas in range(1, quantidade + 1):
        linhas = math.ceil(quantidade / colunas)
        bloco_l = min((largura - espaco * (colunas - 1)) / colunas, (altura - espaco * (linhas - 1)) / linhas * PROPORCAO)
        if bloco_l > maior + 0.5:
            melhor, maior = (colunas, linhas), bloco_l
    return melhor


def blocos_por_pagina(quantidade: int, layout: str) -> int:
    """Quantos blocos cabem numa página. No ``auto`` todos cabem numa só."""
    colunas, linhas = dimensoes_grade(quantidade, layout)
    return max(1, quantidade) if layout == "auto" else colunas * linhas


def total_paginas(quantidade: int, layout: str) -> int:
    return max(1, math.ceil(quantidade / blocos_por_pagina(quantidade, layout)))


def posicionar(
    quantidade: int, colunas: int, linhas: int, largura: int, altura: int, espaco: int
) -> list[Retangulo]:
    """Retângulos dos ``quantidade`` primeiros blocos, mantendo 16:9 e centralizando."""
    if quantidade <= 0 or largura <= 0 or altura <= 0:
        return []
    largura_celula = (largura - espaco * (colunas - 1)) / colunas
    altura_celula = (altura - espaco * (linhas - 1)) / linhas
    bloco_l = max(1.0, min(largura_celula, altura_celula * PROPORCAO))
    bloco_a = bloco_l / PROPORCAO

    linhas_usadas = math.ceil(quantidade / colunas)
    total_a = linhas_usadas * bloco_a + espaco * (linhas_usadas - 1)
    y0 = (altura - total_a) / 2

    retangulos: list[Retangulo] = []
    for indice in range(quantidade):
        linha, coluna = divmod(indice, colunas)
        na_linha = min(colunas, quantidade - linha * colunas)  # a última pode estar incompleta
        total_l = na_linha * bloco_l + espaco * (na_linha - 1)
        x0 = (largura - total_l) / 2
        x = x0 + coluna * (bloco_l + espaco)
        y = y0 + linha * (bloco_a + espaco)
        retangulos.append((round(x), round(y), round(bloco_l), round(bloco_a)))
    return retangulos
