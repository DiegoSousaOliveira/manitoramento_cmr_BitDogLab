"""Visão computacional: movimento (MOG2), rastreador por vista, serviço de detecção e YOLO real."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import cv2
import numpy as np
import pytest

from camera.stream import LeitorVideo, LimitesLeitura
from core.config import Config
from core.modelos import DESCONHECIDO, Camera, DisposicaoLentes, TipoFonte, Vista
from vision.detector import EstadoIA, ResultadoVista, ServicoDeteccao
from vision.motion import AjustesMovimento, DetectorMovimento, interseccao_sobre_area
from vision.rastreador import AjustesRastreio, DeteccaoBruta, RastreadorVista
from tests.conftest import MODELO_ICSEE

NovaCamera = Callable[..., Camera]
AJUSTES_MOV = AjustesMovimento(largura_trabalho=160, historico=50, limiar=25, area_minima=0.005, aquecimento_quadros=5)
AJUSTES_RAST = AjustesRastreio(
    ia_fps=6, perdido_s=1.0, conf_minima=0.40, conf_deteccao=0.15, conf_movimento=0.5, sobreposicao=0.2,
    manter_s=0.0, reassociar=0.5,
)


def ajustes(**mudancas: float) -> AjustesRastreio:
    return replace(AJUSTES_RAST, **mudancas)


def esperar(condicao: Callable[[], bool], limite_s: float = 8.0) -> bool:
    fim = time.monotonic() + limite_s
    while time.monotonic() < fim:
        if condicao():
            return True
        time.sleep(0.02)
    return condicao()


def cena(x: int | None) -> np.ndarray:
    """Fundo texturizado fixo + (opcional) um quadrado branco na posição x."""
    rng = np.random.default_rng(0)
    imagem = (rng.integers(40, 80, size=(360, 640, 3))).astype(np.uint8)
    if x is not None:
        cv2.rectangle(imagem, (x, 150), (x + 60, 230), (255, 255, 255), -1)
    return imagem


# ===================================================================== movimento
def test_cena_parada_nao_tem_movimento() -> None:
    detector = DetectorMovimento(AJUSTES_MOV)
    for _ in range(30):
        resultado = detector.processar(cena(None))
    assert not resultado.tem_movimento and resultado.fracao < 0.01


def test_objeto_se_movendo_vira_regiao_no_lugar_certo() -> None:
    detector = DetectorMovimento(AJUSTES_MOV)
    for _ in range(30):
        detector.processar(cena(None))
    resultado = detector.processar(cena(300))
    assert resultado.tem_movimento
    x1, y1, x2, y2 = resultado.regioes[0]
    assert 0.42 < x1 < 0.5 and 0.52 < x2 < 0.6 and 0.38 < y1 < 0.45 and 0.6 < y2 < 0.67
    assert resultado.mascara is not None and resultado.mascara.shape == (90, 160)


def test_aquecimento_ignora_os_primeiros_quadros() -> None:
    detector = DetectorMovimento(AJUSTES_MOV)
    resultado = detector.processar(cena(300))  # 1º quadro: tudo é "novo", mas está aquecendo
    assert resultado.aquecendo and not resultado.tem_movimento


def test_interseccao_sobre_area() -> None:
    assert interseccao_sobre_area((0, 0, 0.2, 0.2), (0, 0, 1, 1)) == pytest.approx(1.0)
    assert interseccao_sobre_area((0, 0, 0.2, 0.2), (0.1, 0, 1, 1)) == pytest.approx(0.5)
    assert interseccao_sobre_area((0, 0, 0.1, 0.1), (0.5, 0.5, 1, 1)) == 0.0


# ===================================================================== rastreador
def pessoa(x: float, conf: float = 0.9) -> DeteccaoBruta:
    return DeteccaoBruta(0, conf, (x, 0.3, x + 0.1, 0.7))


def test_id_estavel_enquanto_o_objeto_anda() -> None:
    rastreador = RastreadorVista(AJUSTES_RAST)
    ids = set()
    for passo in range(12):
        (d,) = rastreador.atualizar([pessoa(0.1 + passo * 0.01)], [], instante=passo / 6)
        ids.add(d.id_rastreio)
        assert d.classe == "PESSOA" and d.origem == "ia"
    assert len(ids) == 1
    assert rastreador.desde(ids.pop()) == 0.0  # tempo na cena conta desde o 1º quadro


def test_rastreadores_separados_por_vista_nao_misturam_ids() -> None:
    rua, calcada = RastreadorVista(AJUSTES_RAST), RastreadorVista(AJUSTES_RAST)
    for passo in range(5):
        (a,) = rua.atualizar([pessoa(0.1)], [], passo / 6)
        (b,) = calcada.atualizar([pessoa(0.6)], [], passo / 6)
    assert a.id_rastreio == b.id_rastreio == 1  # cada vista conta do zero, sem interferência


def test_confianca_baixa_vira_desconhecido_mas_classe_conhecida_permanece() -> None:
    rastreador = RastreadorVista(AJUSTES_RAST)
    (nova,) = rastreador.atualizar([pessoa(0.5, conf=0.3)], [], 0.0)
    assert nova.classe == DESCONHECIDO
    rastreador2 = RastreadorVista(AJUSTES_RAST)
    for passo in range(3):
        rastreador2.atualizar([pessoa(0.5, conf=0.9)], [], passo / 6)
    (depois,) = rastreador2.atualizar([pessoa(0.5, conf=0.3)], [], 0.5)
    assert depois.classe == "PESSOA"  # já era PESSOA: não "pisca" para DESCONHECIDO


def test_movimento_sem_objeto_vira_desconhecido_e_com_objeto_nao() -> None:
    rastreador = RastreadorVista(AJUSTES_RAST)
    itens = rastreador.classificar([pessoa(0.5)], [(0.5, 0.3, 0.6, 0.7), (0.0, 0.0, 0.1, 0.1)])
    assert [(classe, origem) for _, _, classe, origem in itens] == [("PESSOA", "ia"), (DESCONHECIDO, "movimento")]
    (d,) = RastreadorVista(AJUSTES_RAST).atualizar([], [(0.0, 0.0, 0.1, 0.1)], 0.0)
    assert d.classe == DESCONHECIDO and d.origem == "movimento"


def test_classes_coco_que_nao_usamos_sao_ignoradas() -> None:
    assert RastreadorVista(AJUSTES_RAST).classificar([DeteccaoBruta(56, 0.9, (0, 0, 0.1, 0.1))], []) == []  # cadeira


def test_id_tolera_sumir_por_pouco_tempo() -> None:
    """Oclusão (ex.: passou atrás de um poste): o ID continua o mesmo por até ``perdido_s``.

    Regressão: o ByteTrack conta o "buffer" em quadros a 30 FPS; configurado errado,
    o ID morria em 0,5 s e o objeto voltava com ID novo (zerando o "tempo parado").
    """
    rastreador = RastreadorVista(ajustes(perdido_s=2.0))
    for passo in range(4):
        (antes,) = rastreador.atualizar([pessoa(0.5)], [], passo / 6)
    for passo in range(4, 14):  # 10 quadros = 1,7 s sem ver
        assert rastreador.atualizar([], [], passo / 6) == []
    (depois,) = rastreador.atualizar([pessoa(0.5)], [], 14 / 6)
    assert depois.id_rastreio == antes.id_rastreio
    assert rastreador.desde(depois.id_rastreio) == 0.0  # o tempo na cena NÃO zerou


# ---- estabilidade (sintomas relatados pelo Diego na câmera real)
def carro(x: float, conf: float = 0.8) -> DeteccaoBruta:
    return DeteccaoBruta(2, conf, (x, 0.40, x + 0.12, 0.55))


def test_falha_de_um_quadro_nao_faz_a_caixa_piscar() -> None:
    """Sintoma 1: o YOLO perde o carro em 1 quadro -> antes a caixa sumia (piscava)."""
    rastreador = RastreadorVista(ajustes(perdido_s=3.0, manter_s=1.0))
    for i, tem in enumerate([1, 1, 1, 0, 1, 1]):
        deteccoes = rastreador.atualizar([carro(0.2 + i * 0.02)] if tem else [], [], i / 6)
        assert [(d.classe, d.id_rastreio) for d in deteccoes] == [("CARRO", 1)]
        assert (deteccoes[0].ausente_s > 0) == (not tem)  # o quadro "perdido" vem marcado


def test_objeto_some_da_tela_depois_do_tempo_de_manter() -> None:
    rastreador = RastreadorVista(ajustes(perdido_s=3.0, manter_s=0.5))
    rastreador.atualizar([carro(0.2)], [], 0.0)
    assert rastreador.atualizar([], [], 0.4)  # ainda mantido
    assert rastreador.atualizar([], [], 0.7) == []  # passou de 0,5 s: sai da tela


def test_mancha_de_movimento_do_carro_nao_vira_desconhecido() -> None:
    """Sintoma 2: YOLO perde o carro, o MOG2 vê a mancha (maior, com sombra) -> antes: DESCONHECIDO #2."""
    rastreador = RastreadorVista(ajustes(perdido_s=3.0, manter_s=1.0))
    historico = []
    for i in range(8):
        x = 0.2 + i * 0.02
        mancha = [(x - 0.05, 0.35, x + 0.17, 0.62)]
        deteccoes = rastreador.atualizar([] if i in (3, 4) else [carro(x)], mancha, i / 6)
        historico.append([(d.classe, d.id_rastreio) for d in deteccoes])
    assert all(quadro == [("CARRO", 1)] for quadro in historico), historico


