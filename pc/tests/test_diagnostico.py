"""Diagnóstico de câmera: leitura de vídeo, conclusões e a ferramenta de linha de comando."""

from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np
import pytest

from camera.captura import salvar_jpg
from camera.diagnostico import (
    Diagnostico,
    ParametrosDiagnostico,
    ResultadoOnvif,
    ResultadoStream,
    explicar_erro_onvif,
    concluir,
    diagnosticar,
    testar_onvif,
    testar_stream,
)
from camera.rede import ResultadoPorta
from camera.sonda_rtsp import ResultadoSonda
from core.config import Config
from tests.conftest import MODELO_ICSEE, porta_livre


def parametros(**alteracoes: object) -> ParametrosDiagnostico:
    dados: dict[str, object] = {
        "ip": "127.0.0.1",
        "usuario": "admin",
        "senha": "",
        "canal": 1,
        "porta_rtsp": 554,
        "porta_onvif": 8899,
        "modelo_url": MODELO_ICSEE,
        "portas": [],
        "timeout_s": 2.0,
        "medir_fps_s": 0.5,
        "video_abertura_s": 2.0,
        "video_leitura_s": 2.0,
        "testar_onvif": False,
    }
    dados.update(alteracoes)
    return ParametrosDiagnostico(**dados)  # type: ignore[arg-type]


# ----------------------------------------------------------------- leitura de vídeo
def test_testar_stream_com_arquivo(video_teste: Path) -> None:
    r = testar_stream(str(video_teste), "substream", 2, 2, 0.5)
    assert r.ok, r.mensagem
    assert (r.largura, r.altura) == (64, 48)
    assert r.quadro is not None and r.quadro.shape == (48, 64, 3)
    assert "64x48" in r.mensagem


def test_testar_stream_arquivo_inexistente(tmp_path: Path) -> None:
    r = testar_stream(str(tmp_path / "nada.mp4"), "substream", 2, 2, 0.5)
    assert not r.ok and "não encontrado" in r.mensagem


def test_salvar_jpg_com_acento_no_caminho(tmp_path: Path) -> None:
    pasta = tmp_path / "Câmera Garagem"
    pasta.mkdir()
    salvar_jpg(pasta / "foto.jpg", np.zeros((10, 20, 3), dtype=np.uint8))
    lida = cv2.imdecode(np.fromfile(pasta / "foto.jpg", dtype=np.uint8), cv2.IMREAD_COLOR)
    assert lida.shape == (10, 20, 3)


# ----------------------------------------------------------------- orquestração
def test_diagnostico_de_arquivo(video_teste: Path) -> None:
    eventos: list[tuple[str, bool]] = []
    diag = diagnosticar(
        parametros(ip="", url_direta=str(video_teste)),
        ao_progresso=lambda etapa, resultado: eventos.append((etapa, resultado is None)),
    )
    assert diag.ok
    assert diag.portas == [] and diag.onvif is None and diag.principal is None
    assert eventos == [("stream:substream", True), ("stream:substream", False)]
    assert any("Substream OK" in frase for frase in diag.conclusoes)


def test_porta_rtsp_fechada_pula_os_streams() -> None:
    porta = porta_livre()
    diag = diagnosticar(parametros(porta_rtsp=porta, portas=[porta], timeout_s=5))
    assert not diag.ok
    assert diag.sub is not None and "não testado" in diag.sub.mensagem
    assert diag.principal is not None and "não testado" in diag.principal.mensagem
    assert diag.sonda_sub is None  # nem tentou
    assert any(f"porta RTSP {porta} está fechada" in frase for frase in diag.conclusoes)


def test_cancelar_interrompe(video_teste: Path) -> None:
    import threading

    cancelar = threading.Event()
    cancelar.set()
    diag = diagnosticar(parametros(ip="", url_direta=str(video_teste)), cancelar=cancelar)
    assert diag.cancelado and diag.sub is None


def test_modelo_invalido_vira_falha_e_nao_excecao() -> None:
    diag = diagnosticar(parametros(modelo_url="rtsp://{ip}/{xyz}"))
    assert not diag.ok and diag.sub is not None and "Modelo de URL inválido" in diag.sub.mensagem


def test_senha_nao_aparece_no_repr() -> None:
    p = parametros(senha="SuperSecreta")
    assert "SuperSecreta" not in repr(p)
    assert "SuperSecreta" not in repr(Diagnostico(p))


