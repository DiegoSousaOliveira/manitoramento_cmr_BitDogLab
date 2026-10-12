"""Regra de "parado na zona" (vision/dwell.py) e geometria da zona — trilhas simuladas, sem vídeo."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace

import pytest

from core.config import Config
from core.modelos import DESCONHECIDO, Camera, Deteccao, Nivel, TipoFonte
from vision.dwell import AjustesParado, Aviso, RegraParado, TipoAviso
from vision.zona import area_poligono, indice_insercao, poligono_simples, validar_zona

PASSO = 1 / 6  # a IA analisa ~6 quadros por segundo por vista
ESQUERDA = [(0.0, 0.0), (0.5, 0.0), (0.5, 1.0), (0.0, 1.0)]  # zona = metade esquerda


@pytest.fixture(scope="module")
def ajustes() -> AjustesParado:
    """Os padrões do config.yaml (os da especificação), sem aquecimento.

    Os testes antigos usam lente SEM zona vigiando a imagem inteira (e todas as classes alertando):
    fixa isso aqui, porque o config.yaml real é do usuário (o padrão novo é "ignorar").
    """
    return replace(AjustesParado.do_config(Config.carregar()), aquecimento_s=0.0, sem_zona="imagem_inteira",
                   alertar={}, cooldown_s=120.0, tolerancia_sumico_s=2.0, limpar_apos_sumico_s=5.0,
                   limite_movimento=0.05, passou_min_s=0.5)  # valores da especificação (o usuário muda os dele)


def obj(id_: int, x: float, y: float = 0.5, classe: str = "PESSOA", conf: float = 0.9, ausente: float = 0.0) -> Deteccao:
    return Deteccao(classe, conf, (x - 0.05, y - 0.1, x + 0.05, y + 0.1), id_, ausente_s=ausente)


Gerador = Callable[[float], list[Deteccao]]


def rodar(regra: RegraParado, gerar: Gerador, fim: float, inicio: float = 0.0, proporcao: float = 0.5625) -> list[Aviso]:
    """Chama a regra como a IA chamaria: um resultado a cada ``PASSO`` segundos."""
    avisos: list[Aviso] = []
    n = 0
    while (t := inicio + n * PASSO) <= fim + 1e-9:
        avisos += regra.atualizar(gerar(t), t, proporcao)
        n += 1
    return avisos


def do_tipo(avisos: list[Aviso], tipo: TipoAviso) -> list[Aviso]:
    return [a for a in avisos if a.tipo is tipo]


# ===================================================================== o básico
def test_pessoa_parada_8s_gera_um_unico_alerta(ajustes: AjustesParado) -> None:
    regra = RegraParado((1, 1), ajustes)
    avisos = rodar(regra, lambda t: [obj(7, 0.3)], fim=60)
    assert len(avisos) == 1
    alerta = avisos[0]
    assert alerta.tipo is TipoAviso.ALERTA and alerta.chave == (1, 1) and alerta.id_rastreio == 7
    assert alerta.classe == "PESSOA" and alerta.nivel is Nivel.ALTO and alerta.confianca == pytest.approx(0.9)
    assert 8.0 <= alerta.instante < 8.0 + PASSO + 1e-6 and alerta.segundos >= 8.0


def test_caixa_tremendo_nao_zera_o_relogio(ajustes: AjustesParado) -> None:
    regra = RegraParado((1, 1), ajustes)
    tremido = [0.0, 0.02, -0.015, 0.01, -0.02]  # a caixa da IA "treme" uns pixels
    avisos = rodar(regra, lambda t: [obj(1, 0.3 + tremido[round(t / PASSO) % 5], 0.5 + tremido[round(t / PASSO) % 3])], 12)
    assert [a.tipo for a in avisos] == [TipoAviso.ALERTA]


def test_pessoa_andando_devagar_nao_alerta(ajustes: AjustesParado) -> None:
    regra = RegraParado((1, 1), ajustes)
    avisos = rodar(regra, lambda t: [obj(1, 0.05 + t * 0.03)], fim=29)  # 3% da largura por segundo
    assert do_tipo(avisos, TipoAviso.ALERTA) == []


def test_tempo_e_nivel_dependem_da_classe(ajustes: AjustesParado) -> None:
    regra = RegraParado((1, 1), ajustes)
    avisos = rodar(regra, lambda t: [obj(1, 0.2, classe="CARRO"), obj(2, 0.7, classe="GATO")], fim=40)
    por_id = {a.id_rastreio: a for a in avisos}
    assert 30.0 <= por_id[1].instante < 30.0 + PASSO + 1e-6 and por_id[1].nivel is Nivel.MEDIO  # CARRO: 30 s
    assert 20.0 <= por_id[2].instante < 20.0 + PASSO + 1e-6 and por_id[2].nivel is Nivel.BAIXO  # GATO: 20 s


def test_so_alerta_dentro_da_zona(ajustes: AjustesParado) -> None:
    regra = RegraParado((1, 1), ajustes, zona=ESQUERDA)
    avisos = rodar(regra, lambda t: [obj(1, 0.25), obj(2, 0.75)], fim=20)
    assert [(a.tipo, a.id_rastreio) for a in avisos] == [(TipoAviso.ALERTA, 1)]


def test_deslocamento_vertical_e_medido_em_larguras(ajustes: AjustesParado) -> None:
    """Numa imagem 16:9, subir 8% da ALTURA é andar só 4,5% da LARGURA: ainda é "parado"."""

    def sobe_e_desce(t: float) -> list[Deteccao]:
        return [obj(1, 0.3, 0.46 if round(t / PASSO) % 2 else 0.54)]

    assert do_tipo(rodar(RegraParado((1, 1), ajustes), sobe_e_desce, 10, proporcao=0.5625), TipoAviso.ALERTA)
    assert not do_tipo(rodar(RegraParado((1, 1), ajustes), sobe_e_desce, 10, proporcao=1.0), TipoAviso.ALERTA)


def test_objeto_que_a_ia_perdeu_ha_instantes_conta_como_presente(ajustes: AjustesParado) -> None:
    regra = RegraParado((1, 1), ajustes)
    avisos = rodar(regra, lambda t: [obj(1, 0.3, ausente=0.3 if round(t / PASSO) % 2 else 0.0)], fim=9)
    assert [a.tipo for a in avisos] == [TipoAviso.ALERTA]


# ===================================================================== oclusão e LIMPAR
def test_oclusao_curta_nao_zera_o_relogio(ajustes: AjustesParado) -> None:
    regra = RegraParado((1, 1), ajustes)
    avisos = rodar(regra, lambda t: [] if 4.0 <= t < 5.5 else [obj(1, 0.3)], fim=9)  # some 1,5 s
    alertas = do_tipo(avisos, TipoAviso.ALERTA)
    assert len(alertas) == 1 and alertas[0].instante < 8.0 + PASSO + 1e-6


def test_oclusao_longa_zera_o_relogio_sem_dar_passou(ajustes: AjustesParado) -> None:
    regra = RegraParado((1, 1), ajustes)
    avisos = rodar(regra, lambda t: [] if 4.0 <= t < 7.0 else [obj(1, 0.3)], fim=20)  # some 3 s
    assert [a.tipo for a in avisos] == [TipoAviso.ALERTA]  # nada de PASSOU: ele voltou
    assert avisos[0].instante >= 7.0 + 8.0 - 1e-6  # relógio recomeçou quando voltou


def test_alertado_que_sai_da_zona_gera_limpar_com_o_mesmo_id(ajustes: AjustesParado) -> None:
    regra = RegraParado((1, 1), ajustes, zona=ESQUERDA)
    avisos = rodar(regra, lambda t: [obj(4, 0.25 if t < 12 else 0.8)], fim=15)
    assert [(a.tipo, a.id_rastreio) for a in avisos] == [(TipoAviso.ALERTA, 4), (TipoAviso.LIMPAR, 4)]
    assert avisos[1].motivo == "saiu" and regra.ativos() == []


def test_alertado_que_some_gera_limpar_depois_de_5s_mesmo_sem_imagem(ajustes: AjustesParado) -> None:
    regra = RegraParado((1, 1), ajustes)
    rodar(regra, lambda t: [obj(1, 0.3)], fim=10)  # alertou em ~8 s, visto pela última vez em 10 s
    assert len(regra.ativos()) == 1
    assert regra.expirar(14.9) == []  # a câmera parou de mandar imagem: ainda espera
    limpar = regra.expirar(15.1)
    assert [(a.tipo, a.motivo, a.id_rastreio) for a in limpar] == [(TipoAviso.LIMPAR, "sumiu", 1)]
    assert regra.expirar(16.0) == [] and regra.ativos() == []


def test_encerrar_limpa_os_alertas_ativos(ajustes: AjustesParado) -> None:
    regra = RegraParado((1, 1), ajustes)
    rodar(regra, lambda t: [obj(1, 0.3)], fim=9)
    assert [(a.tipo, a.motivo) for a in regra.encerrar(9.5)] == [(TipoAviso.LIMPAR, "camera")]


def test_cooldown_por_id(ajustes: AjustesParado) -> None:
    """Alerta, sai, volta e para de novo: só alerta outra vez depois de cooldown_s (120 s)."""
    regra = RegraParado((1, 1), ajustes, zona=ESQUERDA)

    def trilha(t: float) -> list[Deteccao]:
        return [obj(1, 0.8 if 12 <= t < 20 else 0.25)]  # sai da zona entre 12 e 20 s

    avisos = rodar(regra, trilha, fim=140)
    alertas = do_tipo(avisos, TipoAviso.ALERTA)
    assert len(alertas) == 2 and len(do_tipo(avisos, TipoAviso.LIMPAR)) == 1
    assert alertas[1].instante - alertas[0].instante >= 120.0


# ===================================================================== aquecimento
def test_aquecimento_ignora_carro_ja_estacionado_ate_ele_se_mover(ajustes: AjustesParado) -> None:
    regra = RegraParado((1, 1), replace(ajustes, aquecimento_s=20.0))

    def trilha(t: float) -> list[Deteccao]:
        x = 0.3 if t < 100 else 0.5  # estacionado desde o início; em 100 s manobra e para mais à frente
        return [obj(1, x, classe="CARRO")]

    avisos = rodar(regra, trilha, fim=140)
    alertas = do_tipo(avisos, TipoAviso.ALERTA)
    assert len(alertas) == 1 and alertas[0].instante >= 130.0  # só depois de se mover e parar 30 s


def test_aquecimento_nao_ignora_quem_chega_andando(ajustes: AjustesParado) -> None:
    regra = RegraParado((1, 1), replace(ajustes, aquecimento_s=20.0))
    avisos = rodar(regra, lambda t: [obj(1, min(0.1 + t * 0.1, 0.5))], fim=14)  # chega e para em 4 s
    alertas = do_tipo(avisos, TipoAviso.ALERTA)
    # Parou em 4 s; os últimos 5% da chegada já contam como "parado" (âncora em ~3,5 s).
    assert len(alertas) == 1 and 11.0 <= alertas[0].instante < 12.5


def test_camera_sem_imagem_por_um_tempo_reinicia_o_aquecimento(ajustes: AjustesParado) -> None:
    regra = RegraParado((1, 1), replace(ajustes, aquecimento_s=20.0))
    rodar(regra, lambda t: [], fim=30)  # câmera ligada, nada na cena
    avisos = rodar(regra, lambda t: [obj(1, 0.3, classe="CARRO")], inicio=60, fim=120)  # caiu 30 s e voltou
    assert do_tipo(avisos, TipoAviso.ALERTA) == []  # o carro já estava lá quando ela voltou


# ===================================================================== PASSOU
def test_passou_uma_vez_por_id_ao_atravessar(ajustes: AjustesParado) -> None:
    regra = RegraParado((1, 1), ajustes, zona=ESQUERDA)

    def vai_e_volta(t: float) -> list[Deteccao]:
        x = 0.1 + t * 0.2 if t < 4 else 0.9 - (t - 4) * 0.2  # atravessa, volta e atravessa de novo
        return [obj(3, x)]

    avisos = rodar(regra, vai_e_volta, fim=8)
    assert [(a.tipo, a.id_rastreio, a.classe) for a in avisos] == [(TipoAviso.PASSOU, 3, "PESSOA")]


def test_passou_ignora_ruido_e_desconhecido(ajustes: AjustesParado) -> None:
    regra = RegraParado((1, 1), ajustes, zona=ESQUERDA)
    rapido = rodar(regra, lambda t: [obj(1, 0.45 if t < 0.3 else 0.9)], fim=2)  # 0,3 s na zona
    folha = rodar(regra, lambda t: [obj(2, 0.1 + t * 0.2, classe="DESCONHECIDO")], inicio=3, fim=8)
    assert rapido == [] and folha == []
    com_desconhecido = RegraParado((1, 1), replace(ajustes, passou_desconhecido=True), zona=ESQUERDA)
    assert do_tipo(rodar(com_desconhecido, lambda t: [obj(2, 0.1 + t * 0.2, classe="DESCONHECIDO")], 4), TipoAviso.PASSOU)


def test_passou_quando_some_dentro_da_zona_so_depois_de_5s(ajustes: AjustesParado) -> None:
    regra = RegraParado((1, 1), ajustes, zona=ESQUERDA)
    avisos = rodar(regra, lambda t: [obj(5, 0.1 + t * 0.1)] if t < 2 else [], fim=10)  # some no meio da zona
    passou = do_tipo(avisos, TipoAviso.PASSOU)
    assert len(passou) == 1 and passou[0].id_rastreio == 5


def test_alertado_nao_gera_passou(ajustes: AjustesParado) -> None:
    regra = RegraParado((1, 1), ajustes, zona=ESQUERDA)
    avisos = rodar(regra, lambda t: [obj(1, 0.25)] if t < 10 else [], fim=20)
    assert [(a.tipo, a.motivo) for a in avisos] == [(TipoAviso.ALERTA, ""), (TipoAviso.LIMPAR, "sumiu")]


# ===================================================================== troca de ID
def test_id_novo_no_mesmo_lugar_herda_o_alerta(ajustes: AjustesParado) -> None:
    """O rastreador perdeu a pessoa parada e a "achou" de novo com outro ID."""
    regra = RegraParado((1, 1), ajustes, zona=ESQUERDA)

    def trilha(t: float) -> list[Deteccao]:
        if t < 20:
            return [obj(1, 0.25)]
        if t < 23:
            return []  # 3 s sem ver (rastreador desistiu do ID 1)
        return [obj(2, 0.26 if t < 40 else 0.8)]  # ID 2 no mesmo lugar; em 40 s sai da zona

    avisos = rodar(regra, trilha, fim=45)
    assert [(a.tipo, a.id_rastreio) for a in avisos] == [(TipoAviso.ALERTA, 1), (TipoAviso.LIMPAR, 1)]


def test_id_novo_depois_de_sumir_muito_tempo_respeita_o_cooldown(ajustes: AjustesParado) -> None:
    regra = RegraParado((1, 1), ajustes)

    def trilha(t: float) -> list[Deteccao]:
        return [obj(1, 0.3)] if t < 15 else [] if t < 30 else [obj(9, 0.31)]

    avisos = rodar(regra, trilha, fim=100)
    assert [a.tipo for a in avisos] == [TipoAviso.ALERTA, TipoAviso.LIMPAR]  # sem 2º alerta (cooldown 120 s)


def test_pessoa_diferente_em_outro_lugar_alerta_normalmente(ajustes: AjustesParado) -> None:
    regra = RegraParado((1, 1), ajustes)
    avisos = rodar(regra, lambda t: [obj(1, 0.3)] if t < 15 else [obj(2, 0.7)], fim=30)
    assert [(a.tipo, a.id_rastreio) for a in do_tipo(avisos, TipoAviso.ALERTA)] == [
        (TipoAviso.ALERTA, 1), (TipoAviso.ALERTA, 2)
    ]


# ===================================================================== consultas e ajustes
def test_progresso_para_o_contador_da_tela(ajustes: AjustesParado) -> None:
    regra = RegraParado((1, 1), ajustes, zona=ESQUERDA)
    rodar(regra, lambda t: [obj(1, 0.25), obj(2, 0.75)], fim=3)
    progresso = regra.progresso(3.0)
    assert set(progresso) == {1}  # o 2 está fora da zona
    assert progresso[1].segundos == pytest.approx(3.0, abs=0.01) and progresso[1].alvo == 8.0
    assert not progresso[1].alertado
    rodar(regra, lambda t: [obj(1, 0.25), obj(2, 0.75)], inicio=3 + PASSO, fim=9)
    assert regra.progresso(9.0)[1].alertado
    assert regra.progresso(12.0) == {}  # não é visto há 3 s: sai do contador


def test_ajustes_por_camera_sobrescrevem_o_config() -> None:
    config = Config.carregar()
    camera = Camera(
        nome="Frente", codigo="FRENTE", fonte=TipoFonte.ARQUIVO, ip="", porta_rtsp=554, usuario="",
        modelo_url="", porta_onvif=8899,
        ajustes={"parado.cooldown_s": 10, "parado.tempo_parado": {"CARRO": 60}, "parado.nivel": {"GATO": "alto"}},
    )
    ajustes = AjustesParado.do_config(config, camera)
    assert ajustes.cooldown_s == 10 and ajustes.tempo("CARRO") == 60 and ajustes.tempo("PESSOA") == 8
    assert ajustes.nivel_da("GATO") is Nivel.ALTO and ajustes.nivel_da("CACHORRO") is Nivel.BAIXO
    camera.ajustes = {"parado.nivel": {"GATO": "urgente"}}
    with pytest.raises(ValueError, match="BAIXO, MEDIO ou ALTO"):
        AjustesParado.do_config(config, camera)


# ===================================================================== geometria da zona
def test_indice_insercao_escolhe_a_aresta_mais_proxima() -> None:
    quadrado = [(0.2, 0.2), (0.8, 0.2), (0.8, 0.8), (0.2, 0.8)]
    assert indice_insercao(quadrado[:2], (0.5, 0.5)) == 2  # menos de 3 pontos: no fim
    assert indice_insercao(quadrado, (0.5, 0.15)) == 1  # perto do lado de cima
    assert indice_insercao(quadrado, (0.85, 0.5)) == 2  # lado direito
    assert indice_insercao(quadrado, (0.1, 0.5)) == 4  # lado esquerdo (entre o último e o primeiro)
    novo = quadrado.copy()
    novo.insert(indice_insercao(novo, (0.5, 0.95)), (0.5, 0.95))
    assert poligono_simples(novo)


def test_validar_zona() -> None:
    assert validar_zona(None) is None and validar_zona([]) is None  # sem zona = quadro inteiro
    assert "3 pontos" in (validar_zona([(0.1, 0.1), (0.5, 0.5)]) or "")
    laco = [(0.1, 0.1), (0.9, 0.9), (0.9, 0.1), (0.1, 0.9)]  # "gravata borboleta"
    assert not poligono_simples(laco) and "cruzam" in (validar_zona(laco) or "")
    assert "pequena" in (validar_zona([(0.5, 0.5), (0.51, 0.5), (0.5, 0.51)]) or "")
    assert validar_zona([(0.1, 0.1), (0.9, 0.1), (0.5, 0.9)]) is None
    assert area_poligono([(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)]) == pytest.approx(1.0)


# ===================================================================== lente sem zona e classes (12/10/2026)
def test_lente_sem_zona_nao_alerta_quando_configurado(ajustes: AjustesParado) -> None:
    """Relato do Diego: a zona estava só na Lente 2 e uma moto na Lente 1 (sem zona) gerou alerta."""
    parada = lambda _t: [obj(1, 0.25)]  # noqa: E731 (pessoa parada)
    sem_zona = RegraParado((1, 1), replace(ajustes, sem_zona="ignorar"), zona=None)
    assert rodar(sem_zona, parada, fim=30) == []  # nem ALERTA nem PASSOU
    assert sem_zona.progresso() == {} and not sem_zona.vigiando
    com_zona = RegraParado((1, 2), replace(ajustes, sem_zona="ignorar"), zona=ESQUERDA)
    assert do_tipo(rodar(com_zona, parada, fim=30), TipoAviso.ALERTA)  # a lente com zona alerta normal
    tudo = RegraParado((1, 1), replace(ajustes, sem_zona="imagem_inteira"), zona=None)
    assert do_tipo(rodar(tudo, parada, fim=30), TipoAviso.ALERTA)  # o jeito antigo continua disponível


def test_apagar_a_zona_limpa_o_alerta_da_lente(ajustes: AjustesParado) -> None:
    regra = RegraParado((1, 1), replace(ajustes, sem_zona="ignorar"), zona=ESQUERDA)
    avisos = rodar(regra, lambda _t: [obj(1, 0.25)], fim=10)
    assert do_tipo(avisos, TipoAviso.ALERTA)
    regra.zona = None  # a pessoa apagou a zona desta lente
    avisos = rodar(regra, lambda _t: [obj(1, 0.25)], inicio=10, fim=20)
    assert do_tipo(avisos, TipoAviso.LIMPAR)  # a placa não fica com um alerta "órfão"


def test_classe_com_alerta_desligado_so_aparece_na_tela(ajustes: AjustesParado) -> None:
    regra = RegraParado((1, 1), replace(ajustes, alertar={"PESSOA": True, DESCONHECIDO: False}), zona=ESQUERDA)

    def cena(_t: float) -> list[Deteccao]:
        return [obj(1, 0.25), obj(2, 0.4, y=0.2, classe=DESCONHECIDO, conf=0.5)]  # pessoa + "mancha" paradas

    alertas = do_tipo(rodar(regra, cena, fim=30), TipoAviso.ALERTA)
    assert [a.classe for a in alertas] == ["PESSOA"]  # a mancha (DESCONHECIDO) não alerta


def test_alertar_por_camera_sobrescreve_o_padrao() -> None:
    config = Config.carregar()
    camera = Camera(
        nome="Frente", codigo="FRENTE", fonte=TipoFonte.ARQUIVO, ip="", porta_rtsp=554, usuario="",
        modelo_url="", porta_onvif=8899, ajustes={"parado.alertar": {"GATO": False}},
    )
    ajustes = AjustesParado.do_config(config, camera)
    assert not ajustes.alerta_ligado("GATO") and ajustes.alerta_ligado("PESSOA")
