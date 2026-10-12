"""Histórico das rodadas de treino (``dados/treino/historico.json``): o que aconteceu em cada uma."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from treino.avaliacao import Metricas


@dataclass
class Rodada:
    inicio: str  # ISO 8601
    fim: str = ""
    modelo_base: str = ""  # o modelo que estava em uso (e serviu de ponto de partida)
    modelo_novo: str = ""  # arquivo em pc/modelos/ (só se foi aprovado)
    imagens_treino: int = 0
    imagens_validacao: int = 0
    rotuladas_agora: int = 0  # pelo professor nesta rodada
    para_revisar: int = 0
    atual: dict[str, Any] | None = None  # Metricas do modelo base
    novo: dict[str, Any] | None = None  # Metricas do modelo treinado
    promovido: bool = False
    motivo: str = ""  # explicação em português
    erro: str = ""
    epocas: int = 0
    duracao_s: float = 0.0
    extras: dict[str, Any] = field(default_factory=dict)

    @property
    def metricas_atual(self) -> Metricas | None:
        return Metricas.de_dict(self.atual)

    @property
    def metricas_novo(self) -> Metricas | None:
        return Metricas.de_dict(self.novo)

    @property
    def treinou(self) -> bool:
        return self.novo is not None

    def para_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def de_dict(cls, dados: dict[str, Any]) -> Rodada:
        conhecidos = {k: v for k, v in dados.items() if k in cls.__dataclass_fields__}
        return cls(**conhecidos)


class Historico:
    def __init__(self, arquivo: Path) -> None:
        self.arquivo = arquivo

    def rodadas(self) -> list[Rodada]:
        try:
            dados = json.loads(self.arquivo.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        return [Rodada.de_dict(item) for item in dados if isinstance(item, dict)]

    def adicionar(self, rodada: Rodada) -> None:
        rodadas = self.rodadas() + [rodada]
        self.arquivo.parent.mkdir(parents=True, exist_ok=True)
        temporario = self.arquivo.with_suffix(".tmp")
        temporario.write_text(json.dumps([r.para_dict() for r in rodadas], ensure_ascii=False, indent=1),
                              encoding="utf-8")
        os.replace(temporario, self.arquivo)  # troca atômica: nunca fica um arquivo pela metade

    def ultima_que_treinou(self) -> Rodada | None:
        return next((r for r in reversed(self.rodadas()) if r.treinou), None)

    def da_modelo(self, nome: str) -> Rodada | None:
        """A rodada que gerou o modelo ``nome`` (para mostrar a nota dele)."""
        return next((r for r in reversed(self.rodadas()) if r.modelo_novo == nome), None)
