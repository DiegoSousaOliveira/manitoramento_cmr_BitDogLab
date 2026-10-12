"""Testes da interface (pytest-qt), sem abrir janelas na tela.

Sempre com config, banco e cofre de senhas TEMPORÁRIOS — nunca os do usuário.
"""

from __future__ import annotations

import shutil
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import cv2
import numpy as np
import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QImage

from camera.gerenciador import GerenciadorCameras
from core.banco import Banco
from core.config import ARQUIVO_PADRAO, Config
from core.modelos import Camera, DisposicaoLentes, TipoFonte
from core.servico_cameras import ServicoCameras
from tests.conftest import MODELO_ICSEE, CofreMemoria, GerenciadorDeTeste, porta_livre
from ui import tema
from ui.bloco_camera import BlocoCamera
from ui.dialogo_camera import DialogoCamera
from ui.distribuidor import converter_para_qimage
from ui.janela_principal import JanelaPrincipal
from ui.toast import Notificador


class Contexto:
    def __init__(self, config: Config, banco: Banco) -> None:
        self.config = config
        self.banco = banco
        self.servico = ServicoCameras(banco, config)
        self.gerenciador = GerenciadorDeTeste(config, self.servico.obter_senha)


@pytest.fixture
def contexto(tmp_path: Path, cofre: CofreMemoria, qapp) -> Iterator[Contexto]:  # noqa: ANN001
    copia = tmp_path / "config.yaml"
    shutil.copy(ARQUIVO_PADRAO, copia)  # cópia: o app grava ajustes (layout, tema) no arquivo
    config = Config.carregar(copia)
    config.definir("geral.pasta_dados", str(tmp_path / "dados"))
    config.definir("captura.reconexao_min_s", 0.2)
    config.definir("captura.reconexao_max_s", 0.5)
    config.definir("interface.layout_mosaico", "auto")
    config.definir("ia.fonte", "substream")  # Fase 2: o HD só com a Tela da câmera aberta
    config.definir("placa.ip", "")  # o config.yaml real pode ter o IP da placa do Diego
    config.definir("ptz.inverter_x", False)  # ...e o PTZ invertido
    config.definir("ptz.inverter_y", False)
    tema.aplicar_tema(qapp, "escuro")
    banco = Banco(tmp_path / "dados" / "sentinela.db")
    ctx = Contexto(config, banco)
    yield ctx
    ctx.gerenciador.parar_tudo()
    banco.fechar()


@pytest.fixture
def video_duas_lentes(tmp_path: Path) -> Path:
    """Vídeo 640x720 com duas "lentes" empilhadas (como a iCSee XM232)."""
    caminho = tmp_path / "duas_lentes.mp4"
    gravador = cv2.VideoWriter(str(caminho), cv2.VideoWriter_fourcc(*"mp4v"), 15.0, (640, 720))
    for i in range(30):
        quadro = np.zeros((720, 640, 3), dtype=np.uint8)
        quadro[:360] = (200, 60, 30)
        quadro[360:] = (30, 60, 200)
        cv2.circle(quadro, (20 + i * 10, 180), 20, (255, 255, 255), -1)
        gravador.write(quadro)
    gravador.release()
    return caminho


def camera_de_arquivo(caminho: Path, **extra: object) -> Camera:
    dados: dict[str, object] = {
        "nome": "Frente da casa", "codigo": "FRENTE", "fonte": TipoFonte.ARQUIVO, "ip": "", "porta_rtsp": 554,
        "usuario": "admin", "modelo_url": MODELO_ICSEE, "porta_onvif": 8899, "caminho": str(caminho),
    }
    dados.update(extra)
    return Camera(**dados)  # type: ignore[arg-type]


# ===================================================================== peças
def test_converter_para_qimage_encaixa_sem_distorcer() -> None:
    imagem = np.zeros((360, 640, 3), dtype=np.uint8)
    imagem[:, :, 2] = 255  # vermelho em BGR
    qimagem = converter_para_qimage(imagem, 320, 320)
    assert (qimagem.width(), qimagem.height()) == (320, 180)
    assert qimagem.pixelColor(10, 10).red() == 255  # convertido para RGB


