"""Geração de relatório semanal em Markdown.

O objectivo é dar ao Dias um artefacto legível, versionável e sincronizável com o
cron do GitHub Actions.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from html import escape
from pathlib import Path
from urllib.parse import urlparse

import structlog

from peel.albums import (
    CANONICAL_ALBUM_QUEUE_SINCE,
    AlbumRecommendation,
    top_album_recommendations,
)
from peel.db import DB, iso_week
from peel.matcher import normalize
from peel.models import AlbumQueueItem
from peel.scoring import build_source_scores
from peel.sources.registry import source_label

log = structlog.get_logger()

REPORTS_DIR = Path("data/reports")


@dataclass(slots=True)
class TrackBucket:
    spotify_uri: str
    spotify_uris: list[str]
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
    recommended_albums: list[AlbumRecommendation | AlbumQueueItem]
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


def generate_weekly_html_report(
    db: DB,
    week: str | None = None,
    output_dir: Path | None = None,
) -> Path:
    """Gera uma preview HTML autónoma, separada dos artefactos versionados."""
    resolved_week = _normalize_week(week or iso_week(datetime.now(UTC)))
    target_dir = (output_dir or REPORTS_DIR) / ".html"
    target_dir.mkdir(parents=True, exist_ok=True)

    report = _load_weekly_report(db, resolved_week)
    path = target_dir / f"{resolved_week}.html"
    path.write_text(_render_html(report), encoding="utf-8")
    return path


def build_weekly_report(db: DB, week: str) -> str:
    """Constrói o relatório Markdown para uma semana ISO."""
    return _render_markdown(_load_weekly_report(db, week))


def build_weekly_html_report(db: DB, week: str) -> str:
    """Constrói uma página HTML autónoma para uma semana ISO."""
    return _render_html(_load_weekly_report(db, week))


def _load_weekly_report(db: DB, week: str) -> WeeklyReport:
    week = _normalize_week(week)
    tracks = _load_weekly_tracks(db, week)
    albums = _load_weekly_albums(db, week)
    recommended_albums = _load_recommended_albums(db, week)
    unmatched = _load_weekly_unmatched(db, week)
    summaries = _build_source_summaries(tracks, unmatched)

    return WeeklyReport(
        week=week,
        tracks=tracks,
        albums=albums,
        recommended_albums=recommended_albums,
        unmatched=unmatched,
        summaries=summaries,
    )


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

    # A mesma faixa pode chegar de várias sources com capitalização ou aliases
    # diferentes ("Nate Sib" vs "nate sib") e, por vezes, URI de edição/região
    # diferente. Agregamos primeiro por URI; se este variar, pela identidade
    # normalizada. A primeira forma recebida fica como apresentação canónica.
    buckets: dict[str, TrackBucket] = {}
    identity_to_bucket: dict[tuple[str, str], str] = {}
    order: list[str] = []

    for spotify_uri, artist, title, added_at_week, added_at, source_id, source_url in rows:
        uri = str(spotify_uri)
        identity = (normalize(str(artist)), normalize(str(title)))
        key = uri if uri in buckets else identity_to_bucket.get(identity, uri)
        bucket = buckets.get(key)
        if bucket is None:
            bucket = TrackBucket(
                spotify_uri=uri,
                spotify_uris=[uri],
                artist=str(artist),
                title=str(title),
                added_at_week=str(added_at_week),
                first_added_at=str(added_at),
                last_added_at=str(added_at),
                sources=[],
            )
            buckets[key] = bucket
            identity_to_bucket[identity] = key
            order.append(key)
        if uri not in bucket.spotify_uris:
            bucket.spotify_uris.append(uri)
        identity_to_bucket[identity] = key
        bucket.last_added_at = str(added_at)
        if str(source_id) not in {existing_source for existing_source, _ in bucket.sources}:
            bucket.sources.append((str(source_id), source_url))

    tracks: list[WeeklyTrack] = []
    for key in order:
        bucket = buckets[key]
        feedbacks = [
            feedback
            for uri in bucket.spotify_uris
            if (feedback := db.feedback_for_track(uri)) is not None
        ]
        feedback = max(feedbacks, key=lambda item: item[0], default=None)
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
    albums: list[WeeklyAlbum] = []
    seen: set[tuple[str, str]] = set()
    for row in rows:
        artist = str(row[0])
        album = str(row[1])
        key = (normalize(artist), normalize(album))
        if key in seen:
            continue
        seen.add(key)
        albums.append(
            WeeklyAlbum(
                artist=artist,
                album=album,
                source_id=str(row[2]),
                source_url=row[3],
            )
        )
    return albums


def _load_recommended_albums(db: DB, week: str) -> list[AlbumRecommendation | AlbumQueueItem]:
    """Carrega a fila canónica; só recalcula semanas sem snapshot legacy."""
    try:
        snapshot = db.album_queue(week)
    except Exception as e:
        log.exception("report.album_snapshot_failed", week=week, error=str(e))
        return []
    if snapshot is not None:
        return snapshot
    if week >= CANONICAL_ALBUM_QUEUE_SINCE:
        raise ValueError(
            f"Sem snapshot canónica de álbuns para {week}; "
            "sincroniza a DB ou corre `peel albums refresh` explicitamente."
        )

    try:
        scores = build_source_scores(db, weeks=4)
        source_quality = {
            score.source_id: (score.avg_rating or 0.0, score.score) for score in scores
        }
    except Exception as e:
        log.exception("report.album_source_scores_failed", error=str(e))
        source_quality = {}

    try:
        # Sem snapshot, mantemos o comportamento legacy para relatórios antigos.
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
            if isinstance(album, AlbumQueueItem):
                if album.listen_url:
                    line += f" — {album.listen_url}"
                lines.append(line)
                lines.append(f"  - Sources: {', '.join(album.source_ids)}")
                if album.editorial_url:
                    lines.append(f"    - Editorial/source: {album.editorial_url}")
                continue
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


_HTML_STYLE = """
:root {
  color-scheme: dark;
  --bg: #0a0a0a;
  --fg: #f3f1ee;
  --muted: #8d8a86;
  --faint: #5a5854;
  --line: #211f1d;
  --hover: #131211;
  --accent: #e2895a;
}
* { box-sizing: border-box; }
html { background: var(--bg); -webkit-font-smoothing: antialiased; }
body {
  margin: 0;
  padding: 0 28px 72px;
  background: var(--bg);
  color: var(--fg);
  font-family: Inter, ui-sans-serif, system-ui, sans-serif;
  line-height: 1.55;
}
a { color: inherit; text-underline-offset: 3px; }
a:hover, a:focus-visible { color: var(--accent); outline: none; }
.wrap { width: min(100%, 980px); margin: 0 auto; }
.mono {
  color: var(--faint);
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 11px;
  letter-spacing: 1.5px;
  text-transform: uppercase;
}
header { padding: 54px 0 48px; border-bottom: 1px solid var(--line); }
.eyebrow { margin: 0 0 12px; }
h1, h2, h3 { font-family: Georgia, 'Times New Roman', serif; font-weight: 500; }
h1 { margin: 0; font-size: clamp(44px, 9vw, 76px); line-height: .98; letter-spacing: -2px; }
h1 em { color: var(--accent); font-style: italic; }
.lede { max-width: 560px; margin: 18px 0 0; color: var(--muted); }
.metrics { display: grid; grid-template-columns: repeat(4, 1fr); gap: 8px; margin-top: 30px; }
.metric { padding: 14px; border: 1px solid var(--line); border-radius: 8px; }
.metric strong { display: block; font-family: Georgia, serif; font-size: 25px; font-weight: 500; }
section { margin-top: 66px; }
.sec-head {
  display: flex;
  justify-content: space-between;
  align-items: baseline;
  gap: 20px;
  margin-bottom: 16px;
}
h2 { margin: 0; font-size: 29px; letter-spacing: -.4px; }
.count { color: var(--faint); }
.album-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 10px; }
.album {
  display: grid;
  grid-template-columns: 25px 1fr;
  grid-template-rows: 1fr auto;
  min-height: 150px;
  gap: 8px;
  padding: 14px;
  border: 1px dashed var(--line);
  border-radius: 9px;
}
.album.consensus { border-color: color-mix(in srgb, var(--accent), var(--line) 45%); }
.album .number, .track .number {
  color: var(--faint);
  font-family: ui-monospace, monospace;
  font-size: 11px;
}
.album h3 { margin: 0 0 5px; font-size: 18px; line-height: 1.15; }
.album .artist, .album .meta { margin: 0; color: var(--muted); font-size: 12px; }
.actions { grid-column: 2; display: flex; flex-wrap: wrap; gap: 10px; align-self: end; }
.action {
  color: var(--faint);
  font-family: ui-monospace, monospace;
  font-size: 10px;
  letter-spacing: .8px;
  text-decoration: none;
  text-transform: uppercase;
}
.action.listen { color: var(--accent); }
.tracks { border-bottom: 1px solid var(--line); }
.track {
  display: grid;
  grid-template-columns: 34px minmax(0, 1fr) auto;
  gap: 16px;
  align-items: center;
  padding: 18px 0;
  border-top: 1px solid var(--line);
}
.track-title { margin: 0; font-size: 15px; font-weight: 600; }
.track-artist, .sources { margin: 3px 0 0; color: var(--muted); font-size: 12px; }
.sources a { color: var(--muted); }
.rating {
  padding: 4px 8px;
  border: 1px solid var(--line);
  border-radius: 999px;
  color: var(--faint);
  font-family: ui-monospace, monospace;
  font-size: 10px;
  letter-spacing: .6px;
  text-transform: uppercase;
}
.rating.love, .rating.like { border-color: var(--accent); color: var(--accent); }
details { border-top: 1px solid var(--line); }
details:last-child { border-bottom: 1px solid var(--line); }
summary { padding: 17px 0; cursor: pointer; color: var(--muted); }
.detail-list { margin: 0 0 20px; padding: 0; list-style: none; }
.detail-list li {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  gap: 18px;
  padding: 8px 0;
  color: var(--muted);
  font-size: 13px;
}
.detail-list a { color: var(--faint); }
.table-wrap { overflow-x: auto; }
table { width: 100%; border-collapse: collapse; font-size: 12px; }
th, td {
  padding: 11px 10px;
  border-bottom: 1px solid var(--line);
  text-align: right;
  white-space: nowrap;
}
th:first-child, td:first-child { padding-left: 0; text-align: left; }
th {
  color: var(--faint);
  font-family: ui-monospace, monospace;
  font-size: 10px;
  font-weight: 400;
  letter-spacing: .7px;
  text-transform: uppercase;
}
.empty { padding: 22px; border: 1px dashed var(--line); border-radius: 8px; color: var(--faint); }
footer {
  margin-top: 64px;
  padding-top: 20px;
  border-top: 1px solid var(--line);
  color: var(--faint);
}
@media (max-width: 720px) {
  body { padding-inline: 18px; }
  .metrics { grid-template-columns: repeat(2, 1fr); }
  .album-grid { grid-template-columns: 1fr; }
  .track { grid-template-columns: 28px minmax(0, 1fr); }
  .track .rating { grid-column: 2; justify-self: start; }
}
"""


def _render_html(report: WeeklyReport) -> str:
    album_cards = "\n".join(
        _render_html_album(index, album)
        for index, album in enumerate(report.recommended_albums, start=1)
    )
    track_rows = "\n".join(
        _render_html_track(index, track) for index, track in enumerate(report.tracks, start=1)
    )
    album_context = "\n".join(_render_html_context(album) for album in report.albums)
    unmatched = "\n".join(_render_html_unmatched(item) for item in report.unmatched)
    summaries = "\n".join(_render_html_summary(item) for item in report.summaries)
    summary_rows = summaries or (
        "<tr><td>—</td><td>0</td><td>0</td><td>0</td><td>0</td><td>—</td></tr>"
    )
    week = escape(report.week)

    return f"""<!doctype html>
