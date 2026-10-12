"""Vistas (lentes recortadas), leitor de vídeo em thread e gerenciador de câmeras."""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path

import numpy as np
import pytest

from camera import stream
from camera.gerenciador import GerenciadorCameras
from camera.stream import EstadoLeitor, LeitorVideo, LimitesLeitura, calcular_espera
from core.config import Config
from core.modelos import Camera, DisposicaoLentes, TipoFonte, TipoStream
from core.segredos import ErroCofre
from tests.conftest import GerenciadorDeTeste

NovaCamera = Callable[..., Camera]
LIMITES_RAPIDOS = LimitesLeitura(abertura_s=2, leitura_s=2, reconexao_min_s=0.05, reconexao_max_s=0.2)


def esperar(condicao: Callable[[], bool], limite_s: float = 5.0) -> bool:
    fim = time.monotonic() + limite_s
    while time.monotonic() < fim:
        if condicao():
            return True
        time.sleep(0.02)
    return condicao()


# ----------------------------------------------------------------- vistas
def test_regioes_das_lentes() -> None:
    assert DisposicaoLentes.UNICA.quantidade == 1
    assert DisposicaoLentes.VERTICAL.regiao(1) == (0.0, 0.0, 1.0, 0.5)
    assert DisposicaoLentes.VERTICAL.regiao(2) == (0.0, 0.5, 1.0, 1.0)
    assert DisposicaoLentes.HORIZONTAL.regiao(2) == (0.5, 0.0, 1.0, 1.0)


def test_vistas_da_camera_de_duas_lentes_empilhadas(nova_camera: NovaCamera) -> None:
    """Caso real do Diego: 1 canal, 2 lentes empilhadas -> 2 vistas."""
    camera = nova_camera(id=7, lentes=DisposicaoLentes.VERTICAL, nomes_vistas={2: "Calçada"})
    vistas = camera.vistas()
    assert [(v.numero, v.canal, v.lente) for v in vistas] == [(1, 1, 1), (2, 1, 2)]
    assert vistas[0].chave == (7, 1)
    assert camera.nome_vista(vistas[0]) == "Frente da casa · Lente 1"
    assert camera.nome_vista(vistas[1]) == "Frente da casa · Calçada"


def test_vistas_com_canais_e_lentes(nova_camera: NovaCamera) -> None:
    camera = nova_camera(id=1, canais=2, lentes=DisposicaoLentes.HORIZONTAL)
    assert [(v.numero, v.canal, v.lente) for v in camera.vistas()] == [(1, 1, 1), (2, 1, 2), (3, 2, 1), (4, 2, 2)]
    assert camera.nome_vista(camera.vistas()[3]) == "Frente da casa · Canal 2 · Lente 2"


def test_camera_simples_tem_uma_vista_com_o_nome_da_camera(nova_camera: NovaCamera) -> None:
    camera = nova_camera(id=1)
    (vista,) = camera.vistas()
    assert camera.nome_vista(vista) == "Frente da casa"


def test_recortar_640x720_em_duas_lentes_640x360(nova_camera: NovaCamera) -> None:
    imagem = np.zeros((720, 640, 3), dtype=np.uint8)
    imagem[360:] = 200  # lente de baixo clara
    cima, baixo = nova_camera(id=1, lentes=DisposicaoLentes.VERTICAL).vistas()
    assert cima.recortar(imagem).shape == (360, 640, 3) and cima.recortar(imagem).max() == 0
    assert baixo.recortar(imagem).shape == (360, 640, 3) and baixo.recortar(imagem).min() == 200


def test_recortar_imagem_inteira_nao_copia(nova_camera: NovaCamera) -> None:
    imagem = np.zeros((10, 10, 3), dtype=np.uint8)
    (vista,) = nova_camera(id=1).vistas()
    assert vista.recortar(imagem) is imagem


# ----------------------------------------------------------------- leitor
def test_backoff_exponencial() -> None:
    assert [calcular_espera(n, 1, 30) for n in range(1, 8)] == [1, 2, 4, 8, 16, 30, 30]


