"""Testes simples de rede (conexão TCP em UMA porta de UM IP — nada de varredura).

Uma conexão TCP em uma porta tem três resultados típicos, e cada um conta uma história:

- **aceita**: há um programa escutando (a porta está "aberta");
- **recusada** (``ConnectionRefusedError``): o aparelho existe e respondeu,
  mas nada escuta nessa porta (porta "fechada");
- **sem resposta** (timeout): o IP não existe/está desligado, ou um firewall
  descarta os pacotes. Daqui não dá para saber qual dos dois.
"""

from __future__ import annotations

import errno
import socket
import time
from dataclasses import dataclass

# Códigos de erro de "rede/host inalcançável" no Linux e no Windows (WSAE...).
_INALCANCAVEL = {
    errno.EHOSTUNREACH,
    errno.ENETUNREACH,
    10065,  # WSAEHOSTUNREACH
    10051,  # WSAENETUNREACH
}


@dataclass
class ResultadoPorta:
    porta: int
    aberta: bool
    tempo_ms: float  # tempo até a resposta (aceita ou recusada)
    mensagem: str
    #: ``True`` quando o aparelho respondeu (aceitou OU recusou) — prova que o IP existe.
    host_respondeu: bool


def descrever_erro_conexao(erro: OSError, ip: str, porta: int) -> str:
    """Traduz um erro de ``socket.connect`` para uma frase em português."""
    if isinstance(erro, ConnectionRefusedError):
        return f"porta {porta} fechada (o aparelho {ip} respondeu, mas nada escuta nessa porta)"
    if isinstance(erro, TimeoutError):
        return f"sem resposta de {ip}:{porta} (IP errado, câmera desligada, outra rede ou firewall)"
    if erro.errno in _INALCANCAVEL or getattr(erro, "winerror", None) in _INALCANCAVEL:
        return f"{ip} inacessível: o PC não tem rota até esse endereço (outra rede?)"
    if isinstance(erro, socket.gaierror):
        return f"endereço inválido: {ip}"
    return f"erro de rede ao conectar em {ip}:{porta}: {erro}"


def testar_porta(ip: str, porta: int, timeout_s: float) -> ResultadoPorta:
    """Tenta abrir uma conexão TCP e fecha em seguida."""
    inicio = time.perf_counter()
    try:
        with socket.create_connection((ip, porta), timeout=timeout_s):
            pass
    except OSError as erro:
        tempo = (time.perf_counter() - inicio) * 1000
        return ResultadoPorta(
            porta=porta,
            aberta=False,
            tempo_ms=tempo,
            mensagem=descrever_erro_conexao(erro, ip, porta),
            host_respondeu=isinstance(erro, ConnectionRefusedError),
        )
    tempo = (time.perf_counter() - inicio) * 1000
    return ResultadoPorta(porta, True, tempo, f"aberta ({tempo:.0f} ms)", host_respondeu=True)
