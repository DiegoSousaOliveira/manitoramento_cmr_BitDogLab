"""Fase 6 de ponta a ponta: app (janela + link) ⇄ simulador da placa, por UDP em 127.0.0.1."""

from __future__ import annotations

import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from PySide6.QtCore import Qt

from core.config import Config
from core.modelos import Nivel
from link.bitdog import EstadoPlaca, LinkBitDog
from tests.test_interface import Contexto, camera_de_arquivo, contexto  # noqa: F401 (fixture)
from tests.test_link import porta_livre
from tools.simulador_bitdoglab import ConexaoPlaca, Ocorrencia, PlacaSimulada
from ui.janela_principal import JanelaPrincipal, descrever_placa
from vision.dwell import Aviso, TipoAviso


@pytest.fixture
def simulador_e_janela(qtbot, contexto: Contexto) -> Iterator[tuple[JanelaPrincipal, ConexaoPlaca, list[Ocorrencia]]]:  # noqa: ANN001, F811
    c = contexto.config
    porta_pc, porta_placa = porta_livre(), porta_livre()
    for chave, valor in {"placa.porta_pc": porta_pc, "placa.porta_placa": porta_placa, "placa.intervalo_ping_s": 0.3}.items():
        c.definir(chave, valor)
    link = LinkBitDog(c, endereco="127.0.0.1").iniciar()  # placa.ip vazio: aprende pelo OLA
    janela = JanelaPrincipal(c, contexto.servico, contexto.gerenciador, link=link)
    qtbot.addWidget(janela)
    janela.show()
    conexao = ConexaoPlaca("127.0.0.1", porta_pc, porta_placa, endereco="127.0.0.1")
    ocorrencias = conexao.iniciar()  # OLA + MODO
    yield janela, conexao, ocorrencias
    janela.close()
    link.parar()
    conexao.fechar()


def test_alerta_aparece_no_simulador_e_status_na_barra_lateral(
    qtbot, simulador_e_janela: tuple[JanelaPrincipal, ConexaoPlaca, list[Ocorrencia]], contexto: Contexto,  # noqa: ANN001, F811
    video_teste: Path,
) -> None:
    """Pronto quando da Fase 6."""
    janela, conexao, ocorrencias = simulador_e_janela
    camera = contexto.servico.cadastrar(camera_de_arquivo(video_teste, codigo="FRENTE", tem_ptz=True), "")
    janela.recarregar_cameras()
    placa: PlacaSimulada = conexao.placa

    def rodando(condicao) -> bool:  # noqa: ANN001
        ocorrencias.extend(conexao.processar())  # a "placa" trabalha enquanto o Qt espera
        return bool(condicao())

    qtbot.waitUntil(lambda: rodando(lambda: janela.barra_lateral.placa_conectada), timeout=8000)
    assert "BitDogLab conectada · 127.0.0.1" in janela.barra_lateral.detalhe_placa
    assert "modo NORMAL" in janela.barra_lateral.detalhe_placa
    qtbot.waitUntil(lambda: rodando(lambda: placa.cam == "FRENTE" and placa.hora != "--:--"), timeout=3000)

    alerta = Aviso(TipoAviso.ALERTA, (camera.id or 0, 1), 7, "PESSOA", time.monotonic(), segundos=8.0,
                   confianca=0.87, nivel=Nivel.ALTO)
    janela.link.receber_aviso(alerta, camera)  # type: ignore[union-attr]  (o ServicoAlertas faz isso)
    qtbot.waitUntil(lambda: rodando(lambda: placa.alerta is not None), timeout=3000)
    textos = [t for t, _ in placa.linhas_oled(time.monotonic())]
    assert textos[:3] == ["ALERTA FRENTE", "PESSOA", "PARADO 8s"]
    assert any(o.tipo == "buzzer" and o.texto == "ALTO" for o in ocorrencias)
    qtbot.waitUntil(lambda: rodando(lambda: "Na placa agora: PESSOA em FRENTE" in janela.barra_lateral.detalhe_placa),
                    timeout=3000)

    # Botão A no simulador: a janela avisa que o alerta foi silenciado
    toasts: list[str] = []
    original = janela.notificador.mostrar

    def anotar(titulo: str, *argumentos: object, **extra: object) -> object:
        toasts.append(titulo)
        return original(titulo, *argumentos, **extra)  # type: ignore[arg-type]

    janela.notificador.mostrar = anotar  # type: ignore[method-assign]
    conexao.enviar(placa.apertar("AA"))  # 2 toques no A: cala o alerta atual
    qtbot.waitUntil(lambda: rodando(lambda: "Alerta silenciado na placa" in toasts), timeout=3000)

    # O objeto foi embora: LIMPAR apaga o alerta do OLED
    janela.link.receber_aviso(alerta.__class__(TipoAviso.LIMPAR, alerta.chave, 7, "PESSOA", time.monotonic()), camera)  # type: ignore[union-attr]
    qtbot.waitUntil(lambda: rodando(lambda: placa.alerta is None), timeout=3000)

    # Simulador fechado: a barra lateral mostra "desconectada" depois do timeout do PONG
    conexao.fechar()
    janela.link._timeout_pong = 0.8  # type: ignore[union-attr]  # noqa: SLF001 (não esperar 6 s no teste)
    qtbot.waitUntil(lambda: not janela.barra_lateral.placa_conectada, timeout=5000)
    assert "Placa BitDogLab desconectada" in toasts


