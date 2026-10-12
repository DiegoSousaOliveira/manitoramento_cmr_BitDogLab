"""Comunicação com a placa (link/bitdog.py) por UDP de verdade em 127.0.0.1, com uma "placa" de teste."""

from __future__ import annotations

import logging
import socket
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from core.config import ARQUIVO_PADRAO, Config
from core.modelos import Camera, Nivel, TipoFonte
from link import bitdog
from link.bitdog import LinkBitDog
from tests.conftest import MODELO_ICSEE
from tools.simulador_bitdoglab import ConexaoPlaca
from vision.dwell import Aviso, TipoAviso


def porta_livre() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def camera(id_: int, codigo: str, ptz: bool = True) -> Camera:
    return Camera(codigo.title(), codigo, TipoFonte.ARQUIVO, "", 554, "", MODELO_ICSEE, 8899, tem_ptz=ptz, id=id_)


def aviso(tipo: TipoAviso, id_: int, classe: str = "PESSOA", nivel: Nivel = Nivel.ALTO, segundos: float = 8.0) -> Aviso:
    return Aviso(tipo, (1, 1), id_, classe, time.monotonic(), segundos=segundos, confianca=0.87, nivel=nivel)


class PlacaDeTeste:
    """Um socket na porta da placa: guarda o que o PC manda e responde quando pedimos."""

    def __init__(self, porta: int, porta_pc: int) -> None:
        self.porta_pc = porta_pc
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.socket.bind(("127.0.0.1", porta))
        self.socket.settimeout(0.05)
        self.recebidas: list[str] = []
        self.responder_ping = True

    def enviar(self, texto: str | bytes) -> None:
        dados = texto if isinstance(texto, bytes) else (texto + "\n").encode("ascii")
        self.socket.sendto(dados, ("127.0.0.1", self.porta_pc))

    def ouvir(self, segundos: float) -> list[str]:
        """Lê por ``segundos`` (respondendo PING, se ligado) e devolve as mensagens novas."""
        inicio, fim = len(self.recebidas), time.monotonic() + segundos
        while time.monotonic() < fim:
            try:
                dados, origem = self.socket.recvfrom(1024)
            except (TimeoutError, ConnectionResetError):
                continue
            self.porta_pc = origem[1]  # como a placa: responde para a porta de onde o PC falou
            texto = dados.decode("ascii").strip()
            self.recebidas.append(texto)
            if texto.startswith("PING;") and self.responder_ping:
                self.enviar("PONG;" + texto.split(";")[1])
        return self.recebidas[inicio:]

    def ouvir_ate(self, condicao: Callable[[list[str]], bool], limite_s: float = 3.0) -> list[str]:
        novas: list[str] = []
        fim = time.monotonic() + limite_s
        while time.monotonic() < fim and not condicao(novas):
            novas += self.ouvir(0.05)
        return novas


def sem_ping(mensagens: list[str]) -> list[str]:
    """Tira as mensagens de rotina (PING/HORA e o CAM que vai "ao conectar", a qualquer momento)."""
    return [m for m in mensagens if not m.startswith(("PING", "HORA", "CAM"))]


@pytest.fixture
def ambiente(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[LinkBitDog, PlacaDeTeste, Config]]:
    copia = tmp_path / "config.yaml"
    copia.write_text(ARQUIVO_PADRAO.read_text(encoding="utf-8"), encoding="utf-8")
    config = Config.carregar(copia)
    porta_pc, porta_placa = porta_livre(), porta_livre()
    for chave, valor in {
        "placa.ip": "127.0.0.1", "placa.porta_pc": porta_pc, "placa.porta_placa": porta_placa,
        "placa.intervalo_ping_s": 0.2, "placa.timeout_pong_s": 0.6, "placa.reenvio_alerta_s": 0.3,
    }.items():
        config.definir(chave, valor)
    monkeypatch.setattr(bitdog, "ATRASO_LIMPAR_REPETIDO_S", 0.2)
    placa = PlacaDeTeste(porta_placa, porta_pc)
    link = LinkBitDog(config, endereco="127.0.0.1").iniciar()
    yield link, placa, config
    link.parar()
    placa.socket.close()


def test_ping_pong_e_status_de_conexao(ambiente: tuple[LinkBitDog, PlacaDeTeste, Config]) -> None:
    link, placa, _ = ambiente
    assert not link.estado().conectada
    placa.ouvir_ate(lambda _m: link.estado().conectada)
    estado = link.estado()
    assert estado.conectada and estado.ip == "127.0.0.1" and estado.resposta_ms is not None
    assert any(m.startswith("PING;") for m in placa.recebidas)
    placa.responder_ping = False  # a placa "desligou"
    placa.ouvir_ate(lambda _m: not link.estado().conectada, 2.0)
    assert not link.estado().conectada and (link.estado().sem_resposta_s or 0) > 0.6


