"""Comparar dois modelos nas imagens de VALIDAÇÃO (que nunca entram no treino).

A "nota" é o **mAP** (de 0 a 1): mede ao mesmo tempo se a IA achou os objetos e se as caixas
ficaram no lugar certo. O modelo novo só substitui o atual se:

1. a nota geral subir pelo menos ``margem_minima`` (melhorar de verdade, não por acaso); e
2. nenhuma classe piorar mais que ``queda_maxima_classe`` (ex.: melhorar CARRO e piorar PESSOA
   não vale a troca).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from core.modelos import CLASSES_COCO


@dataclass(frozen=True)
class Metricas:
    map: float  # mAP50-95: a nota geral (0 a 1)
    map50: float  # mAP com 50% de sobreposição (mais "generosa")
    por_classe: dict[str, float] = field(default_factory=dict)  # mAP50-95 das classes presentes

    def para_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def de_dict(cls, dados: dict[str, Any] | None) -> Metricas | None:
        if not dados:
            return None
        return cls(float(dados["map"]), float(dados["map50"]), {str(k): float(v) for k, v in dados["por_classe"].items()})


def metricas_do_ultralytics(resultado: Any) -> Metricas:
    """``model.val()`` do Ultralytics -> ``Metricas`` (só as nossas classes)."""
    caixa = resultado.box
    por_classe = {
        CLASSES_COCO[int(indice)]: float(caixa.ap[posicao])
        for posicao, indice in enumerate(caixa.ap_class_index)
        if int(indice) in CLASSES_COCO
    }
    return Metricas(float(caixa.map), float(caixa.map50), por_classe)


def decidir_promocao(atual: Metricas, novo: Metricas, margem: float, queda_maxima: float) -> tuple[bool, str]:
    """(trocar?, explicação em português)."""
    if novo.map < atual.map + margem:
        return False, (f"não melhorou o suficiente: nota {atual.map:.3f} → {novo.map:.3f} "
                       f"(precisa de pelo menos +{margem:.3f})")
    piores = [
        f"{classe} ({atual.por_classe[classe]:.2f} → {novo.por_classe.get(classe, 0.0):.2f})"
        for classe in sorted(atual.por_classe)
        if novo.por_classe.get(classe, 0.0) < atual.por_classe[classe] - queda_maxima
    ]
    if piores:
        return False, f"melhorou no geral, mas piorou em {', '.join(piores)}"
    return True, f"melhorou: nota {atual.map:.3f} → {novo.map:.3f}"
