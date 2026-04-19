"""Database layer com SQLite.

Decisão: sqlite3 da stdlib, sem ORM.
Razão: para estado local pequeno (<10MB, single-writer), sqlite3 é mais simples
e mais rápido que SQLAlchemy. Aprender SQL à mão é pedagogicamente valioso.

Schema:
- tracks: (spotify_uri, source_id) PRIMARY KEY — mesma faixa de várias fontes
- sources_state: source_id PRIMARY KEY — estado último de cada source
- unmatched: source_id + artist + title + seen_at — faixas não encontradas

Conexão: single connection longo-vivido (por run inteira como transacção conceptual).
Dates: ISO 8601 UTC via datetime.now(UTC).isoformat().
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import structlog

log = structlog.get_logger()


class DB:
    """Gerenciador de estado com SQLite."""

    def __init__(self, path: str) -> None:
        """Inicializa a conexão ao banco.

        Args:
            path: Caminho do ficheiro .db (ex.: "data/peel.db")
        """
        self.path = path
        # Garante que o diretório existe
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        # Conexão longo-vivida (per-run)
        self.conn = sqlite3.connect(path)
        log.info("db.connected", path=path)

    def init_schema(self) -> None:
        """Cria as 3 tabelas se não existirem (idempotente).

        Esta função é segura chamar múltiplas vezes.
        """
        cursor = self.conn.cursor()

        # Tabela: tracks vistas (pode vir de múltiplas fontes)
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS tracks (
                spotify_uri TEXT NOT NULL,
                source_id   TEXT NOT NULL,
                artist      TEXT NOT NULL,
                title       TEXT NOT NULL,
                source_url  TEXT,
                added_at    TEXT NOT NULL,
                PRIMARY KEY (spotify_uri, source_id)
            )
            """
        )

        # Tabela: estado de cada source
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS sources_state (
                source_id    TEXT PRIMARY KEY,
                last_run_at  TEXT NOT NULL,
                last_status  TEXT NOT NULL,
                last_error   TEXT
            )
            """
        )

        # Tabela: faixas não-emparelhadas (para auditoria)
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS unmatched (
                source_id    TEXT NOT NULL,
                artist       TEXT NOT NULL,
                title        TEXT NOT NULL,
                seen_at      TEXT NOT NULL
            )
            """
        )

        self.conn.commit()
        log.info("db.schema_initialized")

    def already_added(self, spotify_uri: str) -> bool:
        """Verifica se um URI já foi adicionado (por qualquer source).

        Args:
            spotify_uri: Spotify track URI (ex.: "spotify:track:4cOd...")

        Returns:
            True se existe em `tracks`, False caso contrário.
        """
        cursor = self.conn.execute(
            "SELECT 1 FROM tracks WHERE spotify_uri = ? LIMIT 1",
            (spotify_uri,),
        )
        return cursor.fetchone() is not None

    def record_track(
        self,
        uri: str,
        source_id: str,
        artist: str,
        title: str,
        url: str | None,
    ) -> None:
        """Regista uma faixa adicionada (ou ignora se duplicate key).

        Idempotente: chamar 2x com mesma (uri, source_id) só adiciona uma vez.

        Args:
            uri: Spotify track URI
            source_id: ID da fonte (ex.: "pitchfork_bnt")
            artist: Nome do artista
            title: Título da faixa
            url: URL opcional (link para review, etc.)
        """
        self.conn.execute(
            """
            INSERT OR IGNORE INTO tracks
            (spotify_uri, source_id, artist, title, source_url, added_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (uri, source_id, artist, title, url, datetime.now(UTC).isoformat()),
        )
        self.conn.commit()
        log.debug("db.track_recorded", uri=uri, source_id=source_id)

    def record_unmatched(
        self,
        source_id: str,
        artist: str,
        title: str,
    ) -> None:
        """Regista uma faixa que não foi encontrada no Spotify.

        Útil para auditoria: depois podes rever quais faixas falharam matching.

        Args:
            source_id: ID da fonte
            artist: Nome do artista
            title: Título da faixa
        """
        self.conn.execute(
            """
            INSERT INTO unmatched
            (source_id, artist, title, seen_at)
            VALUES (?, ?, ?, ?)
            """,
            (source_id, artist, title, datetime.now(UTC).isoformat()),
        )
        self.conn.commit()
        log.debug("db.unmatched_recorded", source_id=source_id, artist=artist, title=title)

    def update_source_state(
        self,
        source_id: str,
        status: str,
        error: str | None = None,
    ) -> None:
        """Atualiza o estado último de uma source.

        Args:
            source_id: ID da fonte
            status: "ok" ou "error"
            error: Mensagem de erro (opcional, None se status == "ok")
        """
        self.conn.execute(
            """
            INSERT OR REPLACE INTO sources_state
            (source_id, last_run_at, last_status, last_error)
            VALUES (?, ?, ?, ?)
            """,
            (source_id, datetime.now(UTC).isoformat(), status, error),
        )
        self.conn.commit()
        log.debug("db.source_state_updated", source_id=source_id, status=status)

    def close(self) -> None:
        """Fecha a conexão ao banco.

        Importante: liberta locks WAL (peel.db-wal, peel.db-shm) para que o
        ficheiro .db fique disponível para git commit no workflow.
        """
        self.conn.close()
        log.info("db.closed", path=self.path)