def test_id_novo_em_cima_de_objeto_perdido_herda_id_classe_e_tempo() -> None:
    """Se o ByteTrack trocar o ID (caixa mudou muito de forma), o objeto continua o mesmo."""
    rastreador = RastreadorVista(ajustes(perdido_s=3.0, manter_s=0.0))
    for i in range(3):
        (antes,) = rastreador.atualizar([carro(0.40)], [], i / 6)
    rastreador.atualizar([], [], 3 / 6)
    # Volta com uma caixa bem maior (IoU baixo: o ByteTrack cria um ID novo), mas que contém a
    # antiga. O ByteTrack só confirma objeto novo no 2º quadro seguido.
    grande = DeteccaoBruta(2, 0.3, (0.30, 0.30, 0.65, 0.70))
    rastreador.atualizar([grande], [], 4 / 6)
    (depois,) = rastreador.atualizar([grande], [], 5 / 6)
    assert depois.id_rastreio == antes.id_rastreio and depois.classe == "CARRO"
    assert rastreador.desde(depois.id_rastreio) == 0.0  # o tempo na cena não zera


def test_dois_objetos_vizinhos_nao_trocam_de_id() -> None:
    rastreador = RastreadorVista(ajustes(perdido_s=3.0, manter_s=1.0))
    for i in range(5):
        deteccoes = rastreador.atualizar([pessoa(0.30), pessoa(0.45)], [], i / 6)
    assert sorted(d.id_rastreio for d in deteccoes) == [1, 2]


