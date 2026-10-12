"""Senhas das câmeras no cofre de senhas do sistema operacional (biblioteca ``keyring``).

- Windows: Gerenciador de Credenciais (Painel de Controle > Gerenciador de Credenciais >
  Credenciais do Windows; as entradas têm "sentinela-casa" no nome).
- Linux: GNOME Keyring / KWallet (Secret Service).

O banco SQLite guarda só o ID da câmera; a senha fica criptografada pelo sistema.
A chave usa o ID (e não o nome) para que renomear a câmera não exija mexer no cofre.

Senha vazia é válida (câmera sem senha): nesse caso não guardamos nada.
Nunca caímos para "texto puro": se não houver cofre disponível, avisamos o usuário.
"""

from __future__ import annotations

import logging

import keyring
from keyring.errors import KeyringError, PasswordDeleteError

log = logging.getLogger(__name__)

#: Nome do "serviço" sob o qual as senhas ficam agrupadas no cofre.
SERVICO = "sentinela-casa"

_DICA_LINUX = (
    "No Linux, instale e desbloqueie um cofre de senhas (GNOME Keyring ou KWallet) "
    "e tente de novo."
)


class ErroCofre(Exception):
    """O cofre de senhas do sistema não está disponível ou falhou."""


def _chave(camera_id: int) -> str:
    return f"camera-{camera_id}"


def verificar_cofre() -> str | None:
    """Devolve uma mensagem de problema, ou ``None`` se o cofre estiver utilizável."""
    backend = keyring.get_keyring()
    nome = type(backend).__module__ + "." + type(backend).__name__
    if nome.startswith(("keyring.backends.fail", "keyring.backends.null")):
        return f"Nenhum cofre de senhas disponível neste sistema. {_DICA_LINUX}"
    if "plaintext" in nome.lower():
        return "O cofre configurado guarda senhas em texto puro; por segurança ele não é aceito."
    return None


def _garantir_cofre() -> None:
    problema = verificar_cofre()
    if problema:
        raise ErroCofre(problema)


def salvar_senha(camera_id: int, senha: str) -> None:
    """Guarda (ou troca) a senha. Senha vazia apaga a entrada."""
    if not senha:
        apagar_senha(camera_id)
        return
    _garantir_cofre()
    try:
        keyring.set_password(SERVICO, _chave(camera_id), senha)
    except KeyringError as erro:
        raise ErroCofre(f"Não foi possível guardar a senha no cofre do sistema: {erro}") from erro
    log.info("Senha da câmera %d guardada no cofre do sistema", camera_id)


def obter_senha(camera_id: int) -> str:
    """Lê a senha. Devolve ``""`` se a câmera não tiver senha."""
    _garantir_cofre()
    try:
        return keyring.get_password(SERVICO, _chave(camera_id)) or ""
    except KeyringError as erro:
        raise ErroCofre(f"Não foi possível ler a senha do cofre do sistema: {erro}") from erro


def apagar_senha(camera_id: int) -> None:
    """Remove a senha (sem erro se ela não existir).

    Sem cofre disponível não há senha guardada para apagar — assim uma câmera
    SEM senha funciona mesmo num sistema sem cofre.
    """
    if verificar_cofre():
        return
    try:
        keyring.delete_password(SERVICO, _chave(camera_id))
        log.info("Senha da câmera %d removida do cofre do sistema", camera_id)
    except PasswordDeleteError:
        pass  # não havia senha guardada
    except KeyringError as erro:
        raise ErroCofre(f"Não foi possível apagar a senha do cofre do sistema: {erro}") from erro