def test_leitor_de_arquivo_em_loop_no_ritmo_do_video(video_teste: Path) -> None:
    """O vídeo de teste tem 20 quadros a 10 FPS (2 s): deve tocar em tempo real e repetir."""
    leitor = LeitorVideo("teste", str(video_teste), LIMITES_RAPIDOS, arquivo_em_loop=True).iniciar()
    try:
        assert esperar(lambda: leitor.estado is EstadoLeitor.ONLINE)
        assert leitor.mensagem == "64x48"
        time.sleep(1.0)
        quadro = leitor.ultimo_quadro()
        assert quadro is not None and 5 <= quadro.numero <= 16  # ~10 por segundo, não centenas
        assert esperar(lambda: (q := leitor.ultimo_quadro()) is not None and q.numero > 22, 4)  # deu a volta
        assert leitor.estado is EstadoLeitor.ONLINE
        assert 7 <= leitor.fps <= 13
    finally:
        leitor.parar(esperar_s=3)
    assert leitor.estado is EstadoLeitor.PARADO and not leitor.ativo


def test_fps_com_quadros_em_rajadas() -> None:
    """Regressão (câmera real no Wi-Fi): quadros em rajadas não podem inflar o FPS."""
    leitor = LeitorVideo("rajadas", "x.mp4", LIMITES_RAPIDOS)  # não iniciado: só a conta
    imagem = np.zeros((2, 2, 3), dtype=np.uint8)
    instante = 0.0
    for numero in range(1, 241):
        instante += 0.01 if numero % 2 else 0.15  # média 0.08 s = 12,5 FPS
        leitor._registrar(stream.Quadro(imagem, numero, instante))  # noqa: SLF001
    assert 11.5 < leitor.fps < 13.5

    # Rajada ao conectar (FFmpeg entrega o que estava guardado): some da conta em 2 s.
    for numero in range(241, 271):
        instante += 0.001
        leitor._registrar(stream.Quadro(imagem, numero, instante))  # noqa: SLF001
    for numero in range(271, 271 + 40):
        instante += 1 / 12
        leitor._registrar(stream.Quadro(imagem, numero, instante))  # noqa: SLF001
    assert 11.5 < leitor.fps < 12.5


def test_leitor_guarda_so_o_ultimo_quadro(video_teste: Path) -> None:
    leitor = LeitorVideo("teste", str(video_teste), LIMITES_RAPIDOS, arquivo_em_loop=True).iniciar()
    try:
        assert esperar(lambda: leitor.ultimo_quadro() is not None)
        primeiro = leitor.ultimo_quadro()
        assert esperar(lambda: leitor.ultimo_quadro() is not primeiro)
        assert leitor.ultimo_quadro().numero > primeiro.numero  # type: ignore[union-attr]
    finally:
        leitor.parar(esperar_s=3)


class CapturaQueNaoAbre:
    def isOpened(self) -> bool:  # noqa: N802 (nome da API do OpenCV)
        return False

    def release(self) -> None:
        pass


def test_fonte_invalida_fica_offline_e_reconectar_agora_tenta_de_novo(monkeypatch: pytest.MonkeyPatch) -> None:
    tentativas: list[float] = []

    def abrir(*_args: object) -> CapturaQueNaoAbre:
        tentativas.append(time.monotonic())
        return CapturaQueNaoAbre()

    monkeypatch.setattr(stream, "abrir_captura", abrir)
    limites = LimitesLeitura(abertura_s=1, leitura_s=1, reconexao_min_s=30, reconexao_max_s=60)
    leitor = LeitorVideo("cam", "rtsp://10.0.0.1/x", limites).iniciar()
    try:
        assert esperar(lambda: leitor.estado is EstadoLeitor.OFFLINE)
        assert "não foi possível abrir" in leitor.mensagem
        assert 25 < leitor.segundos_para_tentar() <= 30  # vai esperar 30 s...
        leitor.reconectar_agora()  # ...mas o botão "Reconectar" pula a espera
        assert esperar(lambda: len(tentativas) >= 2, 2)
    finally:
        leitor.parar(esperar_s=2)