def test_ola_recebe_hora_camera_e_o_alerta_ativo(ambiente: tuple[LinkBitDog, PlacaDeTeste, Config]) -> None:
    link, placa, _ = ambiente
    link.definir_cameras([camera(1, "FRENTE")])
    placa.ouvir(0.3)
    link.receber_aviso(aviso(TipoAviso.ALERTA, 7), camera(1, "FRENTE"))
    placa.ouvir(0.1)
    placa.enviar("OLA")  # a placa reiniciou: perdeu tudo
    novas = placa.ouvir_ate(lambda m: any(x.startswith("ALERTA") for x in m) and any(x.startswith("HORA") for x in m))
    assert any(m.startswith("HORA;") and len(m) == len("HORA;21:35") for m in novas)
    assert "CAM;FRENTE" in novas
    assert any(m.startswith("ALERTA;FRENTE;PESSOA;") for m in novas)


def test_alerta_prioridade_reenvio_e_limpar_em_dobro(ambiente: tuple[LinkBitDog, PlacaDeTeste, Config]) -> None:
    link, placa, _ = ambiente
    frente = camera(1, "FRENTE")
    link.receber_aviso(aviso(TipoAviso.ALERTA, 1, "CARRO", Nivel.MEDIO, 30), frente)
    novas = sem_ping(placa.ouvir_ate(lambda m: any(x.startswith("ALERTA") for x in m)))
    assert novas[0].startswith("ALERTA;FRENTE;CARRO;30;0.87;1;MEDIO")

    link.receber_aviso(aviso(TipoAviso.ALERTA, 2, "PESSOA", Nivel.ALTO, 8), frente)  # maior nível: na hora
    novas = sem_ping(placa.ouvir_ate(lambda m: any("PESSOA" in x for x in m), 1.0))
    # (um reenvio do CARRO, a cada 0,3 s, pode chegar logo antes: vale o 1º da PESSOA)
    assert next(m for m in novas if "PESSOA" in m).startswith("ALERTA;FRENTE;PESSOA;8;0.87;2;ALTO")
    reenvios = [m for m in sem_ping(placa.ouvir(1.0)) if m.startswith("ALERTA;FRENTE;PESSOA")]
    assert len(reenvios) >= 2  # E1: a cada 0,3 s
    segundos = [int(m.split(";")[3]) for m in reenvios]
    assert segundos == sorted(segundos) and segundos[-1] >= 8  # parado há mais tempo

    link.receber_aviso(aviso(TipoAviso.LIMPAR, 2), frente)
    novas = sem_ping(placa.ouvir(0.6))
    assert novas.count("LIMPAR;FRENTE;2") == 2  # E3: na hora e de novo depois
    assert any(m.startswith("ALERTA;FRENTE;CARRO;") for m in novas)  # volta a mostrar o MEDIO

    link.receber_aviso(aviso(TipoAviso.PASSOU, 9, "MOTO"), frente)
    assert "PASSOU;FRENTE;MOTO" in placa.ouvir_ate(lambda m: "PASSOU;FRENTE;MOTO" in m)


def test_botao_a_na_placa_nao_tira_o_alerta_da_placa(ambiente: tuple[LinkBitDog, PlacaDeTeste, Config]) -> None:
    """E14 (12/10/2026): quem cala é a placa (2 toques no A). O PC continua reenviando o alerta, para
    ele seguir na tela da placa até o objeto sair — antes o PC parava e o alerta sumia em 6 s."""
    link, placa, _ = ambiente
    frente = camera(1, "FRENTE")
    botoes: list[str] = []
    link.assinar_botao(botoes.append)
    link.receber_aviso(aviso(TipoAviso.ALERTA, 1, "CARRO", Nivel.MEDIO), frente)
    link.receber_aviso(aviso(TipoAviso.ALERTA, 2, "PESSOA", Nivel.ALTO), frente)
    placa.ouvir_ate(lambda m: any("PESSOA" in x for x in m))
    placa.enviar("BTN;A")
    novas = sem_ping(placa.ouvir(0.8))
    assert botoes == ["A"]
    assert any(m.startswith("ALERTA;FRENTE;PESSOA") for m in novas)  # continua indo para a placa
    assert not any(m.startswith("ALERTA;FRENTE;CARRO") for m in novas)  # o de maior nível segue na frente


