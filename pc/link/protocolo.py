"""Protocolo UDP PC ⇄ BitDogLab — formato FIXO (``docs/protocolo.md``).

Aqui só há TEXTO, sem rede: montar as mensagens que o PC envia e interpretar (validando
tudo, regra E11) as que a placa envia. Por isso é fácil de testar com os exemplos do
documento.

Regras de formato: ASCII imprimível sem acentos, campos separados por ``;``, termina em
``\\n``, no máximo 64 bytes, decimais com ponto.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import datetime

from core.modelos import CLASSES, Nivel

TAMANHO_MAXIMO = 64  # bytes, contando o "\n"
SEQ_MAXIMO = 65535
ID_MAXIMO = 999999
SEGUNDOS_MAXIMO = 99999
SEM_CAMERA = "-"  # CAM;- = nenhuma câmera com PTZ
BOTOES = ("A", "B", "J")
MODOS = ("NORMAL", "NOITE")

_CODIGO = re.compile(r"^(?=.*[A-Z0-9])[A-Z0-9_-]{1,10}$")
_NUMERO = re.compile(r"^-?\d+(\.\d+)?$")
_INTEIRO = re.compile(r"^\d+$")


class ErroProtocolo(ValueError):
    """Mensagem fora do formato (o motivo vai para o log; a mensagem é ignorada)."""


# ===================================================================== PC -> placa
def _montar(*campos: object) -> str:
    texto = ";".join(str(c) for c in campos) + "\n"
    if any(not 0x20 <= ord(c) <= 0x7E for c in texto[:-1]):
        raise ErroProtocolo(f"caractere fora do ASCII imprimível em {texto!r}")
    if len(texto) > TAMANHO_MAXIMO:
        raise ErroProtocolo(f"mensagem com {len(texto)} bytes (máximo {TAMANHO_MAXIMO})")
    return texto


def _codigo(cam: str) -> str:
    if not _CODIGO.match(cam):
        raise ErroProtocolo(f"código de câmera inválido: {cam!r}")
    return cam


def _classe(classe: str) -> str:
    if classe not in CLASSES:
        raise ErroProtocolo(f"classe inválida: {classe!r}")
    return classe


def _id(id_rastreio: int) -> int:
    if not 1 <= id_rastreio <= ID_MAXIMO:
        raise ErroProtocolo(f"id fora da faixa: {id_rastreio}")
    return id_rastreio


def alerta(cam: str, classe: str, segundos: float, confianca: float, id_rastreio: int, nivel: Nivel | str) -> str:
    """``ALERTA;FRENTE;PESSOA;12;0.87;7;ALTO`` — segundos arredondados para baixo."""
    seg = min(SEGUNDOS_MAXIMO, max(0, math.floor(segundos)))
    conf = min(1.0, max(0.0, confianca))
    nome_nivel = nivel.name if isinstance(nivel, Nivel) else str(nivel)
    if nome_nivel not in Nivel.__members__:
        raise ErroProtocolo(f"nível inválido: {nivel!r}")
    return _montar("ALERTA", _codigo(cam), _classe(classe), seg, f"{conf:.2f}", _id(id_rastreio), nome_nivel)


def limpar(cam: str, id_rastreio: int) -> str:
    return _montar("LIMPAR", _codigo(cam), _id(id_rastreio))


def passou(cam: str, classe: str) -> str:
    return _montar("PASSOU", _codigo(cam), _classe(classe))


def camera(cam: str | None) -> str:
    """``CAM;FRENTE`` — ``None`` (nenhuma câmera com PTZ) vira ``CAM;-``."""
    return _montar("CAM", SEM_CAMERA if cam is None else _codigo(cam))


def ping(seq: int) -> str:
    return _montar("PING", seq % (SEQ_MAXIMO + 1))


def hora(quando: datetime) -> str:
    return _montar("HORA", f"{quando:%H:%M}")


def som(continuo: bool) -> str:
    """``SOM;CONTINUO`` (o buzzer toca até o alerta acabar) ou ``SOM;UMA`` (só quando ele aparece).

    Mensagem nova (E13, 12/10/2026, pedido do Diego); placas antigas a ignoram (comando desconhecido).
    """
    return _montar("SOM", "CONTINUO" if continuo else "UMA")


# ===================================================================== placa -> PC
@dataclass(frozen=True)
class Joy:
    x: float  # -1.00 (esquerda) .. 1.00 (direita)
    y: float  # -1.00 (para baixo) .. 1.00 (para cima)


@dataclass(frozen=True)
class Botao:
    nome: str  # A (silenciar), B (modo), J (próxima câmera)


@dataclass(frozen=True)
class Modo:
    nome: str  # NORMAL ou NOITE


@dataclass(frozen=True)
class Pong:
    seq: int


@dataclass(frozen=True)
class Ola:
    pass


MensagemPlaca = Joy | Botao | Modo | Pong | Ola


def _eixo(texto: str) -> float:
    if not _NUMERO.match(texto):
        raise ErroProtocolo(f"número inválido: {texto!r}")
    valor = float(texto)
    if not -1.0 <= valor <= 1.0:
        raise ErroProtocolo(f"joystick fora da faixa -1.00..1.00: {texto}")
    return valor


def interpretar(dados: bytes) -> MensagemPlaca:
    """Lê UMA mensagem da placa. Levanta ``ErroProtocolo`` (com o motivo) se for inválida.

    Aceita com ou sem ``\\n`` no fim e ignora ``\\r`` (regra E11).
    """
    if len(dados) > TAMANHO_MAXIMO:
        raise ErroProtocolo(f"datagrama com {len(dados)} bytes (máximo {TAMANHO_MAXIMO})")
    try:
        texto = dados.decode("ascii")
    except UnicodeDecodeError:
        raise ErroProtocolo("bytes fora do ASCII") from None
    texto = texto.replace("\r", "")
    if texto.endswith("\n"):
        texto = texto[:-1]
    if not texto or any(not 0x20 <= ord(c) <= 0x7E for c in texto):
        raise ErroProtocolo("mensagem vazia ou com caracteres de controle")
    campos = texto.split(";")
    comando, argumentos = campos[0], campos[1:]
    esperado = {"JOY": 2, "BTN": 1, "MODO": 1, "PONG": 1, "OLA": 0}.get(comando)
    if esperado is None:
        raise ErroProtocolo(f"comando desconhecido: {comando!r}")
    if len(argumentos) != esperado:
        raise ErroProtocolo(f"{comando} precisa de {esperado} campo(s), veio {len(argumentos)}")
    if comando == "JOY":
        return Joy(_eixo(argumentos[0]), _eixo(argumentos[1]))
    if comando == "BTN":
        if argumentos[0] not in BOTOES:
            raise ErroProtocolo(f"botão inválido: {argumentos[0]!r}")
        return Botao(argumentos[0])
    if comando == "MODO":
        if argumentos[0] not in MODOS:
            raise ErroProtocolo(f"modo inválido: {argumentos[0]!r}")
        return Modo(argumentos[0])
    if comando == "PONG":
        if not _INTEIRO.match(argumentos[0]) or int(argumentos[0]) > SEQ_MAXIMO:
            raise ErroProtocolo(f"seq inválido: {argumentos[0]!r}")
        return Pong(int(argumentos[0]))
    return Ola()