# ----------------------------------------------------------------- gerenciador
@pytest.fixture
def config_tmp(tmp_path: Path) -> Config:
    config = Config.carregar()
    config.definir("geral.pasta_dados", str(tmp_path / "dados"))
    config.definir("captura.reconexao_min_s", 0.05)
    config.definir("captura.reconexao_max_s", 0.2)
    config.definir("ia.fonte", "substream")  # cada teste liga o HD da IA quando quiser
    return config


def test_gerenciador_liga_troca_e_desliga_leitores(
    config_tmp: Config, nova_camera: NovaCamera, video_teste: Path, tmp_path: Path
) -> None:
    outro_video = tmp_path / "outro.mp4"
    outro_video.write_bytes(video_teste.read_bytes())
    gerenciador = GerenciadorCameras(config_tmp, obter_senha=lambda _id: "")
    a = nova_camera(id=1, fonte=TipoFonte.ARQUIVO, caminho=str(video_teste), lentes=DisposicaoLentes.VERTICAL)
    b = nova_camera(id=2, nome="B", codigo="B", fonte=TipoFonte.ARQUIVO, caminho=str(video_teste), canais=2)
    try:
        gerenciador.sincronizar([a, b])
        leitor_a = gerenciador.leitor(1, 1)
        assert leitor_a is not None and gerenciador.leitor(2, 2) is not None
        assert gerenciador.leitor_da_vista(a.vistas()[1]) is leitor_a  # as 2 lentes usam a MESMA conexão
        assert esperar(lambda: leitor_a.estado is EstadoLeitor.ONLINE)

        gerenciador.sincronizar([a, b])  # nada mudou: mesmo leitor, sem reconectar
        assert gerenciador.leitor(1, 1) is leitor_a

        a.caminho = str(outro_video)  # fonte mudou: leitor recriado
        gerenciador.sincronizar([a])  # b removida: leitores dela param
        assert gerenciador.leitor(1, 1) is not leitor_a
        assert gerenciador.leitor(2, 1) is None and gerenciador.leitor(2, 2) is None
        assert esperar(lambda: not leitor_a.ativo)
    finally:
        gerenciador.parar_tudo()
    assert gerenciador.leitor(1, 1) is None


def test_stream_principal_abre_cai_para_o_substream_e_fecha(
    config_tmp: Config, nova_camera: NovaCamera, video_teste: Path
) -> None:
    gerenciador = GerenciadorDeTeste(config_tmp, obter_senha=lambda _id: "")
    camera = nova_camera(id=1, caminho=str(video_teste))  # RTSP de teste: o "endereço" é o vídeo
    (vista,) = camera.vistas()
    try:
        gerenciador.sincronizar([camera])
        sub = gerenciador.leitor(1, 1)
        assert gerenciador.melhor_leitor(vista) is sub  # sem tela aberta: substream
        gerenciador.abrir_principal(camera)
        principal = gerenciador.leitor_principal(1, 1)
        assert principal is not None and principal is not sub
        gerenciador.abrir_principal(camera)  # de novo: não duplica
        assert gerenciador.leitor_principal(1, 1) is principal
        assert esperar(lambda: gerenciador.melhor_leitor(vista) is principal)  # chegou imagem: usa o principal

        gerenciador.fechar_principal(1)
        assert gerenciador.leitor_principal(1, 1) is None and gerenciador.melhor_leitor(vista) is sub
        assert esperar(lambda: not principal.ativo)

        gerenciador.abrir_principal(camera)
        reaberto = gerenciador.leitor_principal(1, 1)
        gerenciador.sincronizar([])  # câmera removida: o principal também para
        assert gerenciador.leitor_principal(1, 1) is None
        assert esperar(lambda: reaberto is not None and not reaberto.ativo)
    finally:
        gerenciador.parar_tudo()


