"""Protocolo da placa (link/protocolo.py) e a lógica da placa simulada (tools/simulador_bitdoglab.py)."""

from __future__ import annotations

from datetime import datetime

import pytest

from core.modelos import Nivel
from link import protocolo
from link.protocolo import Botao, ErroProtocolo, Joy, Modo, Ola, Pong, interpretar
from tools.simulador_bitdoglab import ErroMensagem, PlacaSimulada


# ===================================================================== PC -> placa
def test_mensagens_do_pc_iguais_aos_exemplos_do_documento() -> None:
    assert protocolo.alerta("FRENTE", "PESSOA", 12.9, 0.871, 7, Nivel.ALTO) == "ALERTA;FRENTE;PESSOA;12;0.87;7;ALTO\n"
    assert protocolo.limpar("FRENTE", 7) == "LIMPAR;FRENTE;7\n"
    assert protocolo.passou("FRENTE", "CARRO") == "PASSOU;FRENTE;CARRO\n"
    assert protocolo.camera("GARAGEM") == "CAM;GARAGEM\n"
    assert protocolo.camera(None) == "CAM;-\n"
    assert protocolo.ping(42) == "PING;42\n" and protocolo.ping(65536) == "PING;0\n"
    assert protocolo.hora(datetime(2026, 10, 11, 9, 5)) == "HORA;09:05\n"


def test_maior_mensagem_cabe_em_64_bytes() -> None:
    maior = protocolo.alerta("ABCDEFGHIJ", "DESCONHECIDO", 99999, 1.0, 999999, "MEDIO")
    assert len(maior.encode("ascii")) == 55  # seção 8 do protocolo
    assert protocolo.alerta("A", "GATO", 500000, 3.0, 1, "BAIXO") == "ALERTA;A;GATO;99999;1.00;1;BAIXO\n"


@pytest.mark.parametrize(
    "montar",
    [
        lambda: protocolo.camera("frente"),  # minúsculas
        lambda: protocolo.camera("CALÇADA"),  # acento
        lambda: protocolo.camera("ABCDEFGHIJK"),  # 11 caracteres
        lambda: protocolo.passou("FRENTE", "BALAO"),
        lambda: protocolo.limpar("FRENTE", 0),
        lambda: protocolo.alerta("FRENTE", "PESSOA", 1, 0.5, 1, "URGENTE"),
    ],
)
def test_mensagens_invalidas_nao_sao_montadas(montar) -> None:  # noqa: ANN001
    with pytest.raises(ErroProtocolo):
        montar()


# ===================================================================== placa -> PC
@pytest.mark.parametrize(
    ("dados", "esperado"),
    [
        (b"JOY;-0.80;0.00\n", Joy(-0.8, 0.0)),
        (b"JOY;0.00;0.00", Joy(0.0, 0.0)),  # sem \n também vale (E11)
        (b"BTN;J\r\n", Botao("J")),  # \r ignorado
        (b"MODO;NOITE\n", Modo("NOITE")),
        (b"PONG;42\n", Pong(42)),
        (b"OLA\n", Ola()),
    ],
)
def test_interpreta_as_mensagens_da_placa(dados: bytes, esperado: object) -> None:
    assert interpretar(dados) == esperado


@pytest.mark.parametrize(
    "dados",
    [
        b"OLA" + b" " * 70,  # mais de 64 bytes
        "OLÁ\n".encode(),  # fora do ASCII
        b"",
        b"PIN;1\n",  # comando desconhecido
        b"JOY;0.50\n",  # campos a menos
        b"OLA;1\n",  # campos a mais
        b"JOY;1.50;0.00\n",  # fora da faixa
        b"JOY;nan;0.00\n",
        b"JOY;1e0;0.00\n",
        b"PONG;70000\n",
        b"PONG;-1\n",
        b"BTN;X\n",
        b"MODO;DIA\n",
        b"OLA\nOLA\n",  # duas mensagens num datagrama
        b"BTN;\x07\n",  # caractere de controle
    ],
)
def test_mensagens_invalidas_da_placa_sao_recusadas(dados: bytes) -> None:
    with pytest.raises(ErroProtocolo):
        interpretar(dados)


def test_simulador_aceita_tudo_o_que_o_pc_monta() -> None:
    """O simulador tem o próprio leitor (como o firmware): os dois lados têm de concordar."""
    placa = PlacaSimulada()
    for mensagem in (
        protocolo.alerta("ABCDEFGHIJ", "DESCONHECIDO", 99999, 1.0, 999999, "MEDIO"),
        protocolo.limpar("ABCDEFGHIJ", 999999),
        protocolo.passou("FRENTE", "CARRO"),
        protocolo.camera(None),
        protocolo.camera("CAM_01-A"),
        protocolo.ping(65535),
        protocolo.hora(datetime(2026, 1, 1, 23, 59)),
        protocolo.som(True),
        protocolo.som(False),
    ):
        placa.receber(mensagem.encode("ascii"), 0.0)


