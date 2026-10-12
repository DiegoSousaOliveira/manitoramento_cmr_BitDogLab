"""Fase 9: aprendizado contínuo — dados, professor, decisão de troca, ciclo (com peças falsas) e agenda."""

from __future__ import annotations

import json
import sys
import time
from collections.abc import Callable, Iterator
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from camera.captura import salvar_jpg
from core.config import ARQUIVO_PADRAO, Config
from treino.avaliacao import Metricas, decidir_promocao, metricas_do_ultralytics
from treino.ciclo import PREFIXO_MODELO, Pecas, executar_ciclo, limpar_modelos_antigos
from treino.dados import PENDENTES, REVISAR, ROTULADAS, Amostra, ConjuntoTreino, caixa_de_rotulo, rotulo_de_caixa
from treino.historico import Historico, Rodada
from treino.memoria import Memoria, eh_falta_de_memoria, falta_para, medir, orcamento_placa
from treino.rotulador import decidir, rotular_pendentes
from treino.servico import ServicoTreino, deve_rodar
from vision.rastreador import DeteccaoBruta


def esperar(condicao: Callable[[], bool], limite_s: float = 8.0) -> bool:
    fim = time.monotonic() + limite_s
    while time.monotonic() < fim:
        if condicao():
            return True
        time.sleep(0.02)
    return condicao()


def criar_amostra(pasta: Path, nome: str, quando: datetime, camera: str = "Frente", rotulos=None) -> Amostra:  # noqa: ANN001
    pasta.mkdir(parents=True, exist_ok=True)
    amostra = Amostra(pasta / f"{nome}.jpg")
    salvar_jpg(amostra.imagem, np.full((36, 64, 3), 80, dtype=np.uint8))
    amostra.gravar_rotulos(rotulos or [])
    amostra.json.write_text(json.dumps({"camera": camera, "vista": 1, "data": quando.isoformat(), "motivo": "duvida"}),
                            encoding="utf-8")
    return amostra


# ===================================================================== dados
def test_rotulos_ida_e_volta(tmp_path: Path) -> None:
    amostra = criar_amostra(tmp_path, "a", datetime(2026, 10, 11, 10), rotulos=[rotulo_de_caixa(0, (0.1, 0.2, 0.3, 0.6))])
    (classe, cx, cy, w, h), = amostra.rotulos()
    assert classe == 0 and (cx, cy, w, h) == pytest.approx((0.2, 0.4, 0.2, 0.4))
    assert caixa_de_rotulo(amostra.rotulos()[0]) == pytest.approx((0.1, 0.2, 0.3, 0.6))
    assert amostra.data == datetime(2026, 10, 11, 10)


def test_separacao_por_blocos_de_hora_justa_e_fixa(tmp_path: Path) -> None:
    conjunto = ConjuntoTreino(tmp_path)
    inicio = datetime(2026, 10, 11, 8)
    for bloco in range(10):  # 10 horas, 5 fotos "gêmeas" por hora
        for foto in range(5):
            criar_amostra(tmp_path / ROTULADAS, f"h{bloco}_{foto}", inicio + timedelta(hours=bloco, minutes=foto * 10))
    treino, validacao = conjunto.separar(20)
    assert len(validacao) == 10 and len(treino) == 40  # 2 blocos inteiros de 10 = 20%
    lados = {}
    for amostra in treino + validacao:  # nenhum bloco ficou dividido
        lados.setdefault(ConjuntoTreino.bloco(amostra), set()).add(amostra.meta()["lado"])
    assert all(len(lado) == 1 for lado in lados.values())
    nomes_validacao = {a.nome for a in validacao}
    criar_amostra(tmp_path / ROTULADAS, "nova", inicio + timedelta(minutes=55))  # mesma hora da 1ª validação
    treino2, validacao2 = conjunto.separar(20)
    assert nomes_validacao <= {a.nome for a in validacao2}  # quem era validação continua sendo
    bloco_da_nova = ConjuntoTreino.bloco(Amostra(tmp_path / ROTULADAS / "nova.jpg"))
    lado_do_bloco = next(iter(lados[bloco_da_nova]))
    assert ("nova" in {a.nome for a in validacao2}) == (lado_do_bloco == "validacao")


