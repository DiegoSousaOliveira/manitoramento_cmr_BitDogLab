"""Fase 9 na interface: página Treino, revisão das imagens e a troca automática do modelo."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime
from pathlib import Path

import pytest
from PySide6.QtCore import QPoint, Qt

from core.config import Config
from tests.test_interface import Contexto, contexto  # noqa: F401 (fixture)
from tests.test_treino import criar_amostra
from treino.avaliacao import Metricas
from treino.dados import REVISAR, ROTULADAS, Amostra, ConjuntoTreino, rotulo_de_caixa
from treino.historico import Historico, Rodada
from treino.servico import ServicoTreino
from ui.janela_principal import JanelaPrincipal
from ui.revisor import DialogoRevisao
from vision.detector import EstadoIA


class DeteccaoFalsa:
    def __init__(self) -> None:
        self.recargas = 0

    def recarregar_modelo(self) -> None:
        self.recargas += 1

    def assinar(self, _f) -> None:  # noqa: ANN001
        pass

    def definir_cameras(self, _c) -> None:  # noqa: ANN001
        pass

    resultado_da_vista = staticmethod(lambda _v: None)
    estado = EstadoIA.PRONTO  # o timer de status da janela lê isto a cada 2 s
    descricao = "falso · CPU"
    mensagem = "pronto"
    inferencias_por_s = 0.0
    tempo_lote_ms = 0.0


@pytest.fixture
def janela_treino(qtbot, contexto: Contexto) -> Iterator[tuple[JanelaPrincipal, ServicoTreino, Path]]:  # noqa: ANN001, F811
    c = contexto.config
    c.definir("ia.modelo", "yolo11n.pt")  # o config.yaml real pode estar com outro modelo (ex.: yolo11s)
    pasta = c.pasta_dados / "treino"
    criar_amostra(pasta / "pendentes", "p1", datetime(2026, 10, 11, 9))
    criar_amostra(pasta / ROTULADAS, "r1", datetime(2026, 10, 11, 10))
    criar_amostra(pasta / REVISAR, "v1", datetime(2026, 10, 11, 11), rotulos=[rotulo_de_caixa(0, (0.1, 0.1, 0.4, 0.8))])
    modelos = c.resolver_caminho(str(c.obter("ia.pasta_modelos")))
    Historico(pasta / "historico.json").adicionar(Rodada(
        inicio="2026-10-12T03:00:00", modelo_base="yolo11n.pt", modelo_novo="sentinela_20261012_0300.pt",
        promovido=True, motivo="melhorou: nota 0.400 → 0.450", atual=Metricas(0.4, 0.6, {}).para_dict(),
        novo=Metricas(0.45, 0.65, {}).para_dict(), imagens_treino=80, imagens_validacao=20,
    ))
    treino = ServicoTreino(c, comando=lambda _f: ["python", "-c", "pass"])
    janela = JanelaPrincipal(c, contexto.servico, contexto.gerenciador, treino=treino)
    qtbot.addWidget(janela)
    janela.resize(1400, 860)
    janela.show()
    yield janela, treino, modelos
    janela.close()


def test_pagina_treino_mostra_contagens_e_historico(qtbot, janela_treino) -> None:  # noqa: ANN001
    janela, _treino, _modelos = janela_treino
    janela.ir_para("treino")
    pagina = janela.pagina_treino
    assert pagina is not None
    qtbot.waitUntil(lambda: pagina.cartao_pendentes.valor.text() == "1", timeout=5000)
    assert pagina.cartao_prontas.valor.text() == "1" and pagina.cartao_revisar.valor.text() == "1"
    assert pagina.botao_revisar.isEnabled()
    assert "yolo11n" in pagina.cartao_modelo.valor.text()
    assert "melhorou" in pagina.ultima.text()
    pagina.grab()


def test_rodada_aprovada_troca_o_modelo_sozinha(qtbot, janela_treino, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ANN001
    janela, _treino, _modelos = janela_treino
    deteccao = DeteccaoFalsa()
    janela.deteccao = deteccao  # type: ignore[assignment]
    janela._aviso_treino({"fim": True, "manual": False, "rodada": {  # noqa: SLF001
        "inicio": "2026-10-13T03:00:00", "promovido": True, "modelo_novo": "sentinela_20261013_0300.pt",
        "atual": Metricas(0.45, 0.6, {}).para_dict(), "novo": Metricas(0.5, 0.7, {}).para_dict(), "motivo": "melhorou"}})
    assert janela.config.obter("ia.modelo") == "sentinela_20261013_0300.pt"
    assert Config.carregar(janela.config.caminho).obter("ia.modelo") == "sentinela_20261013_0300.pt"
    assert deteccao.recargas == 1  # a IA recarregou sem reiniciar o app
    janela.usar_modelo("yolo11n.pt")  # "Usar este" no modelo original: voltou
    assert janela.config.obter("ia.modelo") == "yolo11n.pt" and deteccao.recargas == 2


def test_rodada_aprovada_sem_troca_automatica_espera_o_usuario(qtbot, janela_treino) -> None:  # noqa: ANN001
    janela, _treino, _modelos = janela_treino
    janela.config.definir("treino.trocar_automatico", False)
    janela._aviso_treino({"fim": True, "rodada": {"inicio": "2026-10-13T03:00:00", "promovido": True,  # noqa: SLF001
                                                   "modelo_novo": "sentinela_20261013_0300.pt"}})
    assert janela.config.obter("ia.modelo") == "yolo11n.pt"


def test_treinar_agora_sem_memoria_explica_e_nao_comeca(qtbot, janela_treino, monkeypatch) -> None:  # noqa: ANN001
    janela, treino, _modelos = janela_treino
    janela.ir_para("treino")
    pagina = janela.pagina_treino
    avisos: list[tuple] = []
    monkeypatch.setattr(pagina, "_notificar", lambda *a, **_k: avisos.append(a))
    monkeypatch.setattr(treino, "falta_memoria", lambda: "pouca memória livre para treinar (2.0 GB)")
    pagina.botao_treinar.click()
    assert avisos and avisos[0][0] == "Não dá para treinar agora" and "Pouca memória" in avisos[0][1]
    assert not treino.estado().rodando and pagina.botao_treinar.isEnabled()


def test_revisao_desenha_corrige_aceita_e_descarta(qtbot, tmp_path: Path) -> None:  # noqa: ANN001
    conjunto = ConjuntoTreino(tmp_path)
    criar_amostra(tmp_path / REVISAR, "a", datetime(2026, 10, 11, 9), rotulos=[rotulo_de_caixa(0, (0.1, 0.1, 0.3, 0.5))])
    criar_amostra(tmp_path / REVISAR, "b", datetime(2026, 10, 11, 10))
    dialogo = DialogoRevisao(conjunto)
    qtbot.addWidget(dialogo)
    dialogo.resize(1200, 760)
    dialogo.show()
    tela = dialogo.tela
    assert len(tela.rotulos) == 1 and "1 de 2" in dialogo.titulo.text()

    area = tela.area()
    inicio = QPoint(round(area.left() + area.width() * 0.6), round(area.top() + area.height() * 0.3))
    fim = QPoint(round(area.left() + area.width() * 0.8), round(area.top() + area.height() * 0.7))
    qtbot.mousePress(tela, Qt.MouseButton.LeftButton, pos=inicio)  # desenha uma caixa nova
    qtbot.mouseMove(tela, fim)
    qtbot.mouseRelease(tela, Qt.MouseButton.LeftButton, pos=fim)
    assert len(tela.rotulos) == 2 and tela.selecionado == 1
    qtbot.keyClick(dialogo, Qt.Key.Key_4)  # tecla 4 = MOTO (COCO 3)
    assert tela.rotulos[1][0] == 3
    qtbot.keyClick(dialogo, Qt.Key.Key_Return)  # aceita
    aceita = Amostra(tmp_path / ROTULADAS / "a.jpg")
    assert aceita.imagem.exists() and aceita.meta()["rotulo"] == "humano"
    assert sorted(r[0] for r in aceita.rotulos()) == [0, 3]

    qtbot.keyClick(dialogo, Qt.Key.Key_D)  # descarta a 2ª
    assert not (tmp_path / REVISAR / "b.jpg").exists() and "Tudo revisado" in dialogo.titulo.text()
    assert (dialogo.aceitas, dialogo.descartadas) == (1, 1)