def test_descrever_placa() -> None:
    assert "cadastrar o IP da placa" in descrever_placa(EstadoPlaca(False, None, 5005))
    assert "sem resposta de 192.168.0.60:5006 há 9 s" in descrever_placa(EstadoPlaca(False, "192.168.0.60", 5005, sem_resposta_s=9.2))
    texto = descrever_placa(EstadoPlaca(True, "192.168.0.60", 5005, modo="NOITE", resposta_ms=12.4, camera="FRENTE"))
    assert "conectada · 192.168.0.60 · modo NOITE · resposta 12 ms" in texto and "câmera FRENTE" in texto
    assert "porta 5005 em uso" in descrever_placa(EstadoPlaca(False, None, 5005, erro="porta 5005 em uso"))


def test_janela_do_simulador_desenha_o_alerta() -> None:
    """A janela Tkinter do simulador monta e mostra o alerta (sem abrir o laço principal)."""
    tk = pytest.importorskip("tkinter")
    from tools.simulador_bitdoglab import SimuladorApp

    try:
        raiz = tk.Tk()
    except tk.TclError:
        pytest.skip("sem tela para o Tkinter")
    raiz.withdraw()
    conexao = ConexaoPlaca("127.0.0.1", porta_livre(), porta_livre(), endereco="127.0.0.1")
    try:
        app = SimuladorApp(raiz, conexao)
        conexao.placa.receber(b"ALERTA;FRENTE;PESSOA;12;0.87;7;ALTO\n", time.monotonic())
        app.desenhar()
        textos = [app.oled.itemcget(item, "text") for item in app.oled.find_all()]
        assert "PESSOA" in textos and "PARADO 12s" in textos
        app.apertar("B")
        app.desenhar()
        assert "NOITE" in app.modo.cget("text")
    finally:
        conexao.fechar()
        raiz.destroy()


