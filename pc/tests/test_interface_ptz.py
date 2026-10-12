"""Fase 7 na interface: painel PTZ, setas do teclado e o joystick da placa (simulador) movendo a câmera."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from PySide6.QtCore import Qt

from camera.ptz import ServicoPTZ
from link.bitdog import LinkBitDog
from tests.test_interface import Contexto, camera_de_arquivo, contexto  # noqa: F401 (fixture)
from tests.test_link import porta_livre
from tools.simulador_bitdoglab import ConexaoPlaca
from ui.janela_principal import JanelaPrincipal


@pytest.fixture
def janela_ptz(qtbot, contexto: Contexto) -> Iterator[tuple[JanelaPrincipal, ServicoPTZ, LinkBitDog]]:  # noqa: ANN001, F811
    c = contexto.config
    porta_pc = porta_livre()
    c.definir("placa.porta_pc", porta_pc)
    c.definir("placa.intervalo_ping_s", 0.3)
    link = LinkBitDog(c, endereco="127.0.0.1").iniciar()
    ptz = ServicoPTZ(c, contexto.servico.obter_senha).iniciar()  # câmera de arquivo: PTZ simulado
    link.assinar_joystick(lambda x, y: ptz.mover(link.camera_joystick, x, y))  # como no main.py
    link.assinar_camera(ptz.parar_outras)
    janela = JanelaPrincipal(c, contexto.servico, contexto.gerenciador, link=link, ptz=ptz)
    qtbot.addWidget(janela)
    janela.resize(1400, 860)
    janela.show()
    yield janela, ptz, link
    janela.close()
    ptz.parar()
    link.parar()


def test_painel_ptz_setas_e_teclado(
    qtbot, janela_ptz: tuple[JanelaPrincipal, ServicoPTZ, LinkBitDog], contexto: Contexto, video_teste: Path,  # noqa: ANN001, F811
) -> None:
    janela, ptz, _link = janela_ptz
    com_motor = contexto.servico.cadastrar(camera_de_arquivo(video_teste, tem_ptz=True), "")
    sem_motor = contexto.servico.cadastrar(camera_de_arquivo(video_teste, nome="Garagem", codigo="GARAGEM"), "")
    janela.recarregar_cameras()

    janela.abrir_camera((sem_motor.id or 0, 1))
    tela = janela.tela_camera
    assert not tela.botao_ptz.isVisible() and not tela.controles_ptz.isVisible()

    janela.abrir_camera((com_motor.id or 0, 1))
    assert tela.botao_ptz.isVisible() and tela.controles_ptz.isVisible()
    qtbot.waitUntil(lambda: (e := ptz.estado(com_motor.id)) is not None and e.tipo == "falso", timeout=3000)
    painel = tela.controles_ptz

    qtbot.mousePress(painel.botoes["direita"], Qt.MouseButton.LeftButton)  # segura a seta
    qtbot.waitUntil(lambda: painel.indicador.movendo and painel.indicador.x > 0, timeout=2000)
    qtbot.mouseRelease(painel.botoes["direita"], Qt.MouseButton.LeftButton)  # soltou: para
    qtbot.waitUntil(lambda: not painel.indicador.movendo, timeout=2000)

    qtbot.keyPress(tela, Qt.Key.Key_Up)  # seta do teclado
    qtbot.waitUntil(lambda: painel.indicador.movendo and painel.indicador.y > 0, timeout=2000)
    qtbot.keyRelease(tela, Qt.Key.Key_Up)
    qtbot.waitUntil(lambda: not ptz.estado(com_motor.id).movendo, timeout=2000)  # type: ignore[union-attr]

    tela.botao_ptz.setChecked(False)  # esconde o painel: as setas voltam a ser só setas
    assert not painel.isVisible()
    qtbot.keyPress(tela, Qt.Key.Key_Left)
    qtbot.wait(200)
    assert not ptz.estado(com_motor.id).movendo  # type: ignore[union-attr]


def test_joystick_da_placa_move_a_camera_e_o_indicador_acompanha(
    qtbot, janela_ptz: tuple[JanelaPrincipal, ServicoPTZ, LinkBitDog], contexto: Contexto, video_teste: Path,  # noqa: ANN001, F811
) -> None:
    """Pronto quando da Fase 7: com o PTZ simulado, o indicador acompanha o joystick."""
    janela, ptz, link = janela_ptz
    camera = contexto.servico.cadastrar(camera_de_arquivo(video_teste, codigo="FRENTE", tem_ptz=True), "")
    janela.recarregar_cameras()
    simulador = ConexaoPlaca(None, porta_placa=porta_livre(), endereco="127.0.0.1")

    def rodando(condicao) -> bool:  # noqa: ANN001
        simulador.processar()
        return bool(condicao())

    try:
        janela.secao_placa.ip.setText("127.0.0.1")
        janela.secao_placa.porta_placa.setValue(simulador.placa.porta)
        assert janela.secao_placa.salvar()
        janela.abrir_camera((camera.id or 0, 1))  # a câmera aberta é a do joystick
        qtbot.waitUntil(lambda: rodando(lambda: link.estado().conectada and simulador.placa.cam == "FRENTE"), timeout=6000)
        painel = janela.tela_camera.controles_ptz

        simulador.enviar(simulador.placa.mover_joystick(0.8, -0.4, 0.0))  # mexeu o joystick
        qtbot.waitUntil(lambda: rodando(lambda: (painel.indicador.x, painel.indicador.y) == (0.8, -0.4)), timeout=3000)
        assert painel.indicador.movendo and "simulado" in painel.tipo.text()

        simulador.enviar(simulador.placa.mover_joystick(0.0, 0.0, 1.0))  # soltou
        qtbot.waitUntil(lambda: rodando(lambda: not painel.indicador.movendo), timeout=3000)
        assert (painel.indicador.x, painel.indicador.y) == (0.0, 0.0)
    finally:
        simulador.fechar()