def test_id_morre_depois_do_tempo_de_perda() -> None:
    rastreador = RastreadorVista(AJUSTES_RAST)  # perdido_s = 1 s
    for passo in range(4):
        (antes,) = rastreador.atualizar([pessoa(0.5)], [], passo / 6)
    for passo in range(4, 20):  # 2,7 s sem ver
        rastreador.atualizar([], [], passo / 6)
    resultado = rastreador.atualizar([pessoa(0.5)], [], 20 / 6)
    assert all(d.id_rastreio != antes.id_rastreio for d in resultado)


# ===================================================================== serviço (detector falso)
class DetectorFalso:
    """Sempre "vê" uma pessoa no meio; conta as chamadas e o tamanho dos lotes."""

    descricao = "falso · CPU"

    def __init__(self) -> None:
        self.lotes: list[int] = []
        self.tamanhos: list[int | None] = []

    def detectar(self, imagens: list[np.ndarray], tamanho: int | None = None) -> list[list[DeteccaoBruta]]:
        self.lotes.append(len(imagens))
        self.tamanhos.append(tamanho)
        return [[pessoa(0.45)] for _ in imagens]


@pytest.fixture
def config_ia(tmp_path: Path) -> Config:
    config = Config.carregar()
    config.definir("geral.pasta_dados", str(tmp_path / "dados"))
    config.definir("ia.ia_fps", 5)
    config.definir("ia.movimento_aquecimento_s", 0.2)
    config.definir("ia.ia_so_com_movimento", False)  # o config.yaml real é do usuário (ele pode ter ligado)
    return config