def test_distribuidor_pula_quadro_sem_memoria_e_segue(qapp, monkeypatch: pytest.MonkeyPatch, caplog) -> None:  # noqa: ANN001
    """12/10/2026: durante um treino o PC ficou sem memória e o OpenCV falhou ao converter um quadro —
    o erro derrubava o "tique" inteiro (todos os blocos) e virava CRITICAL. Agora só aquele quadro é pulado."""
    from types import SimpleNamespace

    from camera.stream import EstadoLeitor
    from core.modelos import Vista
    from ui import distribuidor as modulo
    from ui.distribuidor import Alvo, DistribuidorQuadros

    vistas = [Vista(1, 1, 1, 1, (0.0, 0.0, 1.0, 1.0)), Vista(2, 1, 1, 1, (0.0, 0.0, 1.0, 1.0))]
    numero = [0]

    def leitor(_vista: Vista) -> SimpleNamespace:
        quadro = lambda *_a: SimpleNamespace(numero=numero[0], imagem=np.zeros((36, 64, 3), np.uint8))  # noqa: E731
        return SimpleNamespace(estado=EstadoLeitor.ONLINE, mensagem="", segundos_para_tentar=lambda: 0,
                               ultimo_quadro=quadro, quadro_para_exibir=quadro)

    original = modulo.converter_para_qimage
    falhar = [True]

    def converter(imagem: np.ndarray, largura: int, altura: int) -> QImage:
        if falhar[0] and largura == 320:  # só a câmera 1 (320 px de largura) fica sem memória
            raise cv2.error("OpenCV(5.0.0) alloc.cpp:73: error: (-4:Insufficient memory) Failed to allocate")
        return original(imagem, largura, altura)

    monkeypatch.setattr(modulo, "converter_para_qimage", converter)
    recebidos: list[tuple] = []
    # o objeto vive na thread dele (não iniciada aqui): troca o sinal por um "gravador" síncrono
    monkeypatch.setattr(DistribuidorQuadros, "quadro_pronto",
                        SimpleNamespace(emit=lambda chave, _img: recebidos.append(chave)))
    distribuidor = DistribuidorQuadros(leitor, fps=10)
    distribuidor.definir_alvos([Alvo(vistas[0], 320, 180, 1.0, True), Alvo(vistas[1], 160, 90, 1.0, True)])
    for n in range(1, 4):  # 3 tiques seguidos com a câmera 1 falhando
        numero[0] = n
        distribuidor._tique()  # noqa: SLF001 (chamado direto, sem a thread)
        time.sleep(0.11)  # respeita o limite de atualizações por segundo do bloco (10/s)
    assert recebidos == [(2, 1)] * 3  # a câmera 2 continuou recebendo quadros
    assert len([r for r in caplog.records if "pulado" in r.getMessage()]) == 1  # 1 aviso, não 1 por quadro
    falhar[0] = False
    numero[0] = 4
    time.sleep(0.11)
    distribuidor._tique()  # noqa: SLF001
    assert (1, 1) in recebidos  # a memória voltou: a câmera 1 volta sozinha


def test_bloco_mantem_tela_offline_ao_reconectar(qtbot) -> None:  # noqa: ANN001
    from core.modelos import Vista

    bloco = BlocoCamera(Vista(1, 1, 1, 1, (0.0, 0.0, 1.0, 1.0)), "Teste")
    qtbot.addWidget(bloco)
    bloco.resize(480, 270)
    bloco.show()
    bloco.definir_estado("offline", "não foi possível abrir o vídeo", 4)
    assert bloco._botao.isVisible()  # noqa: SLF001
    bloco.definir_estado("conectando", "conectando…", 0)  # nova tentativa: não pisca para "Conectando"
    assert bloco._mostra_offline() and bloco._botao.isVisible()  # noqa: SLF001
    bloco.definir_estado("online", "640x360", 0)
    assert not bloco._botao.isVisible()  # noqa: SLF001
    bloco.definir_imagem(QImage(480, 270, QImage.Format.Format_RGB888))
    bloco.grab()  # desenha sem erro


