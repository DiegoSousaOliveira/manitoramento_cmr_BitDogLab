"""Rastreamento por vista com ByteTrack (biblioteca ``supervision``) + regra do DESCONHECIDO.

Por que um rastreador POR VISTA? O rastreador associa as detecções de um quadro
às do quadro anterior (pela sobreposição das caixas) e dá a cada objeto um ID
estável ("PESSOA #7"). Se duas câmeras dividissem o mesmo rastreador, a pessoa da
rua e o carro da garagem disputariam os mesmos IDs. Por isso NÃO usamos
``model.track(persist=True)`` do Ultralytics, que guarda um único rastreador.

Regra do DESCONHECIDO (especificação):
- detecção do YOLO com confiança abaixo de ``conf_minima`` -> DESCONHECIDO;
- região de movimento (MOG2) sem nenhum objeto em cima -> DESCONHECIDO.

Estabilidade (o YOLO às vezes "perde" um objeto por um quadro ou dois):
- **Classe estável por ID**: um objeto já reconhecido (ex.: CARRO) continua CARRO
  mesmo se num quadro a confiança cair.
- **Manter na tela**: um objeto que o YOLO deixou de ver continua sendo informado
  por até ``manter_s`` segundos, com ``ausente_s > 0`` (a tela desenha mais apagado).
  Sem isso, uma falha de 1 quadro (166 ms) fazia a caixa "piscar".
- **Mancha de movimento de objeto conhecido**: se o YOLO falha mas o MOG2 vê a
  mancha do carro (com a sombra, maior que a caixa), ela pertence ao carro visto há
  pouco — não vira um DESCONHECIDO novo.
- **Reassociação**: se o ByteTrack criar um ID novo em cima de um objeto
  recém-perdido (acontece quando a caixa muda muito de forma), o objeto mantém o ID,
  a classe e o "tempo na cena" antigos — importante para o "parado há 8 s" (Fase 5).

Os IDs mostrados ao usuário são nossos (1, 2, 3... por vista), não os do ByteTrack.
"""

from __future__ import annotations

import warnings
from dataclasses import dataclass, replace

import numpy as np
import supervision as sv

from core.modelos import CLASSES, CLASSES_COCO, DESCONHECIDO, Deteccao
from vision.motion import Caixa, interseccao_sobre_area

# O supervision avisa que o ByteTrack sairá na versão 0.31 (irá para o pacote "trackers").
# A especificação pede o supervision.ByteTrack; por isso o requirements.txt fixa supervision<0.31
# e aqui só silenciamos o aviso, que encheria o log.
warnings.filterwarnings("ignore", message=".*ByteTrack.*deprecated.*", category=FutureWarning)

#: O ByteTrack trabalha em "pixels"; usamos coordenadas normalizadas x 1000.
ESCALA = 1000.0


@dataclass(frozen=True)
class DeteccaoBruta:
    """Saída do YOLO antes do rastreamento."""

    classe_coco: int
    confianca: float
    caixa: Caixa  # normalizada (0 a 1)


@dataclass(frozen=True)
class AjustesRastreio:
    ia_fps: float
    perdido_s: float  # tempo que um ID sobrevive sem ser visto
    conf_minima: float  # abaixo disso: DESCONHECIDO
    conf_deteccao: float  # menor confiança que o YOLO devolve
    conf_movimento: float  # confiança dada ao DESCONHECIDO de movimento
    sobreposicao: float  # fração de uma região de movimento coberta por um objeto
    manter_s: float  # objeto que o YOLO deixou de ver continua na tela por este tempo
    reassociar: float  # fração de uma caixa dentro da outra para considerar o mesmo objeto


def mesmo_lugar(a: Caixa, b: Caixa, fracao: float) -> bool:
    """Uma caixa está (quase toda) dentro da outra? Ex.: o carro dentro da mancha de movimento."""
    return interseccao_sobre_area(a, b) >= fracao or interseccao_sobre_area(b, a) >= fracao