def camera_video(video: Path, **extra: object) -> Camera:
    dados: dict[str, object] = {
        "nome": "Frente", "codigo": "FRENTE", "fonte": TipoFonte.ARQUIVO, "ip": "", "porta_rtsp": 554,
        "usuario": "", "modelo_url": MODELO_ICSEE, "porta_onvif": 8899, "caminho": str(video), "id": 1,
    }
    dados.update(extra)
    return Camera(**dados)  # type: ignore[arg-type]


@pytest.fixture
def leitor_video(video_teste: Path):  # noqa: ANN201
    limites = LimitesLeitura(abertura_s=2, leitura_s=2, reconexao_min_s=0.1, reconexao_max_s=0.2)
    leitor = LeitorVideo("teste", str(video_teste), limites, arquivo_em_loop=True).iniciar()
    yield leitor
    leitor.parar(esperar_s=2)


def test_servico_detecta_em_lote_no_ritmo_da_ia(config_ia: Config, leitor_video: LeitorVideo, video_teste: Path) -> None:
    falso = DetectorFalso()
    servico = ServicoDeteccao(config_ia, lambda _v: leitor_video, carregar=lambda: falso).iniciar()
    camera = camera_video(video_teste, lentes=DisposicaoLentes.VERTICAL)  # 2 vistas, mesma conexão
    vistas = camera.vistas()
    recebidos: list[ResultadoVista] = []
    servico.assinar(recebidos.append)
    try:
        servico.definir_cameras([camera])
        assert esperar(lambda: all(servico.resultado(v.chave) for v in vistas))
        assert servico.estado is EstadoIA.PRONTO and servico.descricao == "falso · CPU"
        inicio = len(falso.lotes)
        time.sleep(1.0)
        chamadas = len(falso.lotes) - inicio
        assert 3 <= chamadas <= 7  # ~5 por segundo (ia_fps = 5), não a cada quadro do vídeo
        assert max(falso.lotes) == 2  # as duas lentes foram juntas no mesmo lote
        resultado = servico.resultado(vistas[0].chave)
        assert resultado is not None and resultado.deteccoes[0].classe == "PESSOA"
        assert resultado.deteccoes[0].id_rastreio in resultado.desde
        assert recebidos  # assinantes (Fases 4 e 5) recebem cada resultado
    finally:
        servico.parar()


def test_camera_sem_deteccao_nao_usa_o_yolo(config_ia: Config, leitor_video: LeitorVideo, video_teste: Path) -> None:
    """Detecção desligada: com gravação, só o movimento é analisado; sem gravação, nada."""
    falso = DetectorFalso()
    servico = ServicoDeteccao(config_ia, lambda _v: leitor_video, carregar=lambda: falso).iniciar()
    try:
        camera = camera_video(video_teste)
        chave = camera.vistas()[0].chave
        servico.definir_cameras([camera])
        assert esperar(lambda: servico.resultado(chave) is not None)

        camera.deteccao = False  # gravação continua ligada: eventos por movimento, sem YOLO
        servico.definir_cameras([camera])
        assert esperar(lambda: (r := servico.resultado(chave)) is not None and r.deteccoes == ())
        time.sleep(0.3)
        total = len(falso.lotes)
        time.sleep(0.6)
        assert len(falso.lotes) == total  # o YOLO não foi chamado

        camera.gravar = False  # sem detecção e sem gravação: a vista sai da fila
        servico.definir_cameras([camera])
        assert servico.resultado(chave) is None
    finally:
        servico.parar()


