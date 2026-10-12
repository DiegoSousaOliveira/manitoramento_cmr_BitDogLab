"""Quanta memória o computador ainda tem — para o treino nunca derrubar o app.

Dois números importam:

- **RAM livre**: memória física disponível. Pouca RAM deixa tudo lento (o Windows passa a usar o
  disco), mas não quebra nada.
- **Memória reservável livre** ("commit" no Windows = RAM + arquivo de paginação): quando ELA
  acaba, o Windows recusa qualquer pedido de memória e os programas falham — foi o erro de
  12/10/2026 ("Insufficient memory" na câmera e "Unable to allocate" no treino). Cada processo que
  carrega o PyTorch com CUDA reserva alguns GB dela; por isso o treino no Windows não usa
  processos ajudantes e vigia este número enquanto treina.

No Linux, "reservável" = RAM disponível + swap livre (o Linux não reserva antes de usar).

**A placa de vídeo também conta** (medido em 12/10/2026, Windows 10 + RTX 5060): cada GB que o
PyTorch reserva na placa aumenta em 1 GB a memória reservada do processo (o Windows guarda espaço
para poder tirar aquilo da placa). Treinar o YOLO11s em 1280 custa ~4,5 GB fixos + a placa:
lote 1 → 0,9 GB na placa; 2 → 1,7; 4 → 3,1; 8 → 5,1 (total do processo com lote 8: ~10 GB).
Por isso o treino recebe um ORÇAMENTO na placa (``orcamento_placa``) do tamanho do que cabe na
memória livre; se o lote não couber, o PyTorch recusa e o lote é dividido por 2.
"""

from __future__ import annotations

import ctypes
import os
from dataclasses import dataclass

import psutil

GB = 1024**3
#: Quanto o processo de treino ainda cresce FORA da placa durante uma etapa (medido: ~1 GB no treino).
FOLGA_PC_GB = 1.0
#: Placa de vídeo que fica livre para a IA do app e para o Windows.
FOLGA_PLACA_GB = 0.5
#: Menor orçamento na placa que ainda treina (lote 1 em 1280 reservou 0,87 GB).
PLACA_MINIMA_GB = 1.0


class MemoriaInsuficiente(Exception):  # noqa: N818
    """O treino parou para não faltar memória ao app.

    Não é ``RuntimeError`` de propósito: o Ultralytics trata alguns ``RuntimeError`` (falta de
    memória na placa de vídeo) e poderia "engolir" este aviso.
    """


@dataclass(frozen=True)
class Memoria:
    ram_livre_gb: float
    reservavel_livre_gb: float

    def texto(self) -> str:
        return f"{self.reservavel_livre_gb:.1f} GB de memória livre ({self.ram_livre_gb:.1f} GB de RAM)"


def falta_para(livre: Memoria, minimo_gb: float) -> str | None:
    """Explica por que NÃO dá para começar o treino agora, ou ``None`` se há memória suficiente."""
    if livre.reservavel_livre_gb >= minimo_gb:
        return None
    return (f"pouca memória livre para treinar sem atrapalhar as câmeras ({livre.texto()}; "
            f"o treino precisa de {minimo_gb:.1f} GB)")


def orcamento_placa(livre: Memoria, critica_gb: float, reservado_gb: float, livre_placa_gb: float) -> float:
    """Quanto o PyTorch do treino pode ter reservado na placa de vídeo, NO TOTAL (GB).

    ``reservado_gb``: o que ele já reservou (já está descontado da memória livre do PC);
    ``livre_placa_gb``: o que ainda está livre na placa. Vale o MENOR entre:
    - o que cabe na memória do PC sem descer da margem crítica (com folga para o resto do processo);
    - o que cabe na placa, deixando um pouco para a IA do app.
    """
    pelo_pc = reservado_gb + livre.reservavel_livre_gb - critica_gb - FOLGA_PC_GB
    pela_placa = reservado_gb + livre_placa_gb - FOLGA_PLACA_GB
    return max(0.0, min(pelo_pc, pela_placa))


def eh_falta_de_memoria(erro: BaseException) -> bool:
    """O erro foi falta de memória do computador (RAM)? Vale também para erros vindos dos ajudantes."""
    if isinstance(erro, MemoryError):  # inclui o ``_ArrayMemoryError`` do numpy
        return True
    texto = f"{type(erro).__name__}: {erro}".lower()
    return any(trecho in texto for trecho in (
        "memoryerror", "unable to allocate", "insufficient memory", "not enough memory",
        "paging file is too small", "arquivo de paginação",
    ))


class _EstadoMemoria(ctypes.Structure):
    _fields_ = [  # noqa: RUF012 (formato fixo da função GlobalMemoryStatusEx do Windows)
        ("dwLength", ctypes.c_ulong),
        ("dwMemoryLoad", ctypes.c_ulong),
        ("ullTotalPhys", ctypes.c_ulonglong),
        ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong),
        ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong),
        ("ullAvailVirtual", ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]


def medir() -> Memoria:
    if os.name == "nt":
        estado = _EstadoMemoria()
        estado.dwLength = ctypes.sizeof(_EstadoMemoria)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(estado)):  # type: ignore[attr-defined]
            return Memoria(estado.ullAvailPhys / GB, estado.ullAvailPageFile / GB)
    ram = psutil.virtual_memory().available
    return Memoria(ram / GB, (ram + psutil.swap_memory().free) / GB)