class RastreadorVista:
    def __init__(self, ajustes: AjustesRastreio) -> None:
        self.ajustes = ajustes
        # Duas pegadinhas do ByteTrack do supervision (verificadas no código-fonte dele):
        # 1) "lost_track_buffer" é contado em quadros A 30 FPS e reescalado por
        #    frame_rate/30. Para o ID sobreviver ``perdido_s`` segundos com a IA a
        #    6 FPS, o valor certo é perdido_s * 30 (e não perdido_s * 6, que dava 0,5 s).
        # 2) Um objeto NOVO só ganha ID se a confiança for >= limiar de ativação + 0,1.
        #    Usamos limiar = conf_deteccao - 0,1 para qualquer detecção que o YOLO devolve
        #    poder começar um rastro (as fracas viram DESCONHECIDO).
        self._tracker = sv.ByteTrack(
            track_activation_threshold=max(0.01, ajustes.conf_deteccao - 0.1),
            lost_track_buffer=max(1, round(ajustes.perdido_s * 30)),
            frame_rate=max(1, round(ajustes.ia_fps)),
            minimum_consecutive_frames=1,
        )
        self._proximo_id = 1
        self._apelido: dict[int, int] = {}  # ID do ByteTrack -> nosso ID
        self._ultima: dict[int, tuple[Deteccao, float]] = {}  # nosso ID -> (última detecção real, instante)
        self._desde: dict[int, float] = {}  # nosso ID -> quando apareceu
        self._classe: dict[int, str] = {}  # nosso ID -> última classe reconhecida (não DESCONHECIDO)

    # ------------------------------------------------------------ regras de classe
    def classificar(
        self, brutas: list[DeteccaoBruta], movimento: list[Caixa], recentes: list[Caixa] | None = None
    ) -> list[tuple[Caixa, float, str, str]]:
        """Aplica as regras de classe e de DESCONHECIDO (sem rastrear): (caixa, conf, classe, origem).

        ``recentes``: caixas de objetos reconhecidos há pouco (explicam manchas de movimento
        mesmo quando o YOLO falha neste quadro).
        """
        itens: list[tuple[Caixa, float, str, str]] = []
        objetos: list[Caixa] = list(recentes or [])
        for bruta in brutas:
            classe = CLASSES_COCO.get(bruta.classe_coco)
            if classe is None:
                continue  # classe COCO que não usamos (ex.: cadeira)
            if bruta.confianca < self.ajustes.conf_minima:
                classe = DESCONHECIDO
            itens.append((bruta.caixa, bruta.confianca, classe, "ia"))
            objetos.append(bruta.caixa)
        for regiao in movimento:
            coberta = any(
                interseccao_sobre_area(regiao, objeto) >= self.ajustes.sobreposicao
                or interseccao_sobre_area(objeto, regiao) >= self.ajustes.reassociar  # objeto dentro da mancha
                for objeto in objetos
            )
            if not coberta:
                itens.append((regiao, self.ajustes.conf_movimento, DESCONHECIDO, "movimento"))
        return itens

    # ------------------------------------------------------------ rastreamento
    def atualizar(self, brutas: list[DeteccaoBruta], movimento: list[Caixa], instante: float) -> list[Deteccao]:
        """Um quadro analisado -> detecções com ID estável (inclui as mantidas na tela)."""
        recentes = [
            deteccao.caixa
            for deteccao, visto in self._ultima.values()
            if instante - visto <= self.ajustes.manter_s and deteccao.classe != DESCONHECIDO
        ]
        itens = self.classificar(brutas, movimento, recentes)
        if itens:
            entrada = sv.Detections(
                xyxy=np.array([item[0] for item in itens], dtype=np.float32) * ESCALA,
                confidence=np.array([item[1] for item in itens], dtype=np.float32),
                class_id=np.array([CLASSES.index(item[2]) for item in itens], dtype=int),
                # "data" acompanha cada detecção através do rastreador
                data={"movimento": np.array([item[3] == "movimento" for item in itens], dtype=bool)},
            )
        else:
            entrada = sv.Detections.empty()
        saida = self._tracker.update_with_detections(entrada)

        caixas = [self._caixa(saida.xyxy[i]) for i in range(len(saida))]
        ids_tracker = [int(saida.tracker_id[i]) for i in range(len(saida))]
        # IDs que continuam com o mesmo dono neste quadro não podem ser "herdados" por outros.
        ocupados = {self._apelido[t] for t in ids_tracker if t in self._apelido}

        deteccoes: list[Deteccao] = []
        vistos: set[int] = set()
        for i, id_tracker in enumerate(ids_tracker):
            nosso = self._apelido.get(id_tracker)
            if nosso is None:
                nosso = self._reassociar(caixas[i], instante, ocupados) or self._novo_id()
                self._apelido[id_tracker] = nosso
                ocupados.add(nosso)
            classe = CLASSES[int(saida.class_id[i])]
            if classe != DESCONHECIDO:
                self._classe[nosso] = classe
            else:
                classe = self._classe.get(nosso, DESCONHECIDO)
            confianca = float(saida.confidence[i]) if saida.confidence is not None else 0.0
            de_movimento = bool(saida.data["movimento"][i]) if "movimento" in saida.data else False
            deteccao = Deteccao(classe, confianca, caixas[i], nosso, "movimento" if de_movimento else "ia")
            self._desde.setdefault(nosso, instante)
            self._ultima[nosso] = (deteccao, instante)
            vistos.add(nosso)
            deteccoes.append(deteccao)

        # Manter na tela, por pouco tempo, quem o YOLO deixou de ver neste quadro.
        for nosso, (deteccao, visto) in self._ultima.items():
            ausente = instante - visto
            if nosso not in vistos and 0 < ausente <= self.ajustes.manter_s:
                deteccoes.append(replace(deteccao, ausente_s=ausente))
        self._esquecer_antigos(instante)
        return deteccoes

    def desde(self, id_rastreio: int) -> float | None:
        """Quando o objeto apareceu (``time.monotonic()``), para o "tempo na cena"."""
        return self._desde.get(id_rastreio)

    # ------------------------------------------------------------ detalhes
    @staticmethod
    def _caixa(xyxy: np.ndarray) -> Caixa:
        x1, y1, x2, y2 = (float(v) / ESCALA for v in xyxy)
        return (max(0.0, x1), max(0.0, y1), min(1.0, x2), min(1.0, y2))

    def _novo_id(self) -> int:
        novo = self._proximo_id
        self._proximo_id += 1
        return novo

    def _reassociar(self, caixa: Caixa, instante: float, ocupados: set[int]) -> int | None:
        """Objeto recém-perdido (até ``perdido_s``) no mesmo lugar desta caixa nova -> o ID dele."""
        melhor, maior = None, 0.0
        for nosso, (deteccao, visto) in self._ultima.items():
            if nosso in ocupados or not 0 < instante - visto <= self.ajustes.perdido_s:
                continue
            if mesmo_lugar(caixa, deteccao.caixa, self.ajustes.reassociar):
                cobertura = max(
                    interseccao_sobre_area(caixa, deteccao.caixa), interseccao_sobre_area(deteccao.caixa, caixa)
                )
                if cobertura > maior:
                    melhor, maior = nosso, cobertura
        return melhor

    def _esquecer_antigos(self, agora: float) -> None:
        limite = self.ajustes.perdido_s * 3
        for nosso in [i for i, (_d, t) in self._ultima.items() if agora - t > limite]:
            self._ultima.pop(nosso, None)
            self._desde.pop(nosso, None)
            self._classe.pop(nosso, None)
        for id_tracker in [t for t, nosso in self._apelido.items() if nosso not in self._ultima]:
            del self._apelido[id_tracker]