def test_cadastrar_o_ip_da_placa_em_ajustes_conecta_sem_reiniciar(qtbot, contexto: Contexto) -> None:  # noqa: ANN001, F811
    """O fluxo pedido pelo Diego: a placa mostra o IP no display e a pessoa cadastra no app."""
    c = contexto.config
    porta_pc, porta_placa = porta_livre(), porta_livre()
    c.definir("placa.porta_pc", porta_pc)
    c.definir("placa.intervalo_ping_s", 0.3)
    link = LinkBitDog(c, endereco="127.0.0.1").iniciar()
    janela = JanelaPrincipal(c, contexto.servico, contexto.gerenciador, link=link)
    qtbot.addWidget(janela)
    janela.show()
    simulador = ConexaoPlaca(None, porta_placa=porta_placa, endereco="127.0.0.1")  # não sabe onde está o PC

    def rodando(condicao) -> bool:  # noqa: ANN001
        simulador.processar()
        return bool(condicao())

    try:
        assert simulador.iniciar() == []
        display = [t for t, _ in simulador.placa.linhas_oled(time.monotonic())]
        assert display[:3] == ["AGUARDANDO PC", "IP 127.0.0.1", f"PORTA {porta_placa}"]

        janela.barra_lateral.placa_clicada.emit()  # clique na placa da barra lateral
        assert janela.paginas.currentIndex() == janela._indice_pagina["config"]  # noqa: SLF001
        secao = janela.secao_placa
        secao.ip.setText("192.168.1.")  # digitou errado
        assert not secao.salvar() and "IP inválido" in secao.erro.text()
        secao.ip.setText("127.0.0.1")  # o que aparece no display
        secao.porta_placa.setValue(porta_pc)
        assert not secao.salvar() and "diferentes" in secao.erro.text()
        secao.porta_placa.setValue(porta_placa)
        qtbot.mouseClick(secao.botao_salvar, Qt.MouseButton.LeftButton)
        assert Config.carregar(c.caminho).obter("placa.ip") == "127.0.0.1"  # ficou no config.yaml

        qtbot.waitUntil(lambda: rodando(lambda: janela.barra_lateral.placa_conectada), timeout=6000)
        qtbot.waitUntil(lambda: rodando(lambda: secao.status.text().startswith("✓ Conectada")), timeout=3000)
        qtbot.waitUntil(lambda: rodando(lambda: simulador.placa.hora != "--:--"), timeout=3000)  # HORA ao conectar
        assert simulador.placa.linhas_oled(time.monotonic())[0][0] == "MONITORANDO"
        assert simulador.destino == ("127.0.0.1", porta_pc)  # aprendeu o PC pelo PING
        assert link.estado().modo == "NORMAL"  # a placa contou o modo ao conhecer o PC
    finally:
        janela.close()
        link.parar()
        simulador.fechar()


def test_validacao_do_ip_e_das_portas_da_placa() -> None:
    from core.validacao import validar_ip_placa, validar_portas_placa

    assert validar_ip_placa("") is None  # vazio: descobrir pelo OLA
    assert validar_ip_placa(" 192.168.1.60 ") is None and validar_ip_placa("127.0.0.1") is None
    assert validar_ip_placa("192.168.1") and validar_ip_placa("0.0.0.0") and validar_ip_placa("255.255.255.255")
    assert validar_portas_placa(5006, 5005) is None
    assert "diferentes" in (validar_portas_placa(5005, 5005) or "")
    assert "Porta da placa" in (validar_portas_placa(0, 5005) or "")


def test_buzzer_da_placa_e_manual_nos_ajustes(
    qtbot, simulador_e_janela: tuple[JanelaPrincipal, ConexaoPlaca, list[Ocorrencia]],  # noqa: ANN001
) -> None:
    """Ajustes -> Placa: trocar o buzzer vale na hora (o app manda SOM) e o manual está lá."""
    janela, conexao, ocorrencias = simulador_e_janela
    placa: PlacaSimulada = conexao.placa

    def rodando(condicao) -> bool:  # noqa: ANN001
        ocorrencias.extend(conexao.processar())
        return bool(condicao())

    qtbot.waitUntil(lambda: rodando(lambda: janela.barra_lateral.placa_conectada), timeout=8000)
    assert not placa.som_continuo
    janela.ir_para("config")
    secao = janela.secao_placa
    secao.buzzer.setCurrentIndex(secao.buzzer.findData("continuo"))
    qtbot.waitUntil(lambda: rodando(lambda: placa.som_continuo), timeout=3000)
    assert Config.carregar(janela.config.caminho).obter("placa.buzzer") == "continuo"
    assert "2 toques rápidos" in secao.manual.text() and "AGUARDANDO PC" in secao.manual.text()