def test_listas_para_o_ultralytics(tmp_path: Path) -> None:
    conjunto = ConjuntoTreino(tmp_path)
    for hora in range(6):
        criar_amostra(tmp_path / ROTULADAS, f"f{hora}", datetime(2026, 10, 11, hora))
    yaml, n_treino, n_validacao = conjunto.escrever_listas(tmp_path / "exec", 20, {0: "person", 1: "bicycle"})
    texto = yaml.read_text(encoding="utf-8")
    assert "train: treino.txt" in texto and "val: validacao.txt" in texto and '0: "person"' in texto
    assert n_treino + n_validacao == 6 and n_validacao >= 1
    linhas = (tmp_path / "exec" / "treino.txt").read_text(encoding="utf-8").split()
    assert all(Path(l).is_file() and Path(l).with_suffix(".txt").is_file() for l in linhas)  # rótulo ao lado


def test_limite_de_espaco_apaga_as_mais_antigas_e_poupa_as_revisadas(tmp_path: Path) -> None:
    conjunto = ConjuntoTreino(tmp_path)
    velha_humana = criar_amostra(tmp_path / ROTULADAS, "velha_humana", datetime(2026, 1, 1))
    velha_humana.atualizar_meta(rotulo="humano")
    velha = criar_amostra(tmp_path / ROTULADAS, "velha", datetime(2026, 2, 1))
    nova = criar_amostra(tmp_path / PENDENTES, "nova", datetime(2026, 10, 1))
    total = conjunto.tamanho()
    assert conjunto.aplicar_limite(total - 1) == 1
    assert not velha.imagem.exists() and velha_humana.imagem.exists() and nova.imagem.exists()


# ===================================================================== professor
def bruta(classe: int, conf: float) -> DeteccaoBruta:
    return DeteccaoBruta(classe, conf, (0.1, 0.1, 0.2, 0.3))


def test_professor_so_manda_para_o_treino_o_que_tem_certeza() -> None:
    assert decidir([bruta(0, 0.9), bruta(2, 0.7)], 0.5, 0.25).destino == ROTULADAS
    duvida = decidir([bruta(0, 0.9), bruta(3, 0.35)], 0.5, 0.25)
    assert duvida.destino == REVISAR and duvida.duvidas == 1 and len(duvida.rotulos) == 2  # já vem preenchida
    assert decidir([bruta(0, 0.1)], 0.5, 0.25).rotulos == []  # muito fraco: ignorado
    vazia = decidir([], 0.5, 0.25)
    assert vazia.destino == ROTULADAS and vazia.rotulos == []  # sem objetos também ensina (folhas, sombras)


def test_rotular_pendentes(tmp_path: Path) -> None:
    conjunto = ConjuntoTreino(tmp_path)
    criar_amostra(tmp_path / PENDENTES, "certa", datetime(2026, 10, 11, 9))
    criar_amostra(tmp_path / PENDENTES, "duvida", datetime(2026, 10, 11, 10))
    (tmp_path / PENDENTES / "quebrada.jpg").write_bytes(b"nao e jpg")
    respostas = iter([[bruta(0, 0.95)], [bruta(0, 0.4)]])
    contagem = rotular_pendentes(conjunto, lambda _imgs: [next(respostas)], 0.5, 0.25, "yolo11x.pt")
    assert contagem == {ROTULADAS: 1, REVISAR: 1, "ilegiveis": 1}
    certa = Amostra(tmp_path / ROTULADAS / "certa.jpg")
    assert certa.rotulos()[0][0] == 0 and certa.meta()["rotulo"] == "professor" and "rotulada_em" in certa.meta()
    assert conjunto.contar() == {PENDENTES: 0, ROTULADAS: 1, REVISAR: 1}


