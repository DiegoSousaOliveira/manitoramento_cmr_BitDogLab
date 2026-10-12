"""Interface da Fase 4: lista de eventos, visualizador (player + foto) e o fluxo completo."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date, datetime, timedelta
from pathlib import Path

import cv2
import numpy as np
import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QMessageBox

from core.modelos import Deteccao, Evento
from eventos.gravador import ServicoEventos
from tests.test_interface import Contexto, camera_de_arquivo, contexto  # noqa: F401 (fixture)
from tests.test_visao import DetectorFalso
from ui.janela_principal import JanelaPrincipal
from ui.painel_eventos import PainelEventos, nome_do_dia
from ui.visualizador_evento import deteccoes_no_tempo
from vision.detector import ServicoDeteccao


# ===================================================================== peças
def test_caixas_do_player_pelo_tempo() -> None:
    pessoa = (Deteccao("PESSOA", 0.9, (0.1, 0.1, 0.2, 0.2), 1),)
    carro = (Deteccao("CARRO", 0.8, (0.5, 0.5, 0.7, 0.7), 2),)
    tempos, grupos = [1.0, 1.2], [pessoa, carro]
    assert deteccoes_no_tempo(tempos, grupos, 0.5) == ()  # antes da 1ª análise
    assert deteccoes_no_tempo(tempos, grupos, 1.1) == pessoa
    assert deteccoes_no_tempo(tempos, grupos, 1.3) == carro
    assert deteccoes_no_tempo(tempos, grupos, 2.0) == ()  # análise velha demais (> 0,5 s)


def test_nome_do_dia() -> None:
    hoje = date(2026, 10, 11)
    assert nome_do_dia(hoje, hoje) == "Hoje"
    assert nome_do_dia(hoje - timedelta(days=1), hoje) == "Ontem"
    assert nome_do_dia(date(2026, 10, 8), hoje) == "08/10/2026 · quinta"


def test_filtros_da_lista(qtbot, contexto: Contexto, video_teste: Path) -> None:  # noqa: ANN001, F811
    camera = contexto.servico.cadastrar(camera_de_arquivo(video_teste), "")
    hoje = datetime.now().replace(microsecond=0)
    banco = contexto.servico.banco
    for horas, classes, alerta in [(1, {"PESSOA": 1}, True), (2, {"CARRO": 2}, False), (30, {"PESSOA": 1}, False)]:
        inicio = hoje - timedelta(hours=horas)
        banco.adicionar_evento(Evento(camera.id or 0, inicio, fim=inicio + timedelta(seconds=8), classes=classes,
                                      alerta=alerta, pasta="x"))
    painel = PainelEventos(contexto.servico, todas_cameras=True)
    qtbot.addWidget(painel)
    painel.definir_cameras([camera])
    painel.recarregar()
    assert len(painel.modelo.eventos()) == 3
    linhas = [painel.modelo.data(painel.modelo.index(i), Qt.ItemDataRole.UserRole) for i in range(painel.modelo.rowCount())]
    assert sum(isinstance(l, date) and not isinstance(l, datetime) for l in linhas) >= 2  # cabeçalhos por dia
    painel.filtro_classe.setCurrentIndex(painel.filtro_classe.findData("PESSOA"))
    assert len(painel.modelo.eventos()) == 2
    painel.so_alertas.setChecked(True)
    assert len(painel.modelo.eventos()) == 1
    painel.so_alertas.setChecked(False)
    painel.filtrar_dia.setChecked(True)  # hoje
    assert all(e.inicio.date() == date.today() for e in painel.modelo.eventos())
    painel.grab()  # desenha os cartões sem erro


# ===================================================================== fluxo completo
@pytest.fixture
def video_quadrado(tmp_path: Path) -> Path:
    caminho = tmp_path / "quadrado.mp4"
    gravador = cv2.VideoWriter(str(caminho), cv2.VideoWriter_fourcc(*"mp4v"), 12.0, (320, 180))
    for i in range(120):
        quadro = np.full((180, 320, 3), 40, dtype=np.uint8)
        cv2.rectangle(quadro, (i * 2, 60), (i * 2 + 40, 120), (255, 255, 255), -1)
        gravador.write(quadro)
    gravador.release()
    return caminho


@pytest.fixture
def janela_completa(qtbot, contexto: Contexto) -> Iterator[JanelaPrincipal]:  # noqa: ANN001, F811
    """Janela com IA (detector falso, movimento real) e serviço de eventos de verdade."""
    c = contexto.config
    for chave, valor in {
        "eventos.pre_evento_s": 1.0, "eventos.pos_evento_s": 0.5, "eventos.fim_evento_s": 1.0,
        "eventos.max_evento_s": 3.0, "eventos.aquecimento_s": 0.5, "ia.movimento_aquecimento_s": 0.5,
        "treino.coletar": False, "eventos.area": "quadro",  # o config.yaml real pode estar em "Só dentro da zona"
    }.items():
        c.definir(chave, valor)
    deteccao = ServicoDeteccao(c, contexto.gerenciador.leitor_para_ia, carregar=DetectorFalso).iniciar()
    eventos = ServicoEventos(c, contexto.servico, contexto.gerenciador).iniciar()
    deteccao.assinar(eventos.receber_resultado)
    janela = JanelaPrincipal(c, contexto.servico, contexto.gerenciador, deteccao, eventos)
    qtbot.addWidget(janela)
    janela.resize(1400, 860)
    janela.show()
    yield janela
    janela.close()
    deteccao.parar()
    eventos.parar()


def test_movimento_vira_evento_na_lista_e_abre_foto_e_video_com_caixas(
    qtbot, janela_completa: JanelaPrincipal, contexto: Contexto, video_quadrado: Path  # noqa: ANN001, F811
) -> None:
    janela = janela_completa
    camera = contexto.servico.cadastrar(camera_de_arquivo(video_quadrado), "")
    janela.recarregar_cameras()

    painel = janela.pagina_eventos.painel
    qtbot.waitUntil(lambda: bool(painel.modelo.eventos()), timeout=25000)  # chega sozinho (tempo real)
    evento = painel.modelo.eventos()[0]
    assert evento.inicio.date() == date.today() and evento.classes.get("PESSOA") == 1
    assert "Movimento" not in painel.nome_vista(evento)

    janela.ir_para("eventos")
    linha = painel.modelo.linha_do_evento(evento.id or 0)
    retangulo = painel.lista.visualRect(painel.modelo.index(linha))
    qtbot.mouseClick(painel.lista.viewport(), Qt.MouseButton.LeftButton, pos=retangulo.center())
    visualizador = janela.pagina_eventos.visualizador
    assert janela.pagina_eventos.pilha.currentWidget() is visualizador
    assert not visualizador.foto.pixmap().isNull()  # a foto do momento principal
    qtbot.waitUntil(lambda: visualizador.tela.tem_imagem, timeout=5000)  # 1º quadro do clipe

    visualizador.alternar()  # play: as caixas do deteccoes.json aparecem sobre o vídeo
    qtbot.waitUntil(lambda: any(d.classe == "PESSOA" for d in visualizador.tela.deteccoes), timeout=6000)
    visualizador.passo(-1)
    visualizador.mostrar_foto(True)
    assert visualizador.tela.selo == "Foto do momento principal"
    visualizador.grab()

    # O mesmo evento aparece no painel da Tela da câmera
    janela.abrir_camera((camera.id or 0, 1))
    assert any(e.id == evento.id for e in janela.tela_camera.painel.modelo.eventos())
    janela.tela_camera.mostrar_evento(evento)
    assert janela.tela_camera.vendo_evento
    janela._esc()  # noqa: SLF001  (Esc: volta ao vivo)
    assert not janela.tela_camera.vendo_evento and janela.tela_camera.aberta


def test_excluir_evento_apaga_arquivos_e_some_das_listas(
    qtbot, janela_completa: JanelaPrincipal, contexto: Contexto, video_quadrado: Path,  # noqa: ANN001, F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    janela = janela_completa
    camera = contexto.servico.cadastrar(camera_de_arquivo(video_quadrado), "")
    janela.recarregar_cameras()
    painel = janela.pagina_eventos.painel
    qtbot.waitUntil(lambda: bool(painel.modelo.eventos()), timeout=25000)
    evento = painel.modelo.eventos()[-1]
    pasta = contexto.servico.pastas(camera)[0] / evento.pasta
    assert (pasta / "clipe.mp4").is_file()

    janela.pagina_eventos.mostrar_evento(evento)
    visualizador = janela.pagina_eventos.visualizador
    qtbot.waitUntil(lambda: visualizador.tela.tem_imagem, timeout=5000)  # o clipe está aberto
    monkeypatch.setattr(QMessageBox, "question", lambda *_a, **_k: QMessageBox.StandardButton.Yes)
    visualizador.excluir()
    assert not pasta.exists()  # o arquivo aberto foi solto antes de apagar (Windows)
    assert all(e.id != evento.id for e in painel.modelo.eventos())
    assert contexto.servico.banco.obter_evento(evento.id or 0) is None
