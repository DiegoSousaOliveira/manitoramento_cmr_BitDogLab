"""Eventos: buffer, clipe, serviço de eventos (início/fim/arquivos/banco), retenção e coletor de treino.

O serviço de eventos é testado com um vídeo de verdade tocando no leitor e resultados
de IA SINTÉTICOS — assim controlamos exatamente quando há "atividade".
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterator
from datetime import datetime, timedelta
from pathlib import Path

import cv2
import numpy as np
import pytest

from camera.gerenciador import GerenciadorCameras
from camera.stream import Quadro
from core.banco import Banco
from core.config import ARQUIVO_PADRAO, Config
from core.modelos import Camera, Deteccao, Evento, TipoFonte
from core.servico_cameras import ServicoCameras
from eventos.buffer import BufferQuadros
from eventos.clipe import EscritorClipe
from eventos.coletor import ColetorTreino
from eventos.desenho import cor_bgr, desenhar_caixas
from eventos.gravador import ServicoEventos
from eventos.retencao import aplicar_retencao
from tests.conftest import MODELO_ICSEE, CofreMemoria
from vision.detector import ResultadoVista
from vision.motion import ResultadoMovimento
from vision.zona import ponto_no_poligono


def esperar(condicao: Callable[[], bool], limite_s: float = 15.0) -> bool:
    fim = time.monotonic() + limite_s
    while time.monotonic() < fim:
        if condicao():
            return True
        time.sleep(0.05)
    return condicao()


# ===================================================================== peças
def test_ponto_no_poligono() -> None:
    triangulo = [(0.1, 0.1), (0.9, 0.1), (0.5, 0.9)]
    assert ponto_no_poligono((0.5, 0.4), triangulo)
    assert not ponto_no_poligono((0.05, 0.8), triangulo)
    assert ponto_no_poligono((0.0, 0.0), None)  # sem zona = quadro inteiro


def test_buffer_guarda_so_os_ultimos_segundos() -> None:
    buffer = BufferQuadros(segundos=1.0)
    imagem = np.zeros((2, 2, 3), dtype=np.uint8)
    for i in range(30):
        buffer.adicionar(Quadro(imagem, i, i * 0.1))
    assert 1.85 < (buffer.mais_antigo() or 0) <= 2.0 + 1e-9  # ~1 s antes do último (2,9 s)
    assert buffer.ate(2.45).numero == 24  # type: ignore[union-attr]
    assert buffer.ate(1.0) is None  # já saiu do buffer


def test_clipe_h264_em_pasta_com_acento(tmp_path: Path) -> None:
    pasta = tmp_path / "Câmera Calçada"
    pasta.mkdir()
    escritor = EscritorClipe(pasta / "clipe.mp4", 321, 181, 12, 23)  # ímpar: ajustado para par
    for i in range(24):
        escritor.escrever(np.full((181, 321, 3), i * 10, dtype=np.uint8))
    escritor.fechar()
    assert escritor.formato == "h264" and escritor.duracao_s == pytest.approx(2.0)
    captura = cv2.VideoCapture(str(pasta / "clipe.mp4"))
    assert int(captura.get(cv2.CAP_PROP_FRAME_COUNT)) == 24 and int(captura.get(3)) == 320
    captura.release()


def test_clipe_cai_para_mp4v_sem_h264(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def sem_pyav(self: EscritorClipe, _qualidade: int) -> None:
        raise RuntimeError("PyAV indisponível")

    monkeypatch.setattr(EscritorClipe, "_abrir_pyav", sem_pyav)
    escritor = EscritorClipe(tmp_path / "clipe.mp4", 64, 48, 12, 23)
    for _ in range(12):
        escritor.escrever(np.zeros((48, 64, 3), dtype=np.uint8))
    escritor.fechar()
    assert escritor.formato == "mp4v"
    assert int(cv2.VideoCapture(str(tmp_path / "clipe.mp4")).get(cv2.CAP_PROP_FRAME_COUNT)) == 12


def test_desenhar_caixas_nao_altera_a_original() -> None:
    imagem = np.zeros((100, 200, 3), dtype=np.uint8)
    saida = desenhar_caixas(imagem, [Deteccao("PESSOA", 0.9, (0.1, 0.1, 0.5, 0.9), 3)])
    assert imagem.max() == 0 and saida.max() > 0
    assert tuple(saida[50, 20]) == cor_bgr("#FF8A3D")  # borda esquerda da caixa, cor da PESSOA


# ===================================================================== serviço de eventos
@pytest.fixture
def video_movimento(tmp_path: Path) -> Path:
    """320x180, 12 FPS, 10 s: um quadrado andando (o leitor toca em loop)."""
    caminho = tmp_path / "movimento.mp4"
    gravador = cv2.VideoWriter(str(caminho), cv2.VideoWriter_fourcc(*"mp4v"), 12.0, (320, 180))
    for i in range(120):
        quadro = np.full((180, 320, 3), 40, dtype=np.uint8)
        cv2.rectangle(quadro, (i * 2, 60), (i * 2 + 30, 110), (255, 255, 255), -1)
        gravador.write(quadro)
    gravador.release()
    return caminho


class Ambiente:
    def __init__(self, config: Config, banco: Banco, servico: ServicoCameras, gerenciador: GerenciadorCameras,
                 eventos: ServicoEventos, camera: Camera) -> None:
        self.config, self.banco, self.servico = config, banco, servico
        self.gerenciador, self.eventos, self.camera = gerenciador, eventos, camera
        self.gravados: list[Evento] = []
        eventos.assinar_gravacao(lambda evento, _camera: self.gravados.append(evento))
        self.numero = 0

    def resultado(self, ativo: bool, deteccoes: tuple[Deteccao, ...] = (), desde: dict[int, float] | None = None,
                  vista: int = 1) -> ResultadoVista:
        self.numero += 1
        regioes = [(0.4, 0.3, 0.5, 0.6)] if ativo else []
        imagem = np.full((180, 320, 3), 90, dtype=np.uint8)
        return ResultadoVista(
            (self.camera.id or 0, vista), self.numero, time.monotonic(), deteccoes, desde or {},
            ResultadoMovimento(regioes=regioes, fracao=0.05 if ativo else 0.0), imagem=imagem,
        )

    def enviar(self, ativo: bool, segundos: float, **extra: object) -> None:
        """Manda resultados a ~6 por segundo, como a IA faria."""
        fim = time.monotonic() + segundos
        while time.monotonic() < fim:
            self.eventos.receber_resultado(self.resultado(ativo, **extra))  # type: ignore[arg-type]
            time.sleep(1 / 6)


@pytest.fixture
def ambiente(tmp_path: Path, cofre: CofreMemoria, video_movimento: Path) -> Iterator[Ambiente]:
    copia = tmp_path / "config.yaml"
    copia.write_text(ARQUIVO_PADRAO.read_text(encoding="utf-8"), encoding="utf-8")
    config = Config.carregar(copia)
    config.definir("geral.pasta_dados", str(tmp_path / "dados"))
    for chave, valor in {
        "eventos.pre_evento_s": 1.0, "eventos.pos_evento_s": 0.5, "eventos.fim_evento_s": 1.0,
        "eventos.max_evento_s": 30, "eventos.aquecimento_s": 0.5, "eventos.objeto_novo_s": 10,
        "eventos.confirmar_resultados": 2, "treino.coletar": False,
        "eventos.area": "quadro",  # o config.yaml real pode estar em "Só dentro da zona"
    }.items():
        config.definir(chave, valor)
    banco = Banco(tmp_path / "dados" / "sentinela.db")
    servico = ServicoCameras(banco, config)
    camera = servico.cadastrar(
        Camera("Câmera Calçada", "CALCADA", TipoFonte.ARQUIVO, "", 554, "", MODELO_ICSEE, 8899,
               caminho=str(video_movimento)), "",
    )
    gerenciador = GerenciadorCameras(config, servico.obter_senha)
    gerenciador.sincronizar([camera])
    eventos = ServicoEventos(config, servico, gerenciador).iniciar()
    eventos.definir_cameras([camera])
    amb = Ambiente(config, banco, servico, gerenciador, eventos, camera)
    assert esperar(lambda: (l := gerenciador.leitor(camera.id or 0, 1)) is not None and l.ultimo_quadro() is not None)
    time.sleep(1.2)  # o buffer enche (pré-evento) e passa o aquecimento
    yield amb
    eventos.parar()
    gerenciador.parar_tudo()
    banco.fechar()


def test_movimento_gera_evento_com_clipe_fotos_json_e_registro(ambiente: Ambiente) -> None:
    pessoa = Deteccao("PESSOA", 0.87, (0.4, 0.3, 0.5, 0.8), 7)
    inicio = time.monotonic()
    ambiente.enviar(True, 2.0, deteccoes=(pessoa,), desde={7: inicio})
    ambiente.enviar(False, 0.2)
    assert esperar(lambda: len(ambiente.gravados) == 1)

    evento = ambiente.gravados[0]
    assert evento.id is not None and ambiente.banco.obter_evento(evento.id) == evento
    assert evento.classes == {"PESSOA": 1} and evento.maior_conf == pytest.approx(0.87)
    assert 1.5 <= evento.duracao_s <= 2.6
    pasta = ambiente.servico.pastas(ambiente.camera)[0] / evento.pasta
    assert evento.pasta.startswith(f"{evento.inicio:%Y-%m-%d}/{evento.inicio:%H%M%S}")
    for nome in ("clipe.mp4", "foto.jpg", "foto_limpa.jpg", "miniatura.jpg", "deteccoes.json"):
        assert (pasta / nome).is_file(), nome

    captura = cv2.VideoCapture(str(pasta / "clipe.mp4"))
    quadros = int(captura.get(cv2.CAP_PROP_FRAME_COUNT))
    captura.release()
    # pré (1 s) + atividade (~2 s) + pós (0,5 s) a 12 FPS
    assert 30 <= quadros <= 48, quadros

    dados = json.loads((pasta / "deteccoes.json").read_text(encoding="utf-8"))
    assert dados["fps"] == 12 and dados["quadros"]
    primeiro = next(q for q in dados["quadros"] if q["deteccoes"])
    assert 0.8 <= primeiro["t"] <= 1.3  # a pessoa aparece ~1 s depois do início do clipe (o pré-evento)
    assert primeiro["deteccoes"][0] == {
        "classe": "PESSOA", "id": 7, "conf": 0.87, "caixa": [0.4, 0.3, 0.5, 0.8], "origem": "ia",
    }


def test_uma_atividade_isolada_nao_abre_evento(ambiente: Ambiente) -> None:
    ambiente.eventos.receber_resultado(ambiente.resultado(True))
    ambiente.enviar(False, 1.0)
    time.sleep(1.5)
    assert ambiente.gravados == [] and not ambiente.eventos.em_andamento((ambiente.camera.id or 0, 1))


def test_objeto_que_ja_estava_la_nao_abre_evento(ambiente: Ambiente) -> None:
    """Carro do vizinho estacionado desde que a câmera ligou: sem movimento, sem evento."""
    carro = Deteccao("CARRO", 0.9, (0.1, 0.5, 0.3, 0.7), 1)
    ambiente.enviar(False, 2.0, deteccoes=(carro,), desde={1: time.monotonic() - 60})
    time.sleep(1.0)
    assert ambiente.gravados == []


def test_evento_longo_e_dividido(ambiente: Ambiente) -> None:
    ambiente.config.definir("eventos.max_evento_s", 1.5)
    ambiente.eventos.p = type(ambiente.eventos.p).do_config(ambiente.config)
    ambiente.enviar(True, 3.5)
    ambiente.enviar(False, 0.2)
    assert esperar(lambda: len(ambiente.gravados) >= 2)


def test_alerta_marca_o_evento(ambiente: Ambiente) -> None:
    chave = (ambiente.camera.id or 0, 1)
    ambiente.enviar(True, 0.6)
    assert esperar(lambda: ambiente.eventos.em_andamento(chave), 3)
    ambiente.eventos.marcar_alerta(chave)
    ambiente.enviar(False, 0.2)
    assert esperar(lambda: len(ambiente.gravados) == 1)
    assert ambiente.gravados[0].alerta


def test_alerta_sem_evento_abre_um_que_comeca_quando_o_objeto_parou(ambiente: Ambiente) -> None:
    """Nada se mexendo (ex.: carro parado há 30 s): o alerta abre o evento com a duração certa."""
    chave = (ambiente.camera.id or 0, 1)
    ambiente.eventos.marcar_alerta(chave, time.monotonic() - 3.0)
    ambiente.enviar(False, 0.3)
    assert esperar(lambda: len(ambiente.gravados) == 1)
    evento = ambiente.gravados[0]
    assert evento.alerta and 2.7 <= evento.duracao_s <= 3.4  # "parado 3 s", e não "0 s"


def test_camera_sem_gravar_nao_gera_evento(ambiente: Ambiente) -> None:
    ambiente.camera.gravar = False
    ambiente.eventos.definir_cameras([ambiente.camera])
    ambiente.enviar(True, 1.5)
    time.sleep(1.5)
    assert ambiente.gravados == []


def test_fechar_o_app_grava_o_evento_em_andamento(ambiente: Ambiente) -> None:
    chave = (ambiente.camera.id or 0, 1)
    ambiente.enviar(True, 1.0)
    assert esperar(lambda: ambiente.eventos.em_andamento(chave), 3)
    ambiente.eventos.parar()
    assert len(ambiente.gravados) == 1


# ===================================================================== retenção
def _evento_falso(servico: ServicoCameras, camera: Camera, quando: datetime, tamanho: int) -> Evento:
    pasta = f"{quando:%Y-%m-%d}/{quando:%H%M%S}"
    caminho = servico.pastas(camera)[0] / pasta
    caminho.mkdir(parents=True)
    (caminho / "clipe.mp4").write_bytes(b"x" * tamanho)
    return servico.banco.adicionar_evento(
        Evento(camera_id=camera.id or 0, inicio=quando, fim=quando + timedelta(seconds=5), pasta=pasta)
    )


def test_retencao_por_idade_e_por_espaco(ambiente: Ambiente) -> None:
    servico, camera = ambiente.servico, ambiente.camera
    agora = datetime(2026, 10, 20, 12, 0)
    velho = _evento_falso(servico, camera, agora - timedelta(days=9), 1000)
    medio = _evento_falso(servico, camera, agora - timedelta(days=3), 600_000)
    novo = _evento_falso(servico, camera, agora - timedelta(hours=1), 600_000)
    ambiente.config.definir("eventos.retencao_dias", 7)
    ambiente.config.definir("eventos.limite_disco_gb", 1_000_000 / 1024**3)  # ~1 MB

    removidos, liberados = aplicar_retencao(servico, ambiente.config, agora)
    assert set(removidos) == {velho.id, medio.id}  # velho pela idade; médio para caber no limite
    assert liberados == 601_000
    assert [e.id for e in servico.banco.listar_eventos()] == [novo.id]
    assert not (servico.pastas(camera)[0] / velho.pasta).parent.exists()  # pasta do dia vazia some


# ===================================================================== coletor de treino
def test_coletor_guarda_duvidas_com_pre_rotulos(tmp_path: Path) -> None:
    config = Config.carregar()
    config.definir("geral.pasta_dados", str(tmp_path / "dados"))
    config.definir("treino.intervalo_min_s", 0)
    config.definir("treino.max_por_hora", 2)
    coletor = ColetorTreino(config)
    camera = Camera("Frente", "FRENTE", TipoFonte.RTSP, "1.2.3.4", 554, "", MODELO_ICSEE, 8899, id=1)
    imagem = np.zeros((360, 640, 3), dtype=np.uint8)

    def resultado(conf: float) -> ResultadoVista:
        d = Deteccao("CARRO", conf, (0.2, 0.4, 0.4, 0.6), 3)
        return ResultadoVista((1, 2), 1, time.monotonic(), (d,), imagem=imagem)

    coletor._ultima_amostra[(1, 2)] = time.monotonic()  # noqa: SLF001  (sem "amostra" neste teste)
    assert coletor.processar(resultado(0.95), camera) is None  # confiante: não interessa
    caminho = coletor.processar(resultado(0.35), camera)  # dúvida: guarda
    assert caminho is not None and caminho.is_file() and "duvida" in caminho.name
    assert caminho.with_suffix(".txt").read_text(encoding="utf-8").strip() == "2 0.300000 0.500000 0.200000 0.200000"
    assert json.loads(caminho.with_suffix(".json").read_text(encoding="utf-8"))["motivo"] == "duvida"
    assert coletor.processar(resultado(0.30), camera) is not None
    assert coletor.processar(resultado(0.30), camera) is None  # limite de 2 por hora


def test_coletor_desligado_nao_guarda_nada(tmp_path: Path) -> None:
    config = Config.carregar()
    config.definir("geral.pasta_dados", str(tmp_path / "dados"))
    config.definir("treino.coletar", False)
    camera = Camera("Frente", "FRENTE", TipoFonte.RTSP, "1.2.3.4", 554, "", MODELO_ICSEE, 8899, id=1)
    resultado = ResultadoVista((1, 1), 1, time.monotonic(), (), imagem=np.zeros((10, 10, 3), dtype=np.uint8))
    assert ColetorTreino(config).processar(resultado, camera) is None
    assert not (tmp_path / "dados" / "treino").exists()


# ===================================================================== zona e objetos escolhidos (12/10/2026)
def _atividade(config: Config, camera: Camera, zona, resultado: ResultadoVista) -> bool:  # noqa: ANN001
    """Chama a regra de "atividade" do gravador sem abrir câmera nem thread."""
    from types import SimpleNamespace

    from eventos.gravador import Parametros, _VistaEventos

    estado = _VistaEventos(camera.vistas()[0], camera, zona)
    falso = SimpleNamespace(p=Parametros.do_config(config))
    return ServicoEventos._atividade(falso, estado, resultado)  # type: ignore[arg-type]  # noqa: SLF001


def test_so_dentro_da_zona_numa_lente_sem_zona(tmp_path: Path) -> None:
    copia = tmp_path / "config.yaml"
    copia.write_text(ARQUIVO_PADRAO.read_text(encoding="utf-8"), encoding="utf-8")
    config = Config.carregar(copia)
    config.definir("eventos.area", "zona")
    config.definir("parado.sem_zona", "ignorar")
    config.definir("ia.identificar.DESCONHECIDO", True)
    camera = Camera("Frente", "FRENTE", TipoFonte.ARQUIVO, "", 554, "", MODELO_ICSEE, 8899, caminho="x.mp4", id=1)
    mexeu = ResultadoVista((1, 1), 1, 10.0, (), {}, ResultadoMovimento(regioes=((0.6, 0.6, 0.8, 0.8),)))
    assert not _atividade(config, camera, None, mexeu)  # lente sem zona: não há o que vigiar
    assert _atividade(config, camera, [(0.5, 0.5), (1.0, 0.5), (1.0, 1.0)], mexeu)  # com zona, dentro: grava
    config.definir("parado.sem_zona", "imagem_inteira")
    assert _atividade(config, camera, None, mexeu)  # o jeito antigo: sem zona = imagem inteira
    config.definir("eventos.area", "quadro")
    config.definir("parado.sem_zona", "ignorar")
    assert _atividade(config, camera, None, mexeu)  # "Imagem inteira" não depende de zona


def test_sem_desconhecido_a_mancha_de_movimento_sozinha_nao_abre_evento(tmp_path: Path) -> None:
    copia = tmp_path / "config.yaml"
    copia.write_text(ARQUIVO_PADRAO.read_text(encoding="utf-8"), encoding="utf-8")
    config = Config.carregar(copia)
    config.definir("eventos.area", "quadro")
    config.definir("ia.identificar.DESCONHECIDO", False)  # desmarcou DESCONHECIDO em Ajustes -> IA
    camera = Camera("Frente", "FRENTE", TipoFonte.ARQUIVO, "", 554, "", MODELO_ICSEE, 8899, caminho="x.mp4", id=1)
    folhas = ResultadoVista((1, 1), 1, 10.0, (), {}, ResultadoMovimento(regioes=((0.6, 0.6, 0.8, 0.8),)))
    assert not _atividade(config, camera, None, folhas)
    pessoa = ResultadoVista((1, 1), 2, 10.0, (Deteccao("PESSOA", 0.9, (0.1, 0.1, 0.2, 0.5), 1),), {1: 9.0},
                           ResultadoMovimento(regioes=((0.6, 0.6, 0.8, 0.8),)))
    assert _atividade(config, camera, None, pessoa)  # um objeto escolhido que acabou de chegar: grava
    sem_ia = Camera("Garagem", "GARAGEM", TipoFonte.ARQUIVO, "", 554, "", MODELO_ICSEE, 8899, caminho="x.mp4", id=2,
                    deteccao=False)
    assert _atividade(config, sem_ia, None, folhas)  # câmera sem IA só tem o movimento: continua gravando
