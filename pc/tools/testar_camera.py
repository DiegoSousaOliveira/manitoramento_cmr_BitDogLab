"""Diagnóstico de UMA câmera informada por argumentos — sem varredura de rede.

Exemplos (rode de dentro da pasta ``pc/``, com o ambiente virtual ativado):

    python tools/testar_camera.py --ip 192.168.0.50
    python tools/testar_camera.py --ip 192.168.0.50 --pedir-senha
    python tools/testar_camera.py --ip 192.168.0.50 --usuario admin --senha 1234 --canal 2
    python tools/testar_camera.py --ip 192.168.0.50 --salvar-fotos
    python tools/testar_camera.py --url videos/exemplo.mp4      # testa um arquivo de vídeo

Código de saída: 0 = o substream abriu; 1 = falhou; 2 = argumentos inválidos.
O relatório também fica gravado em ``dados/logs/diagnostico.log``.
"""

from __future__ import annotations

import argparse
import getpass
import logging
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

# Rodando como "python tools/testar_camera.py", o Python só enxerga a pasta tools/.
# Acrescentamos a pasta pc/ para que "import core" e "import camera" funcionem.
PASTA_PC = Path(__file__).resolve().parents[1]
if str(PASTA_PC) not in sys.path:
    sys.path.insert(0, str(PASTA_PC))

from camera.captura import configurar_opencv, salvar_jpg  # noqa: E402
from camera.diagnostico import (  # noqa: E402
    Diagnostico,
    ParametrosDiagnostico,
    ResultadoOnvif,
    ResultadoStream,
    descrever_porta,
    diagnosticar,
)
from camera.rede import ResultadoPorta  # noqa: E402
from camera.sonda_rtsp import ResultadoSonda  # noqa: E402
from core.config import Config, ErroConfig  # noqa: E402
from core.logs import configurar_logs  # noqa: E402
from core.modelos import TipoStream  # noqa: E402
from core.url_rtsp import mascarar_url  # noqa: E402
from core.validacao import validar_ip, validar_modelo_url, validar_porta  # noqa: E402

log = logging.getLogger("testar_camera")

OK, FALHA, AVISO, INFO = "[ OK  ]", "[FALHA]", "[AVISO]", "  ...  "
LINHA = "=" * 72


def criar_argumentos(config: Config) -> argparse.ArgumentParser:
    modelos = config.modelos_url()
    parser = argparse.ArgumentParser(
        prog="testar_camera.py",
        description="Testa UMA câmera (portas, substream, stream principal e ONVIF). Não faz varredura de rede.",
    )
    parser.add_argument("--ip", help="IP da câmera na rede local, ex.: 192.168.0.50")
    parser.add_argument("--usuario", default=config.obter("cadastro.usuario"), help="usuário (padrão: %(default)s)")
    senha = parser.add_mutually_exclusive_group()
    senha.add_argument("--senha", default="", help="senha (fica no histórico do terminal; prefira --pedir-senha)")
    senha.add_argument("--pedir-senha", action="store_true", help="digitar a senha sem mostrá-la na tela")
    parser.add_argument("--canal", type=int, default=1, help="canal/lente, começando em 1 (padrão: %(default)s)")
    parser.add_argument("--porta", type=int, default=config.obter("cadastro.porta_rtsp"), help="porta RTSP (padrão: %(default)s)")
    parser.add_argument(
        "--porta-onvif", type=int, default=config.obter("cadastro.porta_onvif"), help="porta ONVIF (padrão: %(default)s)"
    )
    parser.add_argument(
        "--modelo",
        default=config.obter("cadastro.modelo_url"),
        help=f"modelo de URL: {', '.join(modelos)} ou um modelo completo rtsp://... (padrão: %(default)s)",
    )
    parser.add_argument("--url", help="testa esta URL ou arquivo de vídeo diretamente (avançado)")
    parser.add_argument(
        "--timeout",
        type=float,
        default=config.obter("diagnostico.timeout_s"),
        help="segundos por teste de rede: portas, RTSP e ONVIF (padrão: %(default)s)",
    )
    parser.add_argument(
        "--timeout-video",
        type=float,
        default=config.obter("captura.timeout_abertura_s"),
        help="segundos para abrir cada vídeo (padrão: %(default)s, de captura.timeout_abertura_s)",
    )
    parser.add_argument("--sem-principal", action="store_true", help="não testar o stream principal")
    parser.add_argument("--sem-onvif", action="store_true", help="não testar o ONVIF")
    parser.add_argument("--salvar-fotos", action="store_true", help="grava um quadro de cada stream em dados/diagnostico/")
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        config = Config.carregar()
    except ErroConfig as erro:
        sys.stderr.write(f"{erro}\n")
        return 2

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")  # evita erro de acento em consoles antigos
    caminho_log = configurar_logs(
        config.pasta_logs,
        nivel=str(config.obter("geral.log_nivel")),
        tamanho_mb=float(config.obter("geral.log_tamanho_mb")),
        arquivos=int(config.obter("geral.log_arquivos")),
        nome_arquivo="diagnostico.log",
        formato_console="%(message)s",
        saida_console=sys.stdout,
    )

    parser = criar_argumentos(config)
    args = parser.parse_args(argv)
    parametros, nome_modelo = _montar_parametros(parser, args, config)
    configurar_opencv(str(config.obter("captura.transporte_rtsp")))

    relatorio = Relatorio(parametros, nome_modelo, str(config.obter("captura.transporte_rtsp")))
    relatorio.cabecalho()
    try:
        diagnostico = diagnosticar(parametros, ao_progresso=relatorio.progresso)
    except KeyboardInterrupt:
        log.info("\nInterrompido pelo usuário.")
        return 130
    relatorio.resumo(diagnostico)

    if args.salvar_fotos:
        _salvar_fotos(diagnostico, config.pasta_dados / "diagnostico", parametros)
    log.info("\nRelatório gravado também em: %s", caminho_log)
    return 0 if diagnostico.ok else 1


