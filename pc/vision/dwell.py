"""Regra de "parado na zona": decide quando um objeto rastreado vira ALERTA.

Como funciona (uma ``RegraParado`` por vista = canal x lente):

- **Relógio de parado.** Cada ID com o centro DENTRO da zona ganha uma "âncora":
  o ponto onde ele começou a ficar parado. Enquanto o centro não se afasta mais que
  ``limite_movimento`` (5% da largura da imagem) da âncora, o relógio corre; se
  afastar, a âncora muda para o ponto novo e o relógio zera.
- **ALERTA**: parado por ``tempo_parado[classe]`` segundos (PESSOA 8, CARRO 30...).
  Uma vez por ID; o mesmo ID só alerta de novo depois de ``cooldown_s``.
- **LIMPAR**: o objeto alertado sai da zona ou some por mais de ``limpar_apos_sumico_s``.
- **PASSOU**: o objeto atravessou a zona sem alertar (no máximo 1 por ID). Se ele
  SUMIR dentro da zona, só conta como saída depois de ``limpar_apos_sumico_s`` (pode
  ser só alguém passando na frente).
- **Oclusão**: o ID pode sumir até ``tolerancia_sumico_s`` sem zerar o relógio.
- **Aquecimento**: o que aparece nos primeiros ``aquecimento_s`` depois de a câmera
  conectar (ex.: o carro do vizinho estacionado) é ignorado ATÉ SE MOVER.
- **Troca de ID**: se o rastreador perde um objeto parado por muito tempo e ele volta
  com outro ID no mesmo lugar, o ID novo herda a situação do antigo (alerta ativo,
  "ignorado", cooldown) — assim a mesma pessoa não gera um 2º alerta.

Objetos que a IA deixou de ver há instantes (``Deteccao.ausente_s > 0``) contam como
presentes. Tudo aqui é lógica pura: os testes simulam trilhas de posições.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import StrEnum
from typing import Any

import numpy as np

from core.config import Config
from core.modelos import CLASSES, DESCONHECIDO, Camera, Deteccao, Nivel
from vision.zona import Poligono, Ponto, centro, distancia, ponto_no_poligono

Chave = tuple[int, int]
Caixa = tuple[float, float, float, float]

SEM_ZONA_IGNORAR = "ignorar"  # lente sem zona não gera alerta (padrão desde 12/10/2026)
SEM_ZONA_IMAGEM_INTEIRA = "imagem_inteira"  # lente sem zona vigia a imagem inteira (como era antes)


# ===================================================================== ajustes
@dataclass(frozen=True)
class AjustesParado:
    limite_movimento: float  # deslocamento máximo do centro, em larguras da imagem
    cooldown_s: float
    aquecimento_s: float
    tolerancia_sumico_s: float
    limpar_apos_sumico_s: float
    passou_min_s: float
    passou_desconhecido: bool
    tempo_parado: dict[str, float]
    nivel: dict[str, Nivel]
    #: Classes que geram alerta (falta = sim). As desligadas só aparecem na tela.
    alertar: dict[str, bool] = field(default_factory=dict)
    #: Lente SEM zona desenhada: "ignorar" (não alerta) ou "imagem_inteira" (vigia tudo).
    sem_zona: str = SEM_ZONA_IMAGEM_INTEIRA

    @classmethod
    def do_config(cls, config: Config, camera: Camera | None = None) -> AjustesParado:
        """Lê a seção ``parado`` do config.yaml; ``camera.ajustes`` sobrescreve por câmera.

        Ex.: ``camera.ajustes["parado.cooldown_s"] = 60`` ou
        ``camera.ajustes["parado.tempo_parado"] = {"CARRO": 60}`` (só as classes mudadas).
        """
        ajustes = camera.ajustes if camera is not None else {}

        def valor(nome: str) -> Any:
            chave = f"parado.{nome}"
            return ajustes[chave] if chave in ajustes else config.obter(chave)

        tempos = {str(k): float(v) for k, v in dict(config.obter("parado.tempo_parado")).items()}
        tempos.update({str(k): float(v) for k, v in dict(ajustes.get("parado.tempo_parado", {})).items()})
        niveis_texto = {str(k): str(v) for k, v in dict(config.obter("parado.nivel")).items()}
        niveis_texto.update({str(k): str(v) for k, v in dict(ajustes.get("parado.nivel", {})).items()})
        alertar = {str(k): bool(v) for k, v in dict(config.obter_ou("parado.alertar", {})).items()}
        alertar.update({str(k): bool(v) for k, v in dict(ajustes.get("parado.alertar", {})).items()})
        faltando = [c for c in CLASSES if c not in tempos or c not in niveis_texto]
        if faltando:
            raise ValueError(f"parado.tempo_parado / parado.nivel sem as classes: {', '.join(faltando)}")
        try:
            niveis = {c: Nivel[n.strip().upper()] for c, n in niveis_texto.items()}
        except KeyError as erro:
            raise ValueError(f"Nível inválido em parado.nivel: {erro} (use BAIXO, MEDIO ou ALTO)") from None
        return cls(
            limite_movimento=float(valor("limite_movimento")),
            cooldown_s=float(valor("cooldown_s")),
            aquecimento_s=float(valor("aquecimento_s")),
            tolerancia_sumico_s=float(valor("tolerancia_sumico_s")),
            limpar_apos_sumico_s=float(valor("limpar_apos_sumico_s")),
            passou_min_s=float(valor("passou_min_s")),
            passou_desconhecido=bool(valor("passou_desconhecido")),
            tempo_parado=tempos,
            nivel=niveis,
            alertar=alertar,
            sem_zona=str(config.obter_ou("parado.sem_zona", SEM_ZONA_IGNORAR)),
        )

    def alerta_ligado(self, classe: str) -> bool:
        return self.alertar.get(classe, True)

    def tempo(self, classe: str) -> float:
        return self.tempo_parado.get(classe, self.tempo_parado[DESCONHECIDO])

    def nivel_da(self, classe: str) -> Nivel:
        return self.nivel.get(classe, self.nivel[DESCONHECIDO])


# ===================================================================== saídas
class TipoAviso(StrEnum):
    ALERTA = "ALERTA"  # objeto parado na zona
    LIMPAR = "LIMPAR"  # o objeto alertado saiu / sumiu
    PASSOU = "PASSOU"  # atravessou a zona sem parar


@dataclass(frozen=True)
class Aviso:
    """Algo que a regra decidiu. O LIMPAR leva o MESMO id do ALERTA que ele encerra."""

    tipo: TipoAviso
    chave: Chave  # (câmera, vista)
    id_rastreio: int
    classe: str
    instante: float  # time.monotonic()
    segundos: float = 0.0  # ALERTA: tempo parado
    confianca: float = 0.0
    nivel: Nivel = Nivel.BAIXO
    caixa: Caixa = (0.0, 0.0, 0.0, 0.0)
    motivo: str = ""  # LIMPAR: "saiu", "sumiu" ou "camera"
    # Preenchidos pelo serviço de alertas (a regra não conhece relógio nem imagem):
    quando: datetime | None = None
    miniatura: np.ndarray | None = field(default=None, compare=False, repr=False)


@dataclass(frozen=True)
class Progresso:
    """Relógio de um objeto dentro da zona (para o "parado 6/8 s" na tela)."""

    segundos: float
    alvo: float
    alertado: bool


@dataclass(frozen=True)
class SituacaoParado:
    """Foto instantânea de uma vista para a interface (imutável: pode ir entre threads)."""

    versao: int
    progresso: dict[int, Progresso] = field(default_factory=dict)
    alerta: bool = False  # há alerta ativo nesta vista (borda vermelha)


# ===================================================================== regra
@dataclass
class _Trilha:
    id: int
    classe: str
    confianca: float
    caixa: Caixa
    origem: Ponto  # onde foi visto pela 1ª vez (aquecimento: "ignorado até se mover")
    visto: float
    dentro: bool = False
    entrou: float | None = None  # quando entrou na zona (visita atual)
    ancora: Ponto | None = None
    parado_desde: float | None = None
    ignorado: bool = False
    alerta: Aviso | None = None  # alerta ativo
    ultimo_alerta: float | None = None
    passou: bool = False


class RegraParado:
    """Regra de uma vista. Chame ``atualizar`` a cada resultado da IA e ``expirar``
    periodicamente (para limpar alertas quando a câmera para de mandar imagem)."""

    def __init__(self, chave: Chave, ajustes: AjustesParado, zona: Poligono | None = None) -> None:
        self.chave = chave
        self.ajustes = ajustes
        self.zona = zona
        self._trilhas: dict[int, _Trilha] = {}
        self._memoria: list[_Trilha] = []  # trilhas encerradas que estavam paradas (herança de ID)
        self._inicio: float | None = None
        self._ultimo: float | None = None

    # ------------------------------------------------------------ entrada
    @property
    def vigiando(self) -> bool:
        """Esta vista gera alertas? Sem zona desenhada, depende de ``sem_zona``."""
        return bool(self.zona) or self.ajustes.sem_zona == SEM_ZONA_IMAGEM_INTEIRA

    def atualizar(self, deteccoes: Iterable[Deteccao], instante: float, proporcao: float = 1.0) -> list[Aviso]:
        """Processa as detecções de um quadro. ``proporcao`` = altura / largura da imagem.

        Só entram as classes com alerta ligado, e nada entra numa lente sem zona (se ``sem_zona``
        = "ignorar"): para a regra, é como se não houvesse ninguém ali (alertas antigos são limpos).
        """
        a = self.ajustes
        deteccoes = [d for d in deteccoes if self.vigiando and a.alerta_ligado(d.classe)]
        if self._ultimo is not None:
            if instante - self._ultimo > a.limpar_apos_sumico_s:
                self._inicio = instante  # a câmera ficou sem imagem: é como se tivesse reconectado
            instante = max(instante, self._ultimo)
        if self._inicio is None:
            self._inicio = instante
        self._ultimo = instante

        avisos: list[Aviso] = []
        presentes: set[int] = set()
        for d in deteccoes:
            if d.id_rastreio is None or d.id_rastreio in presentes:
                continue
            presentes.add(d.id_rastreio)
            trilha = self._trilhas.get(d.id_rastreio)
            if trilha is None:
                trilha = self._nova_trilha(d, instante, proporcao, presentes)
            avisos += self._observar(trilha, d, instante, proporcao)
        avisos += self.expirar(instante, presentes)
        return avisos

    def expirar(self, agora: float, presentes: set[int] | frozenset[int] = frozenset()) -> list[Aviso]:
        """Trata os objetos que não estão sendo vistos (sumiço, LIMPAR, esquecer)."""
        a = self.ajustes
        avisos: list[Aviso] = []
        for trilha in list(self._trilhas.values()):
            if trilha.id in presentes:
                continue
            ausente = agora - trilha.visto
            if trilha.alerta is not None and ausente > a.limpar_apos_sumico_s:
                avisos.append(self._limpar(trilha, agora, "sumiu"))
            if ausente > max(a.tolerancia_sumico_s, a.limpar_apos_sumico_s):
                if trilha.dentro:  # sumiu dentro da zona: agora sim conta como saída (PASSOU)
                    avisos += self._saiu(trilha, "sumiu")
                del self._trilhas[trilha.id]
                if trilha.ignorado or trilha.ultimo_alerta is not None:
                    self._memoria.append(trilha)
        self._memoria = [t for t in self._memoria if agora - t.visto <= a.cooldown_s]
        return avisos

    def reiniciar(self, agora: float) -> None:
        """A câmera girou (PTZ): tudo na imagem "andou". Recomeça o aquecimento.

        O que for visto nos próximos ``aquecimento_s`` (o carro estacionado, agora em outro
        lugar da imagem) fica ignorado até se mover. Alertas ativos continuam (somem do jeito
        normal, se o objeto sair ou sumir).
        """
        self._inicio = agora
        self._trilhas = {i: t for i, t in self._trilhas.items() if t.alerta is not None}
        self._memoria.clear()

    def encerrar(self, agora: float, motivo: str = "camera") -> list[Aviso]:
        """A vista deixou de ser vigiada (câmera removida, detecção desligada): LIMPAR tudo."""
        avisos = [self._limpar(t, agora, motivo) for t in self._trilhas.values() if t.alerta is not None]
        self._trilhas.clear()
        self._memoria.clear()
        return avisos

    # ------------------------------------------------------------ consultas
    def progresso(self, agora: float | None = None) -> dict[int, Progresso]:
        """Relógio dos objetos dentro da zona sendo contados (ou alertados) e vistos há pouco."""
        saida: dict[int, Progresso] = {}
        for t in self._trilhas.values():
            if not t.dentro or t.parado_desde is None:
                continue
            if agora is not None and agora - t.visto > self.ajustes.tolerancia_sumico_s:
                continue
            if t.ignorado and t.alerta is None:
                continue
            saida[t.id] = Progresso(t.visto - t.parado_desde, self.ajustes.tempo(t.classe), t.alerta is not None)
        return saida

    def ativos(self) -> list[Aviso]:
        """Alertas ativos (ALERTA ainda sem LIMPAR)."""
        return [t.alerta for t in self._trilhas.values() if t.alerta is not None]

    # ------------------------------------------------------------ interno
    def _nova_trilha(self, d: Deteccao, instante: float, proporcao: float, presentes: set[int]) -> _Trilha:
        ponto = centro(d.caixa)
        assert d.id_rastreio is not None and self._inicio is not None
        trilha = _Trilha(
            id=d.id_rastreio,
            classe=d.classe,
            confianca=d.confianca,
            caixa=d.caixa,
            origem=ponto,
            visto=instante,
            ignorado=instante - self._inicio < self.ajustes.aquecimento_s,
        )
        if (antecessor := self._antecessor(d.classe, ponto, proporcao, presentes)) is not None:
            trilha.ignorado = trilha.ignorado or antecessor.ignorado
            trilha.alerta, trilha.ultimo_alerta = antecessor.alerta, antecessor.ultimo_alerta
            antecessor.alerta = None  # o alerta passa para o ID novo (sem LIMPAR no meio)
            self._trilhas.pop(antecessor.id, None)
            if antecessor in self._memoria:
                self._memoria.remove(antecessor)
        self._trilhas[trilha.id] = trilha
        return trilha

    def _antecessor(self, classe: str, ponto: Ponto, proporcao: float, presentes: set[int]) -> _Trilha | None:
        """Objeto PARADO que sumiu neste mesmo lugar (mesma classe): o ID novo é ele de volta."""
        candidatas = [
            t
            for t in (*self._trilhas.values(), *self._memoria)
            if t.id not in presentes
            and (t.alerta is not None or t.ultimo_alerta is not None or t.ignorado)
            and (t.classe == classe or DESCONHECIDO in (t.classe, classe))
            and distancia(centro(t.caixa), ponto, proporcao) <= self.ajustes.limite_movimento
        ]
        return min(candidatas, key=lambda t: distancia(centro(t.caixa), ponto, proporcao), default=None)

    def _observar(self, t: _Trilha, d: Deteccao, instante: float, proporcao: float) -> list[Aviso]:
        a = self.ajustes
        avisos: list[Aviso] = []
        if d.classe != DESCONHECIDO or t.classe == DESCONHECIDO:
            t.classe = d.classe  # um ID já reconhecido não volta a ser DESCONHECIDO
        if d.ausente_s == 0:
            t.confianca = d.confianca
        ponto = centro(d.caixa)
        if instante - t.visto > a.tolerancia_sumico_s:
            t.ancora = t.parado_desde = None  # sumiu tempo demais: o relógio recomeça
        t.caixa, t.visto = d.caixa, instante
        if t.ignorado and distancia(ponto, t.origem, proporcao) > a.limite_movimento:
            t.ignorado = False  # se moveu: deixou de ser "o carro do vizinho"

        if not ponto_no_poligono(ponto, self.zona):
            if t.dentro:
                avisos += self._saiu(t, "saiu")
            t.dentro, t.entrou, t.ancora, t.parado_desde = False, None, None, None
            return avisos

        if not t.dentro:  # entrou na zona: visita nova
            t.dentro, t.entrou = True, instante
        if t.ancora is None or distancia(ponto, t.ancora, proporcao) > a.limite_movimento:
            t.ancora, t.parado_desde = ponto, instante
        assert t.parado_desde is not None
        parado = instante - t.parado_desde
        pode = t.ultimo_alerta is None or instante - t.ultimo_alerta >= a.cooldown_s
        if t.alerta is None and not t.ignorado and pode and parado >= a.tempo(t.classe):
            t.alerta = Aviso(
                TipoAviso.ALERTA,
                self.chave,
                t.id,
                t.classe,
                instante,
                segundos=parado,
                confianca=t.confianca,
                nivel=a.nivel_da(t.classe),
                caixa=t.caixa,
            )
            t.ultimo_alerta = instante
            avisos.append(t.alerta)
        return avisos

    def _saiu(self, t: _Trilha, motivo: str) -> list[Aviso]:
        """Saiu da zona (``motivo`` "saiu") ou sumiu lá dentro ("sumiu")."""
        a = self.ajustes
        if t.alerta is not None:
            return [self._limpar(t, t.visto, motivo)]
        entrou = t.entrou if t.entrou is not None else t.visto
        alertou_na_visita = t.ultimo_alerta is not None and t.ultimo_alerta >= entrou
        if (
            t.passou
            or alertou_na_visita
            or t.visto - entrou < a.passou_min_s
            or (t.classe == DESCONHECIDO and not a.passou_desconhecido)
        ):
            return []
        t.passou = True
        return [
            Aviso(TipoAviso.PASSOU, self.chave, t.id, t.classe, t.visto, confianca=t.confianca,
                  nivel=a.nivel_da(t.classe), caixa=t.caixa)
        ]

    @staticmethod
    def _limpar(t: _Trilha, instante: float, motivo: str) -> Aviso:
        alerta = t.alerta
        assert alerta is not None
        t.alerta = None
        return replace(alerta, tipo=TipoAviso.LIMPAR, instante=instante, motivo=motivo, quando=None, miniatura=None)
