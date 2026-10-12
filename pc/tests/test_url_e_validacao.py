"""Montagem/mascaramento de URLs e validação dos campos do cadastro."""

from __future__ import annotations

import pytest

from core.url_rtsp import mascarar_url, montar_url, variaveis_do_modelo
from core.validacao import (
    gerar_codigo,
    normalizar_nome,
    validar_codigo,
    validar_ip,
    validar_modelo_url,
    validar_nome,
    validar_porta,
)
from tests.conftest import MODELO_ICSEE

MODELO_ONVIF = "rtsp://{usuario}:{senha}@{ip}:{porta}/onvif1"


def _url(modelo: str, senha: str, usuario: str = "admin") -> str:
    return montar_url(modelo, ip="192.168.0.50", porta=554, usuario=usuario, senha=senha, canal=1, stream=1)


# ----------------------------------------------------------------- montar_url
def test_icsee_sem_senha_deixa_parametro_vazio() -> None:
    assert _url(MODELO_ICSEE, "") == "rtsp://192.168.0.50:554/user=admin&password=&channel=1&stream=1.sdp"


def test_icsee_com_senha() -> None:
    assert _url(MODELO_ICSEE, "abc123") == "rtsp://192.168.0.50:554/user=admin&password=abc123&channel=1&stream=1.sdp"


def test_caracteres_que_quebram_a_url_sao_codificados() -> None:
    url = _url(MODELO_ICSEE, "a&b@c:d")
    assert "password=a%26b%40c%3Ad&channel" in url


def test_caracteres_comuns_vao_sem_codificar() -> None:
    # Muitas câmeras não decodificam %XX, então só o necessário é codificado.
    assert "password=Ab!$*_.-~" in _url(MODELO_ICSEE, "Ab!$*_.-~")


def test_onvif_sem_senha_remove_dois_pontos() -> None:
    assert _url(MODELO_ONVIF, "") == "rtsp://admin@192.168.0.50:554/onvif1"


def test_onvif_com_senha() -> None:
    assert _url(MODELO_ONVIF, "xyz") == "rtsp://admin:xyz@192.168.0.50:554/onvif1"


def test_variavel_desconhecida_gera_erro_claro() -> None:
    with pytest.raises(ValueError, match="Modelo de URL inválido"):
        _url("rtsp://{ip}/{senhaa}", "")


def test_variaveis_do_modelo() -> None:
    assert variaveis_do_modelo(MODELO_ICSEE) == {"ip", "porta", "usuario", "senha", "canal", "stream"}


# ----------------------------------------------------------------- mascarar_url
@pytest.mark.parametrize(
    ("original", "mascarada"),
    [
        (
            "rtsp://1.2.3.4:554/user=admin&password=segredo&channel=1&stream=0.sdp",
            "rtsp://1.2.3.4:554/user=admin&password=***&channel=1&stream=0.sdp",
        ),
        (
            "rtsp://1.2.3.4:554/user=admin&password=&channel=1&stream=0.sdp",
            "rtsp://1.2.3.4:554/user=admin&password=&channel=1&stream=0.sdp",
        ),
        ("rtsp://admin:segredo@1.2.3.4:554/onvif1", "rtsp://admin:***@1.2.3.4:554/onvif1"),
        ("rtsp://admin@1.2.3.4:554/onvif1", "rtsp://admin@1.2.3.4:554/onvif1"),
        # Formato devolvido pelo ONVIF das Xiongmai, com "_" como separador
        (
            "rtsp://1.2.3.4:554/user=admin_password=s3_gr3do_channel=1_stream=0.sdp?real_stream",
            "rtsp://1.2.3.4:554/user=admin_password=***_channel=1_stream=0.sdp?real_stream",
        ),
        ("rtsp://1.2.3.4:554/stream", "rtsp://1.2.3.4:554/stream"),
    ],
)
def test_mascarar_url(original: str, mascarada: str) -> None:
    assert mascarar_url(original) == mascarada


