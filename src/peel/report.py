"""Geração de relatório semanal em Markdown.

O objectivo é dar ao Dias um artefacto legível, versionável e sincronizável com o
cron do GitHub Actions.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import structlog

from peel.albums import AlbumRecommendation, top_album_recommendations
from peel.db import DB, iso_week
from peel.scoring import build_source_scores

log = structlog.get_logger()

REPORTS_DIR = Path("data/reports")


@dataclass(slots=True)
class TrackBucket:
    spotify_uri: str
    artist: str
    title: str
    added_at_week: str
    first_added_at: str
    last_added_at: str
    sources: list[tuple[str, str | None]]


@dataclass(slots=True)
class WeeklyTrack:
    spotify_uri: str
    artist: str
    title: str
    added_at_week: str
    source_count: int
    first_added_at: str
    last_added_at: str
    sources: list[tuple[str, str | None]]
    feedback: tuple[int, str, str | None] | None


@dataclass(slots=True)
class WeeklyAlbum:
    artist: str
    album: str
    source_id: str
    source_url: str | None


@dataclass(slots=True)
class SourceSummary:
    source_id: str
    tracks: int = 0
    new: int = 0
    consensus: int = 0
    unmatched: int = 0
    rating_total: int = 0
    rating_count: int = 0

    @property
    def avg_rating(self) -> str:
        if self.rating_count == 0:
            return "—"
        return f"{self.rating_total / self.rating_count:.2f}"


@dataclass(slots=True)
class WeeklyReport:
    week: str
    tracks: list[WeeklyTrack]
    albums: list[WeeklyAlbum]
    recommended_albums: list[AlbumRecommendation]
    unmatched: list[tuple[str, str, str, str | None]]
    summaries: list[SourceSummary]


def generate_weekly_report(
    db: DB,
    week: str | None = None,
    output_dir: Path | None = None,
) -> Path:
    """Gera o Markdown da semana e escreve-o para disco."""
    resolved_week = _normalize_week(week or iso_week(datetime.now(UTC)))
    target_dir = output_dir or REPORTS_DIR
    target_dir.mkdir(parents=True, exist_ok=True)

    report = build_weekly_report(db, resolved_week)
    path = target_dir / f"{resolved_week}.md"
    path.write_text(report, encoding="utf-8")
    return path


def build_weekly_report(db: DB, week: str) -> str:
    """Constrói o relatório Markdown para uma semana ISO."""
    week = _normalize_week(week)
    tracks = _load_weekly_tracks(db, week)
    albums = _load_weekly_albums(db, week)
    recommended_albums = _load_recommended_albums(db, week)
    unmatched = _load_weekly_unmatched(db, week)
    summaries = _build_source_summaries(tracks, unmatched)

    report = WeeklyReport(
        week=week,
        tracks=tracks,
        albums=albums,
        recommended_albums=recommended_albums,
        unmatched=unmatched,
        summaries=summaries,
    )
    return _render_markdown(report)


def _load_weekly_tracks(db: DB, week: str) -> list[WeeklyTrack]:
    rows = db.conn.execute(
        """
        SELECT
            spotify_uri,
            artist,
            title,
            added_at_week,
            added_at,
            source_id,
            source_url
        FROM tracks
        WHERE added_at_week = ?
        ORDER BY added_at ASC, source_id ASC, spotify_uri ASC
        """,
        (week,),
    ).fetchall()

    buckets: dict[tuple[str, str, str], TrackBucket] = {}
    order: list[tuple[str, str, str]] = []

    for spotify_uri, artist, title, added_at_week, added_at, source_id, source_url in rows:
        key = (str(spotify_uri), str(artist), str(title))
        bucket = buckets.get(key)
        if bucket is None:
            bucket = TrackBucket(
                spotify_uri=str(spotify_uri),
                artist=str(artist),
                title=str(title),
                added_at_week=str(added_at_week),
                first_added_at=str(added_at),
                last_added_at=str(added_at),
                sources=[],
            )
            buckets[key] = bucket
            order.append(key)
        bucket.last_added_at = str(added_at)
        bucket.sources.append((str(source_id), source_url))

    tracks: list[WeeklyTrack] = []
    for key in order:
        bucket = buckets[key]
        feedback = db.feedback_for_track(bucket.spotify_uri)
        tracks.append(
            WeeklyTrack(
                spotify_uri=bucket.spotify_uri,
                artist=bucket.artist,
                title=bucket.title,
                added_at_week=bucket.added_at_week,
                source_count=len(bucket.sources),
                first_added_at=bucket.first_added_at,
                last_added_at=bucket.last_added_at,
                sources=list(bucket.sources),
                feedback=feedback,
            )
        )

    tracks.sort(
        key=lambda item: (item.last_added_at, item.artist.lower(), item.title.lower()),
        reverse=True,
    )
    return tracks


def _load_weekly_albums(db: DB, week: str) -> list[WeeklyAlbum]:
    rows = db.conn.execute(
        """
        SELECT artist, album, source_id, source_url
        FROM albums
        WHERE added_at_week = ?
        ORDER BY seen_at ASC, artist COLLATE NOCASE, album COLLATE NOCASE
        """,
        (week,),
    ).fetchall()
    return [
        WeeklyAlbum(
            artist=str(row[0]),
            album=str(row[1]),
            source_id=str(row[2]),
            source_url=row[3],
        )
        for row in rows
    ]


def _load_recommended_albums(db: DB, week: str) -> list[AlbumRecommendation]:
    """Carrega a seleção "7 Álbuns a Ouvir" sem poder rebentar o relatório."""
    try:
        scores = build_source_scores(db, weeks=4)
        source_quality = {
            score.source_id: (score.avg_rating or 0.0, score.score) for score in scores
        }
    except Exception as e:
        log.exception("report.album_source_scores_failed", error=str(e))
        source_quality = {}

    try:
        # DECISÃO: 2 semanas dá pool suficiente para consenso entre críticos sem
        # transformar a seleção semanal num backlog longo.
        return top_album_recommendations(
            db,
            week,
            weeks=2,
            limit=7,
            source_quality=source_quality,
        )
    except Exception as e:
        log.exception("report.album_recommendations_failed", week=week, error=str(e))
        return []


def _load_weekly_unmatched(db: DB, week: str) -> list[tuple[str, str, str, str | None]]:
    start, end = _week_bounds(week)
    rows = db.conn.execute(
        """
        SELECT source_id, artist, title, MAX(source_url)
        FROM unmatched
        WHERE seen_at >= ? AND seen_at < ?
        GROUP BY source_id, artist, title
        ORDER BY source_id ASC, artist COLLATE NOCASE, title COLLATE NOCASE
        """,
        (start.isoformat(), end.isoformat()),
    ).fetchall()
    return [(str(row[0]), str(row[1]), str(row[2]), row[3]) for row in rows]


def _build_source_summaries(
    tracks: list[WeeklyTrack],
    unmatched: list[tuple[str, str, str, str | None]],
) -> list[SourceSummary]:
    summaries: dict[str, SourceSummary] = {}

    def _summary(source_id: str) -> SourceSummary:
        if source_id not in summaries:
            summaries[source_id] = SourceSummary(source_id=source_id)
        return summaries[source_id]

    for track in tracks:
        feedback = track.feedback
        first_source = track.sources[0][0] if track.sources else None
        consensus = len(track.sources) > 1
        for source_id, _ in track.sources:
            summary = _summary(source_id)
            summary.tracks += 1
            if source_id == first_source:
                summary.new += 1
            if consensus:
                summary.consensus += 1
            if feedback is not None:
                summary.rating_total += feedback[0]
                summary.rating_count += 1

    for source_id, _, _, _ in unmatched:
        _summary(source_id).unmatched += 1

    return sorted(summaries.values(), key=lambda item: (-item.tracks, item.source_id))


def _render_markdown(report: WeeklyReport) -> str:
    lines: list[str] = [f"# Peel {report.week}"]

    lines.append("\n## Tracks")
    if report.tracks:
        for track in report.tracks:
            rating = track.feedback[1] if track.feedback else "—"
            artist = _md_escape(track.artist)
            title = _md_escape(track.title)
            lines.append(f"- {artist} — {title} — {track.source_count} fontes — rating: {rating}")
            lines.append(f"  - Spotify: `{track.spotify_uri}`")
            lines.append("  - Sources:")
            for source_id, source_url in track.sources:
                source_line = f"    - {source_id}"
                if source_url:
                    source_line += f" — {source_url}"
                lines.append(source_line)
    else:
        lines.append("- None")

    lines.append("\n## Albums / Context")
    if report.albums:
        for album in report.albums:
            artist = _md_escape(album.artist)
            title = _md_escape(album.album)
            lines.append(f"- {artist} — {title}")
            source_line = f"  - Source: {album.source_id}"
            if album.source_url:
                source_line += f" — {album.source_url}"
            lines.append(source_line)
    else:
        lines.append("- None")

    lines.append("\n## 🎧 7 Álbuns a Ouvir")
    if report.recommended_albums:
        for album in report.recommended_albums:
            artist = _md_escape(album.artist)
            title = _md_escape(album.album)
            line = f"- {artist} — {title} — {album.source_count} fontes"
            if album.link_url:
                line += f" — {album.link_url}"
            lines.append(line)
            lines.append(f"  - Sources: {', '.join(album.sources)}")
            for source_id, source_url in album.source_urls:
                if source_url:
                    lines.append(f"    - {source_id}: {source_url}")
    else:
        lines.append("- None")

    lines.append("\n## Unmatched")
    if report.unmatched:
        for source_id, artist, title, source_url in report.unmatched:
            line = f"- {source_id} — {_md_escape(artist)} — {_md_escape(title)}"
            if source_url:
                line += f" — {source_url}"
            lines.append(line)
    else:
        lines.append("- None")

    lines.append("\n## Source summary")
    lines.append("| Source | Tracks | New | Consensus | Unmatched | Avg rating |")
    lines.append("|---|---:|---:|---:|---:|---:|")
    if report.summaries:
        for summary in report.summaries:
            lines.append(
                f"| {summary.source_id} | {summary.tracks} | {summary.new} | "
                f"{summary.consensus} | {summary.unmatched} | {summary.avg_rating} |"
            )
    else:
        lines.append("| — | 0 | 0 | 0 | 0 | — |")

    return "\n".join(lines) + "\n"


def _week_bounds(week: str) -> tuple[datetime, datetime]:
    normalized = _normalize_week(week)
    year_str, week_str = normalized.split("-W")
    year = int(year_str)
    week_number = int(week_str)
    start = datetime.fromisocalendar(year, week_number, 1).replace(tzinfo=UTC)
    end = start + timedelta(days=7)
    return start, end


def _normalize_week(week: str) -> str:
    """Normaliza semana ISO, aceitando `2026-w19` e devolvendo `2026-W19`."""
    value = week.strip().upper()
    parts = value.split("-W")
    if len(parts) != 2:
        raise ValueError(f"invalid ISO week: {week!r}. Expected format: YYYY-Www")

    year_str, week_str = parts
    try:
        year = int(year_str)
        week_number = int(week_str)
    except ValueError as exc:
        raise ValueError(f"invalid ISO week: {week!r}. Expected format: YYYY-Www") from exc

    if not 1 <= week_number <= 53:
        raise ValueError(f"invalid ISO week: {week!r}. Week must be between 1 and 53")

    return f"{year:04d}-W{week_number:02d}"


def _md_escape(value: str) -> str:
    return value.replace("|", "\\|").replace("`", "\\`")
