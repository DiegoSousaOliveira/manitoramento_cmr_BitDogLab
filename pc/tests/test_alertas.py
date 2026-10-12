"""Serviço de alertas (eventos/alertas.py): da IA até o ALERTA/LIMPAR, com tempos curtos."""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from datetime import datetime
from pathlib import Path

import numpy as np
import pytest

from core.banco import Banco
from core.config import ARQUIVO_PADRAO, Config
from core.modelos import Camera, Deteccao, TipoFonte
from core.servico_cameras import ServicoCameras
from eventos.alertas import ServicoAlertas
from eventos.desenho import recorte_do_objeto
from tests.conftest import MODELO_ICSEE, CofreMemoria
from vision.detector import ResultadoVista
from vision.dwell import Aviso, TipoAviso


def esperar(condicao: Callable[[], bool], limite_s: float = 5.0) -> bool:
    fim = time.monotonic() + limite_s
    while time.monotonic() < fim:
        if condicao():
            return True
        time.sleep(0.02)
    return condicao()


class EventosFalso:
    """Faz o papel do ServicoEventos: só anota quais vistas foram marcadas como ALERTA."""

    def __init__(self) -> None:
        self.marcados: list[tuple[int, int]] = []
        self.desde: list[float | None] = []

    def marcar_alerta(self, chave: tuple[int, int], desde: float | None = None) -> None:
        self.marcados.append(chave)
        self.desde.append(desde)


class Cenario:
    def __init__(self, servico: ServicoCameras, alertas: ServicoAlertas, eventos: EventosFalso, camera: Camera) -> None:
        self.servico, self.alertas, self.eventos, self.camera = servico, alertas, eventos, camera
        self.chave = (camera.id or 0, 1)
        self.avisos: list[tuple[Aviso, Camera]] = []
        alertas.assinar(lambda aviso, cam: self.avisos.append((aviso, cam)))
        self.numero = 0

    def tipos(self) -> list[TipoAviso]:
        return [a.tipo for a, _ in self.avisos]

    def enviar(self, segundos: float, *deteccoes: Deteccao) -> None:
        """Resultados a ~10 por segundo, com o relógio de verdade (o serviço expira por ele)."""
        fim = time.monotonic() + segundos
        while time.monotonic() < fim:
            self.numero += 1
            self.alertas.receber_resultado(
                ResultadoVista(self.chave, self.numero, time.monotonic(), tuple(deteccoes), resolucao=(640, 360),
                               imagem=np.full((360, 640, 3), 80, dtype=np.uint8))
            )
            time.sleep(0.1)


def pessoa(x: float, id_: int = 7) -> Deteccao:
    return Deteccao("PESSOA", 0.88, (x - 0.05, 0.3, x + 0.05, 0.7), id_)


@pytest.fixture
def cenario(tmp_path: Path, cofre: CofreMemoria) -> Iterator[Cenario]:
    copia = tmp_path / "config.yaml"
    copia.write_text(ARQUIVO_PADRAO.read_text(encoding="utf-8"), encoding="utf-8")
    config = Config.carregar(copia)
    config.definir("geral.pasta_dados", str(tmp_path / "dados"))
    for chave, valor in {
        "parado.aquecimento_s": 0, "parado.tolerancia_sumico_s": 0.3, "parado.limpar_apos_sumico_s": 0.6,
        "parado.tempo_parado.PESSOA": 0.6, "parado.sem_zona": "imagem_inteira",  # sem zona = imagem toda
    }.items():
        config.definir(chave, valor)
    banco = Banco(tmp_path / "dados" / "sentinela.db")
    servico = ServicoCameras(banco, config)
    camera = servico.cadastrar(
        Camera("Frente", "FRENTE", TipoFonte.ARQUIVO, "", 554, "", MODELO_ICSEE, 8899, caminho="x.mp4"), ""
    )
    eventos = EventosFalso()
    alertas = ServicoAlertas(config, servico, eventos).iniciar()  # type: ignore[arg-type]
    alertas.definir_cameras([camera])
    yield Cenario(servico, alertas, eventos, camera)
    alertas.parar()
    banco.fechar()