# ===================================================================== decisão de troca
def test_so_troca_se_melhorar_e_nenhuma_classe_piorar() -> None:
    atual = Metricas(0.50, 0.70, {"PESSOA": 0.50, "CARRO": 0.60})
    assert decidir_promocao(atual, Metricas(0.56, 0.75, {"PESSOA": 0.55, "CARRO": 0.58}), 0.01, 0.05)[0]
    nao, motivo = decidir_promocao(atual, Metricas(0.505, 0.71, {"PESSOA": 0.5, "CARRO": 0.6}), 0.01, 0.05)
    assert not nao and "não melhorou o suficiente" in motivo
    nao, motivo = decidir_promocao(atual, Metricas(0.60, 0.80, {"PESSOA": 0.80, "CARRO": 0.40}), 0.01, 0.05)
    assert not nao and "piorou em CARRO" in motivo


def test_metricas_do_ultralytics() -> None:
    resultado = SimpleNamespace(box=SimpleNamespace(map=0.4, map50=0.6, ap_class_index=[0, 3, 56], ap=[0.5, 0.3, 0.9]))
    metricas = metricas_do_ultralytics(resultado)
    assert metricas.map == 0.4 and metricas.por_classe == {"PESSOA": 0.5, "MOTO": 0.3}  # 56 (cadeira) não é nossa
    assert Metricas.de_dict(metricas.para_dict()) == metricas


# ===================================================================== ciclo (peças falsas)
@pytest.fixture
def config_treino(tmp_path: Path) -> Config:
    copia = tmp_path / "config.yaml"
    copia.write_text(ARQUIVO_PADRAO.read_text(encoding="utf-8"), encoding="utf-8")
    config = Config.carregar(copia)
    (tmp_path / "modelos").mkdir()
    (tmp_path / "modelos" / "yolo11n.pt").write_bytes(b"pesos")
    for chave, valor in {"geral.pasta_dados": str(tmp_path / "dados"), "ia.pasta_modelos": str(tmp_path / "modelos"),
                         "ia.modelo": "yolo11n.pt", "treino.min_novas": 20, "treino.min_validacao": 2,
                         # o config.yaml real é do usuário (ele pode ter mudado os limites de memória)
                         "treino.memoria_minima_gb": 7.0, "treino.memoria_critica_gb": 1.5}.items():
        config.definir(chave, valor)
    pendentes = tmp_path / "dados" / "treino" / PENDENTES
    for hora in range(30):
        criar_amostra(pendentes, f"f{hora:02d}", datetime(2026, 10, 11, 0) + timedelta(hours=hora))
    return config


BASTANTE = Memoria(ram_livre_gb=6.0, reservavel_livre_gb=12.0)


def pecas_falsas(nota_atual: float, nota_nova: float, falhar: bool | BaseException = False,
                 memoria: list[Memoria] | None = None) -> tuple[Pecas, list[str]]:
    """``memoria``: leituras de memória livre em sequência (a última se repete); padrão = bastante."""
    chamadas: list[str] = []
    leituras = list(memoria or [BASTANTE])

    def medir_memoria() -> Memoria:
        return leituras.pop(0) if len(leituras) > 1 else leituras[0]

    def treinar(yaml: Path, base: Path, pasta: Path, ao_epoca: Callable[[int, int], None], vigiar) -> Path:  # noqa: ANN001
        chamadas.append(f"treinar {base.name}")
        if isinstance(falhar, BaseException):
            raise falhar
        if falhar:
            raise RuntimeError("placa de vídeo sem memória")
        for epoca in range(1, 4):
            for _lote in range(2):
                vigiar()  # o callback real roda a cada lote
            ao_epoca(epoca, 3)
        (pasta / "weights").mkdir(parents=True)
        (pasta / "weights" / "last.pt").write_bytes(b"novo")
        return pasta / "weights" / "last.pt"

    def validar(modelo: Path, yaml: Path, pasta: Path, vigiar) -> Metricas:  # noqa: ANN001
        chamadas.append(f"validar {modelo.name}")
        vigiar()
        nota = nota_nova if modelo.name == "last.pt" else nota_atual
        return Metricas(nota, nota + 0.2, {"PESSOA": nota})

    def professor() -> Callable:  # noqa: ANN202
        chamadas.append("professor")
        return lambda imagens: [[bruta(0, 0.9)] for _ in imagens]

    return Pecas(professor, treinar, validar, lambda _m: {0: "person"}, medir_memoria), chamadas


