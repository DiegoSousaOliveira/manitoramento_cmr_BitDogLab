"""Banco de dados SQLite: câmeras, zonas e eventos.

Decisões importantes (explicadas para quem está começando):

- **SQLite** é um banco inteiro dentro de um único arquivo (``dados/sentinela.db``),
  sem servidor para instalar. Ótimo para um app desktop.
- **Uma conexão compartilhada + trava (lock)**: a interface lê eventos enquanto a
  thread de gravação insere eventos novos. Para não haver duas threads usando a
  conexão ao mesmo tempo, toda operação passa por ``self._trava``.
- **Modo WAL** (write-ahead log): leituras não esperam escritas terminarem.
- **Migrações**: o número da versão do esquema fica em ``PRAGMA user_version``.
  Para mudar as tabelas no futuro, acrescente um script novo em ``MIGRACOES``;
  bancos antigos são atualizados sozinhos ao abrir.
- Datas são gravadas como texto ISO 8601 em horário local (``2026-10-11T21:35:12.000``),
  que ordena corretamente como texto.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

from core.modelos import CLASSES, Camera, DisposicaoLentes, Evento, TipoFonte

log = logging.getLogger(__name__)

_ESQUEMA_V1 = """
CREATE TABLE cameras (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    nome        TEXT    NOT NULL UNIQUE COLLATE NOCASE,
    codigo      TEXT    NOT NULL UNIQUE,
    fonte       TEXT    NOT NULL CHECK (fonte IN ('rtsp', 'arquivo', 'webcam')),
    ip          TEXT    NOT NULL DEFAULT '',
    porta_rtsp  INTEGER NOT NULL,
    usuario     TEXT    NOT NULL DEFAULT '',
    modelo_url  TEXT    NOT NULL DEFAULT '',
    porta_onvif INTEGER NOT NULL,
    canais      INTEGER NOT NULL DEFAULT 1 CHECK (canais >= 1),
    caminho     TEXT    NOT NULL DEFAULT '',
    tem_ptz     INTEGER NOT NULL DEFAULT 0,
    deteccao    INTEGER NOT NULL DEFAULT 1,
    gravar      INTEGER NOT NULL DEFAULT 1,
    ajustes     TEXT    NOT NULL DEFAULT '{}',
    ordem       INTEGER NOT NULL DEFAULT 0,
    criada_em   TEXT    NOT NULL
);

-- Zona de vigilância de cada canal (polígono normalizado, em JSON).
CREATE TABLE zonas (
    camera_id INTEGER NOT NULL REFERENCES cameras(id) ON DELETE CASCADE,
    canal     INTEGER NOT NULL,
    pontos    TEXT    NOT NULL,
    PRIMARY KEY (camera_id, canal)
);

CREATE TABLE eventos (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    camera_id   INTEGER NOT NULL REFERENCES cameras(id) ON DELETE CASCADE,
    canal       INTEGER NOT NULL DEFAULT 1,
    inicio      TEXT    NOT NULL,
    fim         TEXT,
    classes     TEXT    NOT NULL DEFAULT '{}',
    maior_conf  REAL    NOT NULL DEFAULT 0,
    alerta      INTEGER NOT NULL DEFAULT 0,
    pasta       TEXT    NOT NULL DEFAULT '',
    clipe       TEXT    NOT NULL DEFAULT '',
    foto        TEXT    NOT NULL DEFAULT '',
    foto_limpa  TEXT    NOT NULL DEFAULT '',
    deteccoes   TEXT    NOT NULL DEFAULT ''
);
CREATE INDEX idx_eventos_camera_inicio ON eventos (camera_id, inicio DESC);
CREATE INDEX idx_eventos_inicio ON eventos (inicio DESC);
"""

# v2 (Fase 1): câmeras com várias lentes numa mesma imagem. Zonas e eventos
# passam a ser por VISTA (canal x lente) em vez de por canal.
_MIGRACAO_V2 = """
ALTER TABLE cameras ADD COLUMN lentes TEXT NOT NULL DEFAULT 'unica'
    CHECK (lentes IN ('unica', 'vertical', 'horizontal'));
