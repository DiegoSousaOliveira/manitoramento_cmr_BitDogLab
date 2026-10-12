"""Banco SQLite: câmeras, zonas e eventos."""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Callable
from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from core.banco import MIGRACOES, Banco, ErroBanco, ErroCadastro
from core.modelos import Camera, DisposicaoLentes, Evento, TipoFonte

NovaCamera = Callable[..., Camera]


# ----------------------------------------------------------------- esquema
def test_banco_novo_fica_na_versao_atual(banco: Banco) -> None:
    assert banco.versao == len(MIGRACOES)


def test_reabrir_mantem_os_dados(tmp_path: Path, nova_camera: NovaCamera) -> None:
    caminho = tmp_path / "a.db"
    with Banco(caminho) as b:
        b.adicionar_camera(nova_camera())
    with Banco(caminho) as b:
        assert [c.nome for c in b.listar_cameras()] == ["Frente da casa"]
        assert b.versao == len(MIGRACOES)


def test_migracao_de_um_banco_v1(tmp_path: Path) -> None:
    """Banco criado na Fase 0 (v1) é atualizado sem perder dados."""
    from core.banco import _ESQUEMA_V1

    caminho = tmp_path / "v1.db"
    con = sqlite3.connect(caminho)
    con.executescript(_ESQUEMA_V1 + "PRAGMA user_version = 1;")
    con.execute(
        "INSERT INTO cameras (nome, codigo, fonte, porta_rtsp, porta_onvif, criada_em) "
        "VALUES ('Frente', 'FRENTE', 'rtsp', 554, 8899, '2026-10-11T10:00:00')"
    )
    con.execute("INSERT INTO zonas (camera_id, canal, pontos) VALUES (1, 1, '[[0,0],[1,0],[1,1]]')")
    con.commit()
    con.close()

    with Banco(caminho) as b:
        assert b.versao == len(MIGRACOES)
        camera = b.obter_camera(1)
        assert camera is not None and camera.lentes is DisposicaoLentes.UNICA and camera.nomes_vistas == {}
        assert b.obter_zona(1, 1) == [(0.0, 0.0), (1.0, 0.0), (1.0, 1.0)]


def test_banco_de_versao_mais_nova_e_recusado(tmp_path: Path) -> None:
    caminho = tmp_path / "futuro.db"
    con = sqlite3.connect(caminho)
    con.execute(f"PRAGMA user_version = {len(MIGRACOES) + 1}")
    con.close()
    with pytest.raises(ErroBanco, match="versão mais nova"):
        Banco(caminho)


# ----------------------------------------------------------------- câmeras
def test_adicionar_camera_preenche_id_ordem_e_data(banco: Banco, nova_camera: NovaCamera) -> None:
    camera = banco.adicionar_camera(
        nova_camera(
            ajustes={"cooldown_s": 60},
            lentes=DisposicaoLentes.VERTICAL,
            lente_ptz=2,
            nomes_vistas={1: "Rua", 2: "Calçada"},
        )
    )
    assert camera.id is not None
    assert camera.ordem == 1
    assert camera.criada_em is not None

    lida = banco.obter_camera(camera.id)
    assert lida == camera  # todos os campos voltam iguais (inclusive ajustes e enums)
    assert lida is not None and lida.fonte is TipoFonte.RTSP and lida.lentes is DisposicaoLentes.VERTICAL
    assert lida.nomes_vistas == {1: "Rua", 2: "Calçada"}  # as chaves voltam como int


def test_nome_duplicado_ignorando_maiusculas(banco: Banco, nova_camera: NovaCamera) -> None:
    banco.adicionar_camera(nova_camera())
    with pytest.raises(ErroCadastro, match="Já existe uma câmera chamada"):
        banco.adicionar_camera(nova_camera(nome="FRENTE DA CASA", codigo="OUTRO"))


def test_codigo_duplicado(banco: Banco, nova_camera: NovaCamera) -> None:
    banco.adicionar_camera(nova_camera())
    with pytest.raises(ErroCadastro, match="código 'FRENTE'"):
        banco.adicionar_camera(nova_camera(nome="Outra"))


def test_erro_de_cadastro_nao_deixa_lixo(banco: Banco, nova_camera: NovaCamera) -> None:
    banco.adicionar_camera(nova_camera())
    with pytest.raises(ErroCadastro):
        banco.adicionar_camera(nova_camera(nome="Outra"))
    assert len(banco.listar_cameras()) == 1


