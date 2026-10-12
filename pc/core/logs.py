"""Configuração do ``logging`` com arquivo rotativo em ``dados/logs/``.

"Rotativo" significa: quando o arquivo chega ao tamanho máximo, ele é renomeado
para ``sentinela.log.1`` (o ``.1`` vira ``.2`` e assim por diante) e um novo
arquivo começa. Só os N mais recentes são mantidos, então o log nunca enche o disco.
"""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import TextIO

#: Formato das linhas no arquivo: data, nível, thread e módulo ajudam a depurar.
FORMATO_ARQUIVO = "%(asctime)s %(levelname)-7s [%(threadName)s] %(name)s: %(message)s"

#: Bibliotecas muito "falantes" que só devem aparecer em caso de aviso ou erro.
BIBLIOTECAS_SILENCIADAS = ("zeep", "urllib3", "requests", "PIL", "matplotlib")


def definir_nivel(nivel: str) -> None:
    """Muda o nível do log na hora (DEBUG, INFO, WARNING, ERROR)."""
    logging.getLogger().setLevel(nivel.upper())


def configurar_logs(
    pasta: Path,
    nivel: str = "INFO",
    tamanho_mb: float = 5,
    arquivos: int = 5,
    nome_arquivo: str = "sentinela.log",
    formato_console: str = "%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    saida_console: TextIO | None = None,
) -> Path:
    """Liga o log em arquivo rotativo + console. Devolve o caminho do arquivo de log.

    Pode ser chamada mais de uma vez (ex.: nos testes): os manipuladores antigos
    são removidos antes, para não duplicar linhas.
    """
    pasta.mkdir(parents=True, exist_ok=True)
    caminho = pasta / nome_arquivo

    raiz = logging.getLogger()
    for manipulador in list(raiz.handlers):
        raiz.removeHandler(manipulador)
        manipulador.close()
    raiz.setLevel(nivel.upper())

    arquivo = RotatingFileHandler(
        caminho,
        maxBytes=int(tamanho_mb * 1024 * 1024),
        backupCount=arquivos,
        encoding="utf-8",
    )
    arquivo.setFormatter(logging.Formatter(FORMATO_ARQUIVO))
    raiz.addHandler(arquivo)

    console = logging.StreamHandler(saida_console or sys.stderr)
    console.setFormatter(logging.Formatter(formato_console, datefmt="%H:%M:%S"))
    raiz.addHandler(console)

    for nome in BIBLIOTECAS_SILENCIADAS:
        logging.getLogger(nome).setLevel(logging.WARNING)

    return caminho