<html lang="pt">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="color-scheme" content="dark">
  <title>Peel — {week}</title>
  <style>{_HTML_STYLE}</style>
</head>
<body>
  <main class="wrap">
    <header>
      <p class="eyebrow mono">Peel / relatório semanal</p>
      <h1>Semana <em>{week}</em></h1>
      <p class="lede">
        Estado local da descoberta editorial: fila canónica, contexto,
        falhas de matching e saúde das fontes.
      </p>
      <div class="metrics">
        {_html_metric(len(report.tracks), "faixas")}
        {_html_metric(len(report.recommended_albums), "álbuns")}
        {_html_metric(len(report.unmatched), "sem match")}
        {_html_metric(len(report.summaries), "fontes")}
      </div>
    </header>

    <section>
      {_html_section_heading("7 Álbuns a Ouvir", len(report.recommended_albums))}
      <div class="album-grid">{album_cards or _html_empty("Sem álbuns confirmados.")}</div>
    </section>

    <section>
      {_html_section_heading("Faixas", len(report.tracks))}
      <div class="tracks">{track_rows or _html_empty("Sem faixas nesta semana.")}</div>
    </section>

    <section>
      {_html_section_heading("Auditoria", len(report.albums) + len(report.unmatched))}
      <details>
        <summary>Contexto editorial · {len(report.albums)} menções</summary>
        <ul class="detail-list">{album_context or "<li>Sem menções de álbuns.</li>"}</ul>
      </details>
      <details>
        <summary>Sem match · {len(report.unmatched)} entradas</summary>
        <ul class="detail-list">{unmatched or "<li>Sem falhas de matching.</li>"}</ul>
      </details>
    </section>

    <section>
      {_html_section_heading("Resumo por fonte", len(report.summaries))}
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Fonte</th><th>Faixas</th><th>Novas</th>
              <th>Consenso</th><th>Sem match</th><th>Rating</th>
            </tr>
          </thead>
          <tbody>{summary_rows}</tbody>
        </table>
      </div>
    </section>

    <footer class="mono">Preview local · gerada a partir do estado canónico SQLite</footer>
  </main>
