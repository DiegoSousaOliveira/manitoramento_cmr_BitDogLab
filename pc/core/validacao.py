"""Validação dos campos do cadastro de câmeras e geração do código para a placa.

Cada ``validar_*`` devolve uma **mensagem de erro em português** ou ``None`` se
o valor estiver correto. Esse formato é prático para a interface: basta mostrar
a mensagem embaixo do campo.

As constantes aqui são regras de FORMATO (ex.: o protocolo da placa só aceita
códigos de até 10 caracteres), não ajustes de funcionamento — esses ficam no config.yaml.
"""

from __future__ import annotations

import ipaddress
import re
import unicodedata
from collections.abc import Iterable

from core.url_rtsp import VARIAVEIS_URL, variaveis_do_modelo

#: Tamanho máximo do código (limite do protocolo UDP com a placa).
CODIGO_MAX = 10
#: Tamanho máximo do nome (cabe no bloco do mosaico e em nome de pasta).
NOME_MAX = 40

# Maiúsculas, dígitos, "_" e "-", com pelo menos uma letra ou dígito.
# ("-" sozinho é reservado no protocolo para "nenhuma câmera").
_PADRAO_CODIGO = re.compile(rf"^(?=.*[A-Z0-9])[A-Z0-9_-]{{1,{CODIGO_MAX}}}$")

# O nome vira nome de pasta (dados/eventos/<nome>/), então não pode ter
# caracteres proibidos em nomes de arquivo no Windows.
_PROIBIDOS_NOME = set('<>:"/\\|?*')
_RESERVADOS_WINDOWS = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}

# Palavras que não ajudam a identificar a câmera ao gerar o código.
_PALAVRAS_IGNORADAS = {"A", "O", "AS", "OS", "E", "DA", "DE", "DO", "DAS", "DOS", "NA", "NO", "NAS", "NOS", "EM"}


def remover_acentos(texto: str) -> str:
    """``"Câmera Garagem"`` -> ``"Camera Garagem"``."""
    decomposto = unicodedata.normalize("NFKD", texto)
    return "".join(c for c in decomposto if not unicodedata.combining(c))


def normalizar_nome(nome: str) -> str:
    """Tira espaços das pontas e espaços repetidos: ``"  Frente   da casa "`` -> ``"Frente da casa"``."""
    return " ".join(nome.split())


def validar_nome(nome: str) -> str | None:
    nome = normalizar_nome(nome)
    if not nome:
        return "Informe um nome para a câmera."
    if len(nome) > NOME_MAX:
        return f"Use no máximo {NOME_MAX} caracteres."
    proibidos = sorted({c for c in nome if c in _PROIBIDOS_NOME or ord(c) < 32})
    if proibidos:
        return "O nome não pode conter: " + " ".join(proibidos)
    if nome.endswith("."):
        return "O nome não pode terminar com ponto."
    if nome.upper() in _RESERVADOS_WINDOWS:
        return "Esse nome é reservado pelo Windows; escolha outro."
    return None


def validar_codigo(codigo: str) -> str | None:
    if not codigo:
        return "Informe o código para a placa."
    if len(codigo) > CODIGO_MAX:
        return f"O código pode ter no máximo {CODIGO_MAX} caracteres."
    if not _PADRAO_CODIGO.match(codigo):
        return "Use só letras MAIÚSCULAS sem acento, números, _ ou - (sem espaços)."
    return None


def gerar_codigo(nome: str, em_uso: Iterable[str] = ()) -> str:
    """Sugere um código curto e único a partir do nome.

    ``"Frente da casa"`` -> ``"FRENTE"``; ``"Câmera 2"`` -> ``"CAMERA2"``.
    Se já existir, tenta a primeira palavra + iniciais (``"GARAGEMF"`` para
    "Garagem fundos") e, por último, acrescenta um número (``"SALA2"``).
    """
    ocupados = {c.upper() for c in em_uso}
    palavras = re.findall(r"[A-Z0-9]+", remover_acentos(nome).upper())
    significativas = [p for p in palavras if p not in _PALAVRAS_IGNORADAS] or palavras or ["CAM"]

    primeira, resto = significativas[0], significativas[1:]
    numeros = "".join(p for p in resto if p.isdigit())
    iniciais = "".join(p[0] for p in resto if not p.isdigit())
    candidatos = [primeira + numeros, primeira + iniciais + numeros, "".join(significativas)]

    for candidato in candidatos:
        candidato = candidato[:CODIGO_MAX]
        if candidato not in ocupados:
            return candidato
    for n in range(2, 10_000):
        sufixo = str(n)
        candidato = candidatos[0][: CODIGO_MAX - len(sufixo)] + sufixo
        if candidato not in ocupados:
            return candidato
    raise ValueError("Não foi possível gerar um código livre.")


def validar_ip(ip: str) -> str | None:
    """Aceita só IPv4 (câmeras na rede local), ex.: ``192.168.0.50``."""
    try:
        endereco = ipaddress.IPv4Address(ip.strip())
    except ValueError:
        return "IP inválido. Use o formato 192.168.0.50."
    if endereco.is_unspecified or endereco.is_multicast or endereco == ipaddress.IPv4Address("255.255.255.255"):
        return "Esse endereço não pode ser de uma câmera."
    return None


def validar_porta(porta: int | str) -> str | None:
    try:
        numero = int(porta)
    except (TypeError, ValueError):
        return "A porta deve ser um número."
    if not 1 <= numero <= 65535:
        return "A porta deve estar entre 1 e 65535."
    return None


def validar_ip_placa(ip: str) -> str | None:
    """IP da placa BitDogLab (ela mostra no display). Vazio é permitido: o PC a descobre pelo OLA."""
    if not ip.strip():
        return None
    try:
        endereco = ipaddress.IPv4Address(ip.strip())
    except ValueError:
        return "IP inválido. Use o formato 192.168.1.60 (o número que aparece no display da placa)."
    if endereco.is_unspecified or endereco.is_multicast or endereco == ipaddress.IPv4Address("255.255.255.255"):
        return "Esse endereço não pode ser da placa."
    return None


def validar_portas_placa(porta_placa: int | str, porta_pc: int | str) -> str | None:
    """Portas UDP da placa e do PC (padrão 5006 e 5005): válidas e diferentes."""
    for nome, porta in (("da placa", porta_placa), ("do PC", porta_pc)):
        if erro := validar_porta(porta):
            return f"Porta {nome}: {erro[0].lower()}{erro[1:]}"
    if int(porta_placa) == int(porta_pc):
        return "A porta da placa e a porta do PC precisam ser diferentes."
    return None


def validar_modelo_url(modelo: str) -> str | None:
    modelo = modelo.strip()
    if not modelo.lower().startswith(("rtsp://", "rtsps://")):
        return "O modelo deve começar com rtsp://"
    try:
        variaveis = variaveis_do_modelo(modelo)
    except ValueError:
        return "Chaves { } desbalanceadas no modelo."
    desconhecidas = variaveis - VARIAVEIS_URL
    if desconhecidas:
        return "Variáveis desconhecidas: " + ", ".join(sorted(f"{{{v}}}" for v in desconhecidas))
    if "ip" not in variaveis:
        return "O modelo precisa conter {ip}."
    return None
