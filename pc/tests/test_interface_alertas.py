"""Interface da Fase 5: editor de zona, borda/contador no bloco e o alerta de ponta a ponta."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import cv2
import numpy as np
import pytest
from PySide6.QtCore import QPoint, Qt

from core.modelos import Camera
from eventos.alertas import ServicoAlertas
from eventos.gravador import ServicoEventos
from tests.test_interface import Contexto, camera_de_arquivo, contexto, video_duas_lentes  # noqa: F401 (fixtures)
from tests.test_visao import DetectorFalso
from ui.bloco_camera import BlocoCamera
from ui.janela_principal import JanelaPrincipal
from vision.detector import ServicoDeteccao
from vision.dwell import Aviso, Progresso, SituacaoParado, TipoAviso


def ponto(editor_area, x: float, y: float) -> QPoint:  # noqa: ANN001
    """Fração da imagem (0 a 1) -> posição em pixels no editor."""
    return QPoint(round(editor_area.left() + x * editor_area.width()), round(editor_area.top() + y * editor_area.height()))


# ===================================================================== bloco
def test_bloco_mostra_contador_e_borda_de_alerta(qtbot, contexto: Contexto) -> None:  # noqa: ANN001, F811
    camera = camera_de_arquivo(Path("x.mp4"), id=1)
    bloco = BlocoCamera(camera.vistas()[0], "Frente")
    qtbot.addWidget(bloco)
    bloco.resize(480, 270)
    bloco.definir_zona([(0.1, 0.1), (0.9, 0.1), (0.9, 0.9)])
    bloco.configurar_sobreposicao(caixas=True, chips=True, zona=True)
    bloco.definir_parado(SituacaoParado(1, {7: Progresso(5.0, 8.0, False)}, alerta=False))
    assert not bloco.alerta and bloco.progresso[7].segundos == 5.0
    bloco.grab()
    bloco.definir_parado(SituacaoParado(2, {7: Progresso(9.0, 8.0, True)}, alerta=True))
    assert bloco.alerta
    bloco.grab()  # borda vermelha + selo "ALERTA" desenhados sem erro
    bloco.definir_parado(None)
    assert not bloco.alerta and bloco.progresso == {}


# ===================================================================== editor de zona
@pytest.fixture
def janela_simples(qtbot, contexto: Contexto) -> Iterator[JanelaPrincipal]:  # noqa: ANN001, F811
    janela = JanelaPrincipal(contexto.config, contexto.servico, contexto.gerenciador)
    qtbot.addWidget(janela)
    janela.resize(1400, 860)
    janela.show()
    yield janela
    janela.close()


def test_editor_de_zona_cria_arrasta_apaga_desfaz_e_salva(
    qtbot, janela_simples: JanelaPrincipal, contexto: Contexto, video_teste: Path  # noqa: ANN001, F811
) -> None:
    janela = janela_simples
    camera = contexto.servico.cadastrar(camera_de_arquivo(video_teste), "")
    janela.recarregar_cameras()
    chave = (camera.id or 0, 1)
    janela.abrir_camera(chave)
    tela = janela.tela_camera
    bloco = tela.bloco(chave)
    assert bloco is not None
    qtbot.waitUntil(lambda: bloco.tem_imagem, timeout=8000)

    tela.botao_zona.click()
    editor = tela.editor
    assert tela.editando_zona and editor is not None
    assert not tela.botao_zona.isVisible() and tela.botao_salvar_zona.isVisible()
    area = bloco.area_imagem
    for x, y in [(0.1, 0.2), (0.6, 0.2), (0.6, 0.8), (0.1, 0.8)]:
        qtbot.mouseClick(editor, Qt.MouseButton.LeftButton, pos=ponto(area, x, y))
    assert len(editor.pontos) == 4
    assert editor.pontos[0] == pytest.approx((0.1, 0.2), abs=0.01)

    # Arrasta o 2º ponto para a direita
    inicio = editor.ponto_na_tela(1).toPoint()
    qtbot.mousePress(editor, Qt.MouseButton.LeftButton, pos=inicio)
    qtbot.mouseMove(editor, ponto(area, 0.9, 0.2))
    qtbot.mouseRelease(editor, Qt.MouseButton.LeftButton, pos=ponto(area, 0.9, 0.2))
    assert editor.pontos[1] == pytest.approx((0.9, 0.2), abs=0.01)

    # Botão direito apaga um ponto; Desfazer traz de volta
    qtbot.mouseClick(editor, Qt.MouseButton.RightButton, pos=editor.ponto_na_tela(3).toPoint())
    assert len(editor.pontos) == 3
    tela.botao_desfazer.click()
    assert len(editor.pontos) == 4

    tela.botao_salvar_zona.click()
    salva = contexto.servico.banco.obter_zona(camera.id or 0, 1)
    assert salva is not None and len(salva) == 4 and salva[1] == pytest.approx((0.9, 0.2), abs=0.01)
    assert not tela.editando_zona and bloco.zona == salva and tela.botao_zona.isVisible()

    # Zona inválida (2 pontos) não é salva; Esc cancela a edição
    tela.editar_zona()
    assert tela.editor is not None and len(tela.editor.pontos) == 4  # começa da zona salva
    tela.botao_limpar_zona.click()
    for x, y in [(0.2, 0.2), (0.5, 0.5)]:
        qtbot.mouseClick(tela.editor, Qt.MouseButton.LeftButton, pos=ponto(area, x, y))
    assert not tela.salvar_zona() and tela.editando_zona
    janela._esc()  # noqa: SLF001
    assert not tela.editando_zona and contexto.servico.banco.obter_zona(camera.id or 0, 1) == salva

    # Limpar tudo e salvar = sem zona (a imagem inteira)
    tela.editar_zona()
    tela.botao_limpar_zona.click()
    assert tela.salvar_zona()
    assert contexto.servico.banco.obter_zona(camera.id or 0, 1) is None and bloco.zona is None


def test_zona_da_lente_2_fica_na_lente_2(
    qtbot, janela_simples: JanelaPrincipal, contexto: Contexto, video_duas_lentes: Path  # noqa: ANN001, F811
) -> None:
    """Bug relatado pelo Diego (12/10): na aba "Lente 2", o botão Zona voltava para a Lente 1."""
    from core.modelos import DisposicaoLentes

    janela = janela_simples
    camera = contexto.servico.cadastrar(camera_de_arquivo(video_duas_lentes, lentes=DisposicaoLentes.VERTICAL), "")
    janela.recarregar_cameras()
    lente2 = (camera.id or 0, 2)
    janela.abrir_camera(lente2)  # clicou no bloco da Lente 2 (a do motor, em frente ao portão)
    tela = janela.tela_camera
    bloco = tela.bloco(lente2)
    assert bloco is not None
    qtbot.waitUntil(lambda: bloco.tem_imagem, timeout=8000)
    assert [v.numero for v in tela.vistas_visiveis()] == [2]

    tela.botao_zona.click()
    assert tela.editando_zona and tela.editor is not None
    assert [v.numero for v in tela.vistas_visiveis()] == [2]  # continua na Lente 2
    assert "Lente 2" in tela.texto_zona.text()
    area = bloco.area_imagem
    for x, y in [(0.2, 0.3), (0.8, 0.3), (0.5, 0.9)]:
        qtbot.mouseClick(tela.editor, Qt.MouseButton.LeftButton, pos=ponto(area, x, y))
    assert tela.salvar_zona()
    assert contexto.servico.banco.obter_zona(camera.id or 0, 2) is not None  # gravou na Lente 2...
    assert contexto.servico.banco.obter_zona(camera.id or 0, 1) is None      # ...e não mexeu na Lente 1
    assert bloco.zona is not None and tela.bloco((camera.id or 0, 1)).zona is None  # type: ignore[union-attr]

    tela.definir_foco(None)  # aba "Todas": o menu pergunta a lente; escolher a 2 edita a 2
    tela.editar_zona(2)
    assert [v.numero for v in tela.vistas_visiveis()] == [2] and "Lente 2" in tela.texto_zona.text()
    tela.cancelar_zona()


# ===================================================================== de ponta a ponta
@pytest.fixture
def video_movimento(tmp_path: Path) -> Path:
    """Um quadrado andando (movimento contínuo: o evento fica aberto)."""
    caminho = tmp_path / "movimento.mp4"
    gravador = cv2.VideoWriter(str(caminho), cv2.VideoWriter_fourcc(*"mp4v"), 12.0, (320, 180))
    for i in range(120):
        quadro = np.full((180, 320, 3), 40, dtype=np.uint8)
        cv2.rectangle(quadro, (i * 2, 120), (i * 2 + 40, 170), (255, 255, 255), -1)
        gravador.write(quadro)
    gravador.release()
    return caminho


def test_pessoa_parada_na_zona_gera_um_unico_alerta_com_toast_borda_e_evento(
    qtbot, contexto: Contexto, video_movimento: Path  # noqa: ANN001, F811
) -> None:
    """Pronto quando da Fase 5 (com 1,5 s no lugar de 8 s): parado na zona -> 1 alerta."""
    c = contexto.config
    for chave, valor in {
        "eventos.pre_evento_s": 1.0, "eventos.pos_evento_s": 0.5, "eventos.fim_evento_s": 1.0,
        "eventos.max_evento_s": 4.0, "eventos.aquecimento_s": 0.5, "ia.movimento_aquecimento_s": 0.5,
        "treino.coletar": False, "parado.aquecimento_s": 0, "parado.tempo_parado.PESSOA": 1.5,
    }.items():
        c.definir(chave, valor)
    deteccao = ServicoDeteccao(c, contexto.gerenciador.leitor_para_ia, carregar=DetectorFalso).iniciar()
    eventos = ServicoEventos(c, contexto.servico, contexto.gerenciador).iniciar()
    alertas = ServicoAlertas(c, contexto.servico, eventos).iniciar()
    deteccao.assinar(eventos.receber_resultado)
    deteccao.assinar(alertas.receber_resultado)
    avisos: list[Aviso] = []
    alertas.assinar(lambda aviso, _camera: avisos.append(aviso))
    janela = JanelaPrincipal(c, contexto.servico, contexto.gerenciador, deteccao, eventos, alertas)
    qtbot.addWidget(janela)
    janela.resize(1400, 860)
    janela.show()
    toasts: list[tuple[str, str]] = []
    original = janela.notificador.mostrar

    def anotar(titulo: str, mensagem: str = "", tipo: str = "info", **extra: object) -> object:
        toasts.append((titulo, tipo))
        return original(titulo, mensagem, tipo, **extra)  # type: ignore[arg-type]

    janela.notificador.mostrar = anotar  # type: ignore[method-assign]
    try:
        camera: Camera = contexto.servico.cadastrar(camera_de_arquivo(video_movimento), "")
        # Zona = metade de cima, onde está a "pessoa" do detector falso (o quadrado anda embaixo)
        contexto.servico.banco.salvar_zona(camera.id or 0, 1, [(0.0, 0.0), (1.0, 0.0), (1.0, 0.75), (0.0, 0.75)])
        janela.recarregar_cameras()
        chave = (camera.id or 0, 1)

        qtbot.waitUntil(lambda: ("PESSOA parada na zona", "alerta") in toasts, timeout=20000)
        bloco = janela.mosaico.bloco(chave)
        assert bloco is not None
        qtbot.waitUntil(lambda: bloco.alerta, timeout=3000)  # borda vermelha no mosaico
        assert any(p.alertado for p in bloco.progresso.values())

        qtbot.wait(2500)  # continua parada: nada de 2º alerta
        assert [a.tipo for a in avisos] == [TipoAviso.ALERTA]
        assert sum(1 for titulo, _ in toasts if "parada" in titulo) == 1

        # O evento em andamento recebe o selo ALERTA (fecha em max_evento_s: o vídeo não para de mexer)
        painel = janela.pagina_eventos.painel
        qtbot.waitUntil(lambda: any(e.alerta for e in painel.modelo.eventos()), timeout=40000)
        janela.abrir_alerta(avisos[0])  # clique no toast: abre o evento do alerta
        assert janela.pagina_eventos.pilha.currentWidget() is janela.pagina_eventos.visualizador
    finally:
        janela.close()
        deteccao.parar()
        alertas.parar()
        eventos.parar()
