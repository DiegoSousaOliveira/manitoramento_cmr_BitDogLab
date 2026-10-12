"""Leitura de vídeo em thread própria, guardando só o quadro mais recente.

Por que uma thread por fonte? ``captura.read()`` BLOQUEIA até o próximo quadro
chegar (ou o tempo limite estourar). Se isso rodasse na thread da interface, a
janela congelaria. Cada ``LeitorVideo`` lê sem parar na sua thread e guarda
apenas o ÚLTIMO quadro: quem consome (mosaico, IA...) pega o mais novo quando
quiser, e quadros antigos são descartados — nada se acumula na memória e o
vídeo nunca fica "atrasado".

Quando a conexão cai, o leitor tenta de novo sozinho, esperando cada vez mais
entre as tentativas (1 s, 2 s, 4 s... até o máximo). Isso se chama *backoff*
exponencial: evita martelar uma câmera desligada.

Suavização (12/10/2026): no Wi-Fi os quadros chegam em RAJADAS (medido na câmera do Diego:
25% chegam "grudados", com buracos de até 377 ms). A tela mostrava sempre o mais novo, então
pulava os grudados e congelava nos buracos: as "engasgadas". Agora o leitor também guarda os
poucos quadros mais recentes, cada um com a hora em que deve APARECER (``exibir_em``), calculada
a partir da hora em que a CÂMERA o gravou (``pts``). A tela usa ``quadro_para_exibir``; a IA e a
gravação continuam com ``ultimo_quadro`` (o mais novo, sem atraso).
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, replace
from enum import StrEnum

import cv2
import numpy as np

from camera.captura import abrir_captura
from core.url_rtsp import mascarar_url

log = logging.getLogger(__name__)


#: Janela de tempo usada para medir o FPS (segundos).
JANELA_FPS_S = 2.0
#: Quadros recentes guardados para a suavização (só referências). Os HD pesam ~18 MB cada.
MAX_RECENTES = 8
MAX_RECENTES_GRANDES = 4
PIXELS_GRANDE = 2_000_000


class EstadoLeitor(StrEnum):
    CONECTANDO = "conectando"  # tentando abrir (primeira vez ou nova tentativa)
    ONLINE = "online"  # quadros chegando
    OFFLINE = "offline"  # falhou; esperando a próxima tentativa
    PARADO = "parado"  # parar() foi chamado


@dataclass(frozen=True)
class Quadro:
    imagem: np.ndarray  # BGR (padrão do OpenCV)
    numero: int  # cresce a cada quadro: serve para saber se já vimos este
    instante: float  # time.monotonic() da leitura
    pts: float | None = None  # hora do quadro NA CÂMERA (s, do próprio vídeo); None = desconhecida
    exibir_em: float = 0.0  # time.monotonic() em que a tela deve mostrá-lo (suavização)


class RelogioExibicao:
    """Diz QUANDO mostrar cada quadro para o vídeo andar no ritmo da câmera, e não no da rede.

    ``chegada - pts`` é o atraso (rede + câmera) daquele quadro; o MENOR atraso dos últimos
    segundos é a referência (o caminho "sem engarrafamento"). Cada quadro aparece em
    ``pts + referência + suavizar_s``: quem chegou até ``suavizar_s`` atrasado entra no ritmo
    certo, sem pular nem congelar. Nunca volta no tempo (quadros fora de ordem não "rebobinam").
    """

    JANELA_S = 5.0

    def __init__(self) -> None:
        self._atrasos: deque[tuple[float, float]] = deque()  # (chegada, chegada - pts)
        self._ultimo_pts: float | None = None
        self._ultimo_exibir = float("-inf")

    def reiniciar(self) -> None:
        self._atrasos.clear()
        self._ultimo_pts = None
        self._ultimo_exibir = float("-inf")

    def quando_exibir(self, pts: float | None, chegada: float, suavizar_s: float) -> float:
        if suavizar_s <= 0 or pts is None or pts < 0:
            exibir = chegada  # sem suavização (ou o vídeo não informa a hora dos quadros)
        else:
            if self._ultimo_pts is not None and not -0.5 < pts - self._ultimo_pts < 2.0:
                self._atrasos.clear()  # salto na hora da câmera (reconexão, vídeo voltou ao início)
            self._ultimo_pts = pts
            self._atrasos.append((chegada, chegada - pts))
            while chegada - self._atrasos[0][0] > self.JANELA_S:
                self._atrasos.popleft()
            referencia = min(atraso for _, atraso in self._atrasos)
            exibir = pts + referencia + suavizar_s
        exibir = max(exibir, self._ultimo_exibir)
        self._ultimo_exibir = exibir
        return exibir


@dataclass(frozen=True)
class LimitesLeitura:
    """Tempos de conexão e reconexão (vêm da seção ``captura`` do config.yaml)."""

    abertura_s: float
    leitura_s: float
    reconexao_min_s: float
    reconexao_max_s: float


def calcular_espera(tentativas: int, minimo_s: float, maximo_s: float) -> float:
    """Backoff exponencial: 1ª falha espera o mínimo, depois dobra até o máximo."""
    return min(maximo_s, minimo_s * 2 ** max(0, tentativas - 1))


class LeitorVideo:
    """Lê uma fonte (URL RTSP, arquivo ou webcam) continuamente em segundo plano."""

    def __init__(self, nome: str, fonte: str | int, limites: LimitesLeitura, arquivo_em_loop: bool = False,
                 suavizar_s: float = 0.0) -> None:
        self.nome = nome
        self.fonte = fonte
        self.limites = limites
        self.arquivo_em_loop = arquivo_em_loop
        self.suavizar_s = suavizar_s  # atraso da tela para absorver as rajadas do Wi-Fi (0 = desligado)
        self._trava = threading.Lock()
        self._ultimo: Quadro | None = None
        self._recentes: deque[Quadro] = deque()  # para a suavização da tela
        self._relogio = RelogioExibicao()
        self._estado = EstadoLeitor.CONECTANDO
        self._mensagem = "conectando…"
        self._proxima_tentativa = 0.0
        self._instantes: deque[float] = deque()  # chegada dos quadros dos últimos JANELA_FPS_S
        self._parar = threading.Event()
        self._acordar = threading.Event()
        self._assinantes: list[Callable[[Quadro], None]] = []
        self._thread = threading.Thread(target=self._executar, name=f"leitor-{nome}", daemon=True)

    # ------------------------------------------------------------ controle
    def iniciar(self) -> LeitorVideo:
        self._thread.start()
        return self

    def parar(self, esperar_s: float | None = None) -> None:
        """Pede para parar. Com ``esperar_s``, aguarda a thread terminar por até esse tempo."""
        self._parar.set()
        self._acordar.set()
        if esperar_s is not None and self._thread.is_alive():
            self._thread.join(esperar_s)

    def assinar(self, funcao: Callable[[Quadro], None]) -> None:
        """``funcao(quadro)`` é chamada na thread do leitor a CADA quadro (deve ser rapidíssima)."""
        with self._trava:
            self._assinantes = [*self._assinantes, funcao]

    def cancelar_assinatura(self, funcao: Callable[[Quadro], None]) -> None:
        with self._trava:
            # "!=" e não "is not": métodos ligados (ex.: buffer.adicionar) são objetos novos a cada
            # acesso, mas comparam iguais se forem a mesma função do mesmo objeto.
            self._assinantes = [f for f in self._assinantes if f != funcao]

    def reconectar_agora(self) -> None:
        """Pula a espera do backoff (botão "Reconectar")."""
        self._acordar.set()

    @property
    def ativo(self) -> bool:
        return self._thread.is_alive()

    # ------------------------------------------------------------ leitura do estado (qualquer thread)
    def ultimo_quadro(self) -> Quadro | None:
        """O quadro mais novo (IA, gravação, foto): sem atraso nenhum."""
        with self._trava:
            return self._ultimo

    def quadro_para_exibir(self, agora: float) -> Quadro | None:
        """O quadro que a TELA deve mostrar agora (suavizado). ``None`` = nenhum ainda."""
        with self._trava:
            vencido = None
            for quadro in self._recentes:
                if quadro.exibir_em > agora:
                    break
                vencido = quadro
            if vencido is None:
                return None
            while self._recentes[0] is not vencido:  # os anteriores nunca mais serão mostrados
                self._recentes.popleft()
            return vencido

    @property
    def estado(self) -> EstadoLeitor:
        return self._estado

    @property
    def mensagem(self) -> str:
        return self._mensagem

    @property
    def fps(self) -> float:
        """FPS medido: quadros que chegaram nos últimos ``JANELA_FPS_S`` segundos.

        Por que não uma média de "1/intervalo"? No Wi-Fi os quadros chegam em rajadas
        (alguns grudados, outros espaçados) e, ao conectar, o FFmpeg entrega de uma vez
        os quadros guardados. A média de "1/intervalo" ficava inflada por isso (deu
        32 FPS num stream de 12). Contar quadros numa janela de tempo é robusto.
        """
        with self._trava:
            if len(self._instantes) < 3:
                return 0.0
            duracao = self._instantes[-1] - self._instantes[0]
            return (len(self._instantes) - 1) / duracao if duracao >= 0.5 else 0.0

    def segundos_para_tentar(self) -> float:
        """Quanto falta para a próxima tentativa (0 se não estiver esperando)."""
        return max(0.0, self._proxima_tentativa - time.monotonic())

    # ------------------------------------------------------------ thread
    def _definir_estado(self, estado: EstadoLeitor, mensagem: str) -> None:
        if (estado, mensagem) != (self._estado, self._mensagem):
            self._estado, self._mensagem = estado, mensagem
            nivel = logging.DEBUG if estado is EstadoLeitor.CONECTANDO else logging.INFO
            log.log(nivel, "[%s] %s: %s", self.nome, estado.value, mensagem)

    def _numero_atual(self) -> int:
        quadro = self._ultimo
        return quadro.numero if quadro else 0

    def _executar(self) -> None:
        tentativas = 0
        descricao = mascarar_url(self.fonte) if isinstance(self.fonte, str) else f"webcam {self.fonte}"
        while not self._parar.is_set():
            self._definir_estado(EstadoLeitor.CONECTANDO, "conectando…")
            antes = self._numero_atual()
            falha = self._sessao(descricao) or "fim do vídeo"
            if self._parar.is_set():
                break
            # Se chegaram quadros nesta sessão, é uma queda nova: o backoff recomeça do mínimo.
            tentativas = 1 if self._numero_atual() > antes else tentativas + 1
            espera = calcular_espera(tentativas, self.limites.reconexao_min_s, self.limites.reconexao_max_s)
            self._proxima_tentativa = time.monotonic() + espera
            self._definir_estado(EstadoLeitor.OFFLINE, falha)
            self._acordar.clear()
            self._acordar.wait(espera)
            self._proxima_tentativa = 0.0
        self._definir_estado(EstadoLeitor.PARADO, "parado")

    def _sessao(self, descricao: str) -> str | None:
        """Abre a fonte e lê até falhar. Devolve a mensagem de falha (``None`` = fim normal)."""
        captura = abrir_captura(self.fonte, self.limites.abertura_s, self.limites.leitura_s)
        try:
            if not captura.isOpened():
                return "não foi possível abrir o vídeo"
            eh_arquivo = isinstance(self.fonte, str) and "://" not in self.fonte
            intervalo = 0.0
            if eh_arquivo:
                fps_arquivo = captura.get(cv2.CAP_PROP_FPS)
                intervalo = 1.0 / fps_arquivo if 0 < fps_arquivo <= 240 else 1.0 / 25
            proximo = time.monotonic()
            numero = self._numero_atual()
            log.debug("[%s] lendo %s", self.nome, descricao)
            self._relogio.reiniciar()  # sessão nova: a hora da câmera recomeça

            while not self._parar.is_set():
                ok, imagem = captura.read()
                if not ok or imagem is None:
                    if eh_arquivo and self.arquivo_em_loop and numero > 0:
                        captura.set(cv2.CAP_PROP_POS_FRAMES, 0)  # volta ao início
                        ok, imagem = captura.read()
                    if not ok or imagem is None:
                        return None if eh_arquivo and not self.arquivo_em_loop else "conexão perdida"
                agora = time.monotonic()
                numero += 1
                pts_ms = captura.get(cv2.CAP_PROP_POS_MSEC)  # hora do quadro no vídeo (RTSP: da câmera)
                self._registrar(Quadro(imagem, numero, agora, pts_ms / 1000 if pts_ms >= 0 else None))
                if intervalo:  # arquivo: toca na velocidade original, não o mais rápido possível
                    proximo = max(proximo + intervalo, agora - intervalo)
                    if self._parar.wait(max(0.0, proximo - time.monotonic())):
                        break
            return None
        except cv2.error as erro:
            return f"erro do OpenCV: {erro}"
        finally:
            captura.release()

    def _registrar(self, quadro: Quadro) -> None:
        exibir = self._relogio.quando_exibir(quadro.pts, quadro.instante, self.suavizar_s)
        quadro = replace(quadro, exibir_em=exibir)
        limite = MAX_RECENTES_GRANDES if quadro.imagem.shape[0] * quadro.imagem.shape[1] > PIXELS_GRANDE else MAX_RECENTES
        with self._trava:
            self._ultimo = quadro
            self._recentes.append(quadro)
            while len(self._recentes) > limite:
                self._recentes.popleft()
            self._instantes.append(quadro.instante)
            while quadro.instante - self._instantes[0] > JANELA_FPS_S:
                self._instantes.popleft()
            assinantes = self._assinantes
        for funcao in assinantes:
            try:
                funcao(quadro)
            except Exception:  # noqa: BLE001 (um assinante com defeito não pode parar a leitura)
                log.exception("[%s] assinante de quadros falhou", self.nome)
        if self._estado is not EstadoLeitor.ONLINE:
            altura, largura = quadro.imagem.shape[:2]
            self._definir_estado(EstadoLeitor.ONLINE, f"{largura}x{altura}")
