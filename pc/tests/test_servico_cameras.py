"""Serviço de câmeras: banco + cofre + pastas sempre em sincronia."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import datetime
from pathlib import Path

import keyring
import numpy as np
import pytest
from keyring.backends import fail

from core.banco import Banco, ErroCadastro
from core.config import Config
from core.modelos import Camera, DisposicaoLentes, Evento
from core.segredos import ErroCofre
from core.servico_cameras import ServicoCameras
from tests.conftest import CofreMemoria

NovaCamera = Callable[..., Camera]


@pytest.fixture
def servico(tmp_path: Path, banco: Banco) -> ServicoCameras:
    config = Config.carregar()
    config.definir("geral.pasta_dados", str(tmp_path / "dados"))
    return ServicoCameras(banco, config)


def _criar_arquivos(servico: ServicoCameras, camera: Camera) -> None:
    eventos, fotos = servico.pastas(camera)
    (eventos / "2026-10-11" / "101500").mkdir(parents=True)
    (eventos / "2026-10-11" / "101500" / "clipe.mp4").write_bytes(b"x" * 100)
    fotos.mkdir(parents=True)
    (fotos / "a.jpg").write_bytes(b"y" * 50)


def test_cadastrar_guarda_a_senha_no_cofre(servico: ServicoCameras, cofre: CofreMemoria, nova_camera: NovaCamera) -> None:
    camera = servico.cadastrar(nova_camera(), "s3nha")
    assert camera.id is not None
    assert servico.obter_senha(camera.id) == "s3nha"


def test_cadastrar_sem_cofre_nao_grava_nada(servico: ServicoCameras, nova_camera: NovaCamera) -> None:
    anterior = keyring.get_keyring()
    keyring.set_keyring(fail.Keyring())
    try:
        with pytest.raises(ErroCofre):
            servico.cadastrar(nova_camera(), "s3nha")
    finally:
        keyring.set_keyring(anterior)
    assert servico.listar() == []


def test_cadastrar_sem_senha_funciona_mesmo_sem_cofre(servico: ServicoCameras, nova_camera: NovaCamera) -> None:
    anterior = keyring.get_keyring()
    keyring.set_keyring(fail.Keyring())
    try:
        camera = servico.cadastrar(nova_camera(), "")
        servico.editar(replace(camera, nome="Outra"), "")
        servico.remover(camera.id or 0, apagar_arquivos=False)
    finally:
        keyring.set_keyring(anterior)


def test_renomear_move_as_pastas(servico: ServicoCameras, cofre: CofreMemoria, nova_camera: NovaCamera) -> None:
    camera = servico.cadastrar(nova_camera(), "")
    _criar_arquivos(servico, camera)
    renomeada = servico.editar(replace(camera, nome="Portão"), "")
    eventos, fotos = servico.pastas(renomeada)
    assert (eventos / "2026-10-11" / "101500" / "clipe.mp4").is_file()
    assert (fotos / "a.jpg").is_file()
    assert not any(p.exists() for p in servico.pastas(camera))
    assert servico.obter(camera.id or 0).nome == "Portão"  # type: ignore[union-attr]


def test_renomear_so_maiusculas(servico: ServicoCameras, cofre: CofreMemoria, nova_camera: NovaCamera) -> None:
    camera = servico.cadastrar(nova_camera(), "")
    _criar_arquivos(servico, camera)
    servico.editar(replace(camera, nome="FRENTE DA CASA"), "")
    nomes = {p.name for p in servico.config.pasta_eventos.iterdir()}
    assert nomes == {"FRENTE DA CASA"}


def test_renomear_com_pasta_antiga_no_caminho(
    servico: ServicoCameras, cofre: CofreMemoria, nova_camera: NovaCamera
) -> None:
    camera = servico.cadastrar(nova_camera(), "")
    _criar_arquivos(servico, camera)
    (servico.config.pasta_eventos / "Portão").mkdir(parents=True)  # sobra de uma câmera apagada
    with pytest.raises(ErroCadastro, match="Já existe a pasta"):
        servico.editar(replace(camera, nome="Portão"), "")
    assert servico.obter(camera.id or 0).nome == "Frente da casa"  # type: ignore[union-attr]
    assert all(p.is_dir() for p in servico.pastas(camera))  # nada foi movido


def test_editar_troca_e_apaga_a_senha(servico: ServicoCameras, cofre: CofreMemoria, nova_camera: NovaCamera) -> None:
    camera = servico.cadastrar(nova_camera(), "velha")
    servico.editar(replace(camera, lentes=DisposicaoLentes.VERTICAL), "nova")
    assert servico.obter_senha(camera.id or 0) == "nova"
    servico.editar(camera, "")
    assert servico.obter_senha(camera.id or 0) == ""
    assert servico.obter(camera.id or 0).lentes is DisposicaoLentes.UNICA  # type: ignore[union-attr]


def test_remover_com_e_sem_arquivos(servico: ServicoCameras, cofre: CofreMemoria, nova_camera: NovaCamera) -> None:
    a = servico.cadastrar(nova_camera(), "x")
    b = servico.cadastrar(nova_camera(nome="Garagem", codigo="GARAGEM"), "y")
    _criar_arquivos(servico, a)
    _criar_arquivos(servico, b)
    servico.banco.adicionar_evento(Evento(camera_id=a.id or 0, inicio=datetime(2026, 10, 11, 10, 15)))

    assert servico.resumo_arquivos(a) == (1, 150)
    servico.remover(a.id or 0, apagar_arquivos=True)
    servico.remover(b.id or 0, apagar_arquivos=False)

    assert servico.listar() == []
    assert not any(p.exists() for p in servico.pastas(a))
    assert all(p.is_dir() for p in servico.pastas(b))  # o usuário escolheu manter
    assert cofre.senhas == {}


def test_salvar_foto(servico: ServicoCameras, cofre: CofreMemoria, nova_camera: NovaCamera) -> None:
    camera = servico.cadastrar(nova_camera(lentes=DisposicaoLentes.VERTICAL), "")
    vista = camera.vistas()[1]
    caminho = servico.salvar_foto(camera, vista, np.zeros((36, 64, 3), dtype=np.uint8))
    assert caminho.is_file() and caminho.name.endswith("_v2.jpg")
    assert caminho.parent.parent == servico.pastas(camera)[1]


def test_definir_deteccao(servico: ServicoCameras, cofre: CofreMemoria, nova_camera: NovaCamera) -> None:
    camera = servico.cadastrar(nova_camera(), "")
    assert servico.definir_deteccao(camera.id or 0, False).deteccao is False
    assert servico.obter(camera.id or 0).deteccao is False  # type: ignore[union-attr]