def test_toast_aparece_e_some(qtbot) -> None:  # noqa: ANN001
    from PySide6.QtWidgets import QWidget

    janela = QWidget()
    qtbot.addWidget(janela)
    janela.resize(800, 600)
    janela.show()
    notificador = Notificador(janela)
    clicou = []
    toast = notificador.mostrar("Foto salva", "Frente", "sucesso", ao_clicar=lambda: clicou.append(1), duracao_ms=200)
    assert toast.isVisible()
    assert toast.x() + toast.width() <= janela.width() and toast.y() + toast.height() <= janela.height()
    qtbot.mouseClick(toast, Qt.MouseButton.LeftButton)
    assert clicou == [1]
    qtbot.waitUntil(lambda: not notificador._toasts, timeout=3000)  # noqa: SLF001


# ===================================================================== janela principal
@pytest.fixture
def janela(qtbot, contexto: Contexto) -> Iterator[JanelaPrincipal]:  # noqa: ANN001
    janela = JanelaPrincipal(contexto.config, contexto.servico, contexto.gerenciador)
    qtbot.addWidget(janela)
    janela.resize(1280, 800)
    janela.show()
    yield janela
    janela.close()


def test_sem_cameras_mostra_boas_vindas(janela: JanelaPrincipal) -> None:
    assert janela.paginas_cameras.currentWidget() is janela.boas_vindas
    assert not janela.seletor_layout.isVisible()


def test_mosaico_ao_vivo_com_duas_lentes(
    qtbot, janela: JanelaPrincipal, contexto: Contexto, video_duas_lentes: Path, video_teste: Path  # noqa: ANN001
) -> None:
    contexto.servico.cadastrar(
        camera_de_arquivo(video_duas_lentes, lentes=DisposicaoLentes.VERTICAL, nomes_vistas={2: "Calçada"}), ""
    )
    contexto.servico.cadastrar(camera_de_arquivo(video_teste, nome="Garagem", codigo="GARAGEM"), "")
    janela.recarregar_cameras()

    mosaico = janela.mosaico
    assert janela.paginas_cameras.currentWidget() is mosaico
    assert mosaico.quantidade == 3  # 2 lentes + 1 câmera simples
    blocos = [mosaico.bloco(c) for c in mosaico.chaves_visiveis()]
    qtbot.waitUntil(lambda: all(b is not None and b.tem_imagem and b.estado == "online" for b in blocos), timeout=8000)
    assert blocos[1].titulo == "Frente da casa · Calçada"  # type: ignore[union-attr]

    # Cada lente recebe só a sua metade: cima azul (BGR 200,60,30), baixo vermelho (30,60,200)
    cima, baixo = blocos[0]._imagem, blocos[1]._imagem  # type: ignore[union-attr]  # noqa: SLF001
    assert cima.pixelColor(5, 5).blue() > 150 and baixo.pixelColor(5, 5).red() > 150
    for bloco in blocos:  # 16:9
        assert abs(bloco.width() / bloco.height() - 16 / 9) < 0.02  # type: ignore[union-attr]
    assert "3 de 3 vistas online" in janela.resumo.text()


def test_layout_paginas_e_ajuste_salvo(qtbot, janela: JanelaPrincipal, contexto: Contexto, video_teste: Path) -> None:  # noqa: ANN001
    for nome in ("A", "B", "C"):
        contexto.servico.cadastrar(camera_de_arquivo(video_teste, nome=nome, codigo=nome), "")
    janela.recarregar_cameras()
    janela.definir_layout("1")
    mosaico = janela.mosaico
    assert len(mosaico.chaves_visiveis()) == 1 and mosaico.total_paginas == 3
    assert janela._navegacao.isVisible() and janela.texto_pagina.text() == "1/3"  # noqa: SLF001
    primeira = mosaico.chaves_visiveis()[0]
    janela.proxima_pagina.click()
    assert mosaico.chaves_visiveis()[0] != primeira and janela.texto_pagina.text() == "2/3"
    assert not mosaico.bloco(primeira).isVisible()  # type: ignore[union-attr]
    assert Config.carregar(contexto.config.caminho).obter("interface.layout_mosaico") == "1"  # gravado no arquivo


