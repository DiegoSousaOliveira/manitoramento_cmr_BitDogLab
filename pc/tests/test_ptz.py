"""PTZ (camera/ptz.py): serviço com limite/segurança, ONVIF (com um serviço falso) e reinício do fundo."""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest

from camera import ptz as modulo_ptz
from camera.ptz import ErroPTZ, PTZFalso, PTZOnvif, ServicoPTZ
from core.config import ARQUIVO_PADRAO, Config
from core.modelos import Camera, DisposicaoLentes, Deteccao, TipoFonte
from tests.conftest import MODELO_ICSEE
from vision.dwell import AjustesParado, RegraParado, TipoAviso
from vision.motion import AjustesMovimento, DetectorMovimento


def esperar(condicao: Callable[[], bool], limite_s: float = 3.0) -> bool:
    fim = time.monotonic() + limite_s
    while time.monotonic() < fim:
        if condicao():
            return True
        time.sleep(0.01)
    return condicao()


def camera_ptz(id_: int, nome: str = "Frente", lentes: DisposicaoLentes = DisposicaoLentes.VERTICAL) -> Camera:
    return Camera(nome, nome.upper()[:10], TipoFonte.RTSP, f"192.168.0.{id_}", 554, "admin", MODELO_ICSEE, 8899,
                  tem_ptz=True, lentes=lentes, lente_ptz=2, id=id_)


class Fabrica:
    """Faz o papel do ONVIF: devolve um PTZFalso por câmera (ou falha, se pedido)."""

    def __init__(self, falhar: bool = False) -> None:
        self.falhar = falhar
        self.controles: dict[int, PTZFalso] = {}

    def __call__(self, camera: Camera, senha: str, timeout_s: float) -> PTZFalso:
        if self.falhar:
            raise ErroPTZ("usuário ou senha incorretos para o ONVIF")
        controle = PTZFalso(camera.nome)
        self.controles[camera.id or 0] = controle
        return controle


@pytest.fixture
def config(tmp_path: Path) -> Config:
    copia = tmp_path / "config.yaml"
    copia.write_text(ARQUIVO_PADRAO.read_text(encoding="utf-8"), encoding="utf-8")
    config = Config.carregar(copia)
    for chave, valor in {
        "ptz.timeout_joystick_s": 0.4, "ptz.max_comandos_s": 10, "ptz.reenviar_movimento_s": 0.25,
        "ptz.estabilizar_s": 0.1, "ptz.tempo_preset_s": 0.2,
        "ptz.inverter_x": False, "ptz.inverter_y": False,  # o config.yaml real é do usuário
    }.items():
        config.definir(chave, valor)
    return config


@pytest.fixture
def servico(config: Config) -> Iterator[tuple[ServicoPTZ, Fabrica]]:
    fabrica = Fabrica()
    ptz = ServicoPTZ(config, lambda _id: "senha", conectar=fabrica).iniciar()
    ptz.definir_cameras([camera_ptz(1), camera_ptz(2, "Garagem")])
    assert esperar(lambda: len(fabrica.controles) == 2)
    yield ptz, fabrica
    ptz.parar()


def comandos(controle: PTZFalso, tipo: str) -> list[Any]:
    return [valor for nome, valor in controle.comandos if nome == tipo]


# ===================================================================== serviço
def test_mover_e_parar(servico: tuple[ServicoPTZ, Fabrica]) -> None:
    ptz, fabrica = servico
    controle = fabrica.controles[1]
    ptz.mover(1, 0.75, -0.304)
    assert esperar(lambda: controle.velocidade == (0.75, -0.3))
    estado = ptz.estado(1)
    assert estado is not None and estado.movendo and (estado.x, estado.y) == (0.75, -0.3) and estado.tipo == "falso"
    ptz.parar_camera(1)
    assert esperar(lambda: controle.velocidade == (0.0, 0.0))
    assert not ptz.estado(1).movendo  # type: ignore[union-attr]
    assert fabrica.controles[2].comandos == []  # a outra câmera não foi tocada