def test_hd_da_ia_e_da_tela_so_fecha_quando_ninguem_precisa(
    config_tmp: Config, nova_camera: NovaCamera, video_teste: Path
) -> None:
    """Com ia.fonte = principal o HD tem dois donos: a IA e a Tela da câmera."""
    config_tmp.definir("ia.fonte", "principal")
    gerenciador = GerenciadorDeTeste(config_tmp, obter_senha=lambda _id: "")
    camera = nova_camera(id=1, caminho=str(video_teste))
    (vista,) = camera.vistas()
    try:
        gerenciador.sincronizar([camera])  # a IA pede o HD
        principal = gerenciador.leitor_principal(1, 1)
        assert principal is not None
        assert esperar(lambda: gerenciador.leitor_para_ia(vista) is principal)  # IA lê o HD quando ele chega

        gerenciador.abrir_principal(camera)  # a tela abre...
        gerenciador.fechar_principal(1)  # ...e fecha: o HD continua (a IA ainda precisa)
        assert gerenciador.leitor_principal(1, 1) is principal and principal.ativo

        camera.deteccao = False
        gerenciador.sincronizar([camera])  # detecção desligada: ninguém mais precisa
        assert gerenciador.leitor_principal(1, 1) is None
        assert esperar(lambda: not principal.ativo)
        assert gerenciador.leitor_para_ia(vista) is gerenciador.leitor(1, 1)
    finally:
        gerenciador.parar_tudo()


def test_ia_no_substream_nao_abre_o_hd(config_tmp: Config, nova_camera: NovaCamera, video_teste: Path) -> None:
    gerenciador = GerenciadorDeTeste(config_tmp, obter_senha=lambda _id: "")  # config_tmp: ia.fonte = substream
    try:
        gerenciador.sincronizar([nova_camera(id=1, caminho=str(video_teste))])
        assert gerenciador.leitor_principal(1, 1) is None
    finally:
        gerenciador.parar_tudo()


def test_webcam_e_arquivo_nao_abrem_segunda_conexao(config_tmp: Config, nova_camera: NovaCamera) -> None:
    """Sem stream principal de verdade: a Tela da câmera usa o substream (mantém as caixas no lugar)."""
    gerenciador = GerenciadorCameras(config_tmp, obter_senha=lambda _id: "")
    gerenciador.abrir_principal(nova_camera(id=1, fonte=TipoFonte.WEBCAM, caminho="0"))
    gerenciador.abrir_principal(nova_camera(id=2, fonte=TipoFonte.ARQUIVO, caminho="x.mp4"))
    assert gerenciador.leitor_principal(1, 1) is None and gerenciador.leitor_principal(2, 1) is None


def test_fonte_rtsp_usa_a_senha_do_cofre(config_tmp: Config, nova_camera: NovaCamera) -> None:
    gerenciador = GerenciadorCameras(config_tmp, obter_senha=lambda _id: "s3nha")
    url = gerenciador.fonte(nova_camera(id=1), 1, TipoStream.SUBSTREAM)
    assert url == "rtsp://192.168.0.50:554/user=admin&password=s3nha&channel=1&stream=1.sdp"


def test_fonte_rtsp_com_cofre_indisponivel_nao_derruba(config_tmp: Config, nova_camera: NovaCamera) -> None:
    def falha(_id: int) -> str:
        raise ErroCofre("sem cofre")

    url = GerenciadorCameras(config_tmp, obter_senha=falha).fonte(nova_camera(id=1), 1, TipoStream.SUBSTREAM)
    assert "password=&" in url