def test_ciclo_aprova_o_modelo_melhor(config_treino: Config) -> None:
    pecas, chamadas = pecas_falsas(0.40, 0.48)
    avisos: list[dict] = []
    rodada = executar_ciclo(config_treino, avisos.append, pecas)
    assert rodada.promovido and rodada.modelo_novo.startswith(PREFIXO_MODELO), rodada.motivo
    pasta_modelos = config_treino.resolver_caminho(str(config_treino.obter("ia.pasta_modelos")))
    assert (pasta_modelos / rodada.modelo_novo).read_bytes() == b"novo"
    assert chamadas == ["professor", "validar yolo11n.pt", "treinar yolo11n.pt", "validar last.pt"]
    assert rodada.rotuladas_agora == 30 and rodada.imagens_validacao >= 2 and rodada.epocas == 3
    assert any(a.get("etapa") == "treinando" and a.get("feito") == 3 for a in avisos)
    assert not (config_treino.pasta_dados / "treino" / "execucoes").exists() or not any(
        (config_treino.pasta_dados / "treino" / "execucoes").iterdir())  # temporários apagados
    assert Historico(config_treino.pasta_dados / "treino" / "historico.json").rodadas()[-1].modelo_novo == rodada.modelo_novo


def test_ciclo_descarta_o_modelo_pior(config_treino: Config) -> None:
    rodada = executar_ciclo(config_treino, lambda _a: None, pecas_falsas(0.40, 0.35)[0])
    assert not rodada.promovido and rodada.treinou and "não melhorou" in rodada.motivo
    pasta_modelos = config_treino.resolver_caminho(str(config_treino.obter("ia.pasta_modelos")))
    assert not list(pasta_modelos.glob(f"{PREFIXO_MODELO}*.pt"))


def test_ciclo_espera_juntar_imagens_novas(config_treino: Config) -> None:
    executar_ciclo(config_treino, lambda _a: None, pecas_falsas(0.40, 0.48)[0])  # 1ª rodada: 30 novas, treina
    pecas, chamadas = pecas_falsas(0.40, 0.48)
    rodada = executar_ciclo(config_treino, lambda _a: None, pecas)  # nada novo
    assert "poucas imagens novas" in rodada.motivo and not any(c.startswith("treinar") for c in chamadas)
    assert len(Historico(config_treino.pasta_dados / "treino" / "historico.json").rodadas()) == 1  # não polui o histórico
    rodada = executar_ciclo(config_treino, lambda _a: None, pecas, forcar=True)  # "Treinar agora"
    assert rodada.treinou


def test_ciclo_registra_falha_sem_quebrar(config_treino: Config) -> None:
    rodada = executar_ciclo(config_treino, lambda _a: None, pecas_falsas(0.4, 0.5, falhar=True)[0])
    assert rodada.erro == "placa de vídeo sem memória" and not rodada.promovido and "falhou" in rodada.motivo
    assert Historico(config_treino.pasta_dados / "treino" / "historico.json").rodadas()[-1].erro


# ===================================================================== memória (erro de 12/10/2026)
def test_medir_memoria_de_verdade() -> None:
    livre = medir()
    assert 0 < livre.ram_livre_gb <= livre.reservavel_livre_gb  # reservável = RAM + paginação
    assert "GB" in livre.texto()