def test_no_maximo_10_comandos_por_segundo(servico: tuple[ServicoPTZ, Fabrica]) -> None:
    ptz, fabrica = servico
    inicio = time.monotonic()
    for i in range(60):  # joystick "nervoso": 60 posições em ~0,3 s
        ptz.mover(1, (i % 10) / 10 + 0.05, 0.0)
        time.sleep(0.005)
    time.sleep(0.3)
    duracao = time.monotonic() - inicio
    assert len(fabrica.controles[1].comandos) <= duracao * 10 + 2
    assert fabrica.controles[1].velocidade == (0.95, 0.0)  # o último pedido sempre chega


def test_sem_comando_por_3s_a_camera_para_sozinha(servico: tuple[ServicoPTZ, Fabrica]) -> None:
    """Segurança: um "soltei o joystick" perdido não deixa a câmera girando para sempre."""
    ptz, fabrica = servico
    ptz.mover(1, 0.5, 0.0)
    assert esperar(lambda: fabrica.controles[1].velocidade == (0.5, 0.0))
    assert esperar(lambda: fabrica.controles[1].velocidade == (0.0, 0.0), 1.5)  # timeout de 0,4 s no teste
    assert not ptz.estado(1).movendo  # type: ignore[union-attr]


def test_segurando_o_movimento_e_reenviado(servico: tuple[ServicoPTZ, Fabrica]) -> None:
    ptz, fabrica = servico
    fim = time.monotonic() + 0.9
    while time.monotonic() < fim:  # quem segura repete o comando (E5)
        ptz.mover(1, 0.5, 0.0)
        time.sleep(0.1)
    assert len(comandos(fabrica.controles[1], "mover")) >= 3  # reenviado a cada 0,25 s
    assert fabrica.controles[1].velocidade == (0.5, 0.0)  # e não parou (segurança não disparou)


def test_inverter_eixos_so_no_motor(config: Config) -> None:
    config.definir("ptz.inverter_x", True)
    fabrica = Fabrica()
    ptz = ServicoPTZ(config, lambda _id: "", conectar=fabrica).iniciar()
    try:
        ptz.definir_cameras([camera_ptz(1)])
        assert esperar(lambda: 1 in fabrica.controles)
        ptz.mover(1, 0.6, 0.2)
        assert esperar(lambda: fabrica.controles[1].velocidade == (-0.6, 0.2))
        assert ptz.estado(1).x == 0.6  # type: ignore[union-attr]  # o indicador mostra o que foi pedido
    finally:
        ptz.parar()


def test_onvif_falhou_vira_simulado_com_aviso(config: Config) -> None:
    ptz = ServicoPTZ(config, lambda _id: "", conectar=Fabrica(falhar=True)).iniciar()
    try:
        ptz.definir_cameras([camera_ptz(1)])
        assert esperar(lambda: (e := ptz.estado(1)) is not None and e.tipo == "falso")
        assert "ONVIF falhou" in ptz.estado(1).aviso and "senha" in ptz.estado(1).aviso  # type: ignore[union-attr]
        ptz.mover(1, 0.5, 0.0)  # continua respondendo (simulado)
        assert esperar(lambda: ptz.estado(1).movendo)  # type: ignore[union-attr]
    finally:
        ptz.parar()


def test_camera_de_arquivo_usa_ptz_simulado(config: Config) -> None:
    ptz = ServicoPTZ(config, lambda _id: "").iniciar()  # fábrica padrão
    try:
        camera = Camera("Teste", "TESTE", TipoFonte.ARQUIVO, "", 554, "", MODELO_ICSEE, 8899, caminho="x.mp4",
                        tem_ptz=True, id=5)
        ptz.definir_cameras([camera])
        assert esperar(lambda: (e := ptz.estado(5)) is not None and e.tipo == "falso" and e.aviso == "")
    finally:
        ptz.parar()


