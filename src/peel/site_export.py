"""Exporta dados do Peel para o site estático peel-sept.

O site Astro consome JSON versionável em ``src/data/weeks/YYYY-Www.json``.
Este módulo mantém o contrato de dados separado da CLI para ser testável e
idempotente.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import structlog

from peel.albums import AlbumRecommendation, spotify_album_url, top_album_recommendations
from peel.db import DB, FEEDBACK_RATINGS, SourceQuality, iso_week, rank_window_uris
from peel.matcher import normalize, score
from peel.scoring import build_source_scores
from peel.sources.registry import source_label

log = structlog.get_logger()

# Resolve (artist, album) -> URL Spotify do álbum, ou None se não houver match.
AlbumResolver = Callable[[str, str], str | None]


def make_album_resolver(sp: Any, threshold: int = 85) -> AlbumResolver:
    """Cria um resolver que procura o álbum no Spotify e confirma o match.

    Exige artista E título do álbum acima do threshold (fuzzy), como no matcher
    de faixas. `sp` é um SpotifyClient (Any para evitar import circular/pesado).
    Falhas devolvem None → o cartão cai no link editorial.
    """

    def resolve(artist: str, album: str) -> str | None:
        try:
            candidates = sp.search_album(artist, album)
        except Exception as exc:  # noqa: BLE001 - resolver nunca pode rebentar o export
            log.warning(
                "site_export.album_resolve_failed", artist=artist, album=album, error=str(exc)
            )
            return None
        norm_artist, norm_album = normalize(artist), normalize(album)
        for cand in candidates:
            cand_artist = normalize(", ".join(cand.get("artists") or []))
            cand_album = normalize(cand.get("name") or "")
            artist_ok = score(norm_artist, cand_artist) >= threshold
            album_ok = score(norm_album, cand_album) >= threshold
            if artist_ok and album_ok:
                return cand.get("url")
        return None

    return resolve

SITE_TRACK_LIMIT = 7
SITE_ALBUM_WINDOW_WEEKS = 2
_MONTHS_PT = ["Jan", "Fev", "Mar", "Abr", "Mai", "Jun", "Jul", "Ago", "Set", "Out", "Nov", "Dez"]


@dataclass(frozen=True, slots=True)
class ExportedWeek:
    week: str
    path: Path


@dataclass(slots=True)
class _TrackBucket:
    spotify_uri: str
    artist: str
    title: str
    sources: dict[str, str | None]
    latest_added_at: str


def export_site(
    db: DB,
    site_dir: Path,
    weeks: int,
    playlist_id: str | None,
    current_week: str | None = None,
    album_resolver: AlbumResolver | None = None,
) -> list[ExportedWeek]:
    """Exporta N semanas para o diretório do site.

    Idempotente: para a mesma DB e argumentos, o JSON escrito é estável.
    """
    if weeks < 1:
        raise ValueError("weeks must be >= 1")

    resolved_current_week = current_week or iso_week(datetime.now(UTC))
    output_dir = site_dir / "src" / "data" / "weeks"
    output_dir.mkdir(parents=True, exist_ok=True)

    source_quality = _load_source_quality(db)
    playlist_url = playlist_url_from_id(playlist_id)
    exported: list[ExportedWeek] = []
    for week in weeks_to_export(resolved_current_week, weeks):
        payload = build_site_week_payload(
            db, week, playlist_url, source_quality, album_resolver=album_resolver
        )
        path = output_dir / f"{week}.json"
        path.write_text(_json_dumps(payload), encoding="utf-8")
        exported.append(ExportedWeek(week=week, path=path))
    return exported


def build_site_week_payload(
    db: DB,
    week: str,
    playlist_url: str | None,
    source_quality: dict[str, SourceQuality] | None = None,
    album_resolver: AlbumResolver | None = None,
) -> dict[str, Any]:
    """Constrói o JSON de uma semana seguindo exactamente o contrato do site."""
    quality = source_quality or _load_source_quality(db)
    tracks = _export_tracks(db, week, quality)
    albums = _export_albums(db, week, quality, album_resolver=album_resolver)
    sources = _sources_for_payload(tracks, albums)
    start = _week_start(week)
    end = start + timedelta(days=6)
    return {
        "week": week,
        # Datas locale-neutras (ISO): o site formata por idioma (EN/PT).
        "start_date": start.strftime("%Y-%m-%d"),
        "end_date": end.strftime("%Y-%m-%d"),
        # label/date_range pré-formatados em PT mantidos por compat com o site
        # atual; serão substituídos quando o site passar a formatar de ISO.
        "label": week_label(week),
        "date_range": week_date_range(week),
        "playlist_url": playlist_url,
        "tracks": tracks,
        "albums": albums,
        "sources": sources,
    }


def weeks_to_export(current_week: str, count: int) -> list[str]:
    """Semanas ISO a exportar, em ordem cronológica."""
    if count < 1:
        raise ValueError("count must be >= 1")
    start = _week_start(current_week) - timedelta(weeks=count - 1)
    return [iso_week(start + timedelta(weeks=index)) for index in range(count)]


def week_label(week: str) -> str:
    year, week_number = _split_week(week)
    return f"Semana {week_number} · {year}"


def week_date_range(week: str) -> str:
    start = _week_start(week)
    end = start + timedelta(days=6)
    if start.year == end.year and start.month == end.month:
        return f"{start.day} — {end.day} {_month_name(end)} {end.year}"
    if start.year == end.year:
        return f"{start.day} {_month_name(start)} — {end.day} {_month_name(end)} {end.year}"
    return (
        f"{start.day} {_month_name(start)} {start.year} — {end.day} {_month_name(end)} {end.year}"
    )


def playlist_url_from_id(playlist_id: str | None) -> str | None:
    if not playlist_id:
        return None
    value = playlist_id.strip()
    if not value:
        return None
    if value.startswith("http://") or value.startswith("https://"):
        return value
    prefix = "spotify:playlist:"
    if value.startswith(prefix):
        value = value[len(prefix) :]
    return f"https://open.spotify.com/playlist/{value}"


def _export_tracks(
    db: DB,
    week: str,
    source_quality: dict[str, SourceQuality],
) -> list[dict[str, Any]]:
    rows = _weekly_track_rows(db, week)
    ranked_uris = rank_window_uris(
        [(row[0], row[1], row[2], row[3], row[5]) for row in rows],
        source_quality,
    )
    buckets = _track_buckets(rows, source_quality)
    tracks: list[dict[str, Any]] = []
    for rank, uri in enumerate(ranked_uris[:SITE_TRACK_LIMIT], start=1):
        bucket = buckets.get(uri)
        if bucket is None:
            continue
        best_source = _best_source(bucket.sources, source_quality)
        tracks.append(
            {
                "rank": rank,
                "artist": bucket.artist,
                "title": bucket.title,
                "source": source_label(best_source),
                "source_count": len(bucket.sources),
                "spotify_url": spotify_track_url(bucket.spotify_uri),
            }
        )
    return tracks


def _export_albums(
    db: DB,
    week: str,
    source_quality: dict[str, SourceQuality],
    album_resolver: AlbumResolver | None = None,
) -> list[dict[str, Any]]:
    recommendations = top_album_recommendations(
        db,
        week,
        weeks=SITE_ALBUM_WINDOW_WEEKS,
        limit=7,
        source_quality=source_quality,
    )
    return [
        _album_to_json(index, item, source_quality, album_resolver)
        for index, item in enumerate(recommendations, 1)
    ]


def _album_to_json(
    rank: int,
    item: AlbumRecommendation,
    source_quality: dict[str, SourceQuality],
    album_resolver: AlbumResolver | None = None,
) -> dict[str, Any]:
    spotify_url = spotify_album_url(item.spotify_album_uri) if item.spotify_album_uri else None
    # Se a source não trouxe URI Spotify (ex. Guardian/Quietus), tenta resolver
    # pelo nome no Spotify. Sem match → fica None e o cartão usa o link editorial.
    if spotify_url is None and album_resolver is not None:
        spotify_url = album_resolver(item.artist, item.album)
    editorial_link = _first_source_url(item)
    return {
        "rank": rank,
        "artist": item.artist,
        "title": item.album,
        "source": _album_source_label(item, source_quality),
        "source_count": item.source_count,
        "link": editorial_link or spotify_url,
        "spotify_url": spotify_url,
    }


def _weekly_track_rows(db: DB, week: str) -> list[tuple[str, str, str, str, str | None, str]]:
    rows = db.conn.execute(
        """
        SELECT spotify_uri, artist, title, source_id, source_url, added_at
        FROM tracks
        WHERE added_at_week = ?
        ORDER BY added_at ASC, source_id ASC, spotify_uri ASC
        """,
        (week,),
    ).fetchall()
    banned_uris = _banned_uris(db)
    banned_keys = db.banned_track_keys()
    filtered: list[tuple[str, str, str, str, str | None, str]] = []
    for spotify_uri, artist, title, source_id, source_url, added_at in rows:
        if str(spotify_uri) in banned_uris:
            continue
        if (normalize(str(artist)), normalize(str(title))) in banned_keys:
            continue
        filtered.append(
            (str(spotify_uri), str(artist), str(title), str(source_id), source_url, str(added_at))
        )
    return filtered


def _track_buckets(
    rows: list[tuple[str, str, str, str, str | None, str]],
    source_quality: dict[str, SourceQuality],
) -> dict[str, _TrackBucket]:
    buckets: dict[str, _TrackBucket] = {}
    for spotify_uri, artist, title, source_id, source_url, added_at in rows:
        bucket = buckets.get(spotify_uri)
        if bucket is None:
            bucket = _TrackBucket(
                spotify_uri=spotify_uri,
                artist=artist,
                title=title,
                sources={},
                latest_added_at=added_at,
            )
            buckets[spotify_uri] = bucket
        if _timestamp_sort_value(added_at) >= _timestamp_sort_value(bucket.latest_added_at):
            bucket.artist = artist
            bucket.title = title
            bucket.latest_added_at = added_at
        bucket.sources[source_id] = source_url
    for bucket in buckets.values():
        bucket.sources = dict(
            sorted(
                bucket.sources.items(),
                key=lambda item: _source_sort_key(item[0], source_quality),
            )
        )
    return buckets


def _best_source(
    sources: dict[str, str | None],
    source_quality: dict[str, SourceQuality],
) -> str:
    return sorted(sources, key=lambda source_id: _source_sort_key(source_id, source_quality))[0]


def _source_sort_key(
    source_id: str, source_quality: dict[str, SourceQuality]
) -> tuple[float, float, str]:
    avg_rating, score = source_quality.get(source_id, (0.0, 0.0))
    return (-avg_rating, -score, source_id)


def _album_source_label(
    item: AlbumRecommendation,
    source_quality: dict[str, SourceQuality],
) -> str:
    sources = sorted(
        item.sources, key=lambda source_id: _source_sort_key(source_id, source_quality)
    )
    return ", ".join(source_label(source_id) for source_id in sources)


def _first_source_url(item: AlbumRecommendation) -> str | None:
    for _, source_url in item.source_urls:
        if source_url:
            return source_url
    return None


def _sources_for_payload(tracks: list[dict[str, Any]], albums: list[dict[str, Any]]) -> list[str]:
    seen: set[str] = set()
    sources: list[str] = []
    for row in [*tracks, *albums]:
        raw_source = row.get("source")
        if not raw_source:
            continue
        for source in str(raw_source).split(", "):
            if source in seen:
                continue
            seen.add(source)
            sources.append(source)
    return sources


def _load_source_quality(db: DB) -> dict[str, SourceQuality]:
    try:
        scores = build_source_scores(db, weeks=4)
    except Exception as exc:
        log.exception("site_export.source_scores_failed", error=str(exc))
        return {}
    return {score.source_id: (score.avg_rating or 0.0, score.score) for score in scores}


def _banned_uris(db: DB) -> set[str]:
    rows = db.conn.execute(
        """
        SELECT spotify_uri
        FROM feedback
        WHERE rating = ?
        """,
        (FEEDBACK_RATINGS["ban"],),
    ).fetchall()
    return {str(row[0]) for row in rows}


def spotify_track_url(spotify_uri: str) -> str | None:
    prefix = "spotify:track:"
    if spotify_uri.startswith(prefix):
        return f"https://open.spotify.com/track/{spotify_uri[len(prefix) :]}"
    if spotify_uri.startswith("https://open.spotify.com/track/"):
        return spotify_uri
    return None


def _json_dumps(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2) + "\n"


def _week_start(week: str) -> datetime:
    year, week_number = _split_week(week)
    return datetime.fromisocalendar(year, week_number, 1).replace(tzinfo=UTC)


def _split_week(week: str) -> tuple[int, int]:
    year_str, week_str = week.split("-W")
    return int(year_str), int(week_str)


def _month_name(value: datetime) -> str:
    return _MONTHS_PT[value.month - 1]


def _timestamp_sort_value(value: str) -> float:
    try:
        return datetime.fromisoformat(value).timestamp()
    except ValueError:
        return 0.0