def test_reconhece_falta_de_memoria() -> None:
    from numpy._core._exceptions import _ArrayMemoryError

    erro_numpy = _ArrayMemoryError((1280, 1280, 3), np.dtype(np.uint8))
    assert eh_falta_de_memoria(erro_numpy)
    assert eh_falta_de_memoria(SystemError("Caught SystemError in DataLoader worker process 1. Original "
                                           "numpy._core._exceptions._ArrayMemoryError: Unable to allocate 4.69 MiB"))
    assert eh_falta_de_memoria(RuntimeError("OpenCV(5.0.0) (-4:Insufficient memory) Failed to allocate 17915904 bytes"))
    assert eh_falta_de_memoria(OSError("[WinError 1455] O arquivo de paginação é muito pequeno"))
    assert not eh_falta_de_memoria(RuntimeError("placa de vídeo sem memória"))
    assert not eh_falta_de_memoria(ValueError("nada a ver"))
    assert falta_para(Memoria(2.0, 6.0), 5.0) is None
    assert "precisa de 5.0 GB" in (falta_para(Memoria(1.0, 3.2), 5.0) or "")


def test_orcamento_da_placa_cabe_na_memoria_do_pc() -> None:
    # PC com 7 GB livres, margem crítica 1,5: sobra 7 - 1,5 - 1 (folga do processo) = 4,5 GB para a placa
    assert orcamento_placa(Memoria(2.0, 7.0), 1.5, reservado_gb=0.0, livre_placa_gb=7.0) == pytest.approx(4.5)
    # o que o PyTorch já reservou já saiu da memória livre: entra de volta no orçamento total
    assert orcamento_placa(Memoria(2.0, 4.0), 1.5, reservado_gb=2.0, livre_placa_gb=5.0) == pytest.approx(3.5)
    # placa quase cheia (a IA do app também usa): vale o que cabe nela, deixando 0,5 GB
    assert orcamento_placa(Memoria(2.0, 12.0), 1.5, reservado_gb=0.0, livre_placa_gb=2.0) == pytest.approx(1.5)
    # sem memória: zero (o treino recusa abaixo de PLACA_MINIMA_GB)
    assert orcamento_placa(Memoria(0.5, 2.0), 1.5, reservado_gb=0.0, livre_placa_gb=7.0) == 0.0


def test_ciclo_nem_comeca_sem_memoria(config_treino: Config) -> None:
    pecas, chamadas = pecas_falsas(0.4, 0.5, memoria=[Memoria(0.8, 3.0)])
    rodada = executar_ciclo(config_treino, lambda _a: None, pecas)
    assert "pouca memória livre" in rodada.motivo and "3.0 GB" in rodada.motivo
    assert chamadas == []  # nem o professor foi carregado
    assert ConjuntoTreino(config_treino.pasta_dados / "treino").contar()[PENDENTES] == 30  # nada mexido
    assert not (config_treino.pasta_dados / "treino" / "historico.json").exists()  # não polui o histórico


def test_ciclo_para_se_a_memoria_ficar_critica_no_meio(config_treino: Config) -> None:
    # começa com bastante; durante o treino, outro programa come a memória (cai para 1 GB livre)
    leituras = [BASTANTE] * 33 + [Memoria(0.3, 1.0)]  # 1 no início + 30 imagens do professor + 1 validação + 1 lote
    pecas, chamadas = pecas_falsas(0.4, 0.5, memoria=leituras)
    avisos: list[dict] = []
    rodada = executar_ciclo(config_treino, avisos.append, pecas)
    assert "parou para não faltar memória" in rodada.motivo and "1.0 GB" in rodada.motivo, rodada.motivo
    assert rodada.erro and not rodada.promovido and rodada.epocas == 0
    assert chamadas == ["professor", "validar yolo11n.pt", "treinar yolo11n.pt"]  # parou no meio do treino
    pasta_modelos = config_treino.resolver_caminho(str(config_treino.obter("ia.pasta_modelos")))
    assert not list(pasta_modelos.glob(f"{PREFIXO_MODELO}*.pt"))
    assert not any((config_treino.pasta_dados / "treino" / "execucoes").glob("*"))  # temporários apagados
    assert Historico(config_treino.pasta_dados / "treino" / "historico.json").rodadas()[-1].erro  # fica registrado


