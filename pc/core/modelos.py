"""Modelos de dados do sistema: ``Camera``, ``Vista``, ``Evento`` e ``Deteccao``.

São ``dataclasses``: classes simples que só guardam dados (o Python gera
``__init__``, ``__repr__`` e comparação automaticamente). Elas não sabem nada
de banco nem de interface — quem converte para SQLite é o ``core/banco.py``.

Convenções:
- Coordenadas de caixas e zonas são **normalizadas** (0 a 1), independentes da
  resolução. Assim a mesma caixa detectada no substream (ex.: 640x360) é
  desenhada no stream principal (ex.: 1920x1080) só multiplicando pela largura/altura.
- Datas em horário local do PC.
- Canais são numerados a partir de 1, como no parâmetro ``channel`` das iCSee.

Canal x lente x vista (importante!):
- **canal**: um fluxo RTSP separado (``channel=1``, ``channel=2``...).
- **lente**: algumas câmeras de duas lentes mandam as DUAS imagens num único
  fluxo, empilhadas (ex.: iCSee XM232: 640x720 = duas imagens 640x360). O app
  corta o quadro ao meio — ver ``DisposicaoLentes``.
- **vista**: o que vira um bloco no mosaico = canal x lente. Zonas, detecção,
  eventos e alertas são sempre POR VISTA.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import IntEnum, StrEnum
from typing import TYPE_CHECKING, Any

from core.url_rtsp import montar_url

if TYPE_CHECKING:
    import numpy as np


class TipoFonte(StrEnum):
    """De onde vem o vídeo da câmera."""

    RTSP = "rtsp"  # câmera de verdade na rede
    ARQUIVO = "arquivo"  # vídeo .mp4 tocando em loop ("câmera falsa")
    WEBCAM = "webcam"  # webcam do PC ("câmera falsa")


class DisposicaoLentes(StrEnum):
    """Como as lentes aparecem dentro de UMA imagem do fluxo."""

    UNICA = "unica"  # a imagem inteira é uma lente só
    VERTICAL = "vertical"  # duas lentes empilhadas: 1 = de cima, 2 = de baixo
    HORIZONTAL = "horizontal"  # duas lentes lado a lado: 1 = esquerda, 2 = direita

    @property
    def quantidade(self) -> int:
        return 1 if self is DisposicaoLentes.UNICA else 2

    def regiao(self, lente: int) -> tuple[float, float, float, float]:
        """Retângulo normalizado (x1, y1, x2, y2) da lente dentro da imagem."""
        if self is DisposicaoLentes.UNICA:
            return (0.0, 0.0, 1.0, 1.0)
        if self is DisposicaoLentes.VERTICAL:
            return (0.0, 0.0, 1.0, 0.5) if lente == 1 else (0.0, 0.5, 1.0, 1.0)
        return (0.0, 0.0, 0.5, 1.0) if lente == 1 else (0.5, 0.0, 1.0, 1.0)


class TipoStream(IntEnum):
    """Valor da variável ``{stream}`` no modelo de URL das iCSee."""

    PRINCIPAL = 0  # resolução máxima (Tela da câmera)
    SUBSTREAM = 1  # resolução baixa (mosaico, IA e gravação)


class Nivel(IntEnum):
    """Gravidade de um alerta. É ``IntEnum`` para poder comparar: ``ALTO > MEDIO``."""

    BAIXO = 1
    MEDIO = 2
    ALTO = 3


#: Classes do modelo COCO que usamos -> nome no sistema (MAIÚSCULO, sem acento).
CLASSES_COCO: dict[int, str] = {
    0: "PESSOA",
    1: "BICICLETA",
    2: "CARRO",
    3: "MOTO",
    5: "ONIBUS",
    7: "CAMINHAO",
    15: "GATO",
    16: "CACHORRO",
}

#: Movimento sem detecção do YOLO, ou detecção com confiança baixa.
DESCONHECIDO = "DESCONHECIDO"

#: Todas as classes possíveis, na ordem em que aparecem na interface.
CLASSES: tuple[str, ...] = (*CLASSES_COCO.values(), DESCONHECIDO)

#: Classe do sistema -> id COCO (para os rótulos de treino no formato YOLO).
COCO_DAS_CLASSES: dict[str, int] = {nome: indice for indice, nome in CLASSES_COCO.items()}

#: Cor de cada classe nas caixas, chips e fotos (vermelho é reservado para alertas).
CORES_CLASSES: dict[str, str] = {
    "PESSOA": "#FF8A3D",  # laranja
    "CARRO": "#3B82F6",  # azul
    "MOTO": "#6366F1",  # índigo
    "BICICLETA": "#14B8A6",  # verde-água
    "ONIBUS": "#EAB308",  # amarelo
    "CAMINHAO": "#84CC16",  # verde-limão
    "GATO": "#EC4899",  # rosa
    "CACHORRO": "#A855F7",  # roxo
    "DESCONHECIDO": "#94A3B8",  # cinza
}


@dataclass
class Camera:
    """Uma câmera cadastrada. O ``nome`` é único e identifica a câmera em todo o sistema.

    A senha NÃO fica aqui: ela é guardada no cofre do sistema (``core/segredos.py``)
    e só é lida na hora de montar a URL.
    """

    nome: str
    codigo: str  # código curto enviado à placa (até 10 caracteres, ex.: FRENTE)
    fonte: TipoFonte
    ip: str
    porta_rtsp: int
    usuario: str
    modelo_url: str  # modelo com {ip} {porta} {usuario} {senha} {canal} {stream}
    porta_onvif: int
    canais: int = 1  # fluxos RTSP separados (channel=1, 2...)
    lentes: DisposicaoLentes = DisposicaoLentes.UNICA  # lentes dentro de cada imagem
    caminho: str = ""  # fonte de teste: arquivo .mp4 ou índice da webcam
    tem_ptz: bool = False
    lente_ptz: int = 1  # qual lente o motor move (câmeras de duas lentes)
    deteccao: bool = True
    gravar: bool = True
    ajustes: dict[str, Any] = field(default_factory=dict)  # sobrescreve o config.yaml nesta câmera
    nomes_vistas: dict[int, str] = field(default_factory=dict)  # ex.: {1: "Rua", 2: "Calçada"}
    ordem: int = 0  # posição no mosaico
    id: int | None = None
    criada_em: datetime | None = None

    def url(self, canal: int, stream: TipoStream, senha: str = "") -> str:
        """Monta a URL RTSP de um canal/stream (só para ``fonte == RTSP``)."""
        return montar_url(
            self.modelo_url,
            ip=self.ip,
            porta=self.porta_rtsp,
            usuario=self.usuario,
            senha=senha,
            canal=canal,
            stream=int(stream),
        )

    @property
    def numeros_canais(self) -> range:
        """Números dos canais desta câmera: ``range(1, canais + 1)``."""
        return range(1, self.canais + 1)

    def vistas(self) -> list[Vista]:
        """Todas as vistas (blocos do mosaico): canais x lentes, numeradas a partir de 1."""
        por_canal = self.lentes.quantidade
        return [
            Vista(
                camera_id=self.id or 0,
                numero=(canal - 1) * por_canal + lente,
                canal=canal,
                lente=lente,
                regiao=self.lentes.regiao(lente),
            )
            for canal in self.numeros_canais
            for lente in range(1, por_canal + 1)
        ]

    def nome_vista(self, vista: Vista) -> str:
        """Nome mostrado no bloco: ``"Frente da casa"`` ou ``"Frente da casa · Calçada"``."""
        if vista.numero in self.nomes_vistas and self.nomes_vistas[vista.numero].strip():
            return f"{self.nome} · {self.nomes_vistas[vista.numero].strip()}"
        partes = []
        if self.canais > 1:
            partes.append(f"Canal {vista.canal}")
        if self.lentes.quantidade > 1:
            partes.append(f"Lente {vista.lente}")
        return " · ".join([self.nome, *partes])


@dataclass(frozen=True)
class Vista:
    """Um pedaço de imagem que vira um bloco no mosaico (canal x lente).

    ``frozen=True`` torna o objeto imutável e "hasheável": dá para usá-lo como
    chave de dicionário.
    """

    camera_id: int
    numero: int  # 1..N dentro da câmera — chave de zonas e eventos
    canal: int  # canal físico (parâmetro channel= da URL)
    lente: int  # lente dentro da imagem (1 = de cima/da esquerda)
    regiao: tuple[float, float, float, float]  # x1, y1, x2, y2 normalizados dentro da imagem do canal

    @property
    def chave(self) -> tuple[int, int]:
        """Identificador único no sistema: ``(camera_id, numero)``."""
        return (self.camera_id, self.numero)

    def recortar(self, imagem: np.ndarray) -> np.ndarray:
        """Recorta a lente desta vista (sem copiar memória: é uma "janela" do NumPy)."""
        if self.regiao == (0.0, 0.0, 1.0, 1.0):
            return imagem
        altura, largura = imagem.shape[:2]
        x1, y1, x2, y2 = self.regiao
        return imagem[round(y1 * altura) : round(y2 * altura), round(x1 * largura) : round(x2 * largura)]


@dataclass
class Deteccao:
    """Um objeto encontrado em um quadro."""

    classe: str
    confianca: float
    caixa: tuple[float, float, float, float]  # x1, y1, x2, y2 normalizados (0 a 1)
    id_rastreio: int | None = None  # ID dado pelo ByteTrack (estável entre quadros)
    origem: str = "ia"  # "ia" (YOLO) ou "movimento" (mancha do MOG2 sem objeto reconhecido)
    ausente_s: float = 0.0  # > 0: a IA deixou de ver há este tempo (mantido na tela por instantes)

    @property
    def centro(self) -> tuple[float, float]:
        x1, y1, x2, y2 = self.caixa
        return (x1 + x2) / 2, (y1 + y2) / 2

    def caixa_em_pixels(self, largura: int, altura: int) -> tuple[int, int, int, int]:
        """Converte a caixa para pixels de uma imagem com o tamanho informado."""
        x1, y1, x2, y2 = self.caixa
        return round(x1 * largura), round(y1 * altura), round(x2 * largura), round(y2 * altura)


@dataclass
class Evento:
    """Um evento gravado (movimento/detecção), com foto e clipe.

    ``pasta`` é relativa à pasta de eventos da câmera
    (``dados/eventos/<nome da câmera>/``), ex.: ``"2026-10-11/213512"``.
    Assim, renomear a câmera só exige renomear uma pasta — os registros continuam válidos.
    Os demais caminhos (``clipe``, ``foto``...) são relativos a ``pasta``.
    """

    camera_id: int
    inicio: datetime
    vista: int = 1
    fim: datetime | None = None
    classes: dict[str, int] = field(default_factory=dict)  # ex.: {"PESSOA": 2, "CARRO": 1}
    maior_conf: float = 0.0
    alerta: bool = False
    pasta: str = ""
    clipe: str = ""
    foto: str = ""
    foto_limpa: str = ""
    deteccoes: str = ""
    id: int | None = None

    @property
    def duracao_s(self) -> float:
        """Duração em segundos (0 enquanto o evento não terminou)."""
        return (self.fim - self.inicio).total_seconds() if self.fim else 0.0