# ----------------------------------------------------------------- conclusões
def _porta(porta: int, aberta: bool, respondeu: bool = True) -> ResultadoPorta:
    return ResultadoPorta(porta, aberta, 1.0, "", respondeu)


def test_nada_respondeu_gera_uma_unica_conclusao() -> None:
    diag = Diagnostico(parametros(), portas=[_porta(554, False, False), _porta(8899, False, False)])
    diag.onvif = ResultadoOnvif(False, "x")
    assert len(concluir(diag)) == 1 and "não respondeu em nenhuma porta" in concluir(diag)[0]


@pytest.mark.parametrize(("senha", "trecho"), [("", "pede senha"), ("x", "recusou o login")])
def test_conclusao_de_senha(senha: str, trecho: str) -> None:
    diag = Diagnostico(parametros(senha=senha), portas=[_porta(554, True)])
    diag.sonda_sub = ResultadoSonda(False, "401", status=401)
    assert any(trecho in frase for frase in concluir(diag))


def test_conclusoes_de_camera_boa() -> None:
    diag = Diagnostico(parametros(), portas=[_porta(554, True), _porta(34567, True)])
    diag.sonda_sub = ResultadoSonda(True, "ok", 200)
    diag.sub = ResultadoStream("substream", "", True, "", 640, 360)
    diag.principal = ResultadoStream("principal", "", True, "", 1920, 1080, codec="hevc")
    diag.onvif = ResultadoOnvif(True, "", tem_ptz=True)
    texto = " ".join(concluir(diag))
    assert "Substream OK (640x360)" in texto
    assert "H.265" in texto
    assert "Xiongmai" in texto
    assert "possui motor" in texto


def test_principal_falhou_com_substream_ok() -> None:
    """Caso real (câmera XM232 de 6 MP no Wi-Fi): substream OK, principal sem quadros."""
    diag = Diagnostico(parametros(), portas=[_porta(554, True)])
    diag.sonda_sub = ResultadoSonda(True, "ok", 200, tempo_ms=40)
    diag.sub = ResultadoStream("substream", "", True, "", 640, 720)
    diag.principal = ResultadoStream("principal", "", False, "nenhum quadro completo chegou")
    texto = " ".join(concluir(diag))
    assert "stream principal (resolução máxima) não entregou imagem" in texto
    assert "timeout_abertura_s" in texto


def test_conclusao_de_rede_lenta() -> None:
    diag = Diagnostico(parametros(), portas=[ResultadoPorta(554, True, 1500.0, "", True)])
    diag.sonda_sub = ResultadoSonda(True, "ok", 200, tempo_ms=2500)
    assert any("lenta para responder (a resposta mais rápida levou 1.5 s)" in f for f in concluir(diag))


def test_so_a_primeira_resposta_lenta_nao_e_problema_de_rede() -> None:
    """Caso real: portas em 10 ms, primeiro DESCRIBE em 1989 ms, o seguinte em 98 ms."""
    diag = Diagnostico(parametros(), portas=[ResultadoPorta(554, True, 10.0, "", True)])
    diag.sonda_sub = ResultadoSonda(True, "ok", 200, tempo_ms=1989)
    diag.sonda_principal = ResultadoSonda(True, "ok", 200, tempo_ms=98)
    texto = " ".join(concluir(diag))
    assert "primeira resposta da câmera levou 2.0 s" in texto
    assert "Wi-Fi fraco" not in texto


def test_primeiro_contato_lento_nas_portas() -> None:
    lenta = ResultadoPorta(554, True, 1900.0, "", True)
    diag = Diagnostico(parametros(), portas=[lenta])
    diag.sonda_sub = ResultadoSonda(True, "ok", 200, tempo_ms=30)
    assert any("primeira resposta da câmera levou 1.9 s" in frase for frase in concluir(diag))


def test_respostas_rapidas_sem_comentario_de_latencia() -> None:
    diag = Diagnostico(parametros(), portas=[ResultadoPorta(554, True, 10.0, "", True)])
    diag.sonda_sub = ResultadoSonda(True, "ok", 200, tempo_ms=100)
    assert not any("resposta" in frase for frase in concluir(diag))