def test_som_vai_ao_conectar_e_quando_muda(ambiente: tuple[LinkBitDog, PlacaDeTeste, Config]) -> None:
    link, placa, _ = ambiente
    link.definir_som(True)  # Ajustes -> Placa -> "continua tocando até o alerta acabar"
    assert "SOM;CONTINUO" in placa.ouvir_ate(lambda m: "SOM;CONTINUO" in m)
    link.definir_som(False)
    assert "SOM;UMA" in placa.ouvir_ate(lambda m: "SOM;UMA" in m)
    placa.enviar("OLA")  # a placa reiniciou: o PC reenvia o contexto, com o SOM
    assert "SOM;UMA" in placa.ouvir_ate(lambda m: "SOM;UMA" in m)


def test_botao_j_troca_a_camera_do_joystick(ambiente: tuple[LinkBitDog, PlacaDeTeste, Config]) -> None:
    link, placa, _ = ambiente
    trocas: list[int | None] = []
    link.assinar_camera(trocas.append)
    link.definir_cameras([camera(1, "FRENTE"), camera(2, "GARAGEM"), camera(3, "QUINTAL", ptz=False)])
    assert "CAM;FRENTE" in placa.ouvir_ate(lambda m: "CAM;FRENTE" in m)  # a 1ª com PTZ
    placa.enviar("BTN;J")
    assert "CAM;GARAGEM" in placa.ouvir_ate(lambda m: "CAM;GARAGEM" in m)
    placa.enviar("BTN;J")
    assert "CAM;FRENTE" in placa.ouvir_ate(lambda m: "CAM;FRENTE" in m)  # sem PTZ fica de fora
    link.definir_camera_joystick(3)  # sem PTZ: ignorada
    link.definir_camera_joystick(2)  # aberta na tela: vira a do joystick
    assert "CAM;GARAGEM" in placa.ouvir_ate(lambda m: "CAM;GARAGEM" in m)
    link.definir_cameras([camera(3, "QUINTAL", ptz=False)])
    placa.ouvir(0.2)
    placa.enviar("BTN;J")
    assert "CAM;-" in placa.ouvir_ate(lambda m: "CAM;-" in m)  # nenhuma com PTZ (E10)
    assert trocas[:3] == [1, 2, 1] and trocas[-1] is None


def test_joystick_vai_para_quem_assinou_e_lixo_e_ignorado(
    ambiente: tuple[LinkBitDog, PlacaDeTeste, Config], caplog: pytest.LogCaptureFixture
) -> None:
    link, placa, _ = ambiente
    movimentos: list[tuple[float, float]] = []
    link.assinar_joystick(lambda x, y: movimentos.append((x, y)))
    with caplog.at_level(logging.WARNING, logger="link.bitdog"):
        placa.enviar(b"JOY;9;9\n")  # fora da faixa
        placa.enviar(b"\xff\xfe")
        placa.enviar("JOY;0.75;-0.50")
        placa.ouvir_ate(lambda _m: bool(movimentos))
    assert movimentos == [(0.75, -0.5)]
    assert any("ignorada" in r.getMessage() for r in caplog.records)
    placa.ouvir_ate(lambda _m: link.estado().conectada)  # continua funcionando
    assert link.estado().conectada


def test_modo_da_placa(ambiente: tuple[LinkBitDog, PlacaDeTeste, Config]) -> None:
    link, placa, _ = ambiente
    placa.enviar("BTN;B")
    placa.enviar("MODO;NOITE")
    placa.ouvir_ate(lambda _m: link.estado().modo == "NOITE")
    assert link.estado().modo == "NOITE"


def test_sem_ip_configurado_aprende_pelo_primeiro_pacote(tmp_path: Path) -> None:
    copia = tmp_path / "config.yaml"
    copia.write_text(ARQUIVO_PADRAO.read_text(encoding="utf-8"), encoding="utf-8")
    config = Config.carregar(copia)
    config.definir("placa.ip", "")  # o config.yaml real pode ter o IP da placa do Diego
    porta_pc, porta_placa = porta_livre(), porta_livre()
    config.definir("placa.porta_pc", porta_pc)
    config.definir("placa.porta_placa", porta_placa)
    config.definir("placa.intervalo_ping_s", 0.2)
    placa = PlacaDeTeste(porta_placa, porta_pc)
    link = LinkBitDog(config, endereco="127.0.0.1").iniciar()
    try:
        assert placa.ouvir(0.5) == [] and link.estado().ip is None  # não sabe para onde mandar
        placa.enviar("OLA")
        novas = placa.ouvir_ate(lambda m: any(x.startswith("PING") for x in m))
        assert link.estado().ip == "127.0.0.1" and any(m.startswith("HORA") for m in novas)
    finally:
        link.parar()
        placa.socket.close()


