"""Coletor de imagens para o treinamento (prepara a Fase 9 — aprendizado ativo).

Guarda, em ``dados/treino/pendentes/``, imagens em que a IA provavelmente errou
ou ficou em dúvida, para você revisar depois e o modelo aprender com a SUA rua:

- ``duvida``: objeto com confiança baixa (entre ``ia.conf_deteccao`` e ``treino.conf_duvida``);
- ``troca_classe``: o mesmo objeto mudou de classe (ex.: CARRO -> CAMINHAO);
- ``desconhecido``: algo se mexeu sem a IA reconhecer (pode ser um objeto não visto);
- ``amostra``: uma imagem comum a cada ``treino.amostra_min`` minutos (dia, noite, chuva...).

Cada imagem vai com um ``.txt`` no formato do YOLO (pré-rótulos com os ids COCO,
para você só corrigir) e um ``.json`` com o motivo e as detecções. Há limites por
hora, intervalo mínimo e espaço máximo em disco. Tudo fica só no seu computador.
"""

from __future__ import annotations

import json
import logging
import time
from collections import deque
from datetime import datetime
from pathlib import Path

from camera.captura import salvar_jpg
from core.config import Config
from core.modelos import COCO_DAS_CLASSES, DESCONHECIDO, Camera
from vision.detector import ResultadoVista

log = logging.getLogger(__name__)
Chave = tuple[int, int]


class ColetorTreino:
    def __init__(self, config: Config) -> None:
        self.recarregar(config)
        self._ultimo: dict[Chave, float] = {}
        self._salvos: dict[Chave, deque[float]] = {}
        self._ultima_amostra: dict[Chave, float] = {}
        self._classes: dict[Chave, dict[int, str]] = {}
        self._bytes: int | None = None

    def recarregar(self, config: Config) -> None:
        """(Re)lê os ajustes de ``treino`` (vale na hora, sem reiniciar o app)."""
        c = config
        self.ativo = bool(c.obter("treino.coletar"))
        self.pasta = c.pasta_dados / "treino" / "pendentes"
        self.conf_deteccao = float(c.obter("ia.conf_deteccao"))
        self.conf_duvida = float(c.obter("treino.conf_duvida"))
        self.intervalo_s = float(c.obter("treino.intervalo_min_s"))
        self.max_por_hora = int(c.obter("treino.max_por_hora"))
        self.amostra_s = float(c.obter("treino.amostra_min")) * 60
        self.limite_bytes = float(c.obter("treino.limite_gb")) * 1024**3

    def motivo(self, resultado: ResultadoVista) -> str | None:
        """Por que esta imagem interessa (ou ``None``). Também memoriza as classes por ID."""
        memoria = self._classes.setdefault(resultado.chave, {})
        troca = False
        for d in resultado.deteccoes:
            if d.id_rastreio is None or d.ausente_s > 0 or d.classe == DESCONHECIDO:
                continue
            anterior = memoria.get(d.id_rastreio)
            if anterior is not None and anterior != d.classe:
                troca = True
            memoria[d.id_rastreio] = d.classe
        if len(memoria) > 500:
            memoria.clear()
        if troca:
            return "troca_classe"
        if resultado.inferiu:
            if any(
                d.origem == "ia" and d.ausente_s == 0 and self.conf_deteccao <= d.confianca < self.conf_duvida
                for d in resultado.deteccoes
            ):
                return "duvida"
            if any(d.classe == DESCONHECIDO and d.ausente_s == 0 for d in resultado.deteccoes):
                return "desconhecido"
        if time.monotonic() - self._ultima_amostra.get(resultado.chave, -1e9) >= self.amostra_s:
            return "amostra"
        return None

    def processar(self, resultado: ResultadoVista, camera: Camera) -> Path | None:
        """Guarda a imagem se ela for interessante e os limites permitirem. Devolve o caminho."""
        if not self.ativo or resultado.imagem is None:
            return None
        motivo = self.motivo(resultado)
        if motivo is None:
            return None
        agora = time.monotonic()
        chave = resultado.chave
        salvos = self._salvos.setdefault(chave, deque())
        while salvos and agora - salvos[0] > 3600:
            salvos.popleft()
        if agora - self._ultimo.get(chave, -1e9) < self.intervalo_s or len(salvos) >= self.max_por_hora:
            return None
        if self._espaco_usado() >= self.limite_bytes:
            return None

        self.pasta.mkdir(parents=True, exist_ok=True)
        base = self.pasta / f"{camera.codigo}_v{chave[1]}_{datetime.now():%Y%m%d_%H%M%S}_{motivo}"
        linhas = []
        for d in resultado.deteccoes:
            if d.origem != "ia" or d.ausente_s > 0 or d.classe not in COCO_DAS_CLASSES:
                continue
            x1, y1, x2, y2 = d.caixa
            linhas.append(
                f"{COCO_DAS_CLASSES[d.classe]} {(x1 + x2) / 2:.6f} {(y1 + y2) / 2:.6f} {x2 - x1:.6f} {y2 - y1:.6f}"
            )
        base.with_suffix(".txt").write_text("\n".join(linhas) + ("\n" if linhas else ""), encoding="utf-8")
        meta = {
            "motivo": motivo,
            "camera": camera.nome,
            "vista": chave[1],
            "data": datetime.now().isoformat(timespec="seconds"),
            "resolucao": list(resultado.resolucao),
            "deteccoes": [
                {"classe": d.classe, "id": d.id_rastreio, "conf": round(d.confianca, 3),
                 "caixa": [round(v, 5) for v in d.caixa], "origem": d.origem}
                for d in resultado.deteccoes if d.ausente_s == 0
            ],
        }
        base.with_suffix(".json").write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
        # A foto por ÚLTIMO: o treino (outro processo) só "enxerga" a amostra pelo .jpg — assim ela
        # nunca aparece sem o .txt e o .json.
        salvar_jpg(base.with_suffix(".jpg"), resultado.imagem, qualidade=92)

        self._ultimo[chave] = agora
        salvos.append(agora)
        if motivo == "amostra":
            self._ultima_amostra[chave] = agora
        tamanho = sum(base.with_suffix(s).stat().st_size for s in (".jpg", ".txt", ".json"))
        self._bytes = (self._bytes or 0) + tamanho
        log.debug("Treino: imagem guardada (%s): %s", motivo, base.name)
        return base.with_suffix(".jpg")

    def _espaco_usado(self) -> int:
        if self._bytes is None:  # mede uma vez; depois só soma o que gravamos
            self._bytes = (
                sum(f.stat().st_size for f in self.pasta.rglob("*") if f.is_file()) if self.pasta.is_dir() else 0
            )
        return self._bytes
