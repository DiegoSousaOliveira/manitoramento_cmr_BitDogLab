"""Regras de negócio do cadastro de câmeras: banco + cofre de senhas + pastas no disco.

A interface (diálogo de cadastro, menu do mosaico) só chama estas funções. Assim
toda a lógica fica testável sem abrir janelas, e o banco, o cofre e as pastas
nunca ficam fora de sincronia.

Pastas de cada câmera (nome da pasta = nome da câmera):
- ``dados/eventos/<nome>/``: eventos gravados (Fase 4);
- ``dados/fotos/<nome>/``: fotos tiradas pelo menu "Tirar foto".
"""

from __future__ import annotations

import logging
import os
import shutil
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np

from camera.captura import salvar_jpg
from core import segredos
from core.banco import Banco, ErroBanco, ErroCadastro
from core.config import Config
from core.modelos import Camera, Vista

log = logging.getLogger(__name__)


class ServicoCameras:
    """Cadastrar, editar, renomear e remover câmeras de forma consistente."""

    def __init__(self, banco: Banco, config: Config) -> None:
        self.banco = banco
        self.config = config

    # ------------------------------------------------------------ consultas
    def listar(self) -> list[Camera]:
        return self.banco.listar_cameras()

    def obter(self, camera_id: int) -> Camera | None:
        return self.banco.obter_camera(camera_id)

    def obter_senha(self, camera_id: int) -> str:
        """Senha do cofre (``""`` se a câmera não tiver). Pode levantar ``ErroCofre``."""
        return segredos.obter_senha(camera_id)

    def pastas(self, camera: Camera) -> list[Path]:
        """Pastas de arquivos desta câmera (eventos e fotos)."""
        return [self.config.pasta_eventos / camera.nome, self.config.pasta_dados / "fotos" / camera.nome]

    def resumo_arquivos(self, camera: Camera) -> tuple[int, int]:
        """(quantidade de eventos no banco, bytes ocupados pelas pastas da câmera)."""
        total = 0
        for pasta in self.pastas(camera):
            if pasta.is_dir():
                total += sum(f.stat().st_size for f in pasta.rglob("*") if f.is_file())
        return self.banco.contar_eventos(camera.id or 0), total

    # ------------------------------------------------------------ alterações
    def cadastrar(self, camera: Camera, senha: str) -> Camera:
        """Grava a câmera nova e a senha. Se o cofre falhar, nada fica gravado."""
        if senha and (problema := segredos.verificar_cofre()):
            raise segredos.ErroCofre(problema)
        nova = self.banco.adicionar_camera(camera)
        try:
            segredos.salvar_senha(nova.id or 0, senha)
        except segredos.ErroCofre:
            self.banco.remover_camera(nova.id or 0)
            raise
        return nova

    def editar(self, camera: Camera, senha: str) -> Camera:
        """Grava as alterações. Renomear também renomeia as pastas de eventos e fotos."""
        if camera.id is None:
            raise ValueError("A câmera ainda não foi cadastrada.")
        antiga = self.banco.obter_camera(camera.id)
        if antiga is None:
            raise ErroBanco("Essa câmera não existe mais.")
        if senha and (problema := segredos.verificar_cofre()):
            raise segredos.ErroCofre(problema)

        renomeadas: list[tuple[Path, Path]] = []
        if antiga.nome != camera.nome:
            if self.banco.nome_em_uso(camera.nome, ignorar_id=camera.id):
                raise ErroCadastro(f"Já existe uma câmera chamada '{camera.nome}'.")
            renomeadas = self._renomear_pastas(antiga, camera)
        try:
            self.banco.atualizar_camera(camera)
        except ErroBanco:
            for origem, destino in reversed(renomeadas):  # desfaz para não ficar fora de sincronia
                os.replace(destino, origem)
            raise
        segredos.salvar_senha(camera.id, senha)
        if renomeadas:
            log.info("Câmera renomeada: '%s' -> '%s'", antiga.nome, camera.nome)
        return camera

    def salvar_ajustes(self, camera_id: int, deteccao: bool, gravar: bool, ajustes: dict[str, Any]) -> Camera:
        """Ajustes da câmera (tela de Ajustes → Câmeras). Não mexe na senha nem nas pastas."""
        camera = self.banco.obter_camera(camera_id)
        if camera is None:
            raise ErroBanco("Essa câmera não existe mais.")
        camera = replace(camera, deteccao=deteccao, gravar=gravar, ajustes=dict(ajustes))
        self.banco.atualizar_camera(camera)
        log.info("Ajustes da câmera '%s' salvos: %s", camera.nome, ", ".join(sorted(ajustes)) or "todos no padrão")
        return camera

    def definir_deteccao(self, camera_id: int, ativa: bool) -> Camera:
        camera = self.banco.obter_camera(camera_id)
        if camera is None:
            raise ErroBanco("Essa câmera não existe mais.")
        camera = replace(camera, deteccao=ativa)
        self.banco.atualizar_camera(camera)
        return camera

    def remover(self, camera_id: int, apagar_arquivos: bool) -> None:
        """Remove do banco (zonas e eventos saem junto), apaga a senha e, se pedido, as pastas."""
        camera = self.banco.obter_camera(camera_id)
        if camera is None:
            return
        self.banco.remover_camera(camera_id)
        try:
            segredos.apagar_senha(camera_id)
        except segredos.ErroCofre as erro:
            log.warning("Câmera removida, mas a senha não saiu do cofre: %s", erro)
        if apagar_arquivos:
            for pasta in self.pastas(camera):
                if pasta.is_dir():
                    shutil.rmtree(pasta)
                    log.info("Pasta apagada: %s", pasta)
        log.info("Câmera removida: %s", camera.nome)

    def salvar_foto(self, camera: Camera, vista: Vista, imagem: np.ndarray) -> Path:
        """Grava uma foto em ``dados/fotos/<câmera>/<AAAA-MM-DD>/<HHMMSS>_v<vista>.jpg``."""
        agora = datetime.now()
        pasta = self.config.pasta_dados / "fotos" / camera.nome / f"{agora:%Y-%m-%d}"
        pasta.mkdir(parents=True, exist_ok=True)
        caminho = pasta / f"{agora:%H%M%S}_v{vista.numero}.jpg"
        salvar_jpg(caminho, imagem)
        log.info("Foto salva: %s", caminho)
        return caminho

    # ------------------------------------------------------------ detalhes
    def _renomear_pastas(self, antiga: Camera, nova: Camera) -> list[tuple[Path, Path]]:
        feitas: list[tuple[Path, Path]] = []
        for origem, destino in zip(self.pastas(antiga), self.pastas(nova), strict=True):
            if not origem.is_dir():
                continue
            mesma_pasta = destino.exists() and os.path.samefile(origem, destino)  # só mudou maiúscula/minúscula
            if destino.exists() and not mesma_pasta:
                for o, d in reversed(feitas):
                    os.replace(d, o)
                raise ErroCadastro(
                    f"Já existe a pasta '{destino}'. Mova ou apague essa pasta antes de renomear a câmera."
                )
            os.replace(origem, destino)
            feitas.append((origem, destino))
        return feitas
