"""Uma rodada do aprendizado contínuo. Roda num PROCESSO separado: ``python -m treino.ciclo``.

Por que outro processo? Treinar ocupa CPU, GPU e memória por vários minutos. À parte (e com
prioridade mais baixa), a janela continua leve — e, se algo der errado no treino, o app não
cai. O app acompanha pelo que este processo escreve na saída: uma linha JSON por aviso.

Etapas:

1. o PROFESSOR rotula as imagens pendentes (certeza -> ``rotuladas``; dúvida -> ``revisar``);
2. respeita o limite de espaço;
3. há imagens novas suficientes? (``treino.min_novas``; "Treinar agora" pula esta regra);
4. separa treino/validação (blocos de 1 hora) e escreve as listas;
5. nota do modelo ATUAL na validação;
6. treina a partir do modelo atual (aprendizado contínuo) com TODAS as imagens rotuladas, SEM
   olhar a validação (épocas fixas; fica a última);
7. nota do modelo NOVO na mesma validação — a primeira e única vez que ele vê essas imagens;
8. melhor? copia para ``pc/modelos/sentinela_AAAAMMDD_HHMM.pt`` — quem troca o modelo em uso
   é o app (dá para voltar a qualquer um);
9. registra no histórico (``dados/treino/historico.json``).

Memória (erro de 12/10/2026, PC com 8 GB): o treino só COMEÇA com ``treino.memoria_minima_gb``
livres e PARA sozinho se a memória livre cair abaixo de ``treino.memoria_critica_gb`` (o app e as
câmeras têm prioridade). Ver ``treino/memoria.py``.

Prova honesta (achado de 12/10/2026): se o treino escolhe a "melhor época" pelas imagens de
validação e depois a decisão usa as MESMAS imagens, a nota sai otimista (é como fazer a mesma
prova 20 vezes e entregar a melhor tentativa). O modelo aprovado às 08:57 tinha 0,868 x 0,852
nas imagens que o escolheram, mas 0,635 x 0,822 nas imagens novas. Por isso o treino roda com
``val=False`` (sem "melhor época" e sem parada antecipada) e usa o ``last.pt``.
"""

from __future__ import annotations

import argparse
import ctypes
import gc
import json
import logging
import os
import shutil
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from core.config import Config
from core.modelos import CLASSES_COCO
from treino.avaliacao import Metricas, decidir_promocao, metricas_do_ultralytics
from treino.dados import ROTULADAS, ConjuntoTreino
from treino.historico import Historico, Rodada
from treino.memoria import (
    GB,
    PLACA_MINIMA_GB,
    Memoria,
    MemoriaInsuficiente,
    eh_falta_de_memoria,
    falta_para,
    medir,
    orcamento_placa,
)
from treino.rotulador import Detectar, rotular_pendentes

log = logging.getLogger(__name__)

PREFIXO_MODELO = "sentinela_"
Avisar = Callable[[dict[str, Any]], None]
Vigiar = Callable[[], None]  # levanta MemoriaInsuficiente se a memória livre ficou crítica


@dataclass(frozen=True)
class AjustesTreino:
    pasta_treino: Path
    pasta_modelos: Path
    modelo_atual: str
    dispositivo: str  # "cuda:0" ou "cpu"
    modelo_professor: str
    conf_certa: float
    conf_duvida: float
    min_novas: int
    min_validacao: int
    validacao_porcento: float
    epocas: int
    congelar: int
    lote: int
    tamanho_imagem: int
    margem: float
    queda_maxima: float
    manter_modelos: int
    limite_bytes: float
    ajudantes: int
    memoria_minima_gb: float
    memoria_critica_gb: float

    @classmethod
    def do_config(cls, config: Config) -> AjustesTreino:
        from vision.detector import escolher_dispositivo

        def t(nome: str) -> Any:
            return config.obter(f"treino.{nome}")

        return cls(
            pasta_treino=config.pasta_dados / "treino",
            pasta_modelos=config.resolver_caminho(str(config.obter("ia.pasta_modelos"))),
            modelo_atual=str(config.obter("ia.modelo")),
            dispositivo=escolher_dispositivo(str(config.obter("ia.dispositivo"))),
            modelo_professor=str(t("modelo_professor")),
            conf_certa=float(t("conf_professor")),
            conf_duvida=float(t("conf_professor_duvida")),
            min_novas=int(t("min_novas")),
            min_validacao=int(t("min_validacao")),
            validacao_porcento=float(t("validacao_porcento")),
            epocas=int(t("epocas")),
            congelar=int(t("congelar")),
            lote=int(t("lote")),
            tamanho_imagem=int(t("tamanho_imagem")),
            margem=float(t("margem_minima")),
            queda_maxima=float(t("queda_maxima_classe")),
            manter_modelos=int(t("manter_modelos")),
            limite_bytes=float(t("limite_gb")) * 1024**3,
            ajudantes=int(t("ajudantes")),
            memoria_minima_gb=float(t("memoria_minima_gb")),
            memoria_critica_gb=float(t("memoria_critica_gb")),
        )


