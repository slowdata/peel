"""Fila semanal de álbuns: freshness real, consenso e snapshots."""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from peel.db import SourceQuality

CANONICAL_ALBUM_QUEUE_SINCE = "2026-W29"

if TYPE_CHECKING:
    from collections.abc import Mapping

    from peel.db import DB


@dataclass(frozen=True, slots=True)
class AlbumMention:
    """Uma menção por source. ``first_seen`` nunca muda com polling repetido."""

    artist: str
    album: str
    artist_key: str
    album_key: str
    source_id: str
    source_url: str | None
    spotify_album_uri: str | None
    seen_at: str
    first_seen_at: str | None = None
    first_seen_week: str | None = None
    last_seen_at: str | None = None
    last_seen_week: str | None = None
    first_seen_reliable: bool = True

    @property
    def first_at(self) -> str:
        return self.first_seen_at or self.seen_at

    @property
    def last_at(self) -> str:
        return self.last_seen_at or self.seen_at


@dataclass(frozen=True, slots=True)
class AlbumRecommendation:
    artist: str
    album: str
    source_count: int
    sources: tuple[str, ...]
    source_urls: tuple[tuple[str, str | None], ...]
    spotify_album_uri: str | None
    latest_seen_at: str
    best_avg_rating: float
    best_score: float
    artist_key: str = ""
    album_key: str = ""
    newest_source_week: str = ""
    newest_source_at: str = ""

    @property
    def link_url(self) -> str | None:
        if self.spotify_album_uri:
            return spotify_album_url(self.spotify_album_uri)
        return next((url for _, url in self.source_urls if url), None)


def is_album_url(url: str | None) -> bool:
    """Bandcamp ``/track/`` URLs are singles, never queue-eligible albums."""
    if not url:
        return True
    try:
        parsed = urlparse(url)
    except ValueError:
        return True
    return not (
        parsed.hostname and parsed.hostname.endswith("bandcamp.com") and "/track/" in parsed.path
    )


def first_album_url(urls: object) -> str | None:
    """First valid Bandcamp album URL; editorial URLs are never listen links."""
    values = [url for url in urls if isinstance(url, str) and url]
    for url in values:
        try:
            parsed = urlparse(url)
        except ValueError:
            continue
        if (
            parsed.hostname
            and parsed.hostname.endswith("bandcamp.com")
            and "/album/" in parsed.path
        ):
            return url
    return None


def top_album_recommendations(
    db: DB,
    current_week: str,
    weeks: int = 1,
    limit: int = 7,
    source_quality: Mapping[str, SourceQuality] | None = None,
) -> list[AlbumRecommendation]:
    """Legacy/window ranking, retained for historical weeks without snapshots."""
    return rank_album_recommendations(
        _load_album_mentions(db, current_week, weeks), source_quality=source_quality, limit=limit
    )


def select_album_queue(
    db: DB,
    current_week: str,
    *,
    limit: int = 7,
    source_quality: Mapping[str, SourceQuality] | None = None,
) -> list[tuple[AlbumRecommendation, bool]]:
    """Select current new mentions first, then any unrated pending albums.

    A repeat from one source is not new because ``first_seen_week`` remains
    fixed. A *new* source for an old album is current editorial consensus and
    therefore is eligible in the fresh phase. Source repeat penalties only
    break close calls; they are deliberately not a quota or a hard cap.
    """
    if limit <= 0:
        return []
    # A historical selection is a point-in-time view: W30 discoveries must
    # never leak backwards into W29 as fresh, pending, or consensus.
    rows = _load_all_album_mentions(db, through_week=current_week)
    adjusted_quality = _album_feedback_quality(db, source_quality)
    ranked = rank_album_recommendations(rows, source_quality=adjusted_quality, limit=len(rows))
    eligible = [
        item for item in ranked if db.album_feedback_for_identity(item.artist, item.album) is None
    ]
    fresh = [item for item in eligible if item.newest_source_week == current_week]
    pending = [item for item in eligible if item.newest_source_week != current_week]
    selected = _softly_diverse(fresh, adjusted_quality, limit)
    if len(selected) < limit:
        selected.extend(_softly_diverse(pending, adjusted_quality, limit - len(selected), selected))
    fresh_keys = {(item.artist_key, item.album_key) for item in fresh}
    return [(item, (item.artist_key, item.album_key) in fresh_keys) for item in selected]


