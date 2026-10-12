"""Imagens de treino: onde ficam, rótulos e a separação treino x validação.

Pastas dentro de ``dados/treino/``:

- ``pendentes/`` — guardadas pelo coletor (desde a Fase 4), ainda sem rótulo confiável;
- ``rotuladas/`` — prontas para o treino: rotuladas pelo PROFESSOR (com certeza) ou por você;
- ``revisar/`` — o professor ficou em dúvida: esperam a sua revisão e NÃO entram no treino
  (um objeto sem caixa numa imagem de treino ensinaria a IA a ignorá-lo).

Cada amostra são três arquivos com o mesmo nome: ``.jpg``, ``.txt`` (rótulos no formato do
YOLO: ``classe_coco cx cy largura altura``, tudo de 0 a 1) e ``.json`` (dados: câmera, data,
motivo, quem rotulou...).

**Validação separada por blocos de 1 hora:** fotos da mesma hora são quase iguais (câmera
parada). Se uma fosse para o treino e a "gêmea" para a validação, a prova viraria cola. Então
um bloco inteiro (data + hora) vai para um lado só — e o lado fica gravado no ``.json``.
"""

from __future__ import annotations

import json
import logging
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

Rotulo = tuple[int, float, float, float, float]  # classe COCO, centro x, centro y, largura, altura

PENDENTES, ROTULADAS, REVISAR = "pendentes", "rotuladas", "revisar"
EXTENSOES = (".jpg", ".txt", ".json")