def test_parar_outras_e_parar_ao_fechar(servico: tuple[ServicoPTZ, Fabrica]) -> None:
    ptz, fabrica = servico
    ptz.mover(1, 0.5, 0.0)
    ptz.mover(2, -0.5, 0.0)
    assert esperar(lambda: fabrica.controles[2].velocidade == (-0.5, 0.0))
    ptz.parar_outras(2)  # botão J: o joystick agora é da câmera 2
    assert esperar(lambda: fabrica.controles[1].velocidade == (0.0, 0.0))
    assert fabrica.controles[2].velocidade == (-0.5, 0.0)
    ptz.parar()  # fechando o app
    assert fabrica.controles[2].velocidade == (0.0, 0.0)


def test_vista_movida_avisa_depois_que_para(servico: tuple[ServicoPTZ, Fabrica]) -> None:
    """Só a lente com motor (lente_ptz = 2) e só depois de parar e o vídeo estabilizar."""
    ptz, _fabrica = servico
    movidas: list[tuple[int, int]] = []
    ptz.assinar_vista_movida(movidas.append)
    ptz.mover(1, 0.5, 0.0)
    time.sleep(0.2)
    assert movidas == []  # andando: ainda não
    ptz.parar_camera(1)
    ptz.mover(1, 0.5, 0.0)  # voltou a andar logo depois: o aviso é adiado
    time.sleep(0.15)
    assert movidas == []
    ptz.parar_camera(1)
    assert esperar(lambda: movidas == [(1, 2)])  # (câmera 1, vista 2 = lente de baixo)
    ptz.ir_preset(1, 3)  # posição salva: avisa quando o motor chega
    assert esperar(lambda: movidas == [(1, 2), (1, 2)])


def test_presets(servico: tuple[ServicoPTZ, Fabrica]) -> None:
    ptz, fabrica = servico
    ptz.salvar_preset(1, 1)
    ptz.ir_preset(1, 1)
    assert esperar(lambda: comandos(fabrica.controles[1], "preset") == [1])
    assert comandos(fabrica.controles[1], "salvar_preset") == [1]


# ===================================================================== ONVIF (serviço falso)
class ServicoOnvifFalso:
    """Imita o serviço PTZ do ONVIF; ``recusar`` = operações que a câmera recusa."""

    def __init__(self, erro: Exception | None = None, recusar: tuple[str, ...] = ()) -> None:
        self.chamadas: list[tuple[str, dict[str, Any]]] = []
        self.erro = erro
        self.recusar = recusar
        self.presets: list[SimpleNamespace] = []

    def __getattr__(self, nome: str) -> Callable[[dict[str, Any]], Any]:
        def chamar(pedido: dict[str, Any]) -> Any:
            if self.erro is not None:
                raise self.erro
            if (nome, "PresetToken" in pedido) in self.recusar:
                raise Exception("Invalid PresetToken")  # noqa: TRY002
            self.chamadas.append((nome, pedido))
            if nome == "SetPreset":
                token = pedido.get("PresetToken") or f"camera-{len(self.presets) + 10}"
                self.presets.append(SimpleNamespace(token=token, Name=pedido["PresetName"]))
            return list(self.presets) if nome == "GetPresets" else None

        return chamar


def test_ptz_onvif_monta_os_pedidos() -> None:
    servico = ServicoOnvifFalso()
    onvif = PTZOnvif(servico, "000", "ONVIF teste")
    onvif.mover(0.5, -0.25)
    onvif.parar()
    onvif.salvar_preset(3)
    onvif.ir_preset(3)
    assert servico.chamadas == [
        ("ContinuousMove", {"ProfileToken": "000", "Velocity": {"PanTilt": {"x": 0.5, "y": -0.25}}}),
        ("Stop", {"ProfileToken": "000", "PanTilt": True, "Zoom": True}),
        ("SetPreset", {"ProfileToken": "000", "PresetToken": "3", "PresetName": "Sentinela 3"}),
        ("GetPresets", {"ProfileToken": "000"}),
        ("GotoPreset", {"ProfileToken": "000", "PresetToken": "3"}),
    ]
    with pytest.raises(ErroPTZ, match="ainda não foi salva"):
        onvif.ir_preset(4)


