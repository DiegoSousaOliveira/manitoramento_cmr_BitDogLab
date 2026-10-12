"""Cálculo da grade do mosaico (sem Qt)."""

from __future__ import annotations

import pytest

from ui.grade import blocos_por_pagina, dimensoes_grade, melhor_grade, posicionar, total_paginas


@pytest.mark.parametrize(
    ("quantidade", "esperado"),
    [(0, (1, 1)), (1, (1, 1)), (2, (2, 1)), (3, (2, 2)), (4, (2, 2)), (5, (3, 2)), (7, (3, 3)), (10, (4, 3))],
)
def test_auto_escolhe_a_grade_mais_quadrada(quantidade: int, esperado: tuple[int, int]) -> None:
    assert dimensoes_grade(quantidade, "auto") == esperado


def test_layouts_fixos() -> None:
    assert dimensoes_grade(3, "1") == (1, 1)
    assert dimensoes_grade(1, "3x3") == (3, 3)
    with pytest.raises(ValueError):
        dimensoes_grade(1, "5x5")


def test_paginas() -> None:
    assert blocos_por_pagina(10, "2x2") == 4 and total_paginas(10, "2x2") == 3
    assert total_paginas(10, "auto") == 1
    assert total_paginas(0, "4x4") == 1


def test_blocos_sempre_16_por_9_e_dentro_da_area() -> None:
    for largura, altura in [(1300, 800), (800, 1200), (1920, 1000)]:
        for quantidade in range(1, 10):
            colunas, linhas = dimensoes_grade(quantidade, "auto")
            for x, y, l, a in posicionar(quantidade, colunas, linhas, largura, altura, 12):
                assert abs(l / a - 16 / 9) < 0.02
                assert x >= 0 and y >= 0 and x + l <= largura + 1 and y + a <= altura + 1


def test_ultima_linha_incompleta_fica_centralizada() -> None:
    # 3 blocos em 2x2: o terceiro fica no meio da segunda linha
    (x1, _, l1, _), (x2, _, l2, _), (x3, _, l3, _) = posicionar(3, 2, 2, 1000, 600, 10)
    centro_linha_1 = (x1 + x2 + l2) / 2
    assert abs((x3 + l3 / 2) - centro_linha_1) <= 1


def test_melhor_grade_conforme_o_formato_da_area() -> None:
    assert melhor_grade(2, 1600, 700, 12) == (2, 1)  # janela larga: lado a lado
    assert melhor_grade(2, 800, 1200, 12) == (1, 2)  # tela em pé: uma sobre a outra
    assert melhor_grade(1, 800, 600, 12) == (1, 1)
    assert melhor_grade(4, 1600, 900, 12) == (2, 2)


def test_area_invalida_nao_quebra() -> None:
    assert posicionar(3, 2, 2, 0, 600, 10) == []
    assert posicionar(0, 1, 1, 100, 100, 10) == []