def test_foto_pelo_mosaico(qtbot, janela: JanelaPrincipal, contexto: Contexto, video_teste: Path) -> None:  # noqa: ANN001
    contexto.servico.cadastrar(camera_de_arquivo(video_teste), "")
    contexto.servico.cadastrar(camera_de_arquivo(video_teste, nome="Garagem", codigo="GARAGEM"), "")
    janela.recarregar_cameras()
    chave = janela.mosaico.chaves_visiveis()[1]
    qtbot.waitUntil(lambda: janela.mosaico.bloco(chave).tem_imagem, timeout=8000)  # type: ignore[union-attr]
    janela.tirar_foto(chave)
    fotos = list((contexto.config.pasta_dados / "fotos" / "Garagem").rglob("*.jpg"))
    assert len(fotos) == 1


# ===================================================================== Tela da câmera (Fase 2)
def _abrir_pelo_clique(qtbot, janela: JanelaPrincipal, contexto: Contexto, video: Path, indice: int = 1):  # noqa: ANN001, ANN202
    """Câmera "RTSP" de teste (o endereço é o vídeo): tem substream e principal separados."""
    contexto.servico.cadastrar(
        camera_de_arquivo(
            video,
            fonte=TipoFonte.RTSP,
            ip="127.0.0.1",
            lentes=DisposicaoLentes.VERTICAL,
            nomes_vistas={1: "Rua", 2: "Calçada"},
        ),
        "",
    )
    janela.recarregar_cameras()
    chave = janela.mosaico.chaves_visiveis()[indice]
    bloco_mosaico = janela.mosaico.bloco(chave)
    qtbot.waitUntil(lambda: bloco_mosaico.tem_imagem, timeout=8000)  # type: ignore[union-attr]
    qtbot.mouseClick(bloco_mosaico, Qt.MouseButton.LeftButton)
    return chave


def test_clique_abre_em_alta_resolucao_e_voltar_encerra_o_principal(
    qtbot, janela: JanelaPrincipal, contexto: Contexto, video_duas_lentes: Path  # noqa: ANN001
) -> None:
    chave = _abrir_pelo_clique(qtbot, janela, contexto, video_duas_lentes)
    tela = janela.tela_camera
    assert janela.paginas.currentWidget() is tela and tela.aberta
    assert [v.numero for v in tela.vistas_visiveis()] == [2]  # abre na lente clicada
    principal = contexto.gerenciador.leitor_principal(chave[0], 1)
    assert principal is not None  # stream principal ligado

    bloco = tela.bloco(chave)
    assert bloco is not None
    qtbot.waitUntil(lambda: bloco.tem_imagem and (bloco.selo or "").startswith("HD"), timeout=8000)
    assert "640×360" in (bloco.selo or "")  # a lente recortada do quadro principal
    assert janela.titulo.text() == "Frente da casa"
    qtbot.waitUntil(lambda: janela.resumo.text().startswith("Alta resolução"), timeout=3000)

    tela.definir_foco(None)  # aba "Todas": as duas lentes lado a lado
    assert len(tela.vistas_visiveis()) == 2
    assert all(tela.bloco(v.chave).isVisible() for v in tela.vistas_visiveis())  # type: ignore[union-attr]

    tela.botao_voltar.click()
    assert janela.paginas.currentIndex() == janela._indice_pagina["cameras"]  # noqa: SLF001
    assert contexto.gerenciador.leitor_principal(chave[0], 1) is None
    qtbot.waitUntil(lambda: not principal.ativo, timeout=5000)  # o stream principal foi ENCERRADO
    assert contexto.gerenciador.leitor(chave[0], 1).ativo  # type: ignore[union-attr]  # o substream continua