def test_so_com_movimento_pula_o_yolo_com_a_cena_parada(config_ia: Config, video_teste: Path) -> None:
    """Imagem sempre igual (sem movimento): o YOLO roda 1 vez e depois o resultado se repete."""

    class LeitorParado:
        def __init__(self) -> None:
            self.numero = 0

        def ultimo_quadro(self):  # noqa: ANN202
            from camera.stream import Quadro

            self.numero += 1
            return Quadro(cena(None), self.numero, time.monotonic())

    falso = DetectorFalso()
    config_ia.definir("ia.ia_so_com_movimento", True)
    leitor = LeitorParado()
    servico = ServicoDeteccao(config_ia, lambda _v: leitor, carregar=lambda: falso).iniciar()  # type: ignore[arg-type, return-value]
    try:
        camera = camera_video(video_teste)
        servico.definir_cameras([camera])
        chave = camera.vistas()[0].chave
        assert esperar(lambda: (r := servico.resultado(chave)) is not None and r.numero > 5)
        assert len(falso.lotes) == 1  # só a primeira análise
        resultado = servico.resultado(chave)
        assert resultado is not None and not resultado.inferiu and resultado.deteccoes  # objetos mantidos
    finally:
        servico.parar()


def test_hd_vai_ao_yolo_em_1280_e_substream_em_640() -> None:
    """Lote misto: cada tamanho vai numa chamada, e a ordem dos resultados é preservada."""
    falso = DetectorFalso()
    hd = np.zeros((1296, 2304, 3), dtype=np.uint8)
    sub = np.zeros((360, 640, 3), dtype=np.uint8)
    lote = [(None, sub, None, 0.0), (None, hd, None, 0.0), (None, sub, None, 0.0)]
    resultado = ServicoDeteccao._detectar_por_tamanho(falso, lote, 640, 1280)  # type: ignore[arg-type]  # noqa: SLF001
    assert sorted(zip(falso.tamanhos, falso.lotes, strict=True)) == [(640, 2), (1280, 1)]
    assert len(resultado) == 3 and all(r[0].classe_coco == 0 for r in resultado)


def test_falha_ao_carregar_modelo_vira_estado_de_erro(config_ia: Config) -> None:
    def falhar() -> DetectorFalso:
        raise RuntimeError("sem modelo")

    servico = ServicoDeteccao(config_ia, lambda _v: None, carregar=falhar).iniciar()
    assert esperar(lambda: servico.estado is EstadoIA.ERRO)
    assert "sem modelo" in servico.mensagem
    servico.parar()


# ===================================================================== YOLO de verdade
def _modelo_disponivel() -> bool:
    try:
        import ultralytics  # noqa: F401
    except ImportError:
        return False
    return (Config.carregar().resolver_caminho("modelos") / "yolo11n.pt").is_file()


@pytest.mark.skipif(not _modelo_disponivel(), reason="Ultralytics ou modelos/yolo11n.pt ausentes")
def test_yolo_real_encontra_pessoas_e_onibus() -> None:
    from ultralytics.utils import ASSETS

    from vision.detector import carregar_modelo

    modelo = carregar_modelo(Config.carregar())
    imagem = cv2.imread(str(ASSETS / "bus.jpg"))
    (brutas,) = modelo.detectar([imagem])
    rastreador = RastreadorVista(AJUSTES_RAST)
    classes = sorted(d.classe for d in rastreador.atualizar(brutas, [], 0.0))
    assert classes.count("PESSOA") >= 3 and "ONIBUS" in classes
    assert all(d.id_rastreio is not None for d in rastreador.atualizar(brutas, [], 0.2))


def test_vista_de_lente_recebe_so_a_sua_metade(nova_camera: NovaCamera) -> None:
    """A IA analisa cada lente separada: a vista 2 recorta a metade de baixo."""
    vista: Vista = nova_camera(id=1, lentes=DisposicaoLentes.VERTICAL).vistas()[1]
    imagem = np.zeros((720, 640, 3), dtype=np.uint8)
    assert vista.recortar(imagem).shape == (360, 640, 3)


# ===================================================================== cena parada (12/10/2026)
class LeitorParado:
    """Sempre a MESMA imagem (nada se mexe), com número de quadro novo a cada leitura."""

    def __init__(self) -> None:
        self.numero = 0
        self.imagem = np.full((180, 320, 3), 90, dtype=np.uint8)

    def ultimo_quadro(self):  # noqa: ANN201
        from camera.stream import Quadro

        self.numero += 1
        return Quadro(self.imagem, self.numero, time.monotonic())


