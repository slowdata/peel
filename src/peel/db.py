"""Database layer com SQLite.

Decisão: sqlite3 da stdlib, sem ORM.
Razão: para estado local pequeno (<10MB, single-writer), sqlite3 é mais simples
e mais rápido que SQLAlchemy. Aprender SQL à mão é pedagogicamente valioso.

Schema:
- tracks: (spotify_uri, source_id) PRIMARY KEY — mesma faixa de várias fontes
- sources_state: source_id PRIMARY KEY — estado último de cada source
- unmatched: source_id + artist + title + seen_at — faixas não encontradas
- albums: (artist, album) PRIMARY KEY — álbuns curados (não vão para playlist)

Conexão: single connection longo-vivido (por run inteira como transacção conceptual).
Dates: ISO 8601 UTC via datetime.now(UTC).isoformat().
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import structlog

log = structlog.get_logger()


def iso_week(dt: datetime) -> str:
    """Converte datetime em string ISO week: '2026-W16'.

    Args:
        dt: datetime object (com ou sem timezone)

    Returns:
        String no formato 'YYYY-Www' (ex.: '2026-W16')
    """
    year, week, _ = dt.isocalendar()
    return f"{year}-W{week:02d}"


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

    def _ensure_column(self, table: str, column: str, sql_type: str) -> bool:
        """Adiciona coluna se não existir. Retorna True se adicionada (migração).

        Args:
            table: Nome da tabela
            column: Nome da coluna
            sql_type: Tipo SQL (ex.: "TEXT", "INTEGER")

        Returns:
            True se coluna foi adicionada, False se já existia
        """
        cols = [row[1] for row in self.conn.execute(f"PRAGMA table_info({table})").fetchall()]
        if column in cols:
            return False
        self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {sql_type}")
        self.conn.commit()
        log.info("db.column_added", table=table, column=column)
        return True

    def _backfill_week(self, table: str, timestamp_col: str) -> None:
        """Backfill added_at_week a partir de timestamp_col existente.

        Para cada linha com added_at_week NULL, parse o ISO 8601 timestamp
        e calcula a semana ISO.

        Args:
            table: Nome da tabela ("tracks" ou "albums")
            timestamp_col: Nome da coluna timestamp ("added_at" ou "seen_at")
        """
        cursor = self.conn.cursor()

        # Identifica PK para UPDATE posterior
        if table == "tracks":
            pk_cols = ["spotify_uri", "source_id"]
        elif table == "albums":
            pk_cols = ["artist", "album"]
        else:
            raise ValueError(f"Unknown table: {table}")

        # SELECT todas as linhas com NULL
        query = (
            f"SELECT {', '.join(pk_cols)}, {timestamp_col} FROM {table} WHERE added_at_week IS NULL"
        )
        rows = cursor.execute(query).fetchall()
        count_updated = 0

        for row in rows:
            pk_vals = row[: len(pk_cols)]
            timestamp_str = row[len(pk_cols)]

            try:
                # Parse ISO 8601 timestamp
                dt = datetime.fromisoformat(timestamp_str)
                week_str = iso_week(dt)

                # UPDATE a linha
                where_clause = " AND ".join([f"{col} = ?" for col in pk_cols])
                update_query = f"UPDATE {table} SET added_at_week = ? WHERE {where_clause}"
                cursor.execute(update_query, [week_str] + list(pk_vals))
                count_updated += 1
            except (ValueError, IndexError) as e:
                log.warning(
                    "db.backfill_week_parse_error",
                    table=table,
                    timestamp_str=timestamp_str,
                    error=str(e),
                )

        self.conn.commit()
        log.info("db.backfill_week_completed", table=table, count=count_updated)

    def init_schema(self) -> None:
        """Cria as 4 tabelas se não existirem (idempotente).

        Esta função é segura chamar múltiplas vezes.
        Após criar tabelas, executa migrações idempotentes (adiciona colunas novas se necessário).
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

        # Tabela: álbuns curados
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS albums (
                artist     TEXT NOT NULL,
                album      TEXT NOT NULL,
                source_id  TEXT NOT NULL,
                source_url TEXT,
                seen_at    TEXT NOT NULL,
                PRIMARY KEY (artist, album)
            )
            """
        )

        self.conn.commit()
        log.info("db.schema_initialized")

        # Migrações idempotentes: adiciona colunas novas se faltarem
        if self._ensure_column("tracks", "added_at_week", "TEXT"):
            self._backfill_week("tracks", "added_at")

        if self._ensure_column("albums", "added_at_week", "TEXT"):
            self._backfill_week("albums", "seen_at")

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
        now = datetime.now(UTC)
        self.conn.execute(
            """
            INSERT OR IGNORE INTO tracks
            (spotify_uri, source_id, artist, title, source_url, added_at, added_at_week)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (uri, source_id, artist, title, url, now.isoformat(), iso_week(now)),
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

    def record_album(self, artist: str, album: str, source_id: str, source_url: str | None) -> bool:
        """Insere um álbum se novo. Retorna True se inserido, False se já existia.

        Usa INSERT OR IGNORE com PRIMARY KEY (artist, album) e verifica rowcount.

        Args:
            artist: Nome do artista
            album: Nome do álbum
            source_id: ID da fonte
            source_url: URL opcional (link para review, etc.)

        Returns:
            True se o álbum foi inserido (novo), False se já existia.
        """
        cursor = self.conn.cursor()
        now = datetime.now(UTC)
        cursor.execute(
            """
            INSERT OR IGNORE INTO albums
            (artist, album, source_id, source_url, seen_at, added_at_week)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (artist, album, source_id, source_url, now.isoformat(), iso_week(now)),
        )
        self.conn.commit()
        inserted = cursor.rowcount > 0
        log.debug(
            "db.album_recorded",
            artist=artist,
            album=album,
            source_id=source_id,
            is_new=inserted,
        )
        return inserted

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

    def tracks_in_window(self, current_week: str, window: int) -> list[str]:
        """URIs distintos de tracks adicionadas nas últimas `window` semanas.

        Exemplo: current_week='2026-W16', window=2 → inclui W15 e W16.

        Args:
            current_week: Semana atual em formato ISO (ex.: '2026-W16')
            window: Número de semanas a incluir (ex.: 2 para semanas atuais + 1 anterior)

        Returns:
            Lista de URIs únicos, ordenados por added_at DESC (mais recentes primeiro)
        """
        # Calcula a semana cutoff: current_week - (window - 1)
        year, week = map(int, current_week.split("-W"))

        # Converte para datetime da primeira segunda-feira da semana
        cutoff_dt = datetime.fromisocalendar(year, week, 1) - timedelta(weeks=window - 1)
        cutoff_week_year, cutoff_week_num, _ = cutoff_dt.isocalendar()
        cutoff_week = f"{cutoff_week_year}-W{cutoff_week_num:02d}"

        # Query: tracks cuja semana >= cutoff_week
        cursor = self.conn.execute(
            """
            SELECT DISTINCT spotify_uri FROM tracks
            WHERE added_at_week >= ?
            ORDER BY added_at DESC
            """,
            (cutoff_week,),
        )
        return [row[0] for row in cursor.fetchall()]

    def close(self) -> None:
        """Fecha a conexão ao banco.

        Importante: liberta locks WAL (peel.db-wal, peel.db-shm) para que o
        ficheiro .db fique disponível para git commit no workflow.
        """
        self.conn.close()
        log.info("db.closed", path=self.path)