def test_ciclo_explica_falta_de_ram(config_treino: Config) -> None:
    erro = SystemError("Caught SystemError in DataLoader worker process 1. Original "
                       "numpy._core._exceptions._ArrayMemoryError: Unable to allocate 4.69 MiB")
    rodada = executar_ciclo(config_treino, lambda _a: None, pecas_falsas(0.4, 0.5, falhar=erro)[0])
    assert rodada.motivo.startswith("faltou memória (RAM)") and "Feche outros programas" in rodada.motivo
    assert "DataLoader" in rodada.erro  # o erro original fica guardado (para investigar)


def test_treino_nao_olha_a_validacao(config_treino: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    """12/10/2026: escolher a "melhor época" pela validação e decidir com ela deixava a prova otimista
    (modelo aprovado com 0,868 x 0,852 dava 0,635 x 0,822 em imagens novas)."""
    import ultralytics

    from treino.ciclo import AjustesTreino, pecas_reais

    pedidos: list[dict] = []

    class YoloFalso:
        def __init__(self, _caminho: str) -> None:
            pass

        def add_callback(self, _evento: str, _funcao) -> None:  # noqa: ANN001
            pass

        def train(self, **opcoes) -> None:  # noqa: ANN003
            pedidos.append(opcoes)
            pesos = Path(opcoes["project"]) / opcoes["name"] / "weights"
            pesos.mkdir(parents=True)
            (pesos / "best.pt").write_bytes(b"escolhido pela validacao")
            (pesos / "last.pt").write_bytes(b"ultima epoca")

    monkeypatch.setattr(ultralytics, "YOLO", YoloFalso)
    config_treino.definir("ia.dispositivo", "cpu")
    pecas = pecas_reais(AjustesTreino.do_config(config_treino))
    pasta = config_treino.pasta_dados / "execucao" / "treino"
    pesos = pecas.treinar(Path("dados.yaml"), Path("base.pt"), pasta, lambda _e, _t: None, lambda: None)
    assert pedidos[0]["val"] is False and "patience" not in pedidos[0]  # sem "melhor época", sem parada antecipada
    assert pesos.name == "last.pt" and pesos.read_bytes() == b"ultima epoca"  # mesmo existindo um best.pt


def test_guarda_so_os_ultimos_modelos(tmp_path: Path) -> None:
    for dia in range(1, 6):
        (tmp_path / f"{PREFIXO_MODELO}2026100{dia}_0300.pt").write_bytes(b"x")
    apagados = limpar_modelos_antigos(tmp_path, 2, {f"{PREFIXO_MODELO}20261001_0300.pt"})  # o 1º está em uso
    assert sorted(apagados) == [f"{PREFIXO_MODELO}20261002_0300.pt", f"{PREFIXO_MODELO}20261003_0300.pt"]
    assert (tmp_path / f"{PREFIXO_MODELO}20261001_0300.pt").exists()


# ===================================================================== agenda e processo
def test_agenda_uma_vez_por_dia_a_partir_do_horario() -> None:
    hoje = datetime(2026, 10, 13, 2, 59)
    assert not deve_rodar(hoje, "03:00", None, True)
    assert deve_rodar(hoje.replace(hour=3), "03:00", None, True)
    assert deve_rodar(hoje.replace(hour=9), "03:00", date(2026, 10, 12), True)  # PC estava desligado às 3h
    assert not deve_rodar(hoje.replace(hour=9), "03:00", date(2026, 10, 13), True)  # já rodou hoje
    assert not deve_rodar(hoje.replace(hour=9), "03:00", None, False)  # desligado


@pytest.fixture
def servico_falso(tmp_path: Path) -> Iterator[ServicoTreino]:
    """O "processo de treino" é um script que só escreve avisos (rápido)."""
    script = tmp_path / "falso.py"
    script.write_text(
        "import json, sys, time\n"
        "for i in range(1, 4):\n"
        "    print(json.dumps({'etapa': 'treinando', 'texto': f'época {i} de 3', 'feito': i, 'total': 3}), flush=True)\n"
        "    time.sleep(0.05)\n"
        "if '--demorar' in sys.argv: time.sleep(30)\n"
        "print(json.dumps({'fim': True, 'rodada': {'inicio': '2026-10-13T03:00:00', 'promovido': True,"
        " 'modelo_novo': 'sentinela_20261013_0300.pt', 'motivo': 'melhorou: nota 0.852 \u2192 0.868 (não piorou)'}},"
        " ensure_ascii=False), flush=True)\n",
        encoding="utf-8",
    )
    copia = tmp_path / "config.yaml"
    copia.write_text(ARQUIVO_PADRAO.read_text(encoding="utf-8"), encoding="utf-8")
    config = Config.carregar(copia)
    config.definir("geral.pasta_dados", str(tmp_path / "dados"))
    config.definir("treino.memoria_minima_gb", 7.0)  # o do usuário pode ser outro
    demorar: list[str] = []
    memoria = [BASTANTE]
    servico = ServicoTreino(config, comando=lambda _forcar: [sys.executable, str(script), *demorar],
                            memoria=lambda: memoria[0])
    servico.demorar = demorar  # type: ignore[attr-defined]
    servico.memoria_livre = memoria  # type: ignore[attr-defined]
    yield servico
    servico.parar()


def test_agenda_adia_sem_memoria_e_tenta_de_novo(servico_falso: ServicoTreino) -> None:
    avisos: list[dict] = []
    servico_falso.assinar(avisos.append)
    servico_falso.config.definir("treino.automatico", True)
    servico_falso.config.definir("treino.horario", "03:00")
    servico_falso.memoria_livre[0] = Memoria(0.5, 2.0)  # type: ignore[attr-defined]
    madrugada = datetime(2026, 10, 13, 3, 0)
    assert not servico_falso.verificar_agenda(madrugada)  # adiou: não arrisca derrubar as câmeras
    assert servico_falso._ultimo_dia() is None  # noqa: SLF001 (o dia NÃO foi gasto)
    assert servico_falso.falta_memoria() is not None
    servico_falso.memoria_livre[0] = BASTANTE  # type: ignore[attr-defined]
    assert not servico_falso.verificar_agenda(madrugada + timedelta(minutes=10))  # espera os 30 min
    assert servico_falso.verificar_agenda(madrugada + timedelta(minutes=31))  # agora sim
    assert servico_falso._ultimo_dia() == madrugada.date()  # noqa: SLF001
    assert esperar(lambda: any(a.get("fim") for a in avisos))
    assert avisos[-1]["manual"] is False
    assert not servico_falso.verificar_agenda(madrugada + timedelta(hours=2))  # uma vez por dia


def test_servico_acompanha_o_processo(servico_falso: ServicoTreino) -> None:
    avisos: list[dict] = []
    servico_falso.assinar(avisos.append)
    assert servico_falso.treinar_agora()
    assert esperar(lambda: any(a.get("fim") for a in avisos))
    assert [a["feito"] for a in avisos if a.get("etapa") == "treinando"] == [1, 2, 3]
    final = avisos[-1]
    assert final["manual"] is True and final["rodada"]["modelo_novo"] == "sentinela_20261013_0300.pt"
    assert final["rodada"]["motivo"] == "melhorou: nota 0.852 → 0.868 (não piorou)"  # bug de 12/10: "→" no Windows
    assert esperar(lambda: not servico_falso.estado().rodando)


def test_cancelar_o_treino(servico_falso: ServicoTreino) -> None:
    servico_falso.demorar.append("--demorar")  # type: ignore[attr-defined]
    avisos: list[dict] = []
    servico_falso.assinar(avisos.append)
    assert servico_falso.treinar_agora()
    assert esperar(lambda: any(a.get("feito") == 3 for a in avisos))
    assert not servico_falso.treinar_agora()  # já tem um rodando
    servico_falso.cancelar()
    assert esperar(lambda: any(a.get("fim") for a in avisos), 10)
    assert avisos[-1]["rodada"]["erro"] == "cancelado"


def test_canal_de_avisos_aguenta_acentos_e_setas() -> None:
    """O processo de treino, com a saída num "pipe" (cp1252 no Windows), manda "→" e acentos intactos."""
    import os
    import subprocess

    from treino.servico import PASTA_PC

    filho = ("from treino.ciclo import canal_de_avisos; "
             "canal_de_avisos()({'fim': True, 'rodada': {'motivo': 'melhorou: nota 0.852 \u2192 0.868, não piorou'}})")
    ambiente = {k: v for k, v in os.environ.items() if k not in ("PYTHONIOENCODING", "PYTHONUTF8")}
    saida = subprocess.run([sys.executable, "-c", filho], cwd=PASTA_PC, capture_output=True, env=ambiente,
                           timeout=60, check=True)
    aviso = json.loads(saida.stdout.decode("utf-8"))
    assert aviso["rodada"]["motivo"] == "melhorou: nota 0.852 → 0.868, não piorou"


def test_historico_ida_e_volta(tmp_path: Path) -> None:
    historico = Historico(tmp_path / "historico.json")
    assert historico.rodadas() == [] and historico.ultima_que_treinou() is None
    historico.adicionar(Rodada(inicio="2026-10-12T03:00:00", motivo="poucas imagens"))
    historico.adicionar(Rodada(inicio="2026-10-13T03:00:00", novo=Metricas(0.5, 0.7, {}).para_dict(),
                               modelo_novo="sentinela_20261013_0300.pt", promovido=True))
    assert historico.ultima_que_treinou().inicio == "2026-10-13T03:00:00"  # type: ignore[union-attr]
    assert historico.da_modelo("sentinela_20261013_0300.pt").metricas_novo.map == 0.5  # type: ignore[union-attr]


def test_rotuladas_durante_a_rodada_nao_contam_como_novas(config_treino: Config) -> None:
    """Achado em 12/10/2026 (teste instável): a contagem de "imagens novas" usava o INÍCIO da última
    rodada; as rotuladas durante ela (que já entraram no treino dela) eram contadas de novo."""
    executar_ciclo(config_treino, lambda _a: None, pecas_falsas(0.40, 0.48)[0])
    arquivo = config_treino.pasta_dados / "treino" / "historico.json"
    dados = json.loads(arquivo.read_text(encoding="utf-8"))
    rodadas = dados if isinstance(dados, list) else dados["rodadas"]
    inicio = datetime.fromisoformat(rodadas[-1]["inicio"])
    rodadas[-1]["fim"] = (inicio + timedelta(seconds=2)).isoformat(timespec="seconds")  # a rodada durou 2 s
    arquivo.write_text(json.dumps(dados), encoding="utf-8")
    durante = (inicio + timedelta(seconds=1)).isoformat(timespec="seconds")
    for amostra in ConjuntoTreino(config_treino.pasta_dados / "treino").amostras(ROTULADAS):
        amostra.atualizar_meta(rotulada_em=durante)  # rotuladas NO MEIO da rodada (já usadas nela)
    pecas, chamadas = pecas_falsas(0.40, 0.48)
    rodada = executar_ciclo(config_treino, lambda _a: None, pecas)
    assert "poucas imagens novas para treinar (0 de" in rodada.motivo, rodada.motivo
    assert not any(c.startswith("treinar") for c in chamadas)