ALTER TABLE cameras ADD COLUMN lente_ptz INTEGER NOT NULL DEFAULT 1;
ALTER TABLE cameras ADD COLUMN nomes_vistas TEXT NOT NULL DEFAULT '{}';
ALTER TABLE zonas RENAME COLUMN canal TO vista;
ALTER TABLE eventos RENAME COLUMN canal TO vista;
"""

#: Scripts de migração, em ordem. O índice + 1 é a versão do esquema.
MIGRACOES: list[str] = [_ESQUEMA_V1, _MIGRACAO_V2]

_CAMPOS_CAMERA = (
    "nome", "codigo", "fonte", "ip", "porta_rtsp", "usuario", "modelo_url", "porta_onvif",
    "canais", "lentes", "caminho", "tem_ptz", "lente_ptz", "deteccao", "gravar", "ajustes",
    "nomes_vistas", "ordem",
)
_CAMPOS_EVENTO = (
    "camera_id", "vista", "inicio", "fim", "classes", "maior_conf", "alerta",
    "pasta", "clipe", "foto", "foto_limpa", "deteccoes",
)


class ErroBanco(Exception):
    """Falha no banco de dados (mensagem pronta para o usuário)."""


class ErroCadastro(ErroBanco):
    """Dado de cadastro em conflito (ex.: nome ou código já usados)."""


class Banco:
    """Acesso ao arquivo SQLite. Seguro para usar a partir de várias threads."""

    def __init__(self, caminho: Path) -> None:
        caminho = Path(caminho)
        caminho.parent.mkdir(parents=True, exist_ok=True)
        self.caminho = caminho
        self._trava = threading.RLock()
        self._con = sqlite3.connect(caminho, check_same_thread=False, timeout=10)
        self._con.row_factory = sqlite3.Row
        with self._trava:
            self._con.execute("PRAGMA foreign_keys = ON")
            self._con.execute("PRAGMA journal_mode = WAL")
            self._con.execute("PRAGMA synchronous = NORMAL")
            self._migrar()

    def fechar(self) -> None:
        with self._trava:
            self._con.close()

    def __enter__(self) -> Banco:
        return self

    def __exit__(self, *_: object) -> None:
        self.fechar()

    # ================================================================ esquema
    @property
    def versao(self) -> int:
        with self._trava:
            return int(self._con.execute("PRAGMA user_version").fetchone()[0])

    def _migrar(self) -> None:
        atual = self.versao
        if atual > len(MIGRACOES):
            raise ErroBanco(
                f"O banco {self.caminho.name} foi criado por uma versão mais nova do app "
                f"(esquema {atual}; este app conhece até {len(MIGRACOES)})."
            )
        for numero, script in enumerate(MIGRACOES[atual:], start=atual + 1):
            log.info("Atualizando o esquema do banco para a versão %d", numero)
            # executescript não participa do "with con:", então a transação é explícita.
            try:
                self._con.executescript(f"BEGIN;\n{script}\nPRAGMA user_version = {numero};\nCOMMIT;")
            except sqlite3.Error as erro:
                self._con.rollback()
                raise ErroBanco(f"Falha ao atualizar o banco para a versão {numero}: {erro}") from erro

    @contextmanager
    def _transacao(self, camera: Camera | None = None) -> Iterator[sqlite3.Connection]:
        """Executa um bloco com trava + commit/rollback, traduzindo erros de unicidade."""
        with self._trava:
            try:
                with self._con:
                    yield self._con
            except sqlite3.IntegrityError as erro:
                texto = str(erro)
                if "cameras.nome" in texto:
                    nome = camera.nome if camera else ""
                    raise ErroCadastro(f"Já existe uma câmera chamada '{nome}'.") from erro
                if "cameras.codigo" in texto:
                    codigo = camera.codigo if camera else ""
                    raise ErroCadastro(f"O código '{codigo}' já é usado por outra câmera.") from erro
                raise ErroBanco(f"Dados inválidos para o banco: {texto}") from erro

    # ================================================================ câmeras
    def adicionar_camera(self, camera: Camera) -> Camera:
        """Insere a câmera e devolve uma cópia com ``id``, ``ordem`` e ``criada_em`` preenchidos."""
        criada_em = datetime.now().replace(microsecond=0)
        valores = _parametros_camera(camera)
        with self._transacao(camera) as con:
            ordem = con.execute("SELECT COALESCE(MAX(ordem), 0) + 1 FROM cameras").fetchone()[0]
            valores["ordem"] = ordem
            colunas = ", ".join(_CAMPOS_CAMERA)
            marcadores = ", ".join(f":{c}" for c in _CAMPOS_CAMERA)
            cursor = con.execute(
                f"INSERT INTO cameras ({colunas}, criada_em) VALUES ({marcadores}, :criada_em)",
                {**valores, "criada_em": criada_em.isoformat()},
            )
        log.info("Câmera cadastrada: %s (código %s)", camera.nome, camera.codigo)
        return replace(camera, id=cursor.lastrowid, ordem=ordem, criada_em=criada_em)

    def atualizar_camera(self, camera: Camera) -> None:
        """Grava todas as alterações de uma câmera já cadastrada."""
        if camera.id is None:
            raise ValueError("A câmera ainda não foi cadastrada (id vazio).")
        atribuicoes = ", ".join(f"{c} = :{c}" for c in _CAMPOS_CAMERA)
        with self._transacao(camera) as con:
            cursor = con.execute(
                f"UPDATE cameras SET {atribuicoes} WHERE id = :id",
                {**_parametros_camera(camera), "id": camera.id},
            )
        if cursor.rowcount == 0:
            raise ErroBanco(f"A câmera de id {camera.id} não existe mais.")

    def remover_camera(self, camera_id: int) -> None:
        """Remove a câmera; zonas e registros de eventos saem junto (ON DELETE CASCADE).

        Os ARQUIVOS dos eventos não são apagados aqui — isso é decisão da interface.
        """
        with self._transacao() as con:
            con.execute("DELETE FROM cameras WHERE id = ?", (camera_id,))

    def obter_camera(self, camera_id: int) -> Camera | None:
        with self._trava:
            linha = self._con.execute("SELECT * FROM cameras WHERE id = ?", (camera_id,)).fetchone()
        return _camera_de_linha(linha) if linha else None

    def obter_camera_por_nome(self, nome: str) -> Camera | None:
        """Busca sem diferenciar maiúsculas/minúsculas."""
        with self._trava:
            linha = self._con.execute("SELECT * FROM cameras WHERE nome = ?", (nome,)).fetchone()
        return _camera_de_linha(linha) if linha else None

    def listar_cameras(self) -> list[Camera]:
        with self._trava:
            linhas = self._con.execute("SELECT * FROM cameras ORDER BY ordem, id").fetchall()
        return [_camera_de_linha(linha) for linha in linhas]

    def nome_em_uso(self, nome: str, ignorar_id: int | None = None) -> bool:
        """``True`` se outra câmera já tem esse nome (ignorando maiúsculas/minúsculas)."""
        with self._trava:
            linha = self._con.execute(
                "SELECT 1 FROM cameras WHERE nome = ? AND id IS NOT ?", (nome, ignorar_id)
            ).fetchone()
        return linha is not None

    def codigos_em_uso(self, ignorar_id: int | None = None) -> set[str]:
        with self._trava:
            linhas = self._con.execute("SELECT codigo FROM cameras WHERE id IS NOT ?", (ignorar_id,)).fetchall()
        return {linha[0] for linha in linhas}

    # ================================================================ zonas
    def salvar_zona(self, camera_id: int, vista: int, pontos: list[tuple[float, float]] | None) -> None:
        """Grava o polígono da zona de uma vista. ``None`` (ou lista vazia) = quadro inteiro."""
        with self._transacao() as con:
            if not pontos:
                con.execute("DELETE FROM zonas WHERE camera_id = ? AND vista = ?", (camera_id, vista))
                return
            con.execute(
                "INSERT INTO zonas (camera_id, vista, pontos) VALUES (?, ?, ?) "
                "ON CONFLICT (camera_id, vista) DO UPDATE SET pontos = excluded.pontos",
                (camera_id, vista, json.dumps([[round(x, 5), round(y, 5)] for x, y in pontos])),
            )

    def obter_zona(self, camera_id: int, vista: int) -> list[tuple[float, float]] | None:
        with self._trava:
            linha = self._con.execute(
                "SELECT pontos FROM zonas WHERE camera_id = ? AND vista = ?", (camera_id, vista)
            ).fetchone()
        return [(float(x), float(y)) for x, y in json.loads(linha[0])] if linha else None

    # ================================================================ eventos
    def adicionar_evento(self, evento: Evento) -> Evento:
        with self._transacao() as con:
            colunas = ", ".join(_CAMPOS_EVENTO)
            marcadores = ", ".join(f":{c}" for c in _CAMPOS_EVENTO)
            cursor = con.execute(
                f"INSERT INTO eventos ({colunas}) VALUES ({marcadores})", _parametros_evento(evento)
            )
        return replace(evento, id=cursor.lastrowid)

    def atualizar_evento(self, evento: Evento) -> None:
        if evento.id is None:
            raise ValueError("O evento ainda não foi gravado (id vazio).")
        atribuicoes = ", ".join(f"{c} = :{c}" for c in _CAMPOS_EVENTO)
        with self._transacao() as con:
            con.execute(
                f"UPDATE eventos SET {atribuicoes} WHERE id = :id", {**_parametros_evento(evento), "id": evento.id}
            )

    def obter_evento(self, evento_id: int) -> Evento | None:
        with self._trava:
            linha = self._con.execute("SELECT * FROM eventos WHERE id = ?", (evento_id,)).fetchone()
        return _evento_de_linha(linha) if linha else None

    def listar_eventos(
        self,
        camera_id: int | None = None,
        dia: date | None = None,
        classe: str | None = None,
        so_alertas: bool = False,
        antes_de: datetime | None = None,
        limite: int | None = 500,
    ) -> list[Evento]:
        """Eventos do mais recente para o mais antigo, com filtros opcionais."""
        condicoes: list[str] = []
        parametros: list[Any] = []
        if camera_id is not None:
            condicoes.append("camera_id = ?")
            parametros.append(camera_id)
        if dia is not None:
            inicio_dia = datetime.combine(dia, time.min)
            condicoes.append("inicio >= ? AND inicio < ?")
            parametros += [_texto_data(inicio_dia), _texto_data(inicio_dia + timedelta(days=1))]
        if classe is not None:
            if classe not in CLASSES:
                raise ValueError(f"Classe desconhecida: {classe}")
            condicoes.append("json_extract(classes, ?) IS NOT NULL")
            parametros.append(f"$.{classe}")
        if so_alertas:
            condicoes.append("alerta = 1")
        if antes_de is not None:
            condicoes.append("inicio < ?")
            parametros.append(_texto_data(antes_de))

        sql = "SELECT * FROM eventos"
        if condicoes:
            sql += " WHERE " + " AND ".join(condicoes)
        sql += " ORDER BY inicio DESC, id DESC"
        if limite is not None:
            sql += " LIMIT ?"
            parametros.append(limite)
        with self._trava:
            linhas = self._con.execute(sql, parametros).fetchall()
        return [_evento_de_linha(linha) for linha in linhas]

    def contar_eventos(self, camera_id: int) -> int:
        with self._trava:
            linha = self._con.execute("SELECT COUNT(*) FROM eventos WHERE camera_id = ?", (camera_id,)).fetchone()
        return int(linha[0])

    def remover_evento(self, evento_id: int) -> None:
        """Remove só o registro; apagar os arquivos é tarefa de ``eventos/retencao.py``."""
        with self._transacao() as con:
            con.execute("DELETE FROM eventos WHERE id = ?", (evento_id,))


# ==================================================================== conversões
def _texto_data(valor: datetime) -> str:
    return valor.isoformat(timespec="milliseconds")


def _parametros_camera(camera: Camera) -> dict[str, Any]:
    return {
        "nome": camera.nome,
        "codigo": camera.codigo,
        "fonte": str(camera.fonte),
        "ip": camera.ip,
        "porta_rtsp": camera.porta_rtsp,
        "usuario": camera.usuario,
        "modelo_url": camera.modelo_url,
        "porta_onvif": camera.porta_onvif,
        "canais": camera.canais,
        "lentes": str(camera.lentes),
        "caminho": camera.caminho,
        "tem_ptz": int(camera.tem_ptz),
        "lente_ptz": camera.lente_ptz,
        "deteccao": int(camera.deteccao),
        "gravar": int(camera.gravar),
        "ajustes": json.dumps(camera.ajustes, ensure_ascii=False),
        "nomes_vistas": json.dumps({str(k): v for k, v in camera.nomes_vistas.items()}, ensure_ascii=False),
        "ordem": camera.ordem,
    }


def _camera_de_linha(linha: sqlite3.Row) -> Camera:
    return Camera(
        id=linha["id"],
        nome=linha["nome"],
        codigo=linha["codigo"],
        fonte=TipoFonte(linha["fonte"]),
        ip=linha["ip"],
        porta_rtsp=linha["porta_rtsp"],
        usuario=linha["usuario"],
        modelo_url=linha["modelo_url"],
        porta_onvif=linha["porta_onvif"],
        canais=linha["canais"],
        lentes=DisposicaoLentes(linha["lentes"]),
        caminho=linha["caminho"],
        tem_ptz=bool(linha["tem_ptz"]),
        lente_ptz=linha["lente_ptz"],
        deteccao=bool(linha["deteccao"]),
        gravar=bool(linha["gravar"]),
        ajustes=json.loads(linha["ajustes"]),
        nomes_vistas={int(k): v for k, v in json.loads(linha["nomes_vistas"]).items()},
        ordem=linha["ordem"],
        criada_em=datetime.fromisoformat(linha["criada_em"]),
    )


def _parametros_evento(evento: Evento) -> dict[str, Any]:
    return {
        "camera_id": evento.camera_id,
        "vista": evento.vista,
        "inicio": _texto_data(evento.inicio),
        "fim": _texto_data(evento.fim) if evento.fim else None,
        "classes": json.dumps(evento.classes),
        "maior_conf": evento.maior_conf,
        "alerta": int(evento.alerta),
        "pasta": evento.pasta,
        "clipe": evento.clipe,
        "foto": evento.foto,
        "foto_limpa": evento.foto_limpa,
        "deteccoes": evento.deteccoes,
    }


def _evento_de_linha(linha: sqlite3.Row) -> Evento:
    return Evento(
        id=linha["id"],
        camera_id=linha["camera_id"],
        vista=linha["vista"],
        inicio=datetime.fromisoformat(linha["inicio"]),
        fim=datetime.fromisoformat(linha["fim"]) if linha["fim"] else None,
        classes=json.loads(linha["classes"]),
        maior_conf=linha["maior_conf"],
        alerta=bool(linha["alerta"]),
        pasta=linha["pasta"],
        clipe=linha["clipe"],
        foto=linha["foto"],
        foto_limpa=linha["foto_limpa"],
        deteccoes=linha["deteccoes"],
    )