def test_tela_cheia_foto_e_esc(qtbot, janela: JanelaPrincipal, contexto: Contexto, video_duas_lentes: Path) -> None:  # noqa: ANN001
    chave = _abrir_pelo_clique(qtbot, janela, contexto, video_duas_lentes, indice=0)
    tela = janela.tela_camera
    bloco = tela.bloco(chave)
    qtbot.waitUntil(lambda: bloco is not None and bloco.tem_imagem, timeout=8000)

    qtbot.mouseDClick(bloco, Qt.MouseButton.LeftButton)  # duplo clique no vídeo: tela cheia
    assert not janela.barra_lateral.isVisible() and not tela.barra.isVisible()
    janela._esc()  # noqa: SLF001  (Esc sai da tela cheia)
    assert janela.barra_lateral.isVisible() and tela.barra.isVisible() and tela.aberta

    fotos = tela.tirar_foto()
    assert len(fotos) == 1 and Path(fotos[0]).is_file()

    janela._esc()  # noqa: SLF001  (Esc de novo: volta ao mosaico)
    assert not tela.aberta and contexto.gerenciador.leitor_principal(chave[0], 1) is None


def test_ir_para_outra_pagina_tambem_encerra_o_principal(
    qtbot, janela: JanelaPrincipal, contexto: Contexto, video_duas_lentes: Path  # noqa: ANN001
) -> None:
    chave = _abrir_pelo_clique(qtbot, janela, contexto, video_duas_lentes)
    assert contexto.gerenciador.leitor_principal(chave[0], 1) is not None
    janela.ir_para("eventos")
    assert not janela.tela_camera.aberta and contexto.gerenciador.leitor_principal(chave[0], 1) is None


# ===================================================================== IA na interface (Fase 3)
@pytest.fixture
def janela_com_ia(qtbot, contexto: Contexto) -> Iterator[JanelaPrincipal]:  # noqa: ANN001
    """Janela com o serviço de IA usando um detector falso (sempre "vê" uma pessoa)."""
    from tests.test_visao import DetectorFalso
    from vision.detector import ServicoDeteccao

    contexto.config.definir("ia.movimento_aquecimento_s", 0.2)
    deteccao = ServicoDeteccao(contexto.config, contexto.gerenciador.leitor_da_vista, carregar=DetectorFalso).iniciar()
    janela = JanelaPrincipal(contexto.config, contexto.servico, contexto.gerenciador, deteccao)
    qtbot.addWidget(janela)
    janela.resize(1280, 800)
    janela.show()
    yield janela
    janela.close()
    deteccao.parar()


def test_deteccoes_no_mosaico_e_na_tela_da_camera(
    qtbot, janela_com_ia: JanelaPrincipal, contexto: Contexto, video_teste: Path  # noqa: ANN001
) -> None:
    janela = janela_com_ia
    camera = contexto.servico.cadastrar(camera_de_arquivo(video_teste), "")
    janela.recarregar_cameras()
    chave = janela.mosaico.chaves_visiveis()[0]
    bloco = janela.mosaico.bloco(chave)
    assert bloco is not None and bloco.mostrar_chips
    qtbot.waitUntil(lambda: bool(bloco.deteccoes) and bloco.deteccoes[0].classe == "PESSOA", timeout=8000)
    assert bloco.deteccoes[0].id_rastreio is not None
    bloco.grab()  # desenha caixas e chips sem erro

    janela._atualizar_status_ia()  # noqa: SLF001
    assert janela.barra_lateral._ia.text() == "IA CPU"  # noqa: SLF001  (o detector falso diz "CPU")

    janela.abrir_camera(chave)
    tela = janela.tela_camera
    bloco_tela = tela.bloco(chave)
    qtbot.waitUntil(lambda: bloco_tela is not None and bool(bloco_tela.deteccoes), timeout=8000)
    qtbot.waitUntil(lambda: any("PESSOA #" in t for t in tela.faixa.textos), timeout=3000)
    assert not bloco_tela.mostrar_chips  # na Tela da câmera a faixa "Detectado agora" substitui os chips
    tela.botao_deteccoes.setChecked(False)
    assert not bloco_tela.mostrar_caixas
    tela.botao_movimento.setChecked(True)  # máscara de movimento: chega sem erro
    qtbot.waitUntil(lambda: bloco_tela._mascara is not None, timeout=5000)  # noqa: SLF001
    tela.botao_movimento.setChecked(False)
    assert bloco_tela._mascara is None  # noqa: SLF001

    janela.ir_para("cameras")
    janela.alternar_deteccao(camera.id or 0)  # desligar a detecção apaga as caixas
    bloco = janela.mosaico.bloco(chave)
    qtbot.waitUntil(lambda: bloco is not None and bloco.deteccoes == (), timeout=5000)


