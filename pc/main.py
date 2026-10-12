"""Ponto de entrada do Sentinela Casa (app do PC).

Uso (de dentro da pasta ``pc/``, com o ambiente virtual ativado):  python main.py

Ordem de inicialização:
1. config.yaml e logs;
2. OpenCV/FFmpeg (transporte RTSP, silêncio dos avisos) — antes de abrir câmeras;
3. Qt (QApplication) e tema;
4. banco, serviço de câmeras e gerenciador dos leitores de vídeo;
5. serviço de IA (o modelo carrega em segundo plano: a janela abre na hora);
6. serviço de eventos (recebe os resultados da IA e grava clipe/foto) e coletor de treino;
7. serviço de alertas (regra de "parado na zona": ALERTA, LIMPAR, PASSOU);
8. comunicação com a placa BitDogLab (UDP) — recebe os alertas;
9. PTZ (motor das câmeras): joystick da placa, tela e teclado;
10. aprendizado contínuo (agenda o treino da IA com as imagens das câmeras);
11. janela principal.
"""

from __future__ import annotations

import logging
import platform
import sys
import threading
from types import TracebackType

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QMessageBox

from camera.captura import configurar_opencv
from camera.gerenciador import GerenciadorCameras
from camera.ptz import ServicoPTZ
from core import segredos
from core.banco import Banco, ErroBanco
from core.config import Config, ErroConfig
from core.logs import configurar_logs
from core.servico_cameras import ServicoCameras
from ui import tema
from eventos.alertas import ServicoAlertas
from eventos.coletor import ColetorTreino
from eventos.gravador import ServicoEventos
from link.bitdog import LinkBitDog
from treino.servico import ServicoTreino
from ui.janela_principal import JanelaPrincipal
from vision.detector import ServicoDeteccao

log = logging.getLogger("sentinela")


def _registrar_erro(tipo: type[BaseException], erro: BaseException, rastro: TracebackType | None) -> None:
    """Erros não tratados vão para o log em vez de derrubar o app em silêncio."""
    if issubclass(tipo, KeyboardInterrupt):
        sys.__excepthook__(tipo, erro, rastro)
        return
    log.critical("Erro não tratado", exc_info=(tipo, erro, rastro))


def main() -> int:
    try:
        config = Config.carregar()
    except ErroConfig as erro:
        sys.stderr.write(f"{erro}\n")
        return 2

    caminho_log = configurar_logs(
        config.pasta_logs,
        nivel=str(config.obter("geral.log_nivel")),
        tamanho_mb=float(config.obter("geral.log_tamanho_mb")),
        arquivos=int(config.obter("geral.log_arquivos")),
    )
    sys.excepthook = _registrar_erro
    threading.excepthook = lambda a: _registrar_erro(a.exc_type, a.exc_value or Exception(), a.exc_traceback)
    log.info("Sentinela Casa iniciando (Python %s, %s)", platform.python_version(), platform.platform())
    log.info("Configuração: %s | dados: %s | log: %s", config.caminho, config.pasta_dados, caminho_log)

    configurar_opencv(str(config.obter("captura.transporte_rtsp")))

    QApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QApplication(sys.argv)
    app.setApplicationName("Sentinela Casa")
    app.setOrganizationName("Sentinela Casa")
    tema.aplicar_tema(app, str(config.obter("interface.tema")))

    try:
        banco = Banco(config.arquivo_banco)
    except ErroBanco as erro:
        log.error("%s", erro)
        QMessageBox.critical(None, "Sentinela Casa", f"Não foi possível abrir o banco de dados:\n{erro}")
        return 1
    if problema := segredos.verificar_cofre():
        log.warning("Cofre de senhas: %s", problema)

    servico = ServicoCameras(banco, config)
    gerenciador = GerenciadorCameras(config, servico.obter_senha)
    deteccao = ServicoDeteccao(config, gerenciador.leitor_para_ia).iniciar()  # HD ou substream (ia.fonte)
    eventos = ServicoEventos(config, servico, gerenciador, ColetorTreino(config)).iniciar()
    deteccao.assinar(eventos.receber_resultado)
    alertas = ServicoAlertas(config, servico, eventos).iniciar()
    deteccao.assinar(alertas.receber_resultado)
    link = LinkBitDog(config).iniciar()
    alertas.assinar(link.receber_aviso)
    ptz = ServicoPTZ(config, servico.obter_senha).iniciar()
    # Joystick da placa -> câmera do joystick (a aberta na tela ou a última selecionada).
    link.assinar_joystick(lambda x, y: ptz.mover(link.camera_joystick, x, y))
    link.assinar_camera(ptz.parar_outras)  # botão J: a câmera anterior para
    # A lente girou: a IA reaprende o fundo e a regra de "parado" recomeça o aquecimento.
    ptz.assinar_vista_movida(deteccao.reiniciar_fundo)
    ptz.assinar_vista_movida(alertas.reiniciar_vista)
    treino = ServicoTreino(config).iniciar()
    janela = JanelaPrincipal(config, servico, gerenciador, deteccao, eventos, alertas, link, ptz, treino)
    janela.show()
    if problema:
        janela.notificador.mostrar("Cofre de senhas indisponível", problema, "aviso", duracao_ms=10000)

    codigo = app.exec()
    log.info("Encerrando…")
    deteccao.parar()
    alertas.parar()
    link.parar()
    ptz.parar()  # para as câmeras que estiverem girando
    treino.parar()  # um treino em andamento é cancelado (recomeça na próxima rodada)
    eventos.parar()  # grava o evento que estiver em andamento
    gerenciador.parar_tudo()
    banco.fechar()
    return codigo


if __name__ == "__main__":
    sys.exit(main())
