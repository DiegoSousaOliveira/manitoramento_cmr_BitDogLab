"""config.yaml (leitura/gravação preservando comentários) e cofre de senhas."""

from __future__ import annotations

import shutil
from pathlib import Path

import keyring
import pytest
from keyring.backends import fail

from core import segredos
from core.config import ARQUIVO_PADRAO, PASTA_PC, Config, ErroConfig
from core.modelos import CLASSES
from core.validacao import validar_modelo_url, validar_portas_placa
from tests.conftest import CofreMemoria

#: Chaves que o código da Fase 0 lê. Se alguém apagar do config.yaml, o teste avisa.
CHAVES_USADAS = [
    "geral.pasta_dados", "geral.log_nivel", "geral.log_tamanho_mb", "geral.log_arquivos",
    "cadastro.usuario", "cadastro.porta_rtsp", "cadastro.porta_onvif", "cadastro.modelo_url",
    "captura.transporte_rtsp", "diagnostico.portas", "diagnostico.timeout_s", "diagnostico.medir_fps_s",
    "modelos_url.icsee.url", "parado.tempo_parado.PESSOA", "parado.nivel.PESSOA", "placa.porta_pc",
    "placa.porta_placa",
]


@pytest.fixture
def config_copia(tmp_path: Path) -> Config:
    destino = tmp_path / "config.yaml"
    shutil.copy(ARQUIVO_PADRAO, destino)
    return Config.carregar(destino)


# ----------------------------------------------------------------- config
def test_config_padrao_tem_as_chaves_usadas() -> None:
    config = Config.carregar()
    for chave in CHAVES_USADAS:
        config.obter(chave)  # levanta ErroConfig se faltar


def test_config_tem_valores_validos() -> None:
    """O config.yaml real é editado pela tela de Ajustes (Fase 8): em vez dos valores da
    especificação, conferimos que tudo está dentro das faixas que a tela aceita."""
    config = Config.carregar()
    assert 1 <= config.obter("interface.fps_mosaico") <= 30
    assert 1 <= config.obter("ia.ia_fps") <= 15
    assert 0.10 <= config.obter("ia.conf_minima") <= 0.90
    assert 0 <= config.obter("eventos.pre_evento_s") <= 15
    for classe in CLASSES:
        assert config.obter(f"parado.tempo_parado.{classe}") >= 1
        assert config.obter(f"parado.nivel.{classe}") in ("BAIXO", "MEDIO", "ALTO")
    assert validar_portas_placa(config.obter("placa.porta_placa"), config.obter("placa.porta_pc")) is None
    assert list(config.obter("diagnostico.portas")) == [554, 8899, 34567]  # formato da ferramenta (não vai à tela)


def test_modelos_de_url_do_config_sao_validos() -> None:
    for _nome, modelo in Config.carregar().modelos_url().values():
        assert validar_modelo_url(modelo) is None


def test_chave_inexistente_da_erro_claro() -> None:
    with pytest.raises(ErroConfig, match="ia.nao_existe"):
        Config.carregar().obter("ia.nao_existe")


def test_obter_ou_devolve_padrao() -> None:
    assert Config.carregar().obter_ou("x.y", 42) == 42


def test_arquivo_inexistente(tmp_path: Path) -> None:
    with pytest.raises(ErroConfig, match="não encontrado"):
        Config.carregar(tmp_path / "nada.yaml")


def test_yaml_quebrado(tmp_path: Path) -> None:
    arquivo = tmp_path / "ruim.yaml"
    arquivo.write_text("geral: [1, 2\n", encoding="utf-8")
    with pytest.raises(ErroConfig, match="YAML"):
        Config.carregar(arquivo)


def test_salvar_preserva_comentarios(config_copia: Config) -> None:
    config_copia.definir("interface.tema", "claro")
    config_copia.definir("nova.secao.valor", 3)
    config_copia.salvar()

    texto = config_copia.caminho.read_text(encoding="utf-8")
    assert "# escuro | claro" in texto  # comentário da linha continua lá
    assert "Senhas das câmeras NUNCA ficam aqui" in texto  # comentário do topo também
    relido = Config.carregar(config_copia.caminho)
    assert relido.obter("interface.tema") == "claro"
    assert relido.obter("nova.secao.valor") == 3


def test_caminhos_relativos_ficam_na_pasta_pc() -> None:
    config = Config.carregar()
    assert config.pasta_dados == (PASTA_PC / "dados").resolve()
    assert config.arquivo_banco.name == "sentinela.db"
    assert config.pasta_logs.parent == config.pasta_dados


# ----------------------------------------------------------------- segredos
def test_salvar_e_ler_senha(cofre: CofreMemoria) -> None:
    segredos.salvar_senha(7, "s3gr3d0")
    assert segredos.obter_senha(7) == "s3gr3d0"
    assert cofre.senhas == {("sentinela-casa", "camera-7"): "s3gr3d0"}


def test_camera_sem_senha(cofre: CofreMemoria) -> None:
    assert segredos.obter_senha(99) == ""


def test_senha_vazia_apaga_a_entrada(cofre: CofreMemoria) -> None:
    segredos.salvar_senha(1, "abc")
    segredos.salvar_senha(1, "")
    assert segredos.obter_senha(1) == ""
    assert cofre.senhas == {}


def test_apagar_senha_inexistente_nao_da_erro(cofre: CofreMemoria) -> None:
    segredos.apagar_senha(123)


def test_sem_cofre_disponivel_da_erro_claro() -> None:
    anterior = keyring.get_keyring()
    keyring.set_keyring(fail.Keyring())
    try:
        assert segredos.verificar_cofre() is not None
        with pytest.raises(segredos.ErroCofre, match="cofre"):
            segredos.salvar_senha(1, "abc")
        with pytest.raises(segredos.ErroCofre):
            segredos.obter_senha(1)
    finally:
        keyring.set_keyring(anterior)


def test_cofre_disponivel(cofre: CofreMemoria) -> None:
    assert segredos.verificar_cofre() is None
