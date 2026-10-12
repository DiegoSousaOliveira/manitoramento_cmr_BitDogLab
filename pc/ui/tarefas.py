"""Executar funções demoradas fora da thread da interface.

Regra de ouro do Qt: a thread principal (da interface) só deve desenhar e reagir
a cliques. Qualquer coisa que pode demorar (rede, disco, ONVIF...) vai para o
``QThreadPool``, um conjunto de threads reaproveitáveis. Quando termina, o
resultado volta para a thread da interface por um **sinal** — e só então mexemos
nos widgets (widgets só podem ser tocados pela thread principal).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal

log = logging.getLogger(__name__)


class _Sinais(QObject):
    concluido = Signal(object)
    falhou = Signal(object)  # a exceção


class _Tarefa(QRunnable):
    def __init__(self, funcao: Callable[[], Any], sinais: _Sinais) -> None:
        super().__init__()
        self.funcao = funcao
        self.sinais = sinais

    def run(self) -> None:
        try:
            resultado = self.funcao()
        except Exception as erro:  # noqa: BLE001 (a falha é repassada para a interface)
            log.debug("Tarefa em segundo plano falhou", exc_info=True)
            sinal, valor = self.sinais.falhou, erro
        else:
            sinal, valor = self.sinais.concluido, resultado
        try:
            sinal.emit(valor)
        except RuntimeError:
            pass  # o dono (ex.: diálogo) já foi fechado: resultado descartado


#: Mantém os objetos de sinal vivos até o resultado ser entregue.
_vivos: set[_Sinais] = set()


def executar(
    funcao: Callable[[], Any],
    ao_concluir: Callable[[Any], None],
    ao_falhar: Callable[[BaseException], None] | None = None,
    dono: QObject | None = None,
) -> None:
    """Roda ``funcao()`` numa thread do pool e chama ``ao_concluir(resultado)`` na thread da interface.

    ``dono``: se o widget dono for destruído antes do fim (ex.: diálogo fechado),
    o resultado é simplesmente descartado.
    """
    sinais = _Sinais(dono)
    _vivos.add(sinais)

    def liberar(*_args: object) -> None:
        _vivos.discard(sinais)

    sinais.concluido.connect(ao_concluir)
    sinais.concluido.connect(liberar)
    if ao_falhar:
        sinais.falhou.connect(ao_falhar)
    sinais.falhou.connect(liberar)
    sinais.destroyed.connect(liberar)
    QThreadPool.globalInstance().start(_Tarefa(funcao, sinais))