def test_perfil_onvif_com_fps_absurdo() -> None:
    """O perfil JPEG das Xiongmai informa FrameRateLimit = -3600."""
    from types import SimpleNamespace

    from camera.diagnostico import _ler_perfil

    codificador = SimpleNamespace(
        Resolution=SimpleNamespace(Width=800, Height=448),
        Encoding="JPEG",
        RateControl=SimpleNamespace(FrameRateLimit=-3600),
    )
    perfil = SimpleNamespace(Name="snapStream00", token="002", VideoEncoderConfiguration=codificador)

    class MediaFalsa:
        def GetStreamUri(self, _parametros: object) -> object:  # noqa: N802 (nome da API ONVIF)
            return SimpleNamespace(Uri="rtsp://1.2.3.4:554/user=admin_password=abc_channel=1_stream=2.sdp")

    dados = _ler_perfil(MediaFalsa(), perfil)
    assert (dados.largura, dados.altura, dados.fps, dados.ptz) == (800, 448, 0.0, False)
    assert "abc" not in dados.url


def test_falha_sem_causa_conhecida_ainda_gera_conclusao() -> None:
    diag = Diagnostico(parametros(), portas=[_porta(554, True)])
    diag.sonda_sub = ResultadoSonda(False, "a porta 554 aceitou a conexão, mas não respondeu")
    diag.sub = ResultadoStream("substream", "", False, "o OpenCV não conseguiu abrir o vídeo")
    assert any("O vídeo não abriu (a porta 554 aceitou" in frase for frase in concluir(diag))


def test_descrever_porta_personalizada() -> None:
    from camera.diagnostico import descrever_porta

    assert descrever_porta(5554, parametros(porta_rtsp=5554)) == "RTSP (vídeo)"
    assert descrever_porta(34567) == "DVRIP (protocolo Xiongmai/iCSee)"
    assert descrever_porta(1234) == ""


@pytest.mark.parametrize(
    ("erro", "trecho"),
    [
        (Exception("Sender not Authorized"), "senha incorretos"),
        (Exception("HTTPConnectionPool: Read timed out."), "tempo limite"),
        (Exception("[WinError 10061] connection refused"), "fechada"),
        (Exception("algo estranho"), "algo estranho"),
    ],
)
def test_explicar_erro_onvif(erro: Exception, trecho: str) -> None:
    assert trecho in explicar_erro_onvif(erro)


def test_onvif_com_porta_fechada() -> None:
    r = testar_onvif("127.0.0.1", porta_livre(), "admin", "", 5)
    assert not r.ok and "ONVIF indisponível" in r.mensagem


# ----------------------------------------------------------------- linha de comando
@pytest.fixture
def cli(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, logs_preservados: None):  # noqa: ANN201
    """Importa a ferramenta com a pasta de dados apontando para uma pasta temporária."""
    from tools import testar_camera

    monkeypatch.setattr(Config, "pasta_dados", property(lambda self: tmp_path / "dados"))
    return testar_camera


def test_cli_com_arquivo(cli, video_teste: Path, capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:  # noqa: ANN001
    codigo = cli.main(["--url", str(video_teste), "--salvar-fotos"])
    saida = capsys.readouterr().out
    assert codigo == 0
    assert "[ OK  ]" in saida and "64x48" in saida
    assert "pronta para ser cadastrada" in saida
    assert list((tmp_path / "dados" / "diagnostico").glob("*/substream.jpg"))
    assert (tmp_path / "dados" / "logs" / "diagnostico.log").read_text(encoding="utf-8").count("64x48") >= 1


def test_cli_porta_fechada(cli, capsys: pytest.CaptureFixture[str]) -> None:  # noqa: ANN001
    porta = porta_livre()
    codigo = cli.main(["--ip", "127.0.0.1", "--porta", str(porta), "--sem-onvif", "--senha", "Segredo123"])
    saida = capsys.readouterr().out
    assert codigo == 1
    assert "[FALHA]" in saida and "fechada" in saida
    assert "Segredo123" not in saida  # a senha nunca aparece


@pytest.mark.parametrize("argumentos", [[], ["--ip", "999.1.1.1"], ["--ip", "1.2.3.4", "--porta", "0"]])
def test_cli_argumentos_invalidos(cli, argumentos: list[str]) -> None:  # noqa: ANN001
    with pytest.raises(SystemExit) as saida:
        cli.main(argumentos)
    assert saida.value.code == 2
