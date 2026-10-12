"""Recursos compartilhados pelos testes (fixtures do pytest)."""

from __future__ import annotations

import os

# Testes da interface rodam sem abrir janelas na tela (precisa vir antes de criar o QApplication).
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import logging  # noqa: E402
from collections.abc import Callable, Iterator
from pathlib import Path

import cv2
import keyring
import numpy as np
import pytest
from keyring.backend import KeyringBackend
from keyring.errors import PasswordDeleteError

from camera.gerenciador import GerenciadorCameras
from core.banco import Banco
from core.modelos import Camera, TipoFonte, TipoStream

MODELO_ICSEE = "rtsp://{ip}:{porta}/user={usuario}&password={senha}&channel={canal}&stream={stream}.sdp"


class CofreMemoria(KeyringBackend):
    """Cofre de senhas falso, em memória, para não tocar no cofre real do sistema."""

    priority = 1  # type: ignore[assignment]

    def __init__(self) -> None:
        super().__init__()
        self.senhas: dict[tuple[str, str], str] = {}

    def set_password(self, service: str, username: str, password: str) -> None:
        self.senhas[(service, username)] = password

    def get_password(self, service: str, username: str) -> str | None:
        return self.senhas.get((service, username))

    def delete_password(self, service: str, username: str) -> None:
        if self.senhas.pop((service, username), None) is None:
            raise PasswordDeleteError("senha inexistente")


class GerenciadorDeTeste(GerenciadorCameras):
    """Câmeras RTSP de teste cujo "endereço" é um arquivo de vídeo (em ``camera.caminho``).

    Assim os testes exercitam o caminho de uma câmera de verdade (substream + stream
    principal separados) sem precisar de rede.
    """

    def fonte(self, camera: Camera, canal: int, stream: TipoStream) -> str | int:
        if camera.fonte is TipoFonte.RTSP and camera.caminho:
            return camera.caminho
        return super().fonte(camera, canal, stream)


@pytest.fixture
def cofre() -> Iterator[CofreMemoria]:
    anterior = keyring.get_keyring()
    memoria = CofreMemoria()
    keyring.set_keyring(memoria)
    yield memoria
    keyring.set_keyring(anterior)


@pytest.fixture
def banco(tmp_path: Path) -> Iterator[Banco]:
    with Banco(tmp_path / "teste.db") as b:
        yield b


@pytest.fixture
def nova_camera() -> Callable[..., Camera]:
    """Fábrica de câmeras de exemplo: ``nova_camera(nome="Garagem", codigo="GARAGEM")``."""

    def criar(**alteracoes: object) -> Camera:
        dados: dict[str, object] = {
            "nome": "Frente da casa",
            "codigo": "FRENTE",
            "fonte": TipoFonte.RTSP,
            "ip": "192.168.0.50",
            "porta_rtsp": 554,
            "usuario": "admin",
            "modelo_url": MODELO_ICSEE,
            "porta_onvif": 8899,
        }
        dados.update(alteracoes)
        return Camera(**dados)  # type: ignore[arg-type]

    return criar


@pytest.fixture
def video_teste(tmp_path: Path) -> Path:
    """Gera um vídeo .mp4 curto (64x48, 20 quadros) para testar a leitura sem câmera."""
    caminho = tmp_path / "teste.mp4"
    gravador = cv2.VideoWriter(str(caminho), cv2.VideoWriter_fourcc(*"mp4v"), 10.0, (64, 48))
    for i in range(20):
        quadro = np.full((48, 64, 3), i * 10, dtype=np.uint8)
        gravador.write(quadro)
    gravador.release()
    return caminho


@pytest.fixture
def logs_preservados() -> Iterator[None]:
    """Restaura os manipuladores de log depois de testes que chamam ``configurar_logs``."""
    raiz = logging.getLogger()
    manipuladores, nivel = list(raiz.handlers), raiz.level
    yield
    for manipulador in list(raiz.handlers):
        if manipulador not in manipuladores:
            raiz.removeHandler(manipulador)
            manipulador.close()
    for manipulador in manipuladores:
        if manipulador not in raiz.handlers:
            raiz.addHandler(manipulador)
    raiz.setLevel(nivel)


def porta_livre() -> int:
    """Uma porta TCP local que está fechada (ninguém escutando)."""
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])
