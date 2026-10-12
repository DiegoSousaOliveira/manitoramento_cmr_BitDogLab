"""Grade de câmeras (mosaico): um bloco 16:9 por vista, com páginas.

O mosaico posiciona os blocos à mão (``setGeometry``) usando ``ui/grade.py``,
o que garante 16:9 exato em qualquer tamanho de janela. Sempre que algo muda
(tamanho, página, layout, janela minimizada), ele informa ao distribuidor quais
blocos estão visíveis e em que tamanho — só esses recebem imagens.
"""

from __future__ import annotations

from PySide6.QtCore import QPoint, QSize, Signal
from PySide6.QtGui import QHideEvent, QImage, QResizeEvent, QShowEvent
from PySide6.QtWidgets import QWidget

from core.modelos import Vista
from ui import grade, tema
from ui.bloco_camera import BlocoCamera
from ui.distribuidor import Alvo, DistribuidorQuadros

Chave = tuple[int, int]
MARGEM = 16


class Mosaico(QWidget):
    vista_clicada = Signal(object)
    menu_vista = Signal(object, QPoint)
    reconectar = Signal(object)
    paginas_mudaram = Signal(int, int)  # página atual (0..), total

    def __init__(
        self,
        distribuidor: DistribuidorQuadros,
        layout: str = "auto",
        mostrar_caixas: bool = True,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._distribuidor = distribuidor
        self._mostrar_caixas = mostrar_caixas
        self._blocos: dict[Chave, BlocoCamera] = {}
        self._ordem: list[Chave] = []
        self._layout = layout if layout in grade.LAYOUTS else "auto"
        self._pagina = 0
        self._selecionada: Chave | None = None
        self._minimizado = False

    # ------------------------------------------------------------ conteúdo
    def definir_vistas(self, itens: list[tuple[Vista, str]]) -> None:
        """Recebe (vista, título) na ordem de exibição. Reaproveita blocos que já existem."""
        novas = [vista.chave for vista, _ in itens]
        for chave in set(self._blocos) - set(novas):
            bloco = self._blocos.pop(chave)
            bloco.deleteLater()
        for vista, titulo in itens:
            bloco = self._blocos.get(vista.chave)
            if bloco is None or bloco.vista != vista:
                if bloco is not None:
                    bloco.deleteLater()
                bloco = self._criar_bloco(vista, titulo)
                self._blocos[vista.chave] = bloco
            else:
                bloco.definir_titulo(titulo)
        self._ordem = novas
        if self._selecionada not in self._blocos:
            self._selecionada = None
        self._pagina = min(self._pagina, self.total_paginas - 1)
        self._reposicionar()

    def _criar_bloco(self, vista: Vista, titulo: str) -> BlocoCamera:
        bloco = BlocoCamera(vista, titulo, self)
        bloco.configurar_sobreposicao(caixas=self._mostrar_caixas, chips=True)
        bloco.clicado.connect(self.selecionar)
        bloco.clicado.connect(self.vista_clicada)
        bloco.menu_pedido.connect(self.menu_vista)
        bloco.reconectar_pedido.connect(self.reconectar)
        bloco.hide()
        return bloco

    def bloco(self, chave: Chave) -> BlocoCamera | None:
        return self._blocos.get(chave)

    @property
    def quantidade(self) -> int:
        return len(self._ordem)

    # ------------------------------------------------------------ navegação
    @property
    def layout_atual(self) -> str:
        return self._layout

    @property
    def pagina(self) -> int:
        return self._pagina

    @property
    def total_paginas(self) -> int:
        return grade.total_paginas(len(self._ordem), self._layout)

    @property
    def selecionada(self) -> Chave | None:
        return self._selecionada

    def definir_layout(self, layout: str) -> None:
        self._layout = layout
        self._pagina = 0
        self._reposicionar()

    def mudar_pagina(self, delta: int) -> None:
        self._pagina = max(0, min(self.total_paginas - 1, self._pagina + delta))
        self._reposicionar()

    def selecionar(self, chave: Chave) -> None:
        """Marca a vista escolhida (será a câmera do joystick na Fase 7)."""
        self._selecionada = chave
        for outra, bloco in self._blocos.items():
            bloco.definir_selecionado(outra == chave)

    def definir_minimizado(self, minimizado: bool) -> None:
        self._minimizado = minimizado
        self._atualizar_alvos()

    # ------------------------------------------------------------ dados do distribuidor
    def receber_quadro(self, chave: Chave, imagem: QImage) -> None:
        bloco = self._blocos.get(chave)
        if bloco is not None:
            bloco.definir_imagem(imagem)

    def receber_deteccoes(self, chave: Chave, resultado: object | None) -> None:
        bloco = self._blocos.get(chave)
        if bloco is not None:
            bloco.definir_deteccoes(getattr(resultado, "deteccoes", None))

    def receber_parado(self, chave: Chave, situacao: object | None) -> None:
        """Situação dos alertas da vista: borda vermelha e contadores "parado 6/8 s"."""
        bloco = self._blocos.get(chave)
        if bloco is not None:
            bloco.definir_parado(situacao)  # type: ignore[arg-type]

    def alertas(self) -> list[Chave]:
        """Vistas com alerta ativo agora (borda vermelha)."""
        return [chave for chave, bloco in self._blocos.items() if bloco.alerta]

    def definir_mostrar_caixas(self, mostrar: bool) -> None:
        self._mostrar_caixas = mostrar
        for bloco in self._blocos.values():
            bloco.configurar_sobreposicao(caixas=mostrar, chips=True)

    def receber_estado(self, chave: Chave, estado: str, mensagem: str, segundos: int) -> None:
        bloco = self._blocos.get(chave)
        if bloco is not None:
            bloco.definir_estado(estado, mensagem, segundos)

    def estados(self) -> dict[Chave, str]:
        return {chave: bloco.estado for chave, bloco in self._blocos.items()}

    # ------------------------------------------------------------ posicionamento
    def chaves_visiveis(self) -> list[Chave]:
        por_pagina = grade.blocos_por_pagina(len(self._ordem), self._layout)
        inicio = self._pagina * por_pagina
        return self._ordem[inicio : inicio + por_pagina]

    def _reposicionar(self) -> None:
        visiveis = self.chaves_visiveis()
        colunas, linhas = grade.dimensoes_grade(len(self._ordem), self._layout)
        area = self.rect().adjusted(MARGEM, MARGEM, -MARGEM, -MARGEM)
        retangulos = grade.posicionar(len(visiveis), colunas, linhas, area.width(), area.height(), tema.ESPACO)
        posicoes = dict(zip(visiveis, retangulos, strict=False))
        for chave, bloco in self._blocos.items():
            if chave in posicoes:
                x, y, largura, altura = posicoes[chave]
                bloco.setGeometry(area.x() + x, area.y() + y, largura, altura)
                bloco.show()
            else:
                bloco.hide()
        self.paginas_mudaram.emit(self._pagina, self.total_paginas)
        self._atualizar_alvos()

    def _atualizar_alvos(self) -> None:
        mostrando = self.isVisible() and not self._minimizado
        escala = self.devicePixelRatioF()
        alvos = []
        for chave in self._ordem:
            bloco = self._blocos[chave]
            tamanho: QSize = bloco.size()
            alvos.append(
                Alvo(
                    vista=bloco.vista,
                    largura=round(tamanho.width() * escala),
                    altura=round(tamanho.height() * escala),
                    escala_tela=escala,
                    visivel=mostrando and bloco.isVisibleTo(self),
                )
            )
        self._distribuidor.definir_alvos(alvos)

    def resizeEvent(self, evento: QResizeEvent) -> None:  # noqa: N802
        super().resizeEvent(evento)
        self._reposicionar()

    def showEvent(self, evento: QShowEvent) -> None:  # noqa: N802
        super().showEvent(evento)
        self._atualizar_alvos()

    def hideEvent(self, evento: QHideEvent) -> None:  # noqa: N802
        super().hideEvent(evento)
        self._atualizar_alvos()