@dataclass(frozen=True)
class Amostra:
    imagem: Path

    @property
    def nome(self) -> str:
        return self.imagem.stem

    @property
    def txt(self) -> Path:
        return self.imagem.with_suffix(".txt")

    @property
    def json(self) -> Path:
        return self.imagem.with_suffix(".json")

    def rotulos(self) -> list[Rotulo]:
        if not self.txt.is_file():
            return []
        rotulos: list[Rotulo] = []
        for linha in self.txt.read_text(encoding="utf-8").splitlines():
            partes = linha.split()
            if len(partes) == 5:
                rotulos.append((int(partes[0]), *(float(v) for v in partes[1:])))  # type: ignore[arg-type]
        return rotulos

    def gravar_rotulos(self, rotulos: list[Rotulo]) -> None:
        linhas = [f"{c} {cx:.6f} {cy:.6f} {w:.6f} {h:.6f}" for c, cx, cy, w, h in rotulos]
        self.txt.write_text("\n".join(linhas) + ("\n" if linhas else ""), encoding="utf-8")

    def meta(self) -> dict[str, Any]:
        try:
            return dict(json.loads(self.json.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            return {}

    def atualizar_meta(self, **valores: Any) -> None:
        meta = self.meta()
        meta.update(valores)
        self.json.write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")

    @property
    def data(self) -> datetime:
        """Quando a foto foi tirada (do .json; se faltar, a data do arquivo)."""
        texto = self.meta().get("data")
        try:
            return datetime.fromisoformat(str(texto))
        except ValueError:
            return datetime.fromtimestamp(self.imagem.stat().st_mtime)


def rotulo_de_caixa(classe_coco: int, caixa: tuple[float, float, float, float]) -> Rotulo:
    """Caixa (x1, y1, x2, y2) normalizada -> rótulo do YOLO (centro e tamanho)."""
    x1, y1, x2, y2 = caixa
    return classe_coco, (x1 + x2) / 2, (y1 + y2) / 2, x2 - x1, y2 - y1


def caixa_de_rotulo(rotulo: Rotulo) -> tuple[float, float, float, float]:
    _c, cx, cy, w, h = rotulo
    return cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2


class ConjuntoTreino:
    """As imagens de treino de ``dados/treino/``."""

    def __init__(self, pasta: Path) -> None:
        self.pasta = pasta

    def amostras(self, onde: str) -> list[Amostra]:
        pasta = self.pasta / onde
        return [Amostra(p) for p in sorted(pasta.glob("*.jpg"))] if pasta.is_dir() else []

    def contar(self) -> dict[str, int]:
        return {onde: len(self.amostras(onde)) for onde in (PENDENTES, ROTULADAS, REVISAR)}

    def mover(self, amostra: Amostra, onde: str) -> Amostra:
        destino = self.pasta / onde
        destino.mkdir(parents=True, exist_ok=True)
        for extensao in EXTENSOES:
            origem = amostra.imagem.with_suffix(extensao)
            if origem.exists():
                shutil.move(str(origem), str(destino / origem.name))
        return Amostra(destino / amostra.imagem.name)

    @staticmethod
    def apagar(amostra: Amostra) -> None:
        for extensao in EXTENSOES:
            amostra.imagem.with_suffix(extensao).unlink(missing_ok=True)

    @staticmethod
    def bloco(amostra: Amostra, meta: dict[str, Any] | None = None) -> str:
        """Bloco de 1 hora: data + hora + câmera + vista (fotos "gêmeas" ficam juntas)."""
        meta = amostra.meta() if meta is None else meta
        return f"{amostra.data:%Y%m%d%H}|{meta.get('camera', '')}|{meta.get('vista', '')}"

    def separar(self, porcento: float) -> tuple[list[Amostra], list[Amostra]]:
        """Treino x validação por blocos de 1 hora, com a escolha GUARDADA no ``.json``.

        - bloco que já tem lado: as amostras novas dele vão para o mesmo lado;
        - bloco novo: vai para a validação enquanto ela estiver abaixo de ``porcento``.
        Guardar a escolha garante que uma imagem de validação nunca vire de treino depois
        (senão a "prova" dos próximos modelos teria questões que eles já viram).
        """
        amostras = sorted(self.amostras(ROTULADAS), key=lambda a: a.data)
        metas = {a.imagem: a.meta() for a in amostras}
        lado_do_bloco: dict[str, str] = {}
        for amostra in amostras:
            lado = metas[amostra.imagem].get("lado")
            if lado in ("treino", "validacao"):
                lado_do_bloco.setdefault(self.bloco(amostra, metas[amostra.imagem]), lado)
        contagem = {"treino": 0, "validacao": 0}
        for amostra in amostras:
            if metas[amostra.imagem].get("lado") in contagem:
                contagem[metas[amostra.imagem]["lado"]] += 1
        treino: list[Amostra] = []
        validacao: list[Amostra] = []
        for amostra in amostras:
            meta = metas[amostra.imagem]
            lado = meta.get("lado")
            if lado not in contagem:
                bloco = self.bloco(amostra, meta)
                lado = lado_do_bloco.get(bloco)
                if lado is None:
                    total = contagem["treino"] + contagem["validacao"]
                    lado = "validacao" if contagem["validacao"] < total * porcento / 100 or total == 0 else "treino"
                    lado_do_bloco[bloco] = lado
                contagem[lado] += 1
                amostra.atualizar_meta(lado=lado)
            (validacao if lado == "validacao" else treino).append(amostra)
        return treino, validacao

    def escrever_listas(self, destino: Path, porcento: float, nomes: dict[int, str]) -> tuple[Path, int, int]:
        """Arquivos que o Ultralytics entende: listas de imagens + ``dados.yaml``.

        Os rótulos ficam ao lado de cada imagem (mesmo nome, ``.txt``): é onde o Ultralytics
        procura quando o caminho não tem uma pasta "images".
        """
        destino.mkdir(parents=True, exist_ok=True)
        treino, validacao = self.separar(porcento)
        (destino / "treino.txt").write_text("\n".join(str(a.imagem.resolve()) for a in treino) + "\n", encoding="utf-8")
        (destino / "validacao.txt").write_text(
            "\n".join(str(a.imagem.resolve()) for a in validacao) + "\n", encoding="utf-8"
        )
        linhas = [
            f"path: {destino.resolve().as_posix()}",
            "train: treino.txt",
            "val: validacao.txt",
            "names:",
            *(f"  {indice}: {json.dumps(nome)}" for indice, nome in sorted(nomes.items())),
        ]
        yaml = destino / "dados.yaml"
        yaml.write_text("\n".join(linhas) + "\n", encoding="utf-8")
        return yaml, len(treino), len(validacao)

    def tamanho(self) -> int:
        return sum(p.stat().st_size for p in self.pasta.rglob("*") if p.is_file()) if self.pasta.exists() else 0

    def aplicar_limite(self, limite_bytes: float) -> int:
        """Passou do espaço? Apaga as amostras mais antigas: primeiro as do professor, depois as
        pendentes; as revisadas por você ficam por último (são as mais valiosas)."""
        removidas = 0
        excesso = self.tamanho() - limite_bytes
        if excesso <= 0:
            return 0
        candidatas = sorted(
            self.amostras(ROTULADAS) + self.amostras(PENDENTES) + self.amostras(REVISAR),
            key=lambda a: (a.meta().get("rotulo") == "humano", a.data),
        )
        for amostra in candidatas:
            if excesso <= 0:
                break
            tamanho = sum(amostra.imagem.with_suffix(e).stat().st_size
                          for e in EXTENSOES if amostra.imagem.with_suffix(e).exists())
            self.apagar(amostra)
            excesso -= tamanho
            removidas += 1
        if removidas:
            log.info("Treino: %d imagem(ns) antiga(s) apagada(s) para respeitar o limite de espaço", removidas)
        return removidas