@dataclass
class Pecas:
    """O que fala com o Ultralytics. Nos testes, entram versões falsas (rápidas)."""

    professor: Callable[[], Detectar]
    # yaml, base, pasta, ao_epoca, vigiar -> pesos do modelo treinado
    treinar: Callable[[Path, Path, Path, Callable[[int, int], None], Vigiar], Path]
    validar: Callable[[Path, Path, Path, Vigiar], Metricas]  # modelo, yaml, pasta, vigiar -> nota
    nomes: Callable[[Path], dict[int, str]]  # nomes das classes do modelo (índice -> nome)
    memoria: Callable[[], Memoria] = medir


def criar_vigia(medir_memoria: Callable[[], Memoria], critica_gb: float) -> Vigiar:
    """Chamado a cada imagem/lote: se a memória livre do PC ficou crítica, o treino para ali."""

    def vigiar() -> None:
        livre = medir_memoria()
        if livre.reservavel_livre_gb < critica_gb:
            raise MemoriaInsuficiente(f"a memória livre do computador caiu para {livre.texto()}; "
                                      f"o limite é {critica_gb:.1f} GB")

    return vigiar


def executar_ciclo(config: Config, avisar: Avisar, pecas: Pecas | None = None, forcar: bool = False) -> Rodada:
    """Faz uma rodada completa e devolve o que aconteceu (também vai para o histórico, se treinou)."""
    aj = AjustesTreino.do_config(config)
    inicio = datetime.now()
    relogio = time.monotonic()
    rodada = Rodada(inicio=inicio.isoformat(timespec="seconds"), modelo_base=aj.modelo_atual)
    conjunto = ConjuntoTreino(aj.pasta_treino)
    historico = Historico(aj.pasta_treino / "historico.json")
    execucao = aj.pasta_treino / "execucoes" / f"{inicio:%Y%m%d_%H%M%S}"

    # 0. há memória para treinar sem derrubar o app? (medido ANTES de carregar o PyTorch)
    medir_memoria = pecas.memoria if pecas is not None else medir
    livre = medir_memoria()
    log.info("Memória no início do treino: %s", livre.texto())
    if (motivo := falta_para(livre, aj.memoria_minima_gb)) is not None:
        rodada.motivo = motivo
        return _terminar(rodada, relogio, historico, salvar=False)
    vigiar = criar_vigia(medir_memoria, aj.memoria_critica_gb)

    def etapa(nome: str, texto: str, feito: int = 0, total: int = 0) -> None:
        log.info("Treino: %s", texto)
        avisar({"etapa": nome, "texto": texto, "feito": feito, "total": total})

    def ao_rotular(feitas: int, total: int) -> None:
        vigiar()
        etapa("rotulando", f"O professor está rotulando: {feitas} de {total}", feitas, total)

    try:
        pecas = pecas or pecas_reais(aj)
        # 1. o professor rotula
        pendentes = conjunto.contar()["pendentes"]
        if pendentes:
            etapa("professor", f"Carregando o professor ({aj.modelo_professor})…")
            detectar = pecas.professor()
            contagem = rotular_pendentes(
                conjunto, detectar, aj.conf_certa, aj.conf_duvida, aj.modelo_professor, ao_rotular,
            )
            del detectar
            _liberar_memoria()
            rodada.rotuladas_agora, rodada.para_revisar = contagem[ROTULADAS], contagem["revisar"]
        conjunto.aplicar_limite(aj.limite_bytes)

        # 2. vale a pena treinar?
        ultima = historico.ultima_que_treinou()
        # "Nova" = rotulada DEPOIS do fim da última rodada que treinou: as rotuladas durante ela já entraram
        # no treino dela (comparar com o início contava de novo as que passavam para o segundo seguinte).
        desde = (ultima.fim or ultima.inicio) if ultima is not None else ""
        novas = sum(1 for a in conjunto.amostras(ROTULADAS) if str(a.meta().get("rotulada_em", "")) > desde)
        if novas < aj.min_novas and not forcar:
            rodada.motivo = f"poucas imagens novas para treinar ({novas} de {aj.min_novas})"
            return _terminar(rodada, relogio, historico, salvar=False)

        base = aj.pasta_modelos / aj.modelo_atual
        yaml, n_treino, n_validacao = conjunto.escrever_listas(execucao, aj.validacao_porcento, pecas.nomes(base))
        rodada.imagens_treino, rodada.imagens_validacao = n_treino, n_validacao
        if n_validacao < aj.min_validacao:
            rodada.motivo = f"poucas imagens de validação ({n_validacao} de {aj.min_validacao})"
            return _terminar(rodada, relogio, historico, salvar=False)
        if n_treino < 10:
            rodada.motivo = f"poucas imagens de treino ({n_treino})"
            return _terminar(rodada, relogio, historico, salvar=False)

        # 3. nota atual, treino, nota nova
        etapa("avaliando", f"Medindo o modelo atual ({aj.modelo_atual}) em {n_validacao} imagens de validação…")
        atual = pecas.validar(base, yaml, execucao / "val_atual", vigiar)
        rodada.atual = atual.para_dict()

        def ao_epoca(epoca: int, total: int) -> None:
            epoca = min(epoca, total)  # o Ultralytics avisa uma vez a mais, na avaliação final
            rodada.epocas = epoca
            etapa("treinando", f"Treinando com {n_treino} imagens: época {epoca} de {total}", epoca, total)

        etapa("treinando", f"Treinando com {n_treino} imagens…", 0, aj.epocas)
        melhor = pecas.treinar(yaml, base, execucao / "treino", ao_epoca, vigiar)
        _liberar_memoria()
        etapa("avaliando", "Medindo o modelo novo nas mesmas imagens de validação…")
        novo = pecas.validar(melhor, yaml, execucao / "val_novo", vigiar)
        rodada.novo = novo.para_dict()

        # 4. decide
        rodada.promovido, rodada.motivo = decidir_promocao(atual, novo, aj.margem, aj.queda_maxima)
        if rodada.promovido:
            nome = f"{PREFIXO_MODELO}{inicio:%Y%m%d_%H%M}.pt"
            aj.pasta_modelos.mkdir(parents=True, exist_ok=True)
            shutil.copy2(melhor, aj.pasta_modelos / nome)
            rodada.modelo_novo = nome
            limpar_modelos_antigos(aj.pasta_modelos, aj.manter_modelos, {aj.modelo_atual, nome})
    except MemoriaInsuficiente as erro:
        log.warning("Treino interrompido para proteger o app: %s", erro)
        rodada.erro = str(erro)
        rodada.motivo = f"parou para não faltar memória às câmeras: {erro}"
    except Exception as erro:  # noqa: BLE001 (registra e conta para o app; nunca fica "pela metade")
        log.exception("Rodada de treino falhou")
        rodada.erro = str(erro) or type(erro).__name__
        if eh_falta_de_memoria(erro):
            rodada.motivo = (f"faltou memória (RAM) no computador: {medir_memoria().texto()}. "
                             "Feche outros programas ou deixe o treino para a madrugada")
        elif "out of memory" in rodada.erro.lower():
            rodada.motivo = "faltou memória na placa de vídeo, mesmo com lote 1"
        else:
            rodada.motivo = f"falhou: {rodada.erro[:200]}"
    finally:
        shutil.rmtree(execucao, ignore_errors=True)  # pesos e gráficos temporários (o aprovado já foi copiado)
    return _terminar(rodada, relogio, historico, salvar=True)


