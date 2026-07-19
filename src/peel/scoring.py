"""Source scoring using persisted Peel data.

The score is intentionally simple and observational. It combines normalized
quality signals from matched/unmatched candidates and explicit user feedback.
Every component is a rate (or an average), so a source's score describes the
quality of its output rather than growing with its raw publishing volume.

``tracks_found`` remains the playlist-facing candidate count: matched + unmatched.
Raw source volume is exposed separately via ``fetched_count`` and friends.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from peel.db import DB, iso_week


@dataclass(slots=True)
class SourceScore:
    """Aggregated scoring metrics for one source.

    Definitions:
    - tracks_found: persisted candidates found by the source: matched + unmatched.
    - tracks_matched: Spotify matches attributed to the source in the window.
    - new_unique_tracks: matched tracks first introduced by this source globally.
    - duplicate_mentions: for consensus tracks, number of other sources per track.
    - consensus_hits: matched tracks also mentioned by at least one other source globally.
    """

    source_id: str
    tracks_matched: int = 0
    new_unique_tracks: int = 0
    duplicate_mentions: int = 0
    consensus_hits: int = 0
    unmatched_count: int = 0
    run_count: int = 0
    fetched_count: int = 0
    fresh_count: int = 0
    processed_count: int = 0
    skipped_stale_count: int = 0
    skipped_cap_count: int = 0
    error_count: int = 0
    liked_count: int = 0
    skipped_count: int = 0
    rating_total: int = 0
    rating_count: int = 0

    @property
    def tracks_found(self) -> int:
        return self.tracks_matched + self.unmatched_count

    @property
    def avg_rating(self) -> float | None:
        if self.rating_count == 0:
            return None
        return self.rating_total / self.rating_count

    @property
    def consensus_rate(self) -> float:
        """Partilha de matches que outra source também recomendou."""
        if self.tracks_matched == 0:
            return 0.0
        return self.consensus_hits / self.tracks_matched

    @property
    def new_unique_rate(self) -> float:
        """Partilha de matches inicialmente descoberta por esta source."""
        if self.tracks_matched == 0:
            return 0.0
        return self.new_unique_tracks / self.tracks_matched

    @property
    def unmatched_rate(self) -> float:
        """Partilha de candidatos que não encontrou match no Spotify."""
        if self.tracks_found == 0:
            return 0.0
        return self.unmatched_count / self.tracks_found

    @property
    def skipped_rate(self) -> float:
        """Partilha de feedback explícito negativo entre tracks avaliadas."""
        if self.rating_count == 0:
            return 0.0
        return self.skipped_count / self.rating_count

    @property
    def score(self) -> float:
        """Score comparável entre sources, independente do volume bruto.

        Feedback real continua a ter o maior peso. Consenso e descoberta nova
        são sinais secundários positivos; matches falhados e feedback negativo
        reduzem o score. Todos os contadores são normalizados antes de pesar,
        impedindo uma source prolífica de acumular vantagem só por publicar mais.
        """
        avg = self.avg_rating or 0.0
        return (
            10 * avg
            + 3 * self.consensus_rate
            + 2 * self.new_unique_rate
            - 2 * self.skipped_rate
            - self.unmatched_rate
        )

    @property
    def avg_rating_display(self) -> str:
        if self.avg_rating is None:
            return "—"
        return f"{self.avg_rating:.2f}"


def build_source_scores(
    db: DB,
    weeks: int = 4,
    reference_dt: datetime | None = None,
) -> list[SourceScore]:
    """Calcula métricas de qualidade por source para uma janela temporal.

    Args:
        db: ligação ao SQLite.
        weeks: janela ISO em semanas (inclui a semana corrente).
        reference_dt: data de referência para testes; default é agora UTC.

    Returns:
        Lista ordenada por score desc, depois source_id.
    """
    if weeks < 1:
        raise ValueError("weeks must be >= 1")

    ref_dt = reference_dt or datetime.now(UTC)
    start_dt, end_dt = _window_bounds(ref_dt, weeks)

    track_rows = db.conn.execute(
        """
        SELECT spotify_uri, source_id
        FROM tracks
        WHERE added_at >= ? AND added_at < ?
        ORDER BY added_at ASC, source_id ASC, spotify_uri ASC
        """,
        (start_dt.isoformat(), end_dt.isoformat()),
    ).fetchall()

    scores: dict[str, SourceScore] = {}

    def summary(source_id: str) -> SourceScore:
        if source_id not in scores:
            scores[source_id] = SourceScore(source_id=source_id)
        return scores[source_id]

    sources_by_uri = _window_sources_by_uri(track_rows)
    first_source_by_uri, source_count_by_uri = _global_source_metadata(db, list(sources_by_uri))

    for spotify_uri, source_ids in sources_by_uri.items():
        feedback = db.feedback_for_track(spotify_uri)
        global_source_count = source_count_by_uri.get(spotify_uri, len(source_ids))
        first_source = first_source_by_uri.get(spotify_uri)

        for source_id in source_ids:
            item = summary(source_id)
            item.tracks_matched += 1
            if source_id == first_source:
                item.new_unique_tracks += 1
            if global_source_count > 1:
                item.consensus_hits += 1
                item.duplicate_mentions += global_source_count - 1
            if feedback is not None:
                rating = feedback[0]
                item.rating_total += rating
                item.rating_count += 1
                if rating > 0:
                    item.liked_count += 1
                elif rating < 0:
                    item.skipped_count += 1

    unmatched_rows = db.conn.execute(
        """
        SELECT DISTINCT source_id, artist, title
        FROM unmatched
        WHERE seen_at >= ? AND seen_at < ?
        """,
        (start_dt.isoformat(), end_dt.isoformat()),
    ).fetchall()

    for source_id, _, _ in unmatched_rows:
        summary(str(source_id)).unmatched_count += 1

    _apply_source_run_metrics(db, scores, start_dt, end_dt)

    return sorted(scores.values(), key=lambda item: (-item.score, item.source_id))


def _apply_source_run_metrics(
    db: DB,
    scores: dict[str, SourceScore],
    start_dt: datetime,
    end_dt: datetime,
) -> None:
    """Enriquece scores existentes com métricas agregadas de source_runs.

    Só actualiza sources já presentes no scoring de tracks/unmatched. Assim o comando
    continua focado em playlist sources e não passa a listar fontes puramente album/context.
    """
    if not scores:
        return

    rows = db.conn.execute(
        """
        SELECT
            source_id,
            COUNT(*) AS run_count,
            COALESCE(SUM(fetched_count), 0) AS fetched_count,
            COALESCE(SUM(fresh_count), 0) AS fresh_count,
            COALESCE(SUM(processed_count), 0) AS processed_count,
            COALESCE(SUM(skipped_stale_count), 0) AS skipped_stale_count,
            COALESCE(SUM(skipped_cap_count), 0) AS skipped_cap_count,
            COALESCE(SUM(CASE WHEN status = 'error' THEN 1 ELSE 0 END), 0) AS error_count
        FROM source_runs
        WHERE run_at >= ? AND run_at < ?
        GROUP BY source_id
        """,
        (start_dt.isoformat(), end_dt.isoformat()),
    ).fetchall()

    for row in rows:
        source_id = str(row[0])
        item = scores.get(source_id)
        if item is None:
            continue
        item.run_count = int(row[1])
        item.fetched_count = int(row[2])
        item.fresh_count = int(row[3])
        item.processed_count = int(row[4])
        item.skipped_stale_count = int(row[5])
        item.skipped_cap_count = int(row[6])
        item.error_count = int(row[7])


def _window_sources_by_uri(rows: list[tuple[str, str]]) -> dict[str, list[str]]:
    sources_by_uri: dict[str, list[str]] = {}
    for spotify_uri, source_id in rows:
        uri = str(spotify_uri)
        source = str(source_id)
        sources = sources_by_uri.setdefault(uri, [])
        if source not in sources:
            sources.append(source)
    return sources_by_uri


def _global_source_metadata(
    db: DB,
    spotify_uris: list[str],
) -> tuple[dict[str, str], dict[str, int]]:
    if not spotify_uris:
        return {}, {}

    placeholders = ", ".join("?" for _ in spotify_uris)
    rows = db.conn.execute(
        f"""
        SELECT spotify_uri, source_id
        FROM tracks
        WHERE spotify_uri IN ({placeholders})
        ORDER BY spotify_uri ASC, added_at ASC, source_id ASC
        """,
        spotify_uris,
    ).fetchall()

    first_source_by_uri: dict[str, str] = {}
    source_ids_by_uri: dict[str, set[str]] = {}

    for spotify_uri, source_id in rows:
        uri = str(spotify_uri)
        source = str(source_id)
        first_source_by_uri.setdefault(uri, source)
        source_ids_by_uri.setdefault(uri, set()).add(source)

    source_count_by_uri = {uri: len(sources) for uri, sources in source_ids_by_uri.items()}
    return first_source_by_uri, source_count_by_uri


def _window_bounds(reference_dt: datetime, weeks: int) -> tuple[datetime, datetime]:
    current_week = iso_week(reference_dt)
    year, week = map(int, current_week.split("-W"))
    current_start = datetime.fromisocalendar(year, week, 1).replace(tzinfo=UTC)
    start = current_start - timedelta(weeks=weeks - 1)
    end = current_start + timedelta(days=7)
    return start, end