def _album_feedback_quality(
    db: DB, base: Mapping[str, SourceQuality] | None
) -> dict[str, SourceQuality]:
    """Blend album feedback into source quality with a conservative prior.

    Three observations are needed to move halfway from the track-derived
    baseline to album feedback. A single love/ban therefore cannot crown or
    sink a source, while repeated feedback remains a useful soft signal.
    """
    quality = dict(base or {})
    rows = db.conn.execute(
        """
        SELECT m.source_id, AVG(f.rating), COUNT(*)
        FROM album_mentions m
        JOIN album_feedback f
          ON f.artist_key = m.artist_key AND f.album_key = m.album_key
        WHERE f.label != 'unavailable'
        GROUP BY m.source_id
        """
    ).fetchall()
    for source_id, average, count in rows:
        prior_avg, prior_score = quality.get(str(source_id), (0.0, 0.0))
        weight = int(count) / (int(count) + 3)
        blended = (prior_avg * (1 - weight)) + (float(average) * weight)
        quality[str(source_id)] = (blended, prior_score)
    return quality


def rank_album_recommendations(
    rows: list[AlbumMention],
    source_quality: Mapping[str, SourceQuality] | None = None,
    limit: int = 7,
) -> list[AlbumRecommendation]:
    """Aggregate source mentions deterministically by consensus/quality/recency."""
    if limit <= 0:
        return []
    quality = source_quality or {}
    buckets: dict[tuple[str, str], _AlbumBucket] = {}
    for row in rows:
        if not is_album_url(row.source_url) or is_archival_album_title(row.album):
            continue
        key = (row.artist_key, row.album_key)
        buckets.setdefault(key, _AlbumBucket(row.artist, row.album, *key)).add(row, quality)
    ranked = [bucket.to_recommendation(quality) for bucket in buckets.values()]
    ranked.sort(key=_recommendation_sort_key)
    return ranked[:limit]


def spotify_album_url(spotify_album_uri: str) -> str:
    prefix = "spotify:album:"
    return (
        f"https://open.spotify.com/album/{spotify_album_uri[len(prefix) :]}"
        if spotify_album_uri.startswith(prefix)
        else spotify_album_uri
    )


EDITORIAL_ALBUM_SOURCES = {
    "guardian_music_albums",
    "thequietus",
    "thequietus_feedbacker",
    "aquarium_drunkard",
    "pitchfork_best_albums",
    "pitchfork_album_reviews",
}
LABEL_SOURCE_PREFIX = "bandcamp_"
SOURCE_REPEAT_PENALTY = 2.0
_SOURCE_FAMILIES = {
    "pitchfork_best_albums": "pitchfork",
    "pitchfork_album_reviews": "pitchfork",
    "thequietus": "thequietus",
    "thequietus_feedbacker": "thequietus",
}
_ARCHIVAL_TITLE_RE = re.compile(
    r"(?:\b(?:deluxe|expanded|remaster(?:ed)?|reissue|anniversary|archive|archival)\b|"
    r"\b\d{1,3}(?:st|nd|rd|th)\s+anniversary\b)",
    re.IGNORECASE,
)


def is_archival_album_title(title: str) -> bool:
    """Reject explicit reissue/archive editions from the current-release queue."""
    return bool(_ARCHIVAL_TITLE_RE.search(title))