def _montar_parametros(
    parser: argparse.ArgumentParser, args: argparse.Namespace, config: Config
) -> tuple[ParametrosDiagnostico, str]:
    """Valida os argumentos e monta os parâmetros do diagnóstico."""
    ip = args.ip or ""
    if args.url and "://" in args.url and not ip:
        ip = urlsplit(args.url).hostname or ""
    if not ip and not args.url:
        parser.error("informe --ip (ou --url para testar uma URL/arquivo diretamente)")
    if ip and (erro := validar_ip(ip)):
        parser.error(erro)
    for nome, porta in (("--porta", args.porta), ("--porta-onvif", args.porta_onvif)):
        if erro := validar_porta(porta):
            parser.error(f"{nome}: {erro}")
    if args.canal < 0:
        parser.error("--canal não pode ser negativo")

    modelos = config.modelos_url()
    if args.modelo in modelos:
        nome_modelo, modelo_url = modelos[args.modelo]
    else:
        nome_modelo, modelo_url = "personalizado", args.modelo
        if erro := validar_modelo_url(modelo_url):
            parser.error(f"--modelo: {erro}")

    url_direta = args.url
    if url_direta and "://" not in url_direta:
        url_direta = str(config.resolver_caminho(url_direta))  # arquivo relativo à pasta pc/

    senha = getpass.getpass("Senha da câmera (Enter = sem senha): ") if args.pedir_senha else args.senha
    parametros = ParametrosDiagnostico(
        ip=ip,
        usuario=args.usuario,
        senha=senha,
        canal=args.canal,
        porta_rtsp=args.porta,
        porta_onvif=args.porta_onvif,
        modelo_url=modelo_url,
        portas=[int(p) for p in config.obter("diagnostico.portas")],
        timeout_s=args.timeout,
        medir_fps_s=float(config.obter("diagnostico.medir_fps_s")),
        video_abertura_s=args.timeout_video,
        video_leitura_s=max(args.timeout_video, float(config.obter("captura.timeout_leitura_s"))),
        testar_principal=not args.sem_principal,
        testar_onvif=not args.sem_onvif,
        url_direta=url_direta,
    )
    return parametros, nome_modelo