</body>
</html>
"""


def _html_metric(value: int, label: str) -> str:
    return f'<div class="metric"><strong>{value}</strong><span class="mono">{label}</span></div>'


def _html_section_heading(title: str, count: int) -> str:
    return (
        '<div class="sec-head">'
        f"<h2>{escape(title)}</h2>"
        f'<span class="count mono">{count:02d}</span>'
        "</div>"
    )


def _html_empty(message: str) -> str:
    return f'<p class="empty">{escape(message)}</p>'


def _render_html_album(position: int, album: AlbumRecommendation | AlbumQueueItem) -> str:
    sources, listen_url, editorial_url = _album_html_links(album)
    labels = ", ".join(source_label(source) for source in sources)
    consensus = " consensus" if album.source_count > 1 else ""
    source_word = "fontes" if album.source_count != 1 else "fonte"
    actions = _html_action(listen_url, _listen_label(listen_url), "listen")
    if editorial_url and editorial_url != listen_url:
        actions += _html_action(editorial_url, "Review")
    return f"""
<article class="album{consensus}">
  <span class="number">{position:02d}</span>
  <div>
    <p class="artist">{escape(album.artist)}</p>
    <h3>{escape(album.album)}</h3>
    <p class="meta">{escape(labels)} · {album.source_count} {source_word}</p>
  </div>
  <div class="actions">{actions or '<span class="action">Sem link</span>'}</div>