def test_ia_indisponivel_avisa(qtbot, contexto: Contexto) -> None:  # noqa: ANN001
    from vision.detector import EstadoIA, ServicoDeteccao

    def falhar() -> object:
        raise RuntimeError("modelo não encontrado")

    deteccao = ServicoDeteccao(contexto.config, contexto.gerenciador.leitor_da_vista, carregar=falhar).iniciar()
    janela = JanelaPrincipal(contexto.config, contexto.servico, contexto.gerenciador, deteccao)
    qtbot.addWidget(janela)
    janela.show()
    qtbot.waitUntil(lambda: deteccao.estado is EstadoIA.ERRO, timeout=5000)
    janela._atualizar_status_ia()  # noqa: SLF001
    assert janela.barra_lateral._ia.text() == "IA ✕"  # noqa: SLF001
    assert janela.notificador._toasts  # noqa: SLF001  (toast avisando)
    janela.close()


def test_camera_offline_mostra_reconectar(qtbot, janela: JanelaPrincipal, contexto: Contexto) -> None:  # noqa: ANN001
    contexto.servico.cadastrar(camera_de_arquivo(Path("nao_existe.mp4")), "")
    janela.recarregar_cameras()
    bloco = janela.mosaico.bloco(janela.mosaico.chaves_visiveis()[0])
    qtbot.waitUntil(lambda: bloco is not None and bloco.estado == "offline", timeout=8000)
    assert bloco._botao.isVisible()  # type: ignore[union-attr]  # noqa: SLF001


# ===================================================================== diálogo de cadastro
@pytest.fixture
def dialogo(qtbot, contexto: Contexto) -> Callable[..., DialogoCamera]:  # noqa: ANN001
    def criar(camera: Camera | None = None) -> DialogoCamera:
        d = DialogoCamera(None, contexto.config, contexto.servico, contexto.gerenciador.limites(), camera)
        qtbot.addWidget(d)
        d.show()
        return d

    return criar


def test_codigo_gerado_pelo_nome_e_editavel(qtbot, dialogo) -> None:  # noqa: ANN001
    d = dialogo()
    qtbot.keyClicks(d.nome, "Frente da casa")
    assert d.codigo.text() == "FRENTE"
    d.codigo.clear()
    qtbot.keyClicks(d.codigo, "portao1")
    assert d.codigo.text() == "PORTAO1"  # vira maiúsculo
    qtbot.keyClicks(d.nome, " nova")
    assert d.codigo.text() == "PORTAO1"  # editado à mão: não muda mais


def test_salvar_com_erros_nao_grava(dialogo, contexto: Contexto) -> None:  # noqa: ANN001
    d = dialogo()
    d.ip.setText("192.168.0.999")
    d._salvar()  # noqa: SLF001
    assert d.camera_salva is None and contexto.servico.listar() == []
    assert d._erros["nome"].text() and d._erros["ip"].text() == "IP inválido. Use o formato 192.168.0.50."  # noqa: SLF001