def test_alerta_marca_o_evento_avisa_com_miniatura_e_limpa_quando_some(cenario: Cenario) -> None:
    cenario.enviar(1.2, pessoa(0.3))
    assert esperar(lambda: cenario.tipos() == [TipoAviso.ALERTA])
    alerta, camera = cenario.avisos[0]
    assert camera.codigo == "FRENTE" and alerta.chave == cenario.chave and alerta.id_rastreio == 7
    assert isinstance(alerta.quando, datetime) and alerta.miniatura is not None
    assert alerta.miniatura.shape == (180, 320, 3)
    assert cenario.eventos.marcados == [cenario.chave]  # o evento atual vira ALERTA
    desde = cenario.eventos.desde[0]
    assert desde is not None and alerta.instante - desde == pytest.approx(alerta.segundos)  # quando parou
    situacao = cenario.alertas.situacao(cenario.chave)
    assert situacao is not None and situacao.alerta and situacao.progresso[7].alertado
    assert [a.id_rastreio for a, _ in cenario.alertas.ativos()] == [7]

    # A pessoa some (ou a câmera para de mandar imagem): LIMPAR sozinho, sem resultado novo.
    assert esperar(lambda: cenario.tipos() == [TipoAviso.ALERTA, TipoAviso.LIMPAR], 3)
    assert cenario.avisos[1][0].motivo == "sumiu" and cenario.alertas.ativos() == []
    assert esperar(lambda: not (s := cenario.alertas.situacao(cenario.chave)) or not s.alerta)


def test_zona_vem_do_banco_e_e_relida(cenario: Cenario) -> None:
    banco = cenario.servico.banco
    banco.salvar_zona(cenario.camera.id or 0, 1, [(0.0, 0.0), (0.5, 0.0), (0.5, 1.0), (0.0, 1.0)])
    cenario.alertas.definir_cameras([cenario.camera])
    cenario.enviar(1.0, pessoa(0.75))  # fora da zona (metade esquerda)
    time.sleep(0.2)
    assert cenario.avisos == []
    banco.salvar_zona(cenario.camera.id or 0, 1, [(0.5, 0.0), (1.0, 0.0), (1.0, 1.0), (0.5, 1.0)])
    cenario.alertas.definir_cameras([cenario.camera])  # o editor de zona faz isso ao salvar
    cenario.enviar(1.0, pessoa(0.75))
    assert esperar(lambda: TipoAviso.ALERTA in cenario.tipos())


def test_desligar_a_deteccao_limpa_os_alertas(cenario: Cenario) -> None:
    cenario.enviar(1.0, pessoa(0.3))
    assert esperar(lambda: cenario.tipos() == [TipoAviso.ALERTA])
    cenario.camera.deteccao = False
    cenario.alertas.definir_cameras([cenario.camera])
    assert esperar(lambda: cenario.tipos() == [TipoAviso.ALERTA, TipoAviso.LIMPAR])
    assert cenario.avisos[1][0].motivo == "camera" and cenario.alertas.situacao(cenario.chave) is None


def test_ia_recriada_recomeca_os_ids(cenario: Cenario) -> None:
    cenario.enviar(1.0, pessoa(0.3))
    assert esperar(lambda: cenario.tipos() == [TipoAviso.ALERTA])
    cenario.numero = 0  # a IA recriou a vista: a contagem (e os IDs) recomeçam
    cenario.enviar(0.3, pessoa(0.6, id_=1))
    assert esperar(lambda: cenario.tipos()[:2] == [TipoAviso.ALERTA, TipoAviso.LIMPAR])


def test_recorte_do_objeto_centraliza_e_respeita_as_bordas() -> None:
    imagem = np.zeros((360, 640, 3), dtype=np.uint8)
    imagem[150:210, 300:340] = 255  # o "objeto"
    mini = recorte_do_objeto(imagem, (300 / 640, 150 / 360, 340 / 640, 210 / 360))
    assert mini.shape == (180, 320, 3)
    assert mini[90, 160].tolist() == [255, 255, 255]  # objeto no meio da miniatura
    canto = recorte_do_objeto(imagem, (0.0, 0.0, 0.05, 0.05))  # objeto no canto: recorte não sai da imagem
    assert canto.shape == (180, 320, 3)
