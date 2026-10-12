"""O PROFESSOR: um modelo grande (ex.: YOLO11x) rotula as imagens coletadas.

Por que um professor? Se o modelo pequeno (o da câmera) aprendesse com as próprias respostas,
ele reforçaria os próprios erros — o que ele não vê nunca viraria exemplo. O professor é bem
maior e mais preciso (enxerga a moto lá no fundo); ele só roda durante o treino, então o
peso dele não atrapalha o dia a dia.

Regra de segurança: a imagem só vai para o treino se o professor tiver CERTEZA de tudo nela.
Qualquer detecção "na dúvida" manda a imagem para a sua revisão. Imagem sem nenhum objeto
também serve (ensina o que NÃO é objeto: folhas, sombras...).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

import cv2
import numpy as np

from treino.dados import PENDENTES, REVISAR, ROTULADAS, ConjuntoTreino, Rotulo, rotulo_de_caixa
from vision.rastreador import DeteccaoBruta

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Decisao:
    destino: str  # ROTULADAS (pronta para o treino) ou REVISAR
    rotulos: list[Rotulo]  # as certas + as duvidosas (na revisão você confirma ou apaga)
    duvidas: int


def decidir(deteccoes: list[DeteccaoBruta], conf_certa: float, conf_duvida: float) -> Decisao:
    """Certeza em tudo -> treino; alguma dúvida -> revisão. Abaixo de ``conf_duvida``: ignorado."""
    certas = [d for d in deteccoes if d.confianca >= conf_certa]
    duvidosas = [d for d in deteccoes if conf_duvida <= d.confianca < conf_certa]
    rotulos = [rotulo_de_caixa(d.classe_coco, d.caixa) for d in certas + duvidosas]
    return Decisao(REVISAR if duvidosas else ROTULADAS, rotulos, len(duvidosas))


def ler_imagem(caminho: str) -> np.ndarray | None:
    """``cv2.imread`` falha com acentos no caminho (Windows): lê os bytes e decodifica."""
    try:
        dados = np.fromfile(caminho, dtype=np.uint8)
    except OSError:
        return None
    return cv2.imdecode(dados, cv2.IMREAD_COLOR) if dados.size else None


Detectar = Callable[[list[np.ndarray]], list[list[DeteccaoBruta]]]


def rotular_pendentes(
    conjunto: ConjuntoTreino,
    detectar: Detectar,
    conf_certa: float,
    conf_duvida: float,
    nome_professor: str,
    avisar: Callable[[int, int], None] | None = None,
) -> dict[str, int]:
    """Passa todas as pendentes pelo professor. Devolve quantas foram para cada lado."""
    pendentes = conjunto.amostras(PENDENTES)
    contagem = {ROTULADAS: 0, REVISAR: 0, "ilegiveis": 0}
    for indice, amostra in enumerate(pendentes, start=1):
        imagem = ler_imagem(str(amostra.imagem))
        if imagem is None:
            log.warning("Imagem de treino ilegível, apagada: %s", amostra.imagem.name)
            conjunto.apagar(amostra)
            contagem["ilegiveis"] += 1
            continue
        decisao = decidir(detectar([imagem])[0], conf_certa, conf_duvida)
        amostra.gravar_rotulos(decisao.rotulos)
        amostra.atualizar_meta(
            rotulo="professor", professor=nome_professor, duvidas=decisao.duvidas,
            rotulada_em=datetime.now().isoformat(timespec="seconds"),
        )
        conjunto.mover(amostra, decisao.destino)
        contagem[decisao.destino] += 1
        if avisar is not None:
            avisar(indice, len(pendentes))
    return contagem