def _source_family(source_id: str) -> str:
    """Publication-level identity prevents two feeds from faking consensus."""
    return _SOURCE_FAMILIES.get(source_id, source_id)


def _source_tier(source_id: str) -> int:
    """Editorial reviews beat label/release feeds when other signals tie."""
    if source_id in EDITORIAL_ALBUM_SOURCES:
        return 0
    if source_id.startswith(LABEL_SOURCE_PREFIX):
        return 2
    return 1


def _recommendation_sort_key(
    item: AlbumRecommendation,
) -> tuple[float, float, float, float, str, str]:
    return (
        -item.source_count,
        -item.best_avg_rating,
        -item.best_score,
        -_timestamp(item.latest_seen_at),
        item.artist.lower(),
        item.album.lower(),
    )


def _softly_diverse(
    candidates: list[AlbumRecommendation],
    quality: Mapping[str, SourceQuality] | None,
    limit: int,
    already: list[AlbumRecommendation] | None = None,
) -> list[AlbumRecommendation]:
    """Diminishing returns by representative source without excluding any source."""
    if limit <= 0:
        return []
    remaining = list(candidates)
    selected: list[AlbumRecommendation] = []
    counts = Counter(item.sources[0] for item in (already or []) if item.sources)
    while remaining and len(selected) < limit:

        def key(item: AlbumRecommendation) -> tuple[float, float, int, float, str, str]:
            source = item.sources[0] if item.sources else ""
            # One scalar means feedback quality and repeat penalty genuinely
            # compete. Avg rating is already reflected in score and is never a
            # lexicographic tier that makes diversity unreachable.
            combined = item.best_score + item.best_avg_rating
            adjusted = combined - (SOURCE_REPEAT_PENALTY * counts[source])
            fresh_at = item.newest_source_at or item.latest_seen_at
            return (
                -item.source_count,
                -adjusted,
                _source_tier(source),
                -_timestamp(fresh_at),
                item.artist.lower(),
                item.album.lower(),
            )

        chosen = min(remaining, key=key)
        remaining.remove(chosen)
        selected.append(chosen)
        if chosen.sources:
            counts[chosen.sources[0]] += 1
    return selected


@dataclass(slots=True)
class _AlbumBucket:
    artist: str
    album: str
    artist_key: str
    album_key: str
    source_ids: set[str] = field(default_factory=set)
    source_urls: dict[str, str | None] = field(default_factory=dict)
    source_first_weeks: dict[str, str] = field(default_factory=dict)
    source_first_ats: dict[str, str] = field(default_factory=dict)
    spotify_album_uri: str | None = None
    spotify_seen_ts: float = 0.0
    latest_seen_at: str = ""
    latest_seen_ts: float = 0.0
    best_avg_rating: float = 0.0
    best_score: float = 0.0

    def add(self, row: AlbumMention, quality: Mapping[str, SourceQuality]) -> None:
        last_ts = _timestamp(row.last_at)
        self.source_ids.add(row.source_id)
        self.source_urls[row.source_id] = row.source_url
        # An uncertain legacy secondary mention can help historical display,
        # but cannot manufacture a current fresh source/consensus.
        if row.first_seen_reliable:
            self.source_first_weeks[row.source_id] = row.first_seen_week or _week_from_timestamp(
                row.first_at
            )
            self.source_first_ats[row.source_id] = row.first_at
        if last_ts >= self.latest_seen_ts:
            self.artist, self.album = row.artist, row.album
            self.latest_seen_at, self.latest_seen_ts = row.last_at, last_ts
        avg, score = quality.get(row.source_id, (0.0, 0.0))
        self.best_avg_rating, self.best_score = (
            max(self.best_avg_rating, avg),
            max(self.best_score, score),
        )
        if row.spotify_album_uri and last_ts >= self.spotify_seen_ts:
            self.spotify_album_uri, self.spotify_seen_ts = row.spotify_album_uri, last_ts

    def to_recommendation(self, quality: Mapping[str, SourceQuality]) -> AlbumRecommendation:
        sources = tuple(
            sorted(
                self.source_ids,
                key=lambda s: (
                    _source_tier(s),
                    -quality.get(s, (0.0, 0.0))[0],
                    -quality.get(s, (0.0, 0.0))[1],
                    s,
                ),
            )
        )
        source_count = len({_source_family(source) for source in sources})
        return AlbumRecommendation(
            self.artist,
            self.album,
            source_count,
            sources,
            tuple((source, self.source_urls.get(source)) for source in sources),
            self.spotify_album_uri,
            self.latest_seen_at,
            self.best_avg_rating,
            self.best_score,
            self.artist_key,
            self.album_key,
            max(self.source_first_weeks.values(), default=""),
            max(self.source_first_ats.values(), key=_timestamp, default=""),
        )