def test_mascarar_url_montada_nunca_mostra_a_senha() -> None:
    for senha in ("abc", "a&b", "p@ss:w/rd", "tem espaço"):
        for modelo in (MODELO_ICSEE, MODELO_ONVIF):
            assert senha not in mascarar_url(_url(modelo, senha))


# ----------------------------------------------------------------- código da placa
@pytest.mark.parametrize(
    ("nome", "em_uso", "esperado"),
    [
        ("Frente da casa", set(), "FRENTE"),
        ("Câmera 2", set(), "CAMERA2"),
        ("Garagem fundos", {"GARAGEM"}, "GARAGEMF"),
        ("Sala", {"SALA"}, "SALA2"),
        ("Sala", {"SALA", "SALA2"}, "SALA3"),
        ("Estacionamento", set(), "ESTACIONAM"),
        ("Estacionamento", {"ESTACIONAM"}, "ESTACIONA2"),
        ("Portão do quintal", set(), "PORTAO"),
        ("   ", set(), "CAM"),
        ("da", set(), "DA"),
    ],
)
def test_gerar_codigo(nome: str, em_uso: set[str], esperado: str) -> None:
    assert gerar_codigo(nome, em_uso) == esperado


@pytest.mark.parametrize("nome", ["Frente da casa", "Câmera ção 123", "Ü-ber/Garagem!!", "x" * 40, "º andar"])
def test_codigo_gerado_sempre_e_valido(nome: str) -> None:
    assert validar_codigo(gerar_codigo(nome)) is None


@pytest.mark.parametrize("codigo", ["FRENTE", "CAM2", "GARAGEM_F", "A-1", "ABCDEFGHIJ"])
def test_codigo_valido(codigo: str) -> None:
    assert validar_codigo(codigo) is None


@pytest.mark.parametrize("codigo", ["", "frente", "FRENTE CASA", "ABCDEFGHIJK", "-", "A;B", "CÂMERA"])
def test_codigo_invalido(codigo: str) -> None:
    assert validar_codigo(codigo) is not None


# ----------------------------------------------------------------- nome
def test_normalizar_nome() -> None:
    assert normalizar_nome("  Frente   da casa ") == "Frente da casa"


@pytest.mark.parametrize("nome", ["Frente da casa", "Câmera 2", "Garagem (fundos)"])
def test_nome_valido(nome: str) -> None:
    assert validar_nome(nome) is None


@pytest.mark.parametrize("nome", ["", "   ", "a/b", "Sala?", "CON", "nul", "x" * 41, "Frente."])
def test_nome_invalido(nome: str) -> None:
    assert validar_nome(nome) is not None


# ----------------------------------------------------------------- ip, porta, modelo
@pytest.mark.parametrize("ip", ["192.168.0.50", "10.0.0.2", "127.0.0.1"])
def test_ip_valido(ip: str) -> None:
    assert validar_ip(ip) is None


@pytest.mark.parametrize("ip", ["", "192.168.0", "300.1.1.1", "abc", "0.0.0.0", "255.255.255.255", "224.0.0.1"])
def test_ip_invalido(ip: str) -> None:
    assert validar_ip(ip) is not None


@pytest.mark.parametrize(("porta", "valida"), [(554, True), ("8899", True), (0, False), (70000, False), ("x", False)])
def test_validar_porta(porta: int | str, valida: bool) -> None:
    assert (validar_porta(porta) is None) is valida


@pytest.mark.parametrize(
    ("modelo", "valido"),
    [
        (MODELO_ICSEE, True),
        (MODELO_ONVIF, True),
        ("http://{ip}/video", False),
        ("rtsp://{ip}/{senhaa}", False),
        ("rtsp://camera/stream", False),
        ("rtsp://{ip/stream", False),
    ],
)
def test_validar_modelo_url(modelo: str, valido: bool) -> None:
    assert (validar_modelo_url(modelo) is None) is valido