def _terminar(rodada: Rodada, relogio: float, historico: Historico, salvar: bool) -> Rodada:
    rodada.fim = datetime.now().isoformat(timespec="seconds")
    rodada.duracao_s = round(time.monotonic() - relogio, 1)
    if salvar:
        historico.adicionar(rodada)
    log.info("Rodada de treino: %s", rodada.motivo)
    return rodada


def limpar_modelos_antigos(pasta: Path, manter: int, proteger: set[str]) -> list[str]:
    """Guarda só os ``manter`` modelos treinados mais novos (nunca apaga os protegidos)."""
    treinados = sorted(pasta.glob(f"{PREFIXO_MODELO}*.pt"), key=lambda p: p.name, reverse=True)
    apagados = []
    for arquivo in treinados[manter:]:
        if arquivo.name not in proteger:
            arquivo.unlink(missing_ok=True)
            apagados.append(arquivo.name)
    return apagados


def _liberar_memoria() -> None:
    gc.collect()
    try:
        import torch

        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except Exception:  # noqa: BLE001
        pass


# ===================================================================== Ultralytics de verdade
def pecas_reais(aj: AjustesTreino) -> Pecas:
    from vision.detector import ModeloYolo, desligar_estatisticas_ultralytics, preparar_pesos

    desligar_estatisticas_ultralytics()
    dispositivo = "0" if aj.dispositivo.startswith("cuda") else "cpu"

    def limitar_placa() -> None:
        """Limita o que o PyTorch pode reservar na placa ao que cabe na memória livre AGORA.

        No Windows, a placa conta como memória reservada do PC (ver ``treino/memoria.py``). Acima
        do limite o PyTorch recusa com ``OutOfMemoryError`` e o lote é dividido por 2 — em vez de
        o Windows ficar sem memória e derrubar as câmeras.
        """
        if dispositivo == "cpu":
            return
        import torch

        _liberar_memoria()
        livre_placa, total_placa = torch.cuda.mem_get_info()  # também cria o contexto da placa
        livre = medir()
        orcamento = orcamento_placa(livre, aj.memoria_critica_gb, torch.cuda.memory_reserved() / GB, livre_placa / GB)
        if orcamento < PLACA_MINIMA_GB:
            raise MemoriaInsuficiente(f"não sobra memória para a placa de vídeo treinar ({livre.texto()}; "
                                      f"daria só {orcamento:.1f} GB na placa, o mínimo é {PLACA_MINIMA_GB:.1f} GB)")
        torch.cuda.set_per_process_memory_fraction(min(1.0, orcamento / (total_placa / GB)))
        log.info("Placa de vídeo: o treino pode reservar até %.1f GB (%s)", orcamento, livre.texto())

    def professor() -> Detectar:
        limitar_placa()
        caminho = preparar_pesos(aj.pasta_modelos, aj.modelo_professor)  # ~110 MB na 1ª vez
        modelo = ModeloYolo(caminho, aj.dispositivo, aj.tamanho_imagem, aj.conf_duvida)
        return lambda imagens: modelo.detectar(imagens, aj.tamanho_imagem)

    def treinar(dados: Path, base: Path, pasta: Path, ao_epoca: Callable[[int, int], None], vigiar: Vigiar) -> Path:
        import torch
        from ultralytics import YOLO

        lote, ajudantes = aj.lote, aj.ajudantes
        while True:
            limitar_placa()
            modelo = YOLO(str(base))
            modelo.add_callback("on_fit_epoch_end", lambda t: ao_epoca(int(t.epoch) + 1, int(t.epochs)))
            modelo.add_callback("on_train_batch_end", lambda _t: vigiar())
            modelo.add_callback("on_val_batch_end", lambda _v: vigiar())  # a avaliação de cada época
            try:
                # val=False: o treino NÃO olha a validação (nada de "melhor época" nem parada antecipada);
                # o Ultralytics ainda mede uma vez no fim, só para o relatório dele — não escolhe nada.
                modelo.train(
                    data=str(dados), epochs=aj.epocas, batch=lote, imgsz=aj.tamanho_imagem, val=False,
                    device=dispositivo, project=str(pasta.parent), name=pasta.name, exist_ok=True,
                    workers=ajudantes, plots=False, verbose=False, amp=dispositivo != "cpu", seed=0,
                    freeze=aj.congelar or None,
                )
                break
            except torch.cuda.OutOfMemoryError:
                if lote <= 1:
                    raise
                lote //= 2
                log.warning("Pouca memória na placa de vídeo: treinando de novo com lote %d", lote)
                del modelo
                _liberar_memoria()
            except RuntimeError as erro:
                if ajudantes == 0 or "DataLoader worker" not in str(erro):
                    raise
                ajudantes = 0  # Windows: processos ajudantes falharam -> carrega as imagens no próprio processo
                log.warning("Ajudantes de leitura falharam (%s): treinando sem eles", erro)
        return pasta / "weights" / "last.pt"  # a última época (nunca a "melhor" pela validação)

    def validar(modelo: Path, dados: Path, pasta: Path, vigiar: Vigiar) -> Metricas:
        import torch
        from ultralytics import YOLO

        lote = aj.lote
        while True:
            limitar_placa()
            yolo = YOLO(str(modelo))
            yolo.add_callback("on_val_batch_end", lambda _v: vigiar())
            try:
                # Sem processos ajudantes: a validação é pequena e assim não há surpresas no Windows.
                resultado = yolo.val(
                    data=str(dados), split="val", imgsz=aj.tamanho_imagem, batch=lote, device=dispositivo,
                    plots=False, verbose=False, project=str(pasta.parent), name=pasta.name, exist_ok=True,
                    classes=sorted(CLASSES_COCO), workers=0,
                )
                return metricas_do_ultralytics(resultado)
            except torch.cuda.OutOfMemoryError:
                if lote <= 1:
                    raise
                lote //= 2
                log.warning("Pouca memória na placa de vídeo: avaliando de novo com lote %d", lote)
                del yolo
                _liberar_memoria()

    def nomes(modelo: Path) -> dict[int, str]:
        from ultralytics import YOLO

        return {int(k): str(v) for k, v in YOLO(str(preparar_pesos(modelo.parent, modelo.name))).names.items()}

    return Pecas(professor, treinar, validar, nomes, medir)