# ===================================================================== placa simulada
def test_placa_se_apresenta_e_repete_ola_ate_o_pc_responder() -> None:
    placa = PlacaSimulada()
    assert placa.ligar(0.0) == ["OLA", "MODO;NORMAL"]
    assert placa.tique(4.9) == []
    assert placa.tique(5.0) == ["OLA", "MODO;NORMAL"]  # E8
    assert placa.receber(b"PING;3\n", 6.0).enviar == ["PONG;3"]
    assert placa.tique(20.0) == []  # o PC respondeu: para de repetir


def test_alerta_novo_toca_e_reenvio_so_atualiza() -> None:
    placa = PlacaSimulada()
    saida = placa.receber(b"ALERTA;FRENTE;PESSOA;8;0.87;7;ALTO\n", 0.0)
    assert saida.buzzer == "ALTO" and placa.alerta is not None and placa.alerta.segundos == 8
    assert placa.receber(b"ALERTA;FRENTE;PESSOA;10;0.88;7;ALTO\n", 2.0).buzzer is None  # E1
    assert placa.alerta.segundos == 10
    assert placa.receber(b"ALERTA;FRENTE;CARRO;30;0.70;9;MEDIO\n", 3.0).buzzer == "MEDIO"  # outro objeto
    assert placa.receber(b"ALERTA;FRENTE;GATO;20;0.70;4;BAIXO\n", 4.0).buzzer is None  # BAIXO: sem som
    placa.apertar("B")  # NOITE
    assert placa.receber(b"ALERTA;FRENTE;PESSOA;8;0.90;5;ALTO\n", 5.0).buzzer is None


def test_limpar_so_vale_para_o_alerta_exibido_e_alerta_expira() -> None:
    placa = PlacaSimulada()
    placa.receber(b"ALERTA;FRENTE;PESSOA;8;0.87;7;ALTO\n", 0.0)
    placa.receber(b"LIMPAR;FRENTE;8\n", 1.0)  # outro id: ignora (E3)
    placa.receber(b"LIMPAR;GARAGEM;7\n", 1.0)  # outra câmera: ignora
    assert placa.alerta is not None
    placa.receber(b"LIMPAR;FRENTE;7\n", 1.5)
    assert placa.alerta is None
    placa.receber(b"ALERTA;FRENTE;PESSOA;8;0.87;7;ALTO\n", 10.0)
    placa.tique(15.9)
    assert placa.alerta is not None
    placa.tique(16.1)  # E2: 6 s sem reenvio
    assert placa.alerta is None


def test_botoes_da_placa() -> None:
    placa = PlacaSimulada()
    placa.receber(b"ALERTA;FRENTE;PESSOA;8;0.87;7;ALTO\n", 0.0)
    assert placa.apertar("AA") == ["BTN;A"] and placa.buzzer_silenciado  # 2 toques: cala o alerta atual
    assert placa.apertar("B") == ["BTN;B", "MODO;NOITE"]
    assert placa.apertar("B") == ["BTN;B", "MODO;NORMAL"]
    assert placa.apertar("J") == ["BTN;J"]


def test_botao_a_um_toque_liga_e_desliga_o_mudo() -> None:
    """Pedido do Diego (12/10): 1 toque silencia; outro toque volta ao que era antes."""
    placa = PlacaSimulada()
    assert placa.apertar("A") == [] and placa.mudo  # só na placa: nada vai ao PC
    saida = placa.receber(b"ALERTA;FRENTE;PESSOA;8;0.87;7;ALTO\n", 0.0)
    assert saida.buzzer is None and not placa.som_permitido  # mudo: o alerta aparece, mas sem som
    assert placa.linhas_oled(0.0)[-1][0].endswith("SEM SOM")
    assert placa.apertar("A") == [] and not placa.mudo and placa.som_permitido  # voltou ao normal
    assert placa.receber(b"ALERTA;FRENTE;CARRO;3;0.70;9;MEDIO\n", 1.0).buzzer == "MEDIO"


def test_som_continuo_toca_ate_o_alerta_acabar() -> None:
    placa = PlacaSimulada()
    assert placa.toque_continuo() is None
    placa.receber(b"SOM;CONTINUO\n", 0.0)
    assert placa.som_continuo
    placa.receber(b"ALERTA;FRENTE;PESSOA;8;0.87;7;ALTO\n", 0.0)
    assert placa.toque_continuo() == "ALTO"  # continua tocando enquanto o alerta existir
    placa.apertar("AA")
    assert placa.toque_continuo() is None  # 2 toques no A: calou este alerta
    placa.receber(b"ALERTA;FRENTE;CARRO;3;0.70;9;MEDIO\n", 1.0)
    assert placa.toque_continuo() == "MEDIO"  # alerta novo: volta a tocar
    placa.receber(b"LIMPAR;FRENTE;9\n", 2.0)
    assert placa.toque_continuo() is None  # o objeto saiu: acabou
    placa.receber(b"SOM;UMA\n", 3.0)
    placa.receber(b"ALERTA;FRENTE;GATO;3;0.70;4;MEDIO\n", 3.0)
    assert not placa.som_continuo and placa.toque_continuo() is None  # SOM;UMA: só o bipe da chegada
    with pytest.raises(ErroMensagem):
        placa.receber(b"SOM;SEMPRE\n", 4.0)