def test_renomear_e_editar(banco: Banco, nova_camera: NovaCamera) -> None:
    camera = banco.adicionar_camera(nova_camera())
    banco.atualizar_camera(replace(camera, nome="Portão", codigo="PORTAO", canais=2, tem_ptz=True))
    lida = banco.obter_camera_por_nome("portão")
    assert lida is not None
    assert (lida.id, lida.codigo, lida.canais, lida.tem_ptz) == (camera.id, "PORTAO", 2, True)
    assert banco.obter_camera_por_nome("Frente da casa") is None


def test_renomear_para_nome_existente(banco: Banco, nova_camera: NovaCamera) -> None:
    banco.adicionar_camera(nova_camera())
    outra = banco.adicionar_camera(nova_camera(nome="Garagem", codigo="GARAGEM"))
    with pytest.raises(ErroCadastro):
        banco.atualizar_camera(replace(outra, nome="Frente da casa"))


def test_atualizar_camera_inexistente(banco: Banco, nova_camera: NovaCamera) -> None:
    with pytest.raises(ErroBanco, match="não existe"):
        banco.atualizar_camera(nova_camera(id=999))


def test_listar_em_ordem_de_cadastro(banco: Banco, nova_camera: NovaCamera) -> None:
    for nome, codigo in [("C", "C"), ("A", "A"), ("B", "B")]:
        banco.adicionar_camera(nova_camera(nome=nome, codigo=codigo))
    assert [c.nome for c in banco.listar_cameras()] == ["C", "A", "B"]
    assert [c.ordem for c in banco.listar_cameras()] == [1, 2, 3]


def test_nome_e_codigos_em_uso(banco: Banco, nova_camera: NovaCamera) -> None:
    camera = banco.adicionar_camera(nova_camera())
    banco.adicionar_camera(nova_camera(nome="Garagem", codigo="GARAGEM"))
    assert banco.nome_em_uso("frente da casa")
    assert not banco.nome_em_uso("Frente da casa", ignorar_id=camera.id)  # editando a própria câmera
    assert banco.codigos_em_uso() == {"FRENTE", "GARAGEM"}
    assert banco.codigos_em_uso(ignorar_id=camera.id) == {"GARAGEM"}


def test_camera_falsa_de_arquivo(banco: Banco, nova_camera: NovaCamera) -> None:
    camera = banco.adicionar_camera(
        nova_camera(nome="Vídeo", codigo="VIDEO", fonte=TipoFonte.ARQUIVO, ip="", caminho="videos/rua.mp4")
    )
    lida = banco.obter_camera(camera.id or 0)
    assert lida is not None and lida.fonte is TipoFonte.ARQUIVO and lida.caminho == "videos/rua.mp4"


# ----------------------------------------------------------------- zonas
def test_zona(banco: Banco, nova_camera: NovaCamera) -> None:
    camera_id = banco.adicionar_camera(nova_camera()).id or 0
    assert banco.obter_zona(camera_id, 1) is None  # sem zona = quadro inteiro

    banco.salvar_zona(camera_id, 1, [(0.1, 0.2), (0.9, 0.2), (0.5, 0.95)])
    assert banco.obter_zona(camera_id, 1) == [(0.1, 0.2), (0.9, 0.2), (0.5, 0.95)]
    assert banco.obter_zona(camera_id, 2) is None  # cada canal tem a sua

    banco.salvar_zona(camera_id, 1, [(0.0, 0.0), (1.0, 0.0), (1.0, 1.0)])  # substitui
    assert banco.obter_zona(camera_id, 1) == [(0.0, 0.0), (1.0, 0.0), (1.0, 1.0)]

    banco.salvar_zona(camera_id, 1, None)  # volta ao quadro inteiro
    assert banco.obter_zona(camera_id, 1) is None


# ----------------------------------------------------------------- eventos
def _evento(camera_id: int, inicio: datetime, **extra: object) -> Evento:
    return Evento(camera_id=camera_id, inicio=inicio, fim=inicio + timedelta(seconds=12), **extra)  # type: ignore[arg-type]


def test_evento_ida_e_volta(banco: Banco, nova_camera: NovaCamera) -> None:
    camera_id = banco.adicionar_camera(nova_camera()).id or 0
    inicio = datetime(2026, 10, 11, 21, 35, 12, 345000)
    evento = banco.adicionar_evento(
        _evento(camera_id, inicio, classes={"PESSOA": 2}, maior_conf=0.87, pasta="2026-10-11/213512", clipe="clipe.mp4")
    )
    lido = banco.obter_evento(evento.id or 0)
    assert lido == evento
    assert lido is not None and lido.duracao_s == 12