def test_ip_configurado_ignora_pacotes_de_outros(tmp_path: Path) -> None:
    copia = tmp_path / "config.yaml"
    copia.write_text(ARQUIVO_PADRAO.read_text(encoding="utf-8"), encoding="utf-8")
    config = Config.carregar(copia)
    config.definir("placa.ip", "192.168.0.60")
    link = LinkBitDog(config)  # sem iniciar: só a regra
    botoes: list[str] = []
    link.assinar_botao(botoes.append)
    link._receber(b"BTN;A\n", "192.168.0.99")  # noqa: SLF001 (alguém da rede "fingindo" ser a placa)
    link._receber(b"BTN;B\n", "192.168.0.60")  # noqa: SLF001
    assert botoes == ["B"]


def esperar(condicao: Callable[[], bool], limite_s: float = 3.0) -> bool:
    fim = time.monotonic() + limite_s
    while time.monotonic() < fim:
        if condicao():
            return True
        time.sleep(0.02)
    return condicao()


def test_porta_em_uso_vira_erro_claro_e_da_para_trocar_sem_reiniciar(tmp_path: Path) -> None:
    copia = tmp_path / "config.yaml"
    copia.write_text(ARQUIVO_PADRAO.read_text(encoding="utf-8"), encoding="utf-8")
    config = Config.carregar(copia)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as ocupada:
        ocupada.bind(("127.0.0.1", 0))
        config.definir("placa.porta_pc", ocupada.getsockname()[1])
        link = LinkBitDog(config, endereco="127.0.0.1").iniciar()
        try:
            assert esperar(lambda: "porta UDP" in link.estado().erro)
            link.reconfigurar(None, 5006, porta_livre())  # a pessoa escolhe outra porta em Ajustes
            assert esperar(lambda: link.estado().erro == "")
        finally:
            link.parar()


def test_o_pc_puxa_a_conversa_com_o_ip_cadastrado(tmp_path: Path) -> None:
    """O fluxo novo: IP da placa cadastrado no app; a placa não precisa saber o IP do PC."""
    copia = tmp_path / "config.yaml"
    copia.write_text(ARQUIVO_PADRAO.read_text(encoding="utf-8"), encoding="utf-8")
    config = Config.carregar(copia)
    config.definir("placa.ip", "")  # o config.yaml real pode ter o IP da placa do Diego
    porta_pc, porta_placa = porta_livre(), porta_livre()
    config.definir("placa.porta_pc", porta_pc)
    config.definir("placa.intervalo_ping_s", 0.2)
    placa = PlacaDeTeste(porta_placa, porta_pc=1)  # não sabe a porta do PC: aprende pela origem
    link = LinkBitDog(config, endereco="127.0.0.1").iniciar()
    try:
        link.definir_cameras([camera(1, "FRENTE")])
        assert placa.ouvir(0.4) == []  # sem IP cadastrado: o PC espera
        link.reconfigurar("127.0.0.1", porta_placa, porta_pc)  # "Salvar e conectar"
        novas = placa.ouvir_ate(lambda m: "CAM;FRENTE" in m)
        assert link.estado().conectada and link.estado().configurada
        novas += placa.ouvir_ate(lambda m: any(x.startswith("SOM;") for x in m))
        assert any(m.startswith("HORA;") for m in novas) and "CAM;FRENTE" in novas  # "ao conectar", sem OLA
        assert any(m.startswith("SOM;") for m in novas)  # e o jeito do buzzer (E13)
        nova_porta = porta_livre()
        link.reconfigurar("127.0.0.1", porta_placa, nova_porta)  # trocou a porta do PC: reabre o socket
        placa.ouvir_ate(lambda _m: placa.porta_pc == nova_porta and link.estado().conectada)
        assert link.estado().conectada and link.porta_pc == nova_porta
    finally:
        link.parar()
        placa.socket.close()


def test_simulador_responde_para_a_porta_de_onde_o_pc_falou() -> None:
    """Com um roteador no meio (NAT), a porta de origem pode não ser a 5005: responder a ela funciona."""
    simulador = ConexaoPlaca(None, porta_placa=porta_livre(), endereco="127.0.0.1")
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as pc:
        pc.bind(("127.0.0.1", 0))
        pc.settimeout(2)
        try:
            assert simulador.iniciar() == []  # sem saber o PC, não manda OLA
            pc.sendto(b"PING;7", ("127.0.0.1", simulador.placa.porta))
            assert esperar(lambda: bool(simulador.processar()) and simulador.destino is not None)
            assert simulador.destino == pc.getsockname()
            respostas = {pc.recvfrom(64)[0].strip() for _ in range(2)}
            assert respostas == {b"MODO;NORMAL", b"PONG;7"}  # conheceu o PC: conta o modo e responde
        finally:
            simulador.fechar()