def test_cadastrar_camera_rtsp_com_senha(dialogo, contexto: Contexto, cofre: CofreMemoria) -> None:  # noqa: ANN001
    d = dialogo()
    d.nome.setText("Frente da casa")
    d.codigo.setText("FRENTE")
    d.ip.setText("192.168.0.100")
    d.usuario.setText("ypak")
    d.senha.setText("segredo")
    d.lentes.setCurrentIndex(d.lentes.findData(DisposicaoLentes.VERTICAL))
    d.ptz.setChecked(True)
    d.lente_ptz.setCurrentIndex(1)
    d._nomes_vistas[0].setText("Rua")  # noqa: SLF001
    d._salvar()  # noqa: SLF001
    assert d.camera_salva is not None
    salva = contexto.servico.obter(d.camera_salva.id or 0)
    assert salva is not None
    assert (salva.ip, salva.usuario, salva.lentes, salva.tem_ptz, salva.lente_ptz) == (
        "192.168.0.100", "ypak", DisposicaoLentes.VERTICAL, True, 2,
    )
    assert salva.nomes_vistas == {1: "Rua"}
    assert contexto.servico.obter_senha(salva.id or 0) == "segredo"


def test_nome_duplicado_e_avisado(dialogo, contexto: Contexto, video_teste: Path) -> None:  # noqa: ANN001
    contexto.servico.cadastrar(camera_de_arquivo(video_teste), "")
    d = dialogo()
    d.nome.setText("FRENTE DA CASA")
    d.codigo.setText("OUTRO")
    d.ip.setText("192.168.0.10")
    d._salvar()  # noqa: SLF001
    assert d.camera_salva is None and "Já existe" in d._erros["nome"].text()  # noqa: SLF001


def test_editar_carrega_os_dados_e_a_senha(dialogo, contexto: Contexto) -> None:  # noqa: ANN001
    camera = contexto.servico.cadastrar(
        Camera("Portão", "PORTAO", TipoFonte.RTSP, "192.168.0.7", 8554, "admin", MODELO_ICSEE, 8899, canais=2), "abc"
    )
    d = dialogo(camera)
    assert (d.nome.text(), d.ip.text(), d.porta.value(), d._canais(), d.senha.text()) == (  # noqa: SLF001
        "Portão", "192.168.0.7", 8554, 2, "abc",
    )
    d.nome.setText("Portão da frente")
    d._salvar()  # noqa: SLF001
    assert contexto.servico.obter(camera.id or 0).nome == "Portão da frente"  # type: ignore[union-attr]


def test_testar_conexao_com_video_sugere_dividir_lentes(qtbot, dialogo, video_duas_lentes: Path) -> None:  # noqa: ANN001
    d = dialogo()
    d.fonte.escolhido.emit("arquivo")
    d.caminho.setCurrentText(str(video_duas_lentes))
    d.botao_testar.click()
    qtbot.waitUntil(lambda: d.previa._imagem is not None, timeout=8000)  # noqa: SLF001
    qtbot.waitUntil(lambda: any("duas lentes empilhadas" in r.texto for r in d.resultados()), timeout=3000)
    assert any(r.situacao == "ok" and "640x720" in r.texto for r in d.resultados())
    from PySide6.QtWidgets import QPushButton

    dividir = next(b for b in d.findChildren(QPushButton) if b.text() == "Dividir")
    dividir.click()
    assert d._disposicao() is DisposicaoLentes.VERTICAL  # noqa: SLF001
    d.reject()
    assert d._leitor_teste is None  # fechar o diálogo encerra a conexão de teste  # noqa: SLF001


def test_testar_conexao_com_porta_fechada(qtbot, dialogo) -> None:  # noqa: ANN001
    d = dialogo()
    d.ip.setText("127.0.0.1")
    d.porta.setValue(porta_livre())
    d.botao_testar.click()
    qtbot.waitUntil(lambda: any(r.situacao == "erro" for r in d.resultados()), timeout=10000)
    assert any("fechada" in r.texto for r in d.resultados())
