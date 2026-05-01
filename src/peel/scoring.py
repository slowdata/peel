"""Source scoring using existing Peel data.

The score is intentionally simple and observational. It uses only persisted data:
matched tracks, unmatched candidates and explicit user feedback.

Important limitation: without a historical ``source_runs`` table we cannot recover the
raw number of items fetched by a source. ``tracks_found`` is therefore the known,
persisted total: matched tracks + unmatched candidates in the selected window.
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
    def score(self) -> float:
        avg = self.avg_rating or 0.0
        return (
            10 * avg
            + 3 * self.consensus_hits
            + 2 * self.new_unique_tracks
            - 2 * self.skipped_count
            - 1 * self.unmatched_count
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

    return sorted(scores.values(), key=lambda item: (-item.score, item.source_id))


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
