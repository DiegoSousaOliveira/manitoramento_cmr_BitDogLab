"""Retenção: apaga eventos antigos e mantém a pasta de eventos abaixo do limite de disco.

Regras (config ``eventos``):
1. eventos com mais de ``retencao_dias`` dias são apagados (arquivos + registro);
2. se ainda assim a pasta passar de ``limite_disco_gb``, os mais antigos vão saindo.
"""

from __future__ import annotations

import logging
import shutil
from datetime import datetime, timedelta
from pathlib import Path

from core.config import Config
from core.modelos import Camera, Evento
from core.servico_cameras import ServicoCameras

log = logging.getLogger(__name__)


def pasta_do_evento(servico: ServicoCameras, camera: Camera, evento: Evento) -> Path:
    """``dados/eventos/<câmera>/<AAAA-MM-DD>/<HHMMSS>``."""
    return servico.pastas(camera)[0] / evento.pasta


def tamanho_pasta(pasta: Path) -> int:
    if not pasta.is_dir():
        return 0
    return sum(f.stat().st_size for f in pasta.rglob("*") if f.is_file())


def remover_evento(servico: ServicoCameras, evento: Evento, camera: Camera | None) -> int:
    """Apaga os arquivos e o registro. Devolve os bytes liberados."""
    liberados = 0
    if camera is not None and evento.pasta:
        pasta = pasta_do_evento(servico, camera, evento)
        liberados = tamanho_pasta(pasta)
        shutil.rmtree(pasta, ignore_errors=True)
        dia = pasta.parent
        if dia.is_dir() and not any(dia.iterdir()):
            dia.rmdir()  # a pasta do dia ficou vazia
    if evento.id is not None:
        servico.banco.remover_evento(evento.id)
    return liberados


def aplicar_retencao(servico: ServicoCameras, config: Config, agora: datetime | None = None) -> tuple[list[int], int]:
    """Aplica as duas regras. Devolve (ids removidos, bytes liberados)."""
    agora = agora or datetime.now()
    cameras = {c.id: c for c in servico.listar()}
    dias = float(config.obter("eventos.retencao_dias"))
    limite = float(config.obter("eventos.limite_disco_gb")) * 1024**3
    removidos: list[int] = []
    liberados = 0

    for evento in servico.banco.listar_eventos(antes_de=agora - timedelta(days=dias), limite=None):
        liberados += remover_evento(servico, evento, cameras.get(evento.camera_id))
        removidos.append(evento.id or 0)

    restantes = list(reversed(servico.banco.listar_eventos(limite=None)))  # mais antigos primeiro
    tamanhos = [
        tamanho_pasta(pasta_do_evento(servico, cameras[e.camera_id], e)) if e.camera_id in cameras else 0
        for e in restantes
    ]
    total = sum(tamanhos)
    for evento, tamanho in zip(restantes, tamanhos, strict=True):
        if total <= limite:
            break
        liberados += remover_evento(servico, evento, cameras.get(evento.camera_id))
        removidos.append(evento.id or 0)
        total -= tamanho

    if removidos:
        log.info("Retenção: %d evento(s) apagado(s), %.1f MB liberados", len(removidos), liberados / 1_048_576)
    return removidos, liberados