def test_joystick_zona_morta_so_quando_muda_repeticao_e_limite() -> None:
    placa = PlacaSimulada()
    placa.receber(b"PING;0", 0.0)  # o PC já respondeu (senão o tique repetiria o OLA)
    assert placa.mover_joystick(0.05, -0.1, 0.0) == []  # zona morta = 0.00 (não mudou)
    assert placa.mover_joystick(0.754, 0.0, 0.0) == ["JOY;0.75;0.00"]
    assert placa.mover_joystick(0.751, 0.0, 0.2) == []  # igual depois de arredondar
    assert placa.mover_joystick(0.9, 0.0, 0.3) == ["JOY;0.90;0.00"]
    assert placa.mover_joystick(0.5, 0.0, 0.32) == []  # rápido demais (> 20/s): fica pendente
    assert placa.tique(0.33) == [] and placa.tique(0.36) == ["JOY;0.50;0.00"]
    assert placa.tique(1.3) == [] and placa.tique(1.37) == ["JOY;0.50;0.00"]  # E5: repete a cada 1 s
    assert placa.mover_joystick(0.0, 0.0, 1.5) == ["JOY;0.00;0.00"]  # soltou
    assert placa.tique(5.0) == []  # no centro não repete


@pytest.mark.parametrize(
    "dados",
    [
        b"ALERTA;FRENTE;PESSOA;8;0.9;7;ALTO\n",  # confiança com 1 casa
        b"ALERTA;FRENTE;BALAO;8;0.90;7;ALTO\n",
        b"ALERTA;FRENTE;PESSOA;8;1.50;7;ALTO\n",
        b"ALERTA;FRENTE;PESSOA;8;0.90;0;ALTO\n",
        b"HORA;25:00\n",
        b"CAM;frente\n",
        b"PING\n",
        b"JOY;0.00;0.00\n",  # mensagem da placa, não do PC
        b"PING;1" + b"0" * 70,
    ],
)
def test_placa_ignora_mensagens_invalidas(dados: bytes) -> None:
    with pytest.raises(ErroMensagem):
        PlacaSimulada().receber(dados, 0.0)


def test_display_mostra_o_ip_ate_o_pc_conectar_e_quando_ele_some() -> None:
    """A pessoa lê o IP no display e cadastra no app; sem PING por 6 s, o IP volta a aparecer."""
    placa = PlacaSimulada(ip_local="192.168.1.50", porta=5006)
    placa.conhece_pc = False  # sem --ip-pc: não sabe onde está o PC
    assert [t for t, _ in placa.linhas_oled(0.0)] == ["AGUARDANDO PC", "IP 192.168.1.50", "PORTA 5006", "Cadastre no app"]
    assert placa.ligar(0.0) and placa.tique(10.0) == []  # não fica mandando OLA para lugar nenhum
    placa.receber(b"PING;0\n", 11.0)
    textos = [t for t, _ in placa.linhas_oled(11.0)]
    assert textos[0] == "MONITORANDO" and "IP 192.168.1.50" in textos
    assert [t for t, _ in placa.linhas_oled(17.5)][0] == "AGUARDANDO PC"  # o app fechou


def test_oled_e_matriz() -> None:
    placa = PlacaSimulada()
    placa.receber(b"PING;0\n", 0.0)  # o PC está conversando com a placa
    placa.receber(b"HORA;21:35\n", 0.0)
    placa.receber(b"CAM;FRENTE\n", 0.0)
    placa.receber(b"PASSOU;FRENTE;CARRO\n", 0.0)
    textos = [t for t, _ in placa.linhas_oled(0.0)]
    assert textos[0] == "MONITORANDO" and "HORA 21:35" in textos and "CAM FRENTE" in textos
    assert any("CARRO" in t for t in textos)
    assert all(c is None for linha in placa.matriz(0.0) for c in linha)

    placa.receber(b"ALERTA;FRENTE;PESSOA;12;0.87;7;ALTO\n", 10.0)
    textos = [t for t, _ in placa.linhas_oled(13.4)]
    assert textos[:3] == ["ALERTA FRENTE", "PESSOA", "PARADO 15s"]
    acesas = {c for linha in placa.matriz(10.0) for c in linha if c}
    apagada = {c for linha in placa.matriz(10.25) for c in linha if c}
    assert acesas == {"#EF4444"} and apagada == set()  # ALTO: vermelho piscando
    placa.receber(b"ALERTA;FRENTE;DESCONHECIDO;15;0.00;3;MEDIO\n", 20.0)
    assert {c for linha in placa.matriz(20.0) for c in linha if c} == {"#3B82F6"}  # DESCONHECIDO: azul