def _load_album_mentions(db: DB, current_week: str, weeks: int) -> list[AlbumMention]:
    if weeks < 1:
        raise ValueError("weeks must be >= 1")
    cutoff = _cutoff_week(current_week, weeks)
    return _rows_to_mentions(
        db.conn.execute(
            """
            SELECT artist, album, artist_key, album_key, source_id, source_url, spotify_album_uri,
                   seen_at, first_seen_at, first_seen_week, last_seen_at, last_seen_week,
                   first_seen_reliable
            FROM album_mentions
            WHERE COALESCE(first_seen_week, added_at_week) >= ?
              AND COALESCE(first_seen_week, added_at_week) <= ?
            ORDER BY COALESCE(first_seen_at, seen_at) DESC, source_id ASC
            """,
            (cutoff, current_week),
        ).fetchall()
    )


def _load_all_album_mentions(db: DB, *, through_week: str) -> list[AlbumMention]:
    return _rows_to_mentions(
        db.conn.execute(
            """
        SELECT artist, album, artist_key, album_key, source_id, source_url, spotify_album_uri,
               seen_at, first_seen_at, first_seen_week, last_seen_at, last_seen_week,
               first_seen_reliable
        FROM album_mentions
        WHERE COALESCE(first_seen_week, added_at_week) <= ?
        ORDER BY artist_key, album_key, source_id
        """,
            (through_week,),
        ).fetchall()
    )


def _rows_to_mentions(rows: list[tuple[object, ...]]) -> list[AlbumMention]:
    mentions: list[AlbumMention] = []
    for row in rows:
        (
            artist,
            album,
            artist_key,
            album_key,
            source_id,
            source_url,
            spotify_album_uri,
            seen_at,
            first_seen_at,
            first_seen_week,
            last_seen_at,
            last_seen_week,
            first_seen_reliable,
        ) = row
        mentions.append(
            AlbumMention(
                artist=str(artist),
                album=str(album),
                artist_key=str(artist_key),
                album_key=str(album_key),
                source_id=str(source_id),
                source_url=source_url,
                spotify_album_uri=spotify_album_uri,
                seen_at=str(seen_at),
                first_seen_at=first_seen_at,
                first_seen_week=first_seen_week,
                last_seen_at=last_seen_at,
                last_seen_week=last_seen_week,
                first_seen_reliable=bool(first_seen_reliable),
            )
        )
    return mentions


def _cutoff_week(current_week: str, window: int) -> str:
    year, week = map(int, current_week.split("-W"))
    date = datetime.fromisocalendar(year, week, 1) - timedelta(weeks=window - 1)
    y, w, _ = date.isocalendar()
    return f"{y}-W{w:02d}"


def _week_from_timestamp(value: str) -> str:
    try:
        date = datetime.fromisoformat(value)
        year, week, _ = date.isocalendar()
        return f"{year}-W{week:02d}"
    except ValueError:
        return ""


def _timestamp(value: str) -> float:
    try:
        return datetime.fromisoformat(value).timestamp()
    except ValueError:
        return 0.0