def test_filtros_de_eventos(banco: Banco, nova_camera: NovaCamera) -> None:
    frente = banco.adicionar_camera(nova_camera()).id or 0
    garagem = banco.adicionar_camera(nova_camera(nome="Garagem", codigo="GARAGEM")).id or 0
    dia = datetime(2026, 10, 11, 8, 0)
    banco.adicionar_evento(_evento(frente, dia, classes={"PESSOA": 1}, alerta=True))
    banco.adicionar_evento(_evento(frente, dia + timedelta(hours=2), classes={"CARRO": 1}))
    banco.adicionar_evento(_evento(garagem, dia + timedelta(hours=1), classes={"GATO": 1}))
    banco.adicionar_evento(_evento(frente, dia + timedelta(days=1), classes={"PESSOA": 3, "CARRO": 1}))

    def classes(eventos: list[Evento]) -> list[dict[str, int]]:
        return [e.classes for e in eventos]

    # Ordem: mais recente primeiro
    assert classes(banco.listar_eventos()) == [{"PESSOA": 3, "CARRO": 1}, {"CARRO": 1}, {"GATO": 1}, {"PESSOA": 1}]
    assert len(banco.listar_eventos(camera_id=garagem)) == 1
    assert len(banco.listar_eventos(dia=date(2026, 10, 11))) == 3
    assert classes(banco.listar_eventos(classe="PESSOA")) == [{"PESSOA": 3, "CARRO": 1}, {"PESSOA": 1}]
    assert classes(banco.listar_eventos(so_alertas=True)) == [{"PESSOA": 1}]
    assert len(banco.listar_eventos(camera_id=frente, dia=date(2026, 10, 11), classe="CARRO")) == 1
    assert len(banco.listar_eventos(antes_de=dia + timedelta(hours=1, minutes=30))) == 2
    assert len(banco.listar_eventos(limite=2)) == 2


def test_classe_desconhecida_no_filtro(banco: Banco) -> None:
    with pytest.raises(ValueError):
        banco.listar_eventos(classe="DRAGAO")


def test_marcar_evento_como_alerta(banco: Banco, nova_camera: NovaCamera) -> None:
    camera_id = banco.adicionar_camera(nova_camera()).id or 0
    evento = banco.adicionar_evento(Evento(camera_id=camera_id, inicio=datetime(2026, 10, 11, 9, 0)))
    banco.atualizar_evento(replace(evento, alerta=True, fim=datetime(2026, 10, 11, 9, 1)))
    lido = banco.obter_evento(evento.id or 0)
    assert lido is not None and lido.alerta and lido.duracao_s == 60


def test_remover_camera_remove_zonas_e_eventos(banco: Banco, nova_camera: NovaCamera) -> None:
    camera_id = banco.adicionar_camera(nova_camera()).id or 0
    banco.salvar_zona(camera_id, 1, [(0, 0), (1, 0), (1, 1)])
    banco.adicionar_evento(_evento(camera_id, datetime(2026, 10, 11, 9, 0)))
    banco.remover_camera(camera_id)
    assert banco.listar_cameras() == []
    assert banco.listar_eventos() == []
    assert banco.obter_zona(camera_id, 1) is None


def test_contar_eventos(banco: Banco, nova_camera: NovaCamera) -> None:
    camera_id = banco.adicionar_camera(nova_camera()).id or 0
    for minuto in range(3):
        banco.adicionar_evento(_evento(camera_id, datetime(2026, 10, 11, 9, minuto), vista=2))
    assert banco.contar_eventos(camera_id) == 3
    assert {e.vista for e in banco.listar_eventos()} == {2}


def test_remover_evento(banco: Banco, nova_camera: NovaCamera) -> None:
    camera_id = banco.adicionar_camera(nova_camera()).id or 0
    evento = banco.adicionar_evento(_evento(camera_id, datetime(2026, 10, 11, 9, 0)))
    banco.remover_evento(evento.id or 0)
    assert banco.obter_evento(evento.id or 0) is None


def test_varias_threads_gravando_ao_mesmo_tempo(banco: Banco, nova_camera: NovaCamera) -> None:
    """Simula a gravação de eventos de várias câmeras em paralelo com a interface lendo."""
    camera_id = banco.adicionar_camera(nova_camera()).id or 0
    erros: list[BaseException] = []

    def gravar(n: int) -> None:
        try:
            for i in range(25):
                banco.adicionar_evento(_evento(camera_id, datetime(2026, 10, 11, n, i)))
                banco.listar_eventos(limite=5)
        except BaseException as erro:  # noqa: BLE001
            erros.append(erro)

    threads = [threading.Thread(target=gravar, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert erros == []
    assert len(banco.listar_eventos(limite=None)) == 8 * 25
