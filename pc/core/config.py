"""Leitura e gravação do ``config.yaml``.

Usamos a biblioteca ``ruamel.yaml`` em vez da ``PyYAML`` porque ela faz
"ida e volta" (round-trip): ao salvar, os comentários e a ordem das chaves
do arquivo são preservados. Isso importa porque a tela de Configurações
grava neste arquivo e você continua lendo os comentários depois.

Acesso às chaves com "caminho pontilhado"::

    config = Config.carregar()
    config.obter("ia.ia_fps")            # -> 6
    config.definir("interface.tema", "claro")
    config.salvar()
"""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError

log = logging.getLogger(__name__)

#: Pasta ``pc/`` — base para os caminhos relativos do config.yaml.
PASTA_PC = Path(__file__).resolve().parents[1]

#: Arquivo de configuração usado quando nenhum outro é informado.
ARQUIVO_PADRAO = PASTA_PC / "config.yaml"


class ErroConfig(Exception):
    """Problema ao ler ou interpretar o config.yaml (mensagem pronta para o usuário)."""


class Config:
    """Configurações gerais do app, lidas do ``config.yaml``."""

    def __init__(self, dados: dict[str, Any], caminho: Path) -> None:
        self._dados = dados
        self.caminho = caminho
        self._trava = threading.Lock()
        self._yaml = _criar_yaml()

    # ------------------------------------------------------------------ leitura
    @classmethod
    def carregar(cls, caminho: Path | None = None) -> Config:
        """Lê o arquivo (por padrão ``pc/config.yaml``) e devolve um ``Config``."""
        caminho = Path(caminho) if caminho else ARQUIVO_PADRAO
        if not caminho.is_file():
            raise ErroConfig(f"Arquivo de configuração não encontrado: {caminho}")
        try:
            with caminho.open("r", encoding="utf-8") as arquivo:
                dados = _criar_yaml().load(arquivo)
        except YAMLError as erro:
            raise ErroConfig(f"O arquivo {caminho.name} tem um erro de formatação YAML:\n{erro}") from erro
        if not isinstance(dados, dict):
            raise ErroConfig(f"O arquivo {caminho.name} está vazio ou não é um mapa de chaves.")
        return cls(dados, caminho)

    def obter(self, chave: str) -> Any:
        """Devolve o valor de uma chave pontilhada, ex.: ``"eventos.pre_evento_s"``.

        Levanta ``ErroConfig`` com mensagem clara se a chave não existir — preferimos
        falhar cedo a esconder um valor padrão dentro do código.
        """
        atual: Any = self._dados
        for parte in chave.split("."):
            if not isinstance(atual, dict) or parte not in atual:
                raise ErroConfig(f"A chave '{chave}' não foi encontrada em {self.caminho.name}.")
            atual = atual[parte]
        return atual

    def obter_ou(self, chave: str, padrao: Any) -> Any:
        """Como ``obter``, mas devolve ``padrao`` se a chave não existir (para itens opcionais)."""
        try:
            return self.obter(chave)
        except ErroConfig:
            return padrao

    # ------------------------------------------------------------------ escrita
    def definir(self, chave: str, valor: Any) -> None:
        """Altera (ou cria) uma chave na memória. Chame ``salvar()`` para gravar no disco."""
        partes = chave.split(".")
        with self._trava:
            atual = self._dados
            for parte in partes[:-1]:
                if parte not in atual or not isinstance(atual[parte], dict):
                    atual[parte] = {}
                atual = atual[parte]
            atual[partes[-1]] = valor

    def salvar(self) -> None:
        """Grava no disco de forma atômica (arquivo temporário + troca).

        Assim, se o PC desligar no meio da gravação, o config.yaml antigo continua inteiro.
        """
        temporario = self.caminho.with_suffix(".yaml.tmp")
        with self._trava:
            with temporario.open("w", encoding="utf-8", newline="\n") as arquivo:
                self._yaml.dump(self._dados, arquivo)
            os.replace(temporario, self.caminho)
        log.info("Configurações salvas em %s", self.caminho)

    # ------------------------------------------------------------------ caminhos
    def resolver_caminho(self, valor: str | os.PathLike[str]) -> Path:
        """Converte um caminho do config (relativo à pasta ``pc/``) em caminho absoluto."""
        caminho = Path(valor).expanduser()
        return caminho if caminho.is_absolute() else (PASTA_PC / caminho).resolve()

    @property
    def pasta_dados(self) -> Path:
        """Pasta onde ficam banco, eventos e logs."""
        return self.resolver_caminho(self.obter("geral.pasta_dados"))

    @property
    def pasta_logs(self) -> Path:
        return self.pasta_dados / "logs"

    @property
    def pasta_eventos(self) -> Path:
        return self.pasta_dados / "eventos"

    @property
    def arquivo_banco(self) -> Path:
        return self.pasta_dados / "sentinela.db"

    def modelos_url(self) -> dict[str, tuple[str, str]]:
        """Modelos de URL RTSP do cadastro: ``{chave: (nome amigável, modelo)}``."""
        return {
            chave: (str(item["nome"]), str(item["url"]))
            for chave, item in self.obter("modelos_url").items()
        }


def _criar_yaml() -> YAML:
    """Cria o leitor/gravador YAML no modo que preserva comentários."""
    yaml = YAML(typ="rt")
    yaml.preserve_quotes = True
    yaml.indent(mapping=2, sequence=4, offset=2)
    yaml.width = 120
    return yaml