def test_ptz_onvif_preset_quando_a_camera_escolhe_o_numero() -> None:
    """Câmera que recusa número no SetPreset: salva sem número e acha depois pelo nome."""
    servico = ServicoOnvifFalso(recusar=(("SetPreset", True),))
    onvif = PTZOnvif(servico, "000", "ONVIF teste")
    onvif.salvar_preset(1)
    onvif.ir_preset(1)
    assert servico.chamadas[-1] == ("GotoPreset", {"ProfileToken": "000", "PresetToken": "camera-10"})
    with pytest.raises(ErroPTZ, match="usuário ou senha"):
        PTZOnvif(ServicoOnvifFalso(Exception("Sender not Authorized")), "000", "x").mover(1, 0)


def test_ptz_onvif_acha_o_perfil_com_ptz(monkeypatch: pytest.MonkeyPatch) -> None:
    perfis = [SimpleNamespace(token="002", PTZConfiguration=None), SimpleNamespace(token="000", PTZConfiguration=object())]
    servico = ServicoOnvifFalso()
    camera = SimpleNamespace(
        create_media_service=lambda: SimpleNamespace(GetProfiles=lambda: perfis),
        create_ptz_service=lambda: servico,
    )
    monkeypatch.setattr(modulo_ptz, "conectar_onvif", lambda *_a: camera)
    onvif = PTZOnvif.conectar("192.168.0.100", 8899, "admin", "", 5)
    onvif.mover(0.1, 0.0)
    assert servico.chamadas[0][1]["ProfileToken"] == "000" and onvif.descricao == "ONVIF 192.168.0.100:8899"
    perfis[1].PTZConfiguration = None
    with pytest.raises(ErroPTZ, match="perfil ONVIF com PTZ"):
        PTZOnvif.conectar("192.168.0.100", 8899, "admin", "", 5)


# ===================================================================== a cena mudou (câmera girou)
def test_regra_de_parado_recomeca_o_aquecimento_quando_a_camera_gira() -> None:
    ajustes = AjustesParado.do_config(Config.carregar())  # aquecimento 20 s, CARRO 30 s
    regra = RegraParado((1, 2), ajustes)

    def carro(x: float) -> list[Deteccao]:
        return [Deteccao("CARRO", 0.9, (x - 0.05, 0.4, x + 0.05, 0.6), 1)]

    t = 0.0
    while t < 25:  # carro estacionado desde o início: ignorado
        assert regra.atualizar(carro(0.3), t, 0.56) == []
        t += 0.2
    regra.reiniciar(t)  # a câmera girou: o carro agora aparece em outro lugar da imagem
    avisos = []
    while t < 70:
        avisos += regra.atualizar(carro(0.7), t, 0.56)
        t += 0.2
    assert [a for a in avisos if a.tipo is TipoAviso.ALERTA] == []  # sem alerta falso


def test_detector_de_movimento_reinicia_o_fundo() -> None:
    detector = DetectorMovimento(AjustesMovimento(largura_trabalho=160, historico=50, limiar=16,
                                                  area_minima=0.001, aquecimento_quadros=3))
    cena = np.full((90, 160, 3), 60, dtype=np.uint8)
    for _ in range(5):
        detector.processar(cena)
    outra = np.full((90, 160, 3), 200, dtype=np.uint8)  # a câmera girou: tudo diferente
    detector.reiniciar()
    assert not detector.processar(outra).tem_movimento  # aquecendo de novo: nada de "movimento" falso