def test_fonte_webcam_e_arquivo_relativo(config_tmp: Config, nova_camera: NovaCamera) -> None:
    gerenciador = GerenciadorCameras(config_tmp, obter_senha=lambda _id: "")
    assert gerenciador.fonte(nova_camera(fonte=TipoFonte.WEBCAM, caminho="1"), 1, TipoStream.SUBSTREAM) == 1
    caminho = gerenciador.fonte(nova_camera(fonte=TipoFonte.ARQUIVO, caminho="videos/x.mp4"), 1, TipoStream.SUBSTREAM)
    assert Path(caminho).is_absolute() and caminho.endswith("x.mp4")


# ===================================================================== suavização (12/10/2026)
def test_relogio_espaca_os_quadros_que_chegam_em_rajadas() -> None:
    """Como na câmera do Diego: a câmera grava 1 quadro a cada 83 ms, mas o Wi-Fi entrega em rajadas
    (buraco de ~250 ms e depois 3 de uma vez). Na tela, eles precisam sair espaçados de novo."""
    from camera.stream import RelogioExibicao

    relogio = RelogioExibicao()
    pts = [i * 0.083 for i in range(12)]
    # chegada: os quadros 0, 3, 6, 9 chegam "na hora"; os outros chegam grudados no seguinte
    chegada = [100.0 + (i // 3) * 0.25 + (0.249 if i % 3 == 2 else 0.248 if i % 3 == 1 else 0.0) for i in range(12)]
    exibir = [relogio.quando_exibir(p, c, 0.25) for p, c in zip(pts, chegada, strict=True)]
    intervalos = [b - a for a, b in zip(exibir, exibir[1:])]
    assert all(abs(i - 0.083) < 0.002 for i in intervalos), intervalos  # ritmo da câmera, sem rajadas
    assert all(e >= c - 1e-9 for e, c in zip(exibir, chegada, strict=True))  # nada aparece antes de chegar
    sem_suavizar = RelogioExibicao()
    assert sem_suavizar.quando_exibir(0.5, 200.0, 0.0) == 200.0  # desligado: na hora em que chega


def test_relogio_nao_volta_no_tempo_e_aguenta_saltos() -> None:
    from camera.stream import RelogioExibicao

    relogio = RelogioExibicao()
    a = relogio.quando_exibir(1.000, 10.0, 0.2)
    b = relogio.quando_exibir(0.900, 10.1, 0.2)  # quadro fora de ordem (hora menor)
    assert b >= a
    c = relogio.quando_exibir(500.0, 10.2, 0.2)  # reconectou: a hora da câmera pulou
    assert 10.2 <= c <= 10.2 + 0.2 + 1e-9


def test_leitor_entrega_para_a_tela_o_quadro_da_vez(tmp_path: Path) -> None:
    from camera.stream import LeitorVideo, LimitesLeitura, Quadro

    leitor = LeitorVideo("t", str(tmp_path / "nada.mp4"), LimitesLeitura(1, 1, 1, 1), suavizar_s=0.3)
    imagem = np.zeros((10, 10, 3), np.uint8)
    for i in range(3):  # 3 quadros chegam no ritmo (1 a cada 100 ms): definem o atraso "normal" da rede
        leitor._registrar(Quadro(imagem, i + 1, 49.0 + i * 0.1, pts=i * 0.1))  # noqa: SLF001
    for i in range(3, 6):  # depois, uma RAJADA: 3 quadros chegam juntos, atrasados
        leitor._registrar(Quadro(imagem, i + 1, 49.55, pts=i * 0.1))  # noqa: SLF001
    assert leitor.ultimo_quadro().numero == 6  # type: ignore[union-attr] # a IA continua com o mais novo
    assert leitor.quadro_para_exibir(49.25) is None  # a tela ainda espera a suavização (0,3 s)
    assert leitor.quadro_para_exibir(49.55).numero == 3  # type: ignore[union-attr]
    assert leitor.quadro_para_exibir(49.61).numero == 4  # type: ignore[union-attr] # a rajada sai espaçada:
    assert leitor.quadro_para_exibir(49.71).numero == 5  # type: ignore[union-attr] # um a cada 100 ms
    assert leitor.quadro_para_exibir(49.81).numero == 6  # type: ignore[union-attr]