class DetectorControlado(DetectorFalso):
    """Vê a pessoa enquanto ``ve_pessoa`` for True (ela "vai embora" quando o teste quiser)."""

    def __init__(self) -> None:
        super().__init__()
        self.ve_pessoa = True

    def detectar(self, imagens: list[np.ndarray], tamanho: int | None = None) -> list[list[DeteccaoBruta]]:
        self.lotes.append(len(imagens))
        return [[pessoa(0.45)] if self.ve_pessoa else [] for _ in imagens]


def test_cena_parada_confere_se_o_objeto_ainda_esta_la(config_ia: Config, video_teste: Path) -> None:
    """Bug de 12/10/2026: com "só analisar com movimento", quem ia embora sem o detector de movimento
    perceber ficava CONGELADO na tela (o último resultado era repetido) e virava alerta na placa."""
    config_ia.definir("ia.ia_so_com_movimento", True)
    config_ia.definir("ia.ia_fps", 10)
    config_ia.definir("ia.verificar_parado_s", 0.5)
    config_ia.definir("ia.movimento_aquecimento_s", 0.1)
    falso = DetectorControlado()
    leitor = LeitorParado()  # a mesma "câmera" parada o tempo todo
    servico = ServicoDeteccao(config_ia, lambda _v: leitor, carregar=lambda: falso).iniciar()  # type: ignore[arg-type,return-value]
    camera = camera_video(video_teste)
    chave = camera.vistas()[0].chave
    try:
        servico.definir_cameras([camera])
        assert esperar(lambda: (r := servico.resultado(chave)) is not None and r.deteccoes)
        time.sleep(1.0)
        chamadas = len(falso.lotes)
        time.sleep(1.0)
        assert 1 <= len(falso.lotes) - chamadas <= 4  # cena parada: só confere (~2x/s), não 10x/s
        assert servico.resultado(chave).deteccoes  # type: ignore[union-attr] # a pessoa continua lá
        falso.ve_pessoa = False  # foi embora sem mexer na imagem (de noite, longe...)
        assert esperar(lambda: not servico.resultado(chave).deteccoes, 3.0)  # type: ignore[union-attr]
    finally:
        servico.parar()


def test_repetir_sem_movimento_tira_manchas_e_fantasmas() -> None:
    from core.modelos import Deteccao
    from vision.detector import repetir_sem_movimento

    vista_chave = (1, 1)
    deteccoes = (
        Deteccao("PESSOA", 0.9, (0.1, 0.1, 0.2, 0.4), 1),
        Deteccao(DESCONHECIDO, 0.5, (0.5, 0.5, 0.6, 0.6), 2, origem="movimento"),  # mancha de folhas
        Deteccao("CARRO", 0.8, (0.6, 0.6, 0.9, 0.9), 3, ausente_s=0.4),  # a IA já não via mais
    )
    ultimo = ResultadoVista(vista_chave, 1, 10.0, deteccoes, {1: 5.0, 2: 9.0, 3: 2.0})
    repetidas, desde = repetir_sem_movimento(ultimo)
    assert [d.id_rastreio for d in repetidas] == [1] and desde == {1: 5.0}


def test_so_as_classes_escolhidas_aparecem(config_ia: Config, leitor_video: LeitorVideo, video_teste: Path) -> None:
    config_ia.definir("ia.identificar.PESSOA", False)  # Ajustes -> IA -> desmarcou PESSOA
    falso = DetectorFalso()  # ele sempre "vê" uma pessoa
    servico = ServicoDeteccao(config_ia, lambda _v: leitor_video, carregar=lambda: falso).iniciar()
    camera = camera_video(video_teste)
    chave = camera.vistas()[0].chave
    try:
        servico.definir_cameras([camera])
        assert esperar(lambda: servico.resultado(chave) is not None and len(falso.lotes) >= 3)
        assert all(d.classe != "PESSOA" for d in servico.resultado(chave).deteccoes)  # type: ignore[union-attr]
    finally:
        servico.parar()