def _sem_janela_de_erro() -> None:
    """Windows: se este processo (ou um ajudante dele) quebrar, nada de janela "Erro de Aplicativo".

    A janela deixa o processo quebrado PARADO (segurando a memória) até alguém clicar em OK — de
    madrugada, ninguém clica. Os processos filhos herdam esta escolha.
    """
    if os.name == "nt":
        sem_erro_critico, sem_janela_de_falha = 0x0001, 0x0002  # SEM_FAILCRITICALERRORS | SEM_NOGPFAULTERRORBOX
        ctypes.windll.kernel32.SetErrorMode(sem_erro_critico | sem_janela_de_falha)  # type: ignore[attr-defined]


# ===================================================================== processo separado
def canal_de_avisos() -> Avisar:
    """A saída deste processo vira o canal de avisos para o app: uma linha JSON (UTF-8) por aviso.

    No Windows, a saída ligada a um "pipe" usa a codificação antiga (cp1252), que não tem "→":
    em 12/10/2026 o aviso final "melhorou: nota 0.852 → 0.868" derrubou o processo e o app nunca
    soube que o modelo foi aprovado. Por isso a saída é trocada para UTF-8 aqui (o app também pede
    ``PYTHONIOENCODING=utf-8``). As bibliotecas que "printam" vão para o stderr (não sujam o canal).
    """
    canal = sys.stdout
    if hasattr(canal, "reconfigure"):
        canal.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    sys.stdout = sys.stderr

    def avisar(dados: dict[str, Any]) -> None:
        canal.write(json.dumps(dados, ensure_ascii=False) + "\n")
        canal.flush()

    return avisar


def main(argumentos: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Uma rodada do aprendizado contínuo do Sentinela Casa.")
    parser.add_argument("--config", required=True, help="caminho do config.yaml do app")
    parser.add_argument("--forcar", action="store_true", help="treinar mesmo com poucas imagens novas")
    args = parser.parse_args(argumentos)
    _sem_janela_de_erro()
    avisar = canal_de_avisos()

    from core.logs import configurar_logs

    config = Config.carregar(Path(args.config))
    configurar_logs(config.pasta_logs, nivel=str(config.obter("geral.log_nivel")), nome_arquivo="treino.log",
                    saida_console=sys.stderr)
    rodada = executar_ciclo(config, avisar, forcar=args.forcar)
    avisar({"fim": True, "rodada": rodada.para_dict()})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
