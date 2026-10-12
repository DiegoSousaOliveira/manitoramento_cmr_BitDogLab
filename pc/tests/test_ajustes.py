"""Fase 8: tela de Ajustes (campos, salvar e aplicar na hora, ajustes por câmera) e recargas sem reiniciar."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from PySide6.QtWidgets import QMessageBox

from camera.ptz import PTZFalso, ServicoPTZ
from core.config import Config
from core.logs import definir_nivel
from core.modelos import TipoFonte
from tests.test_interface import Contexto, camera_de_arquivo, contexto  # noqa: F401 (fixture)
from tests.test_visao import DetectorFalso
from ui import tema
from ui.formulario import Campo, EditorAjuste
from ui.janela_principal import JanelaPrincipal
from vision.detector import EstadoIA, ServicoDeteccao


def esperar(condicao: Callable[[], bool], limite_s: float = 5.0) -> bool:
    fim = time.monotonic() + limite_s
    while time.monotonic() < fim:
        if condicao():
            return True
        time.sleep(0.02)
    return condicao()


# ===================================================================== campos
def test_campo_em_porcentagem_valida_a_faixa(qapp) -> None:  # noqa: ANN001
    editor = EditorAjuste(Campo("ia.conf_minima", "Confiança mínima", "decimal", 10, 90, sufixo="%", escala=100), 0.4)
    assert editor._entrada.text() == "40" and not editor.alterado  # noqa: SLF001
    editor.definir(0.55)
    assert editor.valor() == 0.55 and editor.alterado and editor.erro() is None
    editor._entrada.setText("95")  # noqa: SLF001
    assert "entre 10 e 90 %" in (editor.erro() or "")
    editor._entrada.setText("12,5")  # vírgula também vale  # noqa: SLF001
    assert editor.valor() == 0.125 and editor.erro() is None
    editor.descartar()
    assert editor.valor() == 0.4 and not editor.alterado


def test_campo_por_camera_vazio_e_o_padrao(qapp) -> None:  # noqa: ANN001
    numero = EditorAjuste(Campo("parado.cooldown_s", "Esperar", "decimal", 10, 3600, sufixo="s"), None, 120, opcional=True)
    assert numero.valor() is None and numero._entrada.placeholderText() == "padrão: 120"  # noqa: SLF001
    numero.definir(60)
    assert numero.valor() == 60 and isinstance(numero.valor(), int)  # 60 e não 60.0 no YAML
    sim_nao = EditorAjuste(Campo("ptz.inverter_x", "Inverter", "booleano"), None, False, opcional=True)
    assert sim_nao._combo.itemText(0) == "Padrão (Não)" and sim_nao.valor() is None  # noqa: SLF001
    sim_nao.definir(True)
    assert sim_nao.valor() is True


# ===================================================================== a tela
@pytest.fixture
def janela(qtbot, contexto: Contexto) -> Iterator[JanelaPrincipal]:  # noqa: ANN001, F811
    janela = JanelaPrincipal(contexto.config, contexto.servico, contexto.gerenciador)
    qtbot.addWidget(janela)
    janela.resize(1400, 860)
    janela.show()
    janela.ir_para("config")
    yield janela
    janela.close()


def test_salvar_grava_o_config_e_aplica_na_hora(janela: JanelaPrincipal, contexto: Contexto) -> None:  # noqa: F811
    pagina = janela.pagina_ajustes
    assert not pagina.barra.isVisible()
    pagina.editor("interface.fps_mosaico").definir(20)
    pagina.editor("interface.layout_mosaico").definir("2x2")
    pagina.editor("interface.tema").definir("claro")
    pagina.editor("parado.tempo_parado.PESSOA").definir(6)
    assert pagina.barra.isVisible() and pagina.tem_alteracoes
    assert pagina.salvar()
    assert not pagina.barra.isVisible()

    salvo = Config.carregar(contexto.config.caminho)  # foi para o arquivo
    assert salvo.obter("interface.fps_mosaico") == 20 and salvo.obter("interface.layout_mosaico") == "2x2"
    assert salvo.obter("parado.tempo_parado.PESSOA") == 6
    assert "# atualizações por segundo" in contexto.config.caminho.read_text(encoding="utf-8")  # comentários ficam
    assert janela.distribuidor.intervalo_ms == 50  # 20 por segundo, sem reiniciar
    assert janela.mosaico.layout_atual == "2x2"
    assert tema.paleta_atual().nome == "claro"  # (o fixture "contexto" volta ao escuro no próximo teste)


def test_valor_invalido_nao_salva(janela: JanelaPrincipal, contexto: Contexto) -> None:  # noqa: F811
    pagina = janela.pagina_ajustes
    editor = pagina.editor("interface.fps_mosaico")
    editor._entrada.setText("99")  # noqa: SLF001
    assert not pagina.salvar() and "entre 1 e 30" in pagina.erro.text()
    assert not pagina.botao_salvar.isEnabled()
    assert Config.carregar(contexto.config.caminho).obter("interface.fps_mosaico") == 12


def test_sair_dos_ajustes_com_alteracoes_pergunta(janela: JanelaPrincipal, monkeypatch: pytest.MonkeyPatch) -> None:
    pagina = janela.pagina_ajustes
    pagina.editor("interface.fps_mosaico").definir(25)
    monkeypatch.setattr(QMessageBox, "question", lambda *_a, **_k: QMessageBox.StandardButton.Cancel)
    janela.ir_para("cameras")
    assert janela.paginas.currentIndex() == janela._indice_pagina["config"]  # noqa: SLF001  (cancelou: ficou)
    monkeypatch.setattr(QMessageBox, "question", lambda *_a, **_k: QMessageBox.StandardButton.Discard)
    janela.ir_para("cameras")
    assert janela.paginas.currentIndex() == janela._indice_pagina["cameras"]  # noqa: SLF001
    assert pagina.editor("interface.fps_mosaico").valor() == 12  # descartado


def test_ajustes_por_camera(janela: JanelaPrincipal, contexto: Contexto, video_teste: Path) -> None:  # noqa: F811
    camera = contexto.servico.cadastrar(camera_de_arquivo(video_teste, tem_ptz=True), "")
    camera = contexto.servico.salvar_ajustes(camera.id or 0, True, True, {"outra.tela": 1})  # chave de outra tela
    janela.recarregar_cameras()
    pagina = janela.pagina_ajustes
    pagina.mostrar_secao("cameras")
    pagina.escolher_camera(camera.id or 0)
    pagina.editor_camera("camera.deteccao").definir(False)
    pagina.editor_camera("parado.cooldown_s").definir(60)
    pagina.editor_camera("parado.tempo_parado.PESSOA").definir(5)
    pagina.editor_camera("ptz.inverter_x").definir(True)
    pagina.editor_camera("eventos.area").definir("zona")
    assert pagina.salvar()
    salva = contexto.servico.obter(camera.id or 0)
    assert salva is not None and not salva.deteccao and salva.gravar
    assert salva.ajustes == {
        "outra.tela": 1, "parado.cooldown_s": 60, "parado.tempo_parado": {"PESSOA": 5},
        "ptz.inverter_x": True, "eventos.area": "zona",
    }
    assert janela._cameras[camera.id or 0].deteccao is False  # noqa: SLF001  (aplicado: a janela recarregou)

    pagina.botao_padroes.click()  # "Voltar tudo ao padrão"
    assert pagina.salvar()
    assert contexto.servico.obter(camera.id or 0).ajustes == {"outra.tela": 1}  # type: ignore[union-attr]


def test_secoes_e_placa(janela: JanelaPrincipal) -> None:
    pagina = janela.pagina_ajustes
    for nome in ("aparencia", "ia", "eventos", "alertas", "placa", "ptz", "cameras", "sistema"):
        pagina.mostrar_secao(nome)
        assert pagina.secao_atual == nome
        pagina.grab()  # desenha sem erro
    janela.ir_para("cameras")
    janela.barra_lateral.placa_clicada.emit()  # clique na placa da barra lateral
    assert pagina.secao_atual == "placa"


# ===================================================================== recargas sem reiniciar
def test_ia_troca_o_modelo_sem_parar(contexto: Contexto) -> None:  # noqa: F811
    carregados: list[str] = []

    def carregar() -> DetectorFalso:
        if len(carregados) == 2:
            raise RuntimeError("arquivo corrompido")
        carregados.append("ok")
        return DetectorFalso()

    servico = ServicoDeteccao(contexto.config, lambda _v: None, carregar=carregar).iniciar()
    try:
        assert esperar(lambda: servico.estado is EstadoIA.PRONTO)
        servico.recarregar_modelo()
        assert esperar(lambda: len(carregados) == 2 and servico.estado is EstadoIA.PRONTO)
        servico.recarregar_modelo()  # o 3º falha: continua o anterior, com explicação
        assert esperar(lambda: "usando o anterior" in servico.mensagem)
        assert servico.estado is EstadoIA.PRONTO
    finally:
        servico.parar()


def test_ptz_inverter_por_camera(contexto: Contexto, video_teste: Path) -> None:  # noqa: F811
    controles: dict[int, PTZFalso] = {}

    def conectar(camera, _senha, _timeout):  # noqa: ANN001, ANN202
        controles[camera.id] = PTZFalso(camera.nome)
        return controles[camera.id]

    contexto.config.definir("ptz.inverter_x", False)  # o config.yaml real é do usuário (ele pode ter invertido)
    contexto.config.definir("ptz.inverter_y", False)
    ptz = ServicoPTZ(contexto.config, lambda _id: "", conectar=conectar).iniciar()
    try:
        normal = camera_de_arquivo(video_teste, tem_ptz=True, id=1)
        invertida = camera_de_arquivo(video_teste, nome="B", codigo="B", tem_ptz=True, id=2,
                                      ajustes={"ptz.inverter_x": True})
        assert normal.fonte is TipoFonte.ARQUIVO
        ptz.definir_cameras([normal, invertida])
        assert esperar(lambda: len(controles) == 2)
        ptz.mover(1, 0.5, 0.0)
        ptz.mover(2, 0.5, 0.0)
        assert esperar(lambda: controles[1].velocidade == (0.5, 0.0) and controles[2].velocidade == (-0.5, 0.0))
    finally:
        ptz.parar()


def test_nivel_do_log_muda_na_hora() -> None:
    raiz = logging.getLogger()
    antes = raiz.level
    try:
        definir_nivel("debug")
        assert raiz.level == logging.DEBUG
    finally:
        raiz.setLevel(antes)