class Relatorio:
    """Escreve o andamento e o resultado no log (que vai para a tela e para o arquivo)."""

    def __init__(self, parametros: ParametrosDiagnostico, nome_modelo: str, transporte: str) -> None:
        self.p = parametros
        self.nome_modelo = nome_modelo
        self.transporte = transporte
        self._secao_atual = ""
        self._numero = 0

    def cabecalho(self) -> None:
        p = self.p
        alvo = p.ip or p.url_direta or ""
        log.info(LINHA)
        log.info(" Sentinela Casa - diagnóstico de %s", alvo)
        log.info(" %s", datetime.now().strftime("%d/%m/%Y %H:%M:%S"))
        log.info(LINHA)
        if not p.url_direta:
            log.info(
                "Usuário: %s | Senha: %s | Canal: %d | Modelo de URL: %s | RTSP via %s",
                p.usuario, "(informada)" if p.senha else "(vazia)", p.canal, self.nome_modelo, self.transporte.upper(),
            )

    # ------------------------------------------------------------ andamento
    def _secao(self, etapa: str) -> str:
        if etapa.startswith("porta:"):
            return "Portas"
        if etapa.endswith(":substream"):
            return "Substream (stream=1: mosaico, IA e gravação)" if not self.p.url_direta else "Vídeo"
        if etapa.endswith(":principal"):
            return "Stream principal (stream=0: resolução máxima)"
        return f"ONVIF (porta {self.p.porta_onvif}: informações e PTZ)"

    def progresso(self, etapa: str, resultado: object | None) -> None:
        secao = self._secao(etapa)
        if secao != self._secao_atual:
            self._secao_atual = secao
            self._numero += 1
            log.info("\n%d) %s", self._numero, secao)
            if etapa.startswith(("rtsp:", "stream:")):
                fonte = self.p.url_direta or ""
                try:
                    tipo = TipoStream.SUBSTREAM if etapa.endswith("substream") else TipoStream.PRINCIPAL
                    fonte = mascarar_url(self.p.url(tipo))
                except ValueError:
                    pass
                log.info("   URL: %s", fonte)

        if resultado is None:
            if etapa.startswith("stream:"):
                log.info("   %s abrindo o vídeo e medindo o FPS (pode levar alguns segundos)", INFO)
            elif etapa == "onvif":
                log.info("   %s consultando o ONVIF", INFO)
            return

        if isinstance(resultado, ResultadoPorta):
            descricao = descrever_porta(resultado.porta, self.p)
            log.info("   %s %-6d %-34s %s", OK if resultado.aberta else FALHA, resultado.porta, descricao, resultado.mensagem)
        elif isinstance(resultado, ResultadoSonda):
            extras = [f"servidor: {resultado.servidor}"] if resultado.servidor else []
            if resultado.autenticacao:
                extras.append(f"autenticação {resultado.autenticacao}")
            sufixo = f" | {' | '.join(extras)}" if extras else ""
            log.info("   %s %s%s", OK if resultado.ok else FALHA, resultado.mensagem, sufixo)
        elif isinstance(resultado, ResultadoStream):
            log.info("   %s %s", OK if resultado.ok else FALHA, resultado.mensagem)
        elif isinstance(resultado, ResultadoOnvif):
            self._onvif(resultado)

    def _onvif(self, r: ResultadoOnvif) -> None:
        if not r.ok:
            log.info("   %s %s", FALHA, r.mensagem)
            return
        log.info("   %s Fabricante: %s | Modelo: %s | Firmware: %s", OK, r.fabricante or "?", r.modelo or "?", r.firmware or "?")
        if r.perfis:
            log.info("   Perfis de vídeo:")
            for perfil in r.perfis:
                fps = f" {perfil.fps:.0f} fps" if perfil.fps else ""
                ptz = " [PTZ]" if perfil.ptz else ""
                log.info(
                    "     - %s (token %s): %dx%d %s%s%s",
                    perfil.nome, perfil.token, perfil.largura, perfil.altura, perfil.codec, fps, ptz,
                )
                if perfil.url:
                    log.info("       %s", perfil.url)
        elif r.mensagem != "ONVIF respondeu":
            log.info("   %s %s", AVISO, r.mensagem)
        log.info("   %s PTZ: %s", OK if r.tem_ptz else AVISO, r.detalhes_ptz)

    # ------------------------------------------------------------ resumo
    def resumo(self, diag: Diagnostico) -> None:
        log.info("\n%s", LINHA)
        log.info(" Resumo")
        log.info(LINHA)
        for frase in diag.conclusoes:
            log.info(" - %s", frase)
        if diag.ok:
            log.info("\n%s Resultado: a câmera está pronta para ser cadastrada.", OK)
        else:
            log.info("\n%s Resultado: o vídeo não abriu. Veja as mensagens acima.", FALHA)


def _salvar_fotos(diag: Diagnostico, pasta_base: Path, p: ParametrosDiagnostico) -> None:
    quadros = [(r.nome, r.quadro) for r in (diag.sub, diag.principal) if r is not None and r.quadro is not None]
    if not quadros:
        log.info("\nNenhuma foto para salvar (nenhum stream abriu).")
        return
    identificacao = (p.ip or "arquivo").replace(":", "_")
    pasta = pasta_base / f"{identificacao}_{datetime.now():%Y%m%d-%H%M%S}"
    pasta.mkdir(parents=True, exist_ok=True)
    for nome, quadro in quadros:
        salvar_jpg(pasta / f"{nome}.jpg", quadro)
    log.info("\nFotos salvas em: %s", pasta)


if __name__ == "__main__":
    sys.exit(main())
