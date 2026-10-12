"""Cria e para os leitores de vídeo das câmeras cadastradas.

Regra (da especificação): cada câmera x canal tem UMA leitura do **substream**
sempre ligada — ela alimenta o mosaico, a IA e a gravação. Câmeras de duas
lentes numa só imagem continuam com UMA conexão: o corte em vistas é feito
depois, em cima do quadro (ver ``Vista.recortar``).

O stream principal (resolução máxima) tem "donos": a Tela da câmera (enquanto
visível) e a IA (se ``ia.fonte = principal``). Ele só é encerrado quando nenhum
dono precisa mais dele.
Enquanto a alta resolução não chega (pode levar alguns segundos no Wi-Fi),
``melhor_leitor()`` devolve o substream — a tela nunca fica preta.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable

from camera.stream import EstadoLeitor, LeitorVideo, LimitesLeitura
from core.config import Config
from core.modelos import Camera, TipoFonte, TipoStream, Vista
from core.segredos import ErroCofre

log = logging.getLogger(__name__)


class GerenciadorCameras:
    """Mantém um ``LeitorVideo`` por (câmera, canal) do substream."""

    def __init__(self, config: Config, obter_senha: Callable[[int], str]) -> None:
        self.config = config
        self._obter_senha = obter_senha
        self._trava = threading.Lock()
        self._leitores: dict[tuple[int, int], LeitorVideo] = {}
        self._fontes: dict[tuple[int, int], str | int] = {}
        self._principais: dict[tuple[int, int], LeitorVideo] = {}
        self._donos: dict[tuple[int, int], set[str]] = {}  # quem precisa de cada principal ("tela", "ia")

    # ------------------------------------------------------------ fontes
    def suavizar_s(self) -> float:
        """Atraso da tela contra as rajadas do Wi-Fi (``interface.suavizar_ms``)."""
        return max(0.0, float(self.config.obter_ou("interface.suavizar_ms", 0)) / 1000)

    def definir_suavizacao(self) -> None:
        """Ajustes mudaram: vale na hora para todos os leitores (sem reconectar)."""
        suavizar = self.suavizar_s()
        with self._trava:
            for leitor in [*self._leitores.values(), *self._principais.values()]:
                leitor.suavizar_s = suavizar

    def limites(self) -> LimitesLeitura:
        return LimitesLeitura(
            abertura_s=float(self.config.obter("captura.timeout_abertura_s")),
            leitura_s=float(self.config.obter("captura.timeout_leitura_s")),
            reconexao_min_s=float(self.config.obter("captura.reconexao_min_s")),
            reconexao_max_s=float(self.config.obter("captura.reconexao_max_s")),
        )

    def fonte(self, camera: Camera, canal: int, stream: TipoStream) -> str | int:
        """URL RTSP (com a senha do cofre), caminho do arquivo ou índice da webcam."""
        if camera.fonte is TipoFonte.ARQUIVO:
            return str(self.config.resolver_caminho(camera.caminho))
        if camera.fonte is TipoFonte.WEBCAM:
            return int(camera.caminho or 0)
        try:
            senha = self._obter_senha(camera.id or 0)
        except ErroCofre as erro:
            log.error("Senha da câmera '%s' indisponível: %s", camera.nome, erro)
            senha = ""
        return camera.url(canal, stream, senha)

    # ------------------------------------------------------------ ciclo de vida
    def sincronizar(self, cameras: list[Camera]) -> None:
        """Deixa ligados exatamente os leitores das câmeras informadas.

        Leitores de câmeras removidas param; se a fonte mudou (IP, senha, arquivo...),
        o leitor é recriado; os demais continuam rodando sem interrupção.
        """
        desejados: dict[tuple[int, int], tuple[Camera, str | int]] = {}
        for camera in cameras:
            for canal in camera.numeros_canais:
                desejados[(camera.id or 0, canal)] = (camera, self.fonte(camera, canal, TipoStream.SUBSTREAM))

        limites = self.limites()
        with self._trava:
            for chave in list(self._leitores):
                if chave not in desejados or desejados[chave][1] != self._fontes[chave]:
                    self._leitores.pop(chave).parar()
                    self._fontes.pop(chave)
                    if chave in self._principais:  # câmera removida/alterada: o principal também cai
                        self._principais.pop(chave).parar()
                        self._donos.pop(chave, None)
            for chave, (camera, fonte) in desejados.items():
                if chave not in self._leitores:
                    nome = camera.nome if camera.canais == 1 else f"{camera.nome} c{chave[1]}"
                    leitor = LeitorVideo(nome, fonte, limites, arquivo_em_loop=True, suavizar_s=self.suavizar_s())
                    self._leitores[chave] = leitor.iniciar()
                    self._fontes[chave] = fonte
        # IA em HD: o principal das câmeras com detecção fica aberto (dono "ia").
        ia_hd = self.ia_usa_principal
        for camera in cameras:
            if ia_hd and camera.deteccao:
                self.abrir_principal(camera, dono="ia")
            else:
                self.fechar_principal(camera.id, dono="ia")

    @property
    def ia_usa_principal(self) -> bool:
        return str(self.config.obter_ou("ia.fonte", "substream")) == "principal"

    def leitor_para_ia(self, vista: Vista) -> LeitorVideo | None:
        """De onde a IA lê: o HD (se configurado e já com imagem) ou o substream."""
        return self.melhor_leitor(vista) if self.ia_usa_principal else self.leitor_da_vista(vista)

    def leitor(self, camera_id: int, canal: int) -> LeitorVideo | None:
        with self._trava:
            return self._leitores.get((camera_id, canal))

    def leitor_da_vista(self, vista: Vista) -> LeitorVideo | None:
        return self.leitor(vista.camera_id, vista.canal)

    # ------------------------------------------------------------ stream principal
    def abrir_principal(self, camera: Camera, dono: str = "tela") -> None:
        """Liga a leitura em resolução máxima para ``dono`` ("tela" ou "ia"). Não duplica.

        Só câmeras RTSP têm um stream principal de verdade. Arquivo e webcam usam o
        próprio substream: abrir o arquivo de novo tocaria em OUTRO ponto do vídeo e as
        caixas da IA (calculadas no substream) ficariam fora do lugar; a webcam nem abre duas vezes.
        """
        if camera.fonte is not TipoFonte.RTSP:
            return
        limites = self.limites()
        with self._trava:
            for canal in camera.numeros_canais:
                chave = (camera.id or 0, canal)
                self._donos.setdefault(chave, set()).add(dono)
                if chave not in self._principais:
                    fonte = self.fonte(camera, canal, TipoStream.PRINCIPAL)
                    nome = f"{camera.nome} HD" if camera.canais == 1 else f"{camera.nome} c{canal} HD"
                    leitor = LeitorVideo(nome, fonte, limites, arquivo_em_loop=True, suavizar_s=self.suavizar_s())
                    self._principais[chave] = leitor.iniciar()

    def fechar_principal(self, camera_id: int | None = None, dono: str = "tela") -> None:
        """``dono`` não precisa mais do HD (de uma câmera, ou de todas com ``None``).

        O leitor só é encerrado quando ninguém mais precisa dele.
        """
        with self._trava:
            leitores = []
            for chave in [c for c in self._principais if camera_id is None or c[0] == camera_id]:
                donos = self._donos.setdefault(chave, set())
                donos.discard(dono)
                if not donos:
                    leitores.append(self._principais.pop(chave))
                    self._donos.pop(chave, None)
        for leitor in leitores:
            leitor.parar()

    def leitor_principal(self, camera_id: int, canal: int) -> LeitorVideo | None:
        with self._trava:
            return self._principais.get((camera_id, canal))

    def melhor_leitor(self, vista: Vista) -> LeitorVideo | None:
        """O principal, se já estiver mandando imagem; senão, o substream."""
        principal = self.leitor_principal(vista.camera_id, vista.canal)
        if principal is not None and principal.estado is EstadoLeitor.ONLINE and principal.ultimo_quadro():
            return principal
        return self.leitor_da_vista(vista)

    def reconectar(self, camera_id: int) -> None:
        """Botão "Reconectar": todos os canais da câmera tentam de novo agora."""
        with self._trava:
            todos = [*self._leitores.items(), *self._principais.items()]
            leitores = [leitor for (cid, _), leitor in todos if cid == camera_id]
        for leitor in leitores:
            leitor.reconectar_agora()

    def parar_tudo(self, esperar_s: float = 3.0) -> None:
        """Para todos os leitores. Primeiro avisa todos, depois espera (em paralelo, não um por um)."""
        with self._trava:
            leitores = [*self._leitores.values(), *self._principais.values()]
            self._leitores.clear()
            self._fontes.clear()
            self._principais.clear()
            self._donos.clear()
        for leitor in leitores:
            leitor.parar()
        limite = time.monotonic() + esperar_s
        for leitor in leitores:
            leitor.parar(max(0.0, limite - time.monotonic()))