</article>"""


def _album_html_links(
    album: AlbumRecommendation | AlbumQueueItem,
) -> tuple[tuple[str, ...], str | None, str | None]:
    if isinstance(album, AlbumQueueItem):
        return album.source_ids, album.listen_url, album.editorial_url
    editorial = next(
        (url for _, url in album.source_urls if url and url != album.link_url),
        None,
    )
    return album.sources, album.link_url, editorial


def _render_html_track(position: int, track: WeeklyTrack) -> str:
    source_links = ", ".join(
        _html_link(source_url, source_label(source_id))
        if source_url
        else escape(source_label(source_id))
        for source_id, source_url in track.sources
    )
    rating = track.feedback[1] if track.feedback else "pendente"
    spotify_url = _spotify_track_url(track.spotify_uri)
    title = _html_link(spotify_url, track.title) if spotify_url else escape(track.title)
    return f"""
<article class="track">
  <span class="number">{position:02d}</span>
  <div>
    <p class="track-title">{title}</p>
    <p class="track-artist">{escape(track.artist)} · <span class="sources">{source_links}</span></p>
  </div>
  <span class="rating {escape(rating)}">{escape(rating)}</span>
</article>"""


def _render_html_context(album: WeeklyAlbum) -> str:
    label = f"{album.artist} — {album.album}"
    source = source_label(album.source_id)
    return (
        f"<li><span>{escape(label)}</span>"
        f"{_html_link(album.source_url, source) if album.source_url else escape(source)}</li>"
    )


def _render_html_unmatched(item: tuple[str, str, str, str | None]) -> str:
    source_id, artist, title, source_url = item
    label = f"{artist} — {title}"
    source = source_label(source_id)
    return (
        f"<li><span>{escape(label)}</span>"
        f"{_html_link(source_url, source) if source_url else escape(source)}</li>"
    )


def _render_html_summary(summary: SourceSummary) -> str:
    return (
        f"<tr><td>{escape(source_label(summary.source_id))}</td>"
        f"<td>{summary.tracks}</td><td>{summary.new}</td>"
        f"<td>{summary.consensus}</td><td>{summary.unmatched}</td>"
        f"<td>{escape(summary.avg_rating)}</td></tr>"
    )


def _html_action(url: str | None, label: str, extra_class: str = "") -> str:
    if not _safe_http_url(url):
        return ""
    classes = "action" + (f" {extra_class}" if extra_class else "")
    return (
        f'<a class="{classes}" href="{escape(str(url), quote=True)}" '
        f'target="_blank" rel="noreferrer">{escape(label)}</a>'
    )


def _html_link(url: str | None, label: str) -> str:
    if not _safe_http_url(url):
        return escape(label)
    return (
        f'<a href="{escape(str(url), quote=True)}" target="_blank" '
        f'rel="noreferrer">{escape(label)}</a>'
    )


def _safe_http_url(url: str | None) -> bool:
    if not url:
        return False
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _spotify_track_url(uri: str) -> str | None:
    prefix = "spotify:track:"
    if not uri.startswith(prefix):
        return None
    track_id = uri.removeprefix(prefix)
    if not track_id.isalnum():
        return None
    return f"https://open.spotify.com/track/{track_id}"


def _listen_label(url: str | None) -> str:
    if not url:
        return "Ouvir"
    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return "Ouvir"
    if host.endswith("spotify.com"):
        return "Spotify"
    if host.endswith("bandcamp.com"):
        return "Bandcamp"
    return "Ouvir"


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
