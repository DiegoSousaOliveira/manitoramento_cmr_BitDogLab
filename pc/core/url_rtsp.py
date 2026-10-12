"""Montagem de URLs RTSP a partir de modelos e mascaramento de senhas para o log.

Exemplo com o modelo padrão das câmeras iCSee::

    montar_url("rtsp://{ip}:{porta}/user={usuario}&password={senha}&channel={canal}&stream={stream}.sdp",
               ip="192.168.0.50", porta=554, usuario="admin", senha="", canal=1, stream=1)
    # -> "rtsp://192.168.0.50:554/user=admin&password=&channel=1&stream=1.sdp"

Regra de ouro: **nunca** escreva uma URL no log sem passar por ``mascarar_url``.
"""

from __future__ import annotations

import re
import string

#: Variáveis aceitas nos modelos de URL.
VARIAVEIS_URL = frozenset({"ip", "porta", "usuario", "senha", "canal", "stream"})

# Caracteres que "quebram" a estrutura da URL se aparecerem dentro do usuário
# ou da senha (ex.: "&" separa parâmetros nas iCSee; "@" e ":" separam o login
# no formato usuario:senha@ip). Só esses são codificados como %XX — o resto vai
# como está, porque o firmware de algumas câmeras não decodifica %XX.
_CARACTERES_PERIGOSOS = frozenset('%&@:/?# ;"')

# password=..., passwd=... ou pwd=... até o próximo separador. O ONVIF das
# Xiongmai devolve URLs com "_" como separador: user=admin_password=xxx_channel=1.
_SENHA_EM_PARAMETRO = re.compile(
    r"(?i)((?:password|passwd|pwd)=)(.*?)(?=&|_channel=|_stream=|\.sdp|\?|$)"
)
# rtsp://usuario:senha@host
_SENHA_EM_LOGIN = re.compile(r"^([a-zA-Z][a-zA-Z0-9+.-]*://[^:/@]*:)([^@/]*)(@)")


def escapar(valor: str) -> str:
    """Codifica como ``%XX`` só os caracteres que quebrariam a URL."""
    return "".join(f"%{ord(c):02X}" if c in _CARACTERES_PERIGOSOS else c for c in valor)


def variaveis_do_modelo(modelo: str) -> set[str]:
    """Lista as variáveis ``{x}`` usadas em um modelo. Levanta ``ValueError`` se mal formado."""
    return {nome for _, nome, _, _ in string.Formatter().parse(modelo) if nome is not None}


def montar_url(
    modelo: str,
    *,
    ip: str,
    porta: int,
    usuario: str,
    senha: str,
    canal: int,
    stream: int,
) -> str:
    """Preenche o modelo. Com senha vazia, ``usuario:{senha}@`` vira ``usuario@``."""
    if not senha:
        modelo = modelo.replace(":{senha}@", "@")
    try:
        return modelo.format(
            ip=ip,
            porta=porta,
            usuario=escapar(usuario),
            senha=escapar(senha),
            canal=canal,
            stream=stream,
        )
    except (KeyError, IndexError, ValueError) as erro:
        raise ValueError(f"Modelo de URL inválido ({erro}): use só {sorted(VARIAVEIS_URL)}") from erro


def mascarar_url(url: str) -> str:
    """Troca a senha por ``***`` (senha vazia continua vazia, o que ajuda no diagnóstico)."""

    def trocar(m: re.Match[str]) -> str:
        sufixo = m.group(3) if m.re is _SENHA_EM_LOGIN else ""
        return m.group(1) + ("***" if m.group(2) else "") + sufixo

    return _SENHA_EM_LOGIN.sub(trocar, _SENHA_EM_PARAMETRO.sub(trocar, url))
