"""Database layer com SQLite.

Decisão: sqlite3 da stdlib, sem ORM.
Razão: para estado local pequeno (<10MB, single-writer), sqlite3 é mais simples
e mais rápido que SQLAlchemy. Aprender SQL à mão é pedagogicamente valioso.

Schema:
- tracks: (spotify_uri, source_id) PRIMARY KEY — mesma faixa de várias fontes
- sources_state: source_id PRIMARY KEY — estado último de cada source
- unmatched: source_id + artist + title + source_url + seen_at — faixas não encontradas
- feedback: spotify_uri PRIMARY KEY — feedback do utilizador por track
- albums: (artist, album) PRIMARY KEY — álbuns curados (não vão para playlist)
- album_mentions: uma linha por source/álbum para consenso cross-source
- source_runs: histórico por source/run para métricas futuras

Conexão: single connection longo-vivido (por run inteira como transacção conceptual).
Dates: ISO 8601 UTC via datetime.now(UTC).isoformat().
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Iterable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path

import structlog

from peel.matcher import normalize
from peel.models import ReviewQueueItem

FEEDBACK_RATINGS: dict[str, int] = {
    "love": 2,
    "like": 1,
    "meh": 0,
    "skip": -1,
    "ban": -2,
}

WindowTrackRow = tuple[str, str, str, str, str]
SourceQuality = tuple[float, float]  # (avg_rating, score)

log = structlog.get_logger()


def rank_window_uris(
    rows: Iterable[WindowTrackRow],
    source_quality: Mapping[str, SourceQuality] | None = None,
    affinity_scorer: Callable[[str], float] | None = None,
) -> list[str]:
    """Ordena URIs de uma janela por qualidade, deduplicando por faixa.

    Chave, por ordem: consenso (nº de sources distinas), melhor avg_rating de
    source, melhor score de source e recência. Sources sem score são neutras
    (0, 0).

    DEDUP DE FAIXA: o mesmo (artista, título) pode aparecer com URIs Spotify
    diferentes (variantes de edição/região/clean/explicit). Se deduplicassemos
    só por URI, a mesma música apareceria duas vezes na playlist. Aqui
    bucketeamos por (artista, título) normalizados e escolhemos um URI
    representativo — o com mais sources (consenso); empate → mais recente →
    URI lexicográfico (determinismo). O consenso agregado cruza URIs: uma faixa
    citada por 3 fontes (em 2 URIs) continua a contar como consenso 3.
    """
    quality = source_quality or {}
    # bucket: (artist_key, title_key) -> estado agregado + mapa per-URI
    buckets: dict[tuple[str, str], dict[str, object]] = {}
    for spotify_uri, artist, title, source_id, added_at in rows:
        uri = str(spotify_uri)
        key = (normalize(str(artist)), normalize(str(title)))
        bucket = buckets.setdefault(
            key,
            {
                "key": key,
                "artist": str(artist),
                "uris": set(),
                "uri_sources": {},  # uri -> set[source_id]
                "uri_latest_ts": {},  # uri -> float
                "sources": set(),  # consenso agregado (cruza URIs)
                "best_avg": None,
                "best_score": None,
                "latest_ts": 0.0,
            },
        )
        uris = bucket["uris"]
        assert isinstance(uris, set)
        uris.add(uri)
        uri_sources = bucket["uri_sources"]
        assert isinstance(uri_sources, dict)
        uri_sources.setdefault(uri, set()).add(str(source_id))
        uri_latest = bucket["uri_latest_ts"]
        assert isinstance(uri_latest, dict)
        ts = _timestamp_sort_value(added_at)
        uri_latest[uri] = max(float(uri_latest.get(uri, 0.0)), ts)
        sources = bucket["sources"]
        assert isinstance(sources, set)
        sources.add(str(source_id))
        avg_rating, score = quality.get(str(source_id), (0.0, 0.0))
        current_avg = bucket["best_avg"]
        current_score = bucket["best_score"]
        bucket["best_avg"] = (
            avg_rating if current_avg is None else max(float(current_avg), avg_rating)
        )
        bucket["best_score"] = score if current_score is None else max(float(current_score), score)
        bucket["latest_ts"] = max(float(bucket["latest_ts"]), ts)

    # Escolhe o URI representativo de cada faixa: mais sources (consenso),
    # depois mais recente, depois URI lexicográfico (determinismo).
    for bucket in buckets.values():
        uri_sources = bucket["uri_sources"]
        uri_latest = bucket["uri_latest_ts"]
        assert isinstance(uri_sources, dict) and isinstance(uri_latest, dict)
        bucket["uri"] = min(
            bucket["uris"],  # type: ignore[arg-type]
            key=lambda u: (-len(uri_sources[u]), -uri_latest[u], u),
        )

    def sort_key(bucket: dict[str, object]) -> tuple[int, float, float, float, float, str]:
        sources = bucket["sources"]
        assert isinstance(sources, set)
        affinity = affinity_scorer(str(bucket["artist"])) if affinity_scorer else 0.0
        return (
            -len(sources),
            -float(bucket["best_avg"] or 0.0),
            -float(bucket["best_score"] or 0.0),
            -float(affinity),
            -float(bucket["latest_ts"]),
            str(bucket["uri"]),
        )

    return [str(bucket["uri"]) for bucket in sorted(buckets.values(), key=sort_key)]


def _timestamp_sort_value(value: str) -> float:
    try:
        return datetime.fromisoformat(value).timestamp()
    except ValueError:
        return 0.0


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
        """Cria as tabelas se não existirem (idempotente).

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
                source_url   TEXT,
                seen_at      TEXT NOT NULL
            )
            """
        )

        # Tabela: feedback do utilizador
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS feedback (
                spotify_uri TEXT PRIMARY KEY,
                rating      INTEGER NOT NULL,
                label       TEXT NOT NULL,
                comment     TEXT,
                rated_at    TEXT NOT NULL
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

        # Tabela: menções de álbuns por source.
        # DECISÃO: manter `albums` como dedupe canónico e pôr consenso aqui,
        # evitando recriar a tabela antiga ou perder dados existentes.
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS album_mentions (
                artist            TEXT NOT NULL,
                album             TEXT NOT NULL,
                artist_key        TEXT NOT NULL,
                album_key         TEXT NOT NULL,
                source_id         TEXT NOT NULL,
                source_url        TEXT,
                spotify_album_uri TEXT,
                seen_at           TEXT NOT NULL,
                added_at_week     TEXT NOT NULL,
                PRIMARY KEY (artist_key, album_key, source_id)
            )
            """
        )
        cursor.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_album_mentions_week
            ON album_mentions (added_at_week)
            """
        )
        cursor.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_album_mentions_album
            ON album_mentions (artist_key, album_key)
            """
        )

        # Tabela: histórico de runs por source (para scoring futuro)
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS source_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_id TEXT NOT NULL,
                run_at TEXT NOT NULL,
                fetched_count INTEGER NOT NULL,
                fresh_count INTEGER NOT NULL,
                processed_count INTEGER NOT NULL,
                matched_count INTEGER NOT NULL,
                new_unique_count INTEGER NOT NULL,
                unmatched_count INTEGER NOT NULL,
                album_count INTEGER NOT NULL,
                skipped_stale_count INTEGER NOT NULL,
                skipped_cap_count INTEGER NOT NULL,
                status TEXT NOT NULL,
                error TEXT
            )
            """
        )
        cursor.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_source_runs_source_run_at
            ON source_runs (source_id, run_at)
            """
        )

        # Snapshot da fila confirmada na playlist de triagem. Permite que CLI,
        # Telegram e Spotify partilhem uma fonte de verdade após cada replace.
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS review_queue (
                playlist_id   TEXT NOT NULL,
                position      INTEGER NOT NULL,
                spotify_uri   TEXT NOT NULL,
                source_id     TEXT NOT NULL,
                artist        TEXT NOT NULL,
                title         TEXT NOT NULL,
                source_url    TEXT,
                source_count  INTEGER NOT NULL,
                affinity      REAL NOT NULL,
                is_new        INTEGER NOT NULL,
                added_at_week TEXT NOT NULL,
                current_week  TEXT NOT NULL,
                updated_at    TEXT NOT NULL,
                PRIMARY KEY (playlist_id, position),
                UNIQUE (playlist_id, spotify_uri)
            )
            """
        )

        # Cache local de géneros por artista. Vazia por defeito; preenchida só
        # por comando explícito (`peel affinity backfill-genres`) para manter a
        # pipeline semanal sem chamadas extra à Spotify API.
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS artist_genres (
                artist     TEXT PRIMARY KEY,
                genres     TEXT NOT NULL,
                fetched_at TEXT NOT NULL
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

        self._ensure_column("unmatched", "source_url", "TEXT")
        self._ensure_column("album_mentions", "spotify_album_uri", "TEXT")
        self._ensure_column("artist_genres", "source", "TEXT")
        self._ensure_column("artist_genres", "external_id", "TEXT")
        self._backfill_album_mentions()

    def _backfill_album_mentions(self) -> None:
        """Migra álbuns canónicos antigos para menções por source.

        Idempotente: `INSERT OR IGNORE` garante que correr `init_schema()` várias
        vezes não duplica menções já migradas.
        """
        rows = self.conn.execute(
            """
            SELECT artist, album, source_id, source_url, seen_at, added_at_week
            FROM albums
            """
        ).fetchall()
        count = 0
        for artist, album, source_id, source_url, seen_at, added_at_week in rows:
            week = (
                str(added_at_week) if added_at_week else iso_week(datetime.fromisoformat(seen_at))
            )
            cursor = self.conn.execute(
                """
                INSERT OR IGNORE INTO album_mentions
                (artist, album, artist_key, album_key, source_id, source_url,
                 spotify_album_uri, seen_at, added_at_week)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(artist),
                    str(album),
                    normalize(str(artist)),
                    normalize(str(album)),
                    str(source_id),
                    source_url,
                    None,
                    str(seen_at),
                    week,
                ),
            )
            count += cursor.rowcount
        self.conn.commit()
        log.info("db.album_mentions_backfilled", count=count)

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
    ) -> bool:
        """Regista uma faixa adicionada (ou ignora se duplicate key).

        Idempotente: chamar 2x com mesma (uri, source_id) só adiciona uma vez.

        Args:
            uri: Spotify track URI
            source_id: ID da fonte (ex.: "pitchfork_bnt")
            artist: Nome do artista
            title: Título da faixa
            url: URL opcional (link para review, etc.)

        Returns:
            True se a row foi inserida, False se já existia.
        """
        now = datetime.now(UTC)
        cursor = self.conn.execute(
            """
            INSERT OR IGNORE INTO tracks
            (spotify_uri, source_id, artist, title, source_url, added_at, added_at_week)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (uri, source_id, artist, title, url, now.isoformat(), iso_week(now)),
        )
        self.conn.commit()
        inserted = cursor.rowcount > 0
        log.debug("db.track_recorded", uri=uri, source_id=source_id, inserted=inserted)
        return inserted

    def track_sources(self, spotify_uri: str) -> list[tuple[str, str | None]]:
        """Lista as fontes associadas a uma URI Spotify.

        Args:
            spotify_uri: Spotify track URI.

        Returns:
            Lista de tuplos (source_id, source_url), ordenada por added_at.
        """
        cursor = self.conn.execute(
            """
            SELECT source_id, source_url
            FROM tracks
            WHERE spotify_uri = ?
            ORDER BY added_at ASC, source_id ASC
            """,
            (spotify_uri,),
        )
        return [(row[0], row[1]) for row in cursor.fetchall()]

    def recent_tracks_with_sources(
        self,
        limit: int = 50,
    ) -> list[tuple[str, str, str, str, int, str, str]]:
        """Tracks agregadas com contagem de fontes.

        Returns:
            Lista de tuplos:
            (spotify_uri, artist, title, added_at_week, source_count, first_added_at, last_added_at)
        """
        cursor = self.conn.execute(
            """
            SELECT
                spotify_uri,
                artist,
                title,
                MAX(added_at_week) AS added_at_week,
                COUNT(DISTINCT source_id) AS source_count,
                MIN(added_at) AS first_added_at,
                MAX(added_at) AS last_added_at
            FROM tracks
            GROUP BY spotify_uri, artist, title
            ORDER BY last_added_at DESC, artist COLLATE NOCASE, title COLLATE NOCASE
            LIMIT ?
            """,
            (limit,),
        )
        return [tuple(row) for row in cursor.fetchall()]

    def feedback_for_track(self, spotify_uri: str) -> tuple[int, str, str | None] | None:
        """Feedback registado para uma track, se existir."""
        cursor = self.conn.execute(
            """
            SELECT rating, label, comment
            FROM feedback
            WHERE spotify_uri = ?
            """,
            (spotify_uri,),
        )
        row = cursor.fetchone()
        if row is None:
            return None
        return int(row[0]), str(row[1]), row[2]

    def feedback_for_track_identity(self, spotify_uri: str) -> tuple[int, str, str | None] | None:
        """Feedback mais recente de qualquer URI da mesma faixa normalizada."""
        identity_rows = self.conn.execute(
            "SELECT artist, title FROM tracks WHERE spotify_uri = ?",
            (spotify_uri,),
        ).fetchall()
        identities = {
            (normalize(str(artist)), normalize(str(title))) for artist, title in identity_rows
        }
        if not identities:
            return self.feedback_for_track(spotify_uri)

        rows = self.conn.execute(
            """
            SELECT t.artist, t.title, f.rating, f.label, f.comment, f.rated_at
            FROM tracks t
            JOIN feedback f ON f.spotify_uri = t.spotify_uri
            """
        ).fetchall()
        matches = [
            (int(rating), str(label), comment, str(rated_at))
            for artist, title, rating, label, comment, rated_at in rows
            if (normalize(str(artist)), normalize(str(title))) in identities
        ]
        if not matches:
            return None
        rating, label, comment, _ = max(matches, key=lambda item: item[3])
        return rating, label, comment

    def has_feedback_for_track_identity(self, spotify_uri: str) -> bool:
        """True se qualquer URI da mesma faixa normalizada já recebeu feedback."""
        return self.feedback_for_track_identity(spotify_uri) is not None

    def canonical_uri_for_track_identity(self, artist: str, title: str) -> str | None:
        """URI representativa já persistida para a mesma identidade normalizada."""
        target = (normalize(artist), normalize(title))
        rows = self.conn.execute(
            "SELECT spotify_uri, artist, title, source_id, added_at FROM tracks"
        ).fetchall()
        by_uri: dict[str, tuple[set[str], float]] = {}
        for spotify_uri, row_artist, row_title, source_id, added_at in rows:
            if (normalize(str(row_artist)), normalize(str(row_title))) != target:
                continue
            uri = str(spotify_uri)
            sources, latest = by_uri.setdefault(uri, (set(), 0.0))
            sources.add(str(source_id))
            by_uri[uri] = (sources, max(latest, _timestamp_sort_value(str(added_at))))
        if not by_uri:
            return None
        return min(
            by_uri,
            key=lambda uri: (-len(by_uri[uri][0]), -by_uri[uri][1], uri),
        )

    def is_banned_uri(self, spotify_uri: str) -> bool:
        """True se a URI tem feedback explícito `ban`.

        `ban` é semântica de faixa/sugestão, não ban automático de artista.
        """
        cursor = self.conn.execute(
            """
            SELECT 1
            FROM feedback
            WHERE spotify_uri = ? AND rating = ?
            LIMIT 1
            """,
            (spotify_uri, FEEDBACK_RATINGS["ban"]),
        )
        return cursor.fetchone() is not None

    def banned_track_keys(self) -> set[tuple[str, str]]:
        """Identidades normalizadas `(artist, title)` com feedback `ban`.

        DECISÃO: isto evita reintroduzir a mesma música se o Spotify devolver
        outra URI, mas não bloqueia automaticamente o artista inteiro.
        """
        rows = self.conn.execute(
            """
            SELECT DISTINCT t.artist, t.title
            FROM tracks t
            JOIN feedback f ON f.spotify_uri = t.spotify_uri
            WHERE f.rating = ?
            """,
            (FEEDBACK_RATINGS["ban"],),
        ).fetchall()
        return {(normalize(str(artist)), normalize(str(title))) for artist, title in rows}

    def _banned_uris(self) -> set[str]:
        rows = self.conn.execute(
            """
            SELECT spotify_uri
            FROM feedback
            WHERE rating = ?
            """,
            (FEEDBACK_RATINGS["ban"],),
        ).fetchall()
        return {str(row[0]) for row in rows}

    def upsert_feedback(
        self,
        spotify_uri: str,
        label: str,
        comment: str | None = None,
    ) -> None:
        """Guarda feedback explícito do utilizador para uma track."""
        normalized = label.strip().lower()
        if normalized not in FEEDBACK_RATINGS:
            allowed = ", ".join(sorted(FEEDBACK_RATINGS))
            raise ValueError(f"invalid feedback label: {label!r}. Allowed: {allowed}")

        rating = FEEDBACK_RATINGS[normalized]
        now = datetime.now(UTC).isoformat()
        self.conn.execute(
            """
            INSERT INTO feedback (spotify_uri, rating, label, comment, rated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(spotify_uri) DO UPDATE SET
                rating = excluded.rating,
                label = excluded.label,
                comment = excluded.comment,
                rated_at = excluded.rated_at
            """,
            (spotify_uri, rating, normalized, comment, now),
        )
        self.conn.commit()
        log.debug(
            "db.feedback_upserted",
            spotify_uri=spotify_uri,
            label=normalized,
            rating=rating,
        )

    def replace_review_queue(
        self,
        playlist_id: str,
        items: list[ReviewQueueItem],
    ) -> None:
        """Persiste a ordem exacta confirmada na playlist de triagem."""
        now = datetime.now(UTC).isoformat()
        self.conn.execute("DELETE FROM review_queue WHERE playlist_id = ?", (playlist_id,))
        self.conn.executemany(
            """
            INSERT INTO review_queue (
                playlist_id, position, spotify_uri, source_id, artist, title,
                source_url, source_count, affinity, is_new, added_at_week,
                current_week, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    playlist_id,
                    position,
                    item.spotify_uri,
                    item.source_id,
                    item.artist,
                    item.title,
                    item.source_url,
                    item.source_count,
                    item.affinity,
                    int(item.is_new),
                    item.added_at_week,
                    item.current_week,
                    now,
                )
                for position, item in enumerate(items, start=1)
            ],
        )
        self.conn.commit()
        log.info("db.review_queue_replaced", playlist_id=playlist_id, count=len(items))

    def review_queue(self, playlist_id: str) -> list[ReviewQueueItem]:
        """Snapshot persistido da triagem, pela mesma ordem da playlist Spotify."""
        rows = self.conn.execute(
            """
            SELECT source_id, artist, title, spotify_uri, source_url, source_count,
                   affinity, is_new, added_at_week, current_week
            FROM review_queue
            WHERE playlist_id = ?
            ORDER BY position ASC
            """,
            (playlist_id,),
        ).fetchall()
        return [
            ReviewQueueItem(
                source_id=str(source_id),
                artist=str(artist),
                title=str(title),
                spotify_uri=str(spotify_uri),
                source_url=source_url,
                source_count=int(source_count),
                affinity=float(affinity),
                is_new=bool(is_new),
                added_at_week=str(added_at_week),
                current_week=str(current_week),
            )
            for (
                source_id,
                artist,
                title,
                spotify_uri,
                source_url,
                source_count,
                affinity,
                is_new,
                added_at_week,
                current_week,
            ) in rows
        ]

    def unrated_tracks(
        self,
        week: str | None = None,
        limit: int = 50,
    ) -> list[tuple[str, str, str, str, int, str, str]]:
        """Tracks ainda sem feedback explícito."""
        params: list[object] = []
        where_clause = """
            WHERE f.spotify_uri IS NULL
              AND NOT EXISTS (
                SELECT 1
                FROM tracks rated_track
                JOIN feedback rated_feedback
                  ON rated_feedback.spotify_uri = rated_track.spotify_uri
                WHERE rated_track.artist = t.artist
                  AND rated_track.title = t.title
              )
        """
        if week is not None:
            where_clause += " AND t.added_at_week = ?"
            params.append(week)
        params.append(limit)

        cursor = self.conn.execute(
            f"""
            SELECT
                t.spotify_uri,
                t.artist,
                t.title,
                MAX(t.added_at_week) AS added_at_week,
                COUNT(DISTINCT t.source_id) AS source_count,
                MIN(t.added_at) AS first_added_at,
                MAX(t.added_at) AS last_added_at
            FROM tracks t
            LEFT JOIN feedback f ON f.spotify_uri = t.spotify_uri
            {where_clause}
            GROUP BY t.spotify_uri, t.artist, t.title
            ORDER BY last_added_at DESC, t.artist COLLATE NOCASE, t.title COLLATE NOCASE
            LIMIT ?
            """,
            params,
        )
        return [tuple(row) for row in cursor.fetchall()]

    def record_unmatched(
        self,
        source_id: str,
        artist: str,
        title: str,
        source_url: str | None = None,
    ) -> None:
        """Regista uma faixa que não foi encontrada no Spotify.

        Útil para auditoria: depois podes rever quais faixas falharam matching.

        Args:
            source_id: ID da fonte
            artist: Nome do artista
            title: Título da faixa
            source_url: URL opcional da source original
        """
        self.conn.execute(
            """
            INSERT INTO unmatched
            (source_id, artist, title, source_url, seen_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (source_id, artist, title, source_url, datetime.now(UTC).isoformat()),
        )
        self.conn.commit()
        log.debug("db.unmatched_recorded", source_id=source_id, artist=artist, title=title)

    def list_unmatched(self, max_age_days: int) -> list[tuple[str, str, str]]:
        """Lista distinct (source_id, artist, title) de unmatched recentes.

        Args:
            max_age_days: apenas rows com seen_at nos últimos N dias

        Returns:
            Lista de tuplos (source_id, artist, title), sem duplicados.
        """
        cutoff = (datetime.now(UTC) - timedelta(days=max_age_days)).isoformat()
        cursor = self.conn.execute(
            """
            SELECT DISTINCT source_id, artist, title FROM unmatched
            WHERE seen_at >= ?
            """,
            (cutoff,),
        )
        return [tuple(row) for row in cursor.fetchall()]

    def list_unmatched_with_urls(self, max_age_days: int) -> list[tuple[str, str, str, str | None]]:
        """Lista unmatched recentes preservando source_url quando existe."""
        cutoff = (datetime.now(UTC) - timedelta(days=max_age_days)).isoformat()
        cursor = self.conn.execute(
            """
            SELECT source_id, artist, title, MAX(source_url)
            FROM unmatched
            WHERE seen_at >= ?
            GROUP BY source_id, artist, title
            """,
            (cutoff,),
        )
        return [(str(row[0]), str(row[1]), str(row[2]), row[3]) for row in cursor.fetchall()]

    def delete_unmatched(self, source_id: str, artist: str, title: str) -> int:
        """Remove todas as rows unmatched que batam (source_id, artist, title).

        Retorna o número de rows apagadas.
        """
        cursor = self.conn.execute(
            """
            DELETE FROM unmatched
            WHERE source_id = ? AND artist = ? AND title = ?
            """,
            (source_id, artist, title),
        )
        self.conn.commit()
        return cursor.rowcount

    def prune_unmatched(self, max_age_days: int) -> int:
        """Remove rows unmatched mais antigas que N dias. Retorna rows apagadas."""
        cutoff = (datetime.now(UTC) - timedelta(days=max_age_days)).isoformat()
        cursor = self.conn.execute(
            "DELETE FROM unmatched WHERE seen_at < ?",
            (cutoff,),
        )
        self.conn.commit()
        return cursor.rowcount

    def record_album(
        self,
        artist: str,
        album: str,
        source_id: str,
        source_url: str | None,
        spotify_album_uri: str | None = None,
    ) -> bool:
        """Insere álbum canónico e grava a menção da source.

        O valor de retorno mantém a semântica antiga: True só quando o álbum
        canónico em `albums` é novo. Mesmo quando retorna False, a menção por
        source é gravada/actualizada em `album_mentions` para permitir consenso.
        """
        cursor = self.conn.cursor()
        now = datetime.now(UTC)
        now_iso = now.isoformat()
        week = iso_week(now)
        artist_key = normalize(artist)
        album_key = normalize(album)
        already_seen = cursor.execute(
            """
            SELECT 1
            FROM album_mentions
            WHERE artist_key = ? AND album_key = ?
            LIMIT 1
            """,
            (artist_key, album_key),
        ).fetchone()
        if already_seen:
            inserted = False
        else:
            cursor.execute(
                """
                INSERT OR IGNORE INTO albums
                (artist, album, source_id, source_url, seen_at, added_at_week)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (artist, album, source_id, source_url, now_iso, week),
            )
            inserted = cursor.rowcount > 0
        self._record_album_mention(
            artist=artist,
            album=album,
            source_id=source_id,
            source_url=source_url,
            spotify_album_uri=spotify_album_uri,
            seen_at=now_iso,
            added_at_week=week,
        )
        self.conn.commit()
        log.debug(
            "db.album_recorded",
            artist=artist,
            album=album,
            source_id=source_id,
            is_new=inserted,
            spotify_album_uri=spotify_album_uri,
        )
        return inserted

    def _record_album_mention(
        self,
        *,
        artist: str,
        album: str,
        source_id: str,
        source_url: str | None,
        spotify_album_uri: str | None,
        seen_at: str,
        added_at_week: str,
    ) -> None:
        """Grava/actualiza uma menção de álbum por source.

        DECISÃO: se a mesma source voltar a mencionar o álbum noutra semana,
        actualizamos `seen_at`/`added_at_week`; assim a selecção semanal reflecte
        a rotação actual, não apenas a primeira vez que vimos o álbum.
        """
        self.conn.execute(
            """
            INSERT INTO album_mentions
            (artist, album, artist_key, album_key, source_id, source_url,
             spotify_album_uri, seen_at, added_at_week)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(artist_key, album_key, source_id) DO UPDATE SET
                artist = excluded.artist,
                album = excluded.album,
                source_url = COALESCE(excluded.source_url, album_mentions.source_url),
                spotify_album_uri = COALESCE(
                    excluded.spotify_album_uri,
                    album_mentions.spotify_album_uri
                ),
                seen_at = excluded.seen_at,
                added_at_week = excluded.added_at_week
            """,
            (
                artist,
                album,
                normalize(artist),
                normalize(album),
                source_id,
                source_url,
                spotify_album_uri,
                seen_at,
                added_at_week,
            ),
        )

    def record_source_run(
        self,
        source_id: str,
        run_at: str,
        fetched_count: int,
        fresh_count: int,
        processed_count: int,
        matched_count: int,
        new_unique_count: int,
        unmatched_count: int,
        album_count: int,
        skipped_stale_count: int,
        skipped_cap_count: int,
        status: str,
        error: str | None = None,
    ) -> None:
        """Regista métricas de uma run de source.

        Args:
            source_id: ID estável da source.
            run_at: timestamp ISO 8601 UTC.
            fetched_count: items devolvidos pela source antes de filtros.
            fresh_count: items que passaram o filtro de idade.
            processed_count: items realmente processados/searchados.
            matched_count: items com match Spotify.
            new_unique_count: matches novos na BD/playlist.
            unmatched_count: items sem match Spotify.
            album_count: álbuns novos registados.
            skipped_stale_count: items ignorados por idade.
            skipped_cap_count: items ignorados por caps.
            status: "ok" ou "error".
            error: mensagem opcional, truncada a 500 chars.
        """
        if status not in {"ok", "error"}:
            raise ValueError("source run status must be 'ok' or 'error'")
        truncated_error = error[:500] if error else None
        self.conn.execute(
            """
            INSERT INTO source_runs (
                source_id,
                run_at,
                fetched_count,
                fresh_count,
                processed_count,
                matched_count,
                new_unique_count,
                unmatched_count,
                album_count,
                skipped_stale_count,
                skipped_cap_count,
                status,
                error
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                source_id,
                run_at,
                fetched_count,
                fresh_count,
                processed_count,
                matched_count,
                new_unique_count,
                unmatched_count,
                album_count,
                skipped_stale_count,
                skipped_cap_count,
                status,
                truncated_error,
            ),
        )
        self.conn.commit()
        log.debug("db.source_run_recorded", source_id=source_id, status=status)

    def upsert_artist_genres(
        self,
        artist: str,
        genres: list[str] | tuple[str, ...],
        fetched_at: str | None = None,
        source: str | None = None,
        external_id: str | None = None,
    ) -> None:
        """Guarda a cache local de géneros para um artista.

        `artist` fica no formato legível recebido; lookups normalizados são
        feitos em Python porque a tabela é pequena.
        """
        now = fetched_at or datetime.now(UTC).isoformat()
        clean_genres = [str(genre).strip() for genre in genres if str(genre).strip()]
        self.conn.execute(
            """
            INSERT INTO artist_genres (artist, genres, fetched_at, source, external_id)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(artist) DO UPDATE SET
                genres = excluded.genres,
                fetched_at = excluded.fetched_at,
                source = excluded.source,
                external_id = excluded.external_id
            """,
            (
                artist,
                json.dumps(clean_genres, ensure_ascii=False),
                now,
                source,
                external_id,
            ),
        )
        self.conn.commit()

    def genres_for_artist(self, artist: str) -> list[str] | None:
        """Lê géneros em cache para um artista, se existirem."""
        target = normalize(artist)
        rows = self.conn.execute("SELECT artist, genres FROM artist_genres").fetchall()
        for row_artist, raw_genres in rows:
            if normalize(str(row_artist)) != target:
                continue
            try:
                parsed = json.loads(str(raw_genres))
            except json.JSONDecodeError:
                return None
            if not isinstance(parsed, list):
                return None
            return [str(item) for item in parsed]
        return None

    def artists_missing_genre_cache(
        self,
        refresh_days: int = 180,
        limit: int | None = None,
    ) -> list[str]:
        """Artistas de tracks sem cache fresca de géneros.

        Ordena artistas com feedback primeiro, para um backfill parcial maximizar
        impacto no perfil de afinidade.
        """
        cutoff = (datetime.now(UTC) - timedelta(days=refresh_days)).isoformat()
        fresh_rows = self.conn.execute(
            "SELECT artist, fetched_at FROM artist_genres WHERE fetched_at >= ?",
            (cutoff,),
        ).fetchall()
        fresh_keys = {normalize(str(artist)) for artist, _ in fresh_rows}

        rows = self.conn.execute(
            """
            SELECT
                t.artist,
                COUNT(DISTINCT t.spotify_uri) AS track_count,
                SUM(CASE WHEN f.spotify_uri IS NULL THEN 0 ELSE 1 END) AS rating_count
            FROM tracks t
            LEFT JOIN feedback f ON f.spotify_uri = t.spotify_uri
            GROUP BY t.artist
            ORDER BY rating_count DESC, track_count DESC, t.artist COLLATE NOCASE
            """
        ).fetchall()

        result: list[str] = []
        seen: set[str] = set()
        for artist, _, _ in rows:
            artist_name = str(artist)
            key = normalize(artist_name)
            if not key or key in seen or key in fresh_keys:
                continue
            seen.add(key)
            result.append(artist_name)
            if limit is not None and len(result) >= limit:
                break
        return result

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
        rows = self._filtered_window_track_rows(current_week, window)
        uris: list[str] = []
        seen: set[str] = set()
        for spotify_uri, *_ in rows:
            uri = str(spotify_uri)
            if uri in seen:
                continue
            seen.add(uri)
            uris.append(uri)
        return uris

    def ranked_tracks_in_window(
        self,
        current_week: str,
        window: int,
        source_quality: Mapping[str, SourceQuality] | None = None,
        affinity_scorer: Callable[[str], float] | None = None,
    ) -> list[str]:
        """URIs da janela ordenados por qualidade, mantendo filtro de bans.

        DECISÃO: este método só reordena o resultado elegível da janela. Se a
        chamada falhar no orquestrador, `tracks_in_window` continua como fallback
        de recência.
        """
        rows = self._filtered_window_track_rows(current_week, window)
        return rank_window_uris(rows, source_quality, affinity_scorer)

    def week_keeper_uris(
        self,
        week: str,
        source_quality: Mapping[str, SourceQuality] | None = None,
        limit: int = 7,
        affinity_scorer: Callable[[str], float] | None = None,
    ) -> list[str]:
        """Top N da semana pelo ranking, sem o que o utilizador rejeitou.

        Regra (site + ``peel finalize``): top `limit` faixas da semana ordenadas
        pelo ranking (consenso/qualidade/recência), EXCLUINDO ban/meh/skip e
        MANTENDO love/like e as ainda não avaliadas. Bans já saem do ranking.
        """
        keep = FEEDBACK_RATINGS["like"]
        keepers: list[str] = []
        for uri in self.ranked_tracks_in_window(week, 1, source_quality, affinity_scorer):
            feedback = self.feedback_for_track(uri)
            if feedback is None or feedback[0] >= keep:
                keepers.append(uri)
        return keepers[:limit]

    def source_count_for_track_identity(self, artist: str, title: str) -> int:
        """Nº distinto de sources para uma identidade normalizada de faixa."""
        target = (normalize(artist), normalize(title))
        rows = self.conn.execute(
            """
            SELECT artist, title, source_id
            FROM tracks
            """
        ).fetchall()
        sources = {
            str(source_id)
            for row_artist, row_title, source_id in rows
            if (normalize(str(row_artist)), normalize(str(row_title))) == target
        }
        return max(1, len(sources))

    def _filtered_window_track_rows(self, current_week: str, window: int) -> list[WindowTrackRow]:
        cutoff_week = self._cutoff_week(current_week, window)
        cursor = self.conn.execute(
            """
            SELECT spotify_uri, artist, title, source_id, added_at
            FROM tracks
            WHERE added_at_week >= ?
              AND added_at_week <= ?
            ORDER BY added_at DESC
            """,
            (cutoff_week, current_week),
        )
        banned_uris = self._banned_uris()
        banned_keys = self.banned_track_keys()
        rows: list[WindowTrackRow] = []
        for spotify_uri, artist, title, source_id, added_at in cursor.fetchall():
            if str(spotify_uri) in banned_uris:
                continue
            if (normalize(str(artist)), normalize(str(title))) in banned_keys:
                continue
            rows.append((str(spotify_uri), str(artist), str(title), str(source_id), str(added_at)))
        return rows

    def _cutoff_week(self, current_week: str, window: int) -> str:
        # Calcula a semana cutoff: current_week - (window - 1)
        year, week = map(int, current_week.split("-W"))
        cutoff_dt = datetime.fromisocalendar(year, week, 1) - timedelta(weeks=window - 1)
        cutoff_week_year, cutoff_week_num, _ = cutoff_dt.isocalendar()
        return f"{cutoff_week_year}-W{cutoff_week_num:02d}"

    def close(self) -> None:
        """Fecha a conexão ao banco.

        Importante: liberta locks WAL (peel.db-wal, peel.db-shm) para que o
        ficheiro .db fique disponível para git commit no workflow.
        """
        self.conn.close()
        log.info("db.closed", path=self.path)
