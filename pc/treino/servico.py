"""Lado do app do aprendizado contínuo: agenda, dispara e acompanha as rodadas de treino.

- Uma vez por dia, a partir de ``treino.horario`` (ex.: 03:00), se ``treino.automatico``
  estiver ligado, dispara uma rodada (``python -m treino.ciclo``) num processo à parte, com
  prioridade baixa e sem janela. Se o PC estava desligado às 03:00, roda quando ligar.
- O botão "Treinar agora" dispara na hora (mesmo com poucas imagens novas).
- Lê os avisos do processo (uma linha JSON por aviso) e repassa a quem assinou.
- Quem TROCA o modelo em uso é a janela (``trocar_automatico``): ela grava ``ia.modelo`` no
  config.yaml e pede para a IA recarregar — sem reiniciar o app.
- Memória: com menos de ``treino.memoria_minima_gb`` livres, a rodada automática é ADIADA (nova
  tentativa a cada ``ADIAR_S``) em vez de arriscar derrubar as câmeras; "Treinar agora" explica.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

from core.config import Config
from treino.dados import ConjuntoTreino
from treino.historico import Historico, Rodada
from treino.memoria import Memoria, falta_para, medir

log = logging.getLogger(__name__)
PASTA_PC = Path(__file__).resolve().parent.parent
ARQUIVO_ESTADO = "agenda.json"
ADIAR_S = 30 * 60  # rodada automática sem memória livre: tenta de novo depois disto


def deve_rodar(agora: datetime, horario: str, ultimo_dia: date | None, automatico: bool) -> bool:
    """Roda uma vez por dia, a partir do horário (se o PC estava desligado, roda quando ligar)."""
    if not automatico:
        return False
    try:
        hora = time.fromisoformat(horario)
    except ValueError:
        hora = time(3, 0)
    return agora.time() >= hora and ultimo_dia != agora.date()


@dataclass(frozen=True)
class EstadoTreino:
    rodando: bool = False
    etapa: str = ""
    texto: str = ""
    feito: int = 0
    total: int = 0


class ServicoTreino:
    def __init__(self, config: Config, comando: Callable[[bool], list[str]] | None = None,
                 memoria: Callable[[], Memoria] = medir) -> None:
        """``comando(forcar)``: o que executar; ``memoria()``: memória livre (nos testes, falsos)."""
        self.config = config
        self._comando = comando or self._comando_padrao
        self._memoria = memoria
        self._adiado_ate: datetime | None = None
        self._processo: subprocess.Popen[str] | None = None
        self._trava = threading.Lock()
        self._estado = EstadoTreino()
        self._manual = False
        self._assinantes: list[Callable[[dict[str, Any]], None]] = []
        self._parar = threading.Event()
        self._agenda = threading.Thread(target=self._agendar, name="treino-agenda", daemon=True)

    # ------------------------------------------------------------ pastas e dados
    @property
    def pasta(self) -> Path:
        return self.config.pasta_dados / "treino"

    def conjunto(self) -> ConjuntoTreino:
        return ConjuntoTreino(self.pasta)

    def historico(self) -> list[Rodada]:
        return Historico(self.pasta / "historico.json").rodadas()

    def estado(self) -> EstadoTreino:
        with self._trava:
            return self._estado

    def falta_memoria(self) -> str | None:
        """Por que NÃO dá para treinar agora (pouca memória livre), ou ``None``."""
        return falta_para(self._memoria(), float(self.config.obter("treino.memoria_minima_gb")))

    def assinar(self, funcao: Callable[[dict[str, Any]], None]) -> None:
        """``funcao(aviso)`` a cada linha do processo e no fim (``{"fim": True, "rodada": {...}}``)."""
        self._assinantes.append(funcao)

    # ------------------------------------------------------------ ciclo de vida
    def iniciar(self) -> ServicoTreino:
        self._agenda.start()
        return self

    def parar(self) -> None:
        self._parar.set()
        self.cancelar()

    def treinar_agora(self) -> bool:
        """Dispara uma rodada já (mesmo com poucas imagens novas). False se já houver uma rodando."""
        return self._disparar(forcar=True)

    def cancelar(self) -> None:
        with self._trava:
            processo = self._processo
        if processo is None or processo.poll() is not None:
            return
        log.info("Treino cancelado")
        if os.name == "nt":  # o treino abre processos auxiliares: derruba a "árvore" inteira
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(processo.pid)], capture_output=True, check=False)
        else:
            processo.kill()

    # ------------------------------------------------------------ agenda
    def _ultimo_dia(self) -> date | None:
        try:
            dados = json.loads((self.pasta / ARQUIVO_ESTADO).read_text(encoding="utf-8"))
            return date.fromisoformat(str(dados["ultimo_dia"]))
        except (OSError, ValueError, KeyError):
            return None

    def _marcar_dia(self, dia: date) -> None:
        self.pasta.mkdir(parents=True, exist_ok=True)
        (self.pasta / ARQUIVO_ESTADO).write_text(json.dumps({"ultimo_dia": dia.isoformat()}), encoding="utf-8")

    def _agendar(self) -> None:
        while not self._parar.wait(30):
            try:
                self.verificar_agenda(datetime.now())
            except Exception:  # noqa: BLE001
                log.exception("Falha na agenda do treino")

    def verificar_agenda(self, agora: datetime) -> bool:
        """Uma verificação da agenda (a thread chama a cada 30 s). True se disparou uma rodada."""
        automatico = bool(self.config.obter("treino.automatico"))
        if not deve_rodar(agora, str(self.config.obter("treino.horario")), self._ultimo_dia(), automatico):
            return False
        if self._adiado_ate is not None and agora < self._adiado_ate:
            return False
        if (motivo := self.falta_memoria()) is not None:
            self._adiado_ate = agora + timedelta(seconds=ADIAR_S)
            log.warning("Treino automático adiado para %s: %s", f"{self._adiado_ate:%H:%M}", motivo)
            return False
        self._adiado_ate = None
        self._marcar_dia(agora.date())  # uma tentativa por dia, mesmo se não houver o que treinar
        return self._disparar(forcar=False)

    # ------------------------------------------------------------ processo
    def _comando_padrao(self, forcar: bool) -> list[str]:
        comando = [sys.executable, "-m", "treino.ciclo", "--config", str(self.config.caminho)]
        return comando + (["--forcar"] if forcar else [])

    def _disparar(self, forcar: bool) -> bool:
        with self._trava:
            if self._processo is not None and self._processo.poll() is None:
                return False
            opcoes: dict[str, Any] = {}
            if os.name == "nt":  # sem janela preta e com prioridade baixa (o app continua leve)
                opcoes["creationflags"] = subprocess.CREATE_NO_WINDOW | subprocess.BELOW_NORMAL_PRIORITY_CLASS
            log.info("Rodada de treino iniciada (%s)", "manual" if forcar else "automática")
            self._manual = forcar
            self._processo = subprocess.Popen(  # noqa: S603 (comando fixo, sem shell)
                self._comando(forcar), cwd=PASTA_PC, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace",
                env={**os.environ, "PYTHONIOENCODING": "utf-8"},  # Windows: o "pipe" seria cp1252 (sem "→")
                **opcoes,
            )
            self._estado = EstadoTreino(rodando=True, etapa="iniciando", texto="Preparando o treino…")
            processo = self._processo
        threading.Thread(target=self._acompanhar, args=(processo,), name="treino-acompanhar", daemon=True).start()
        return True

    def _acompanhar(self, processo: subprocess.Popen[str]) -> None:
        final: dict[str, Any] | None = None
        assert processo.stdout is not None
        for linha in processo.stdout:
            try:
                aviso = json.loads(linha)
            except ValueError:
                continue  # algo que não é aviso (não deveria acontecer)
            if not isinstance(aviso, dict):
                continue
            if aviso.get("fim"):
                aviso["manual"] = self._manual
                final = aviso
            else:
                with self._trava:
                    self._estado = EstadoTreino(True, str(aviso.get("etapa", "")), str(aviso.get("texto", "")),
                                                int(aviso.get("feito", 0)), int(aviso.get("total", 0)))
            self._avisar(aviso)
        codigo = processo.wait()
        with self._trava:
            self._estado = EstadoTreino()
        if final is None:  # cancelado ou caiu antes de terminar
            motivo = "cancelado" if codigo != 0 else "terminou sem resultado"
            final = {"fim": True, "manual": self._manual,
                     "rodada": Rodada(inicio=datetime.now().isoformat(timespec="seconds"), motivo=motivo,
                                      erro=motivo).para_dict()}
            self._avisar(final)
        log.info("Rodada de treino terminou: %s", final["rodada"].get("motivo", ""))

    def _avisar(self, aviso: dict[str, Any]) -> None:
        for funcao in list(self._assinantes):
            try:
                funcao(aviso)
            except Exception:  # noqa: BLE001
                log.exception("Assinante do treino falhou")
