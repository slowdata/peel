"""Seleção semanal de álbuns por consenso cross-source."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from peel.db import SourceQuality

if TYPE_CHECKING:
    from collections.abc import Mapping

    from peel.db import DB


@dataclass(frozen=True, slots=True)
class AlbumMention:
    """Uma menção de álbum por source."""

    artist: str
    album: str
    artist_key: str
    album_key: str
    source_id: str
    source_url: str | None
    spotify_album_uri: str | None
    seen_at: str


@dataclass(frozen=True, slots=True)
class AlbumRecommendation:
    """Álbum recomendado para a seleção semanal."""

    artist: str
    album: str
    source_count: int
    sources: tuple[str, ...]
    source_urls: tuple[tuple[str, str | None], ...]
    spotify_album_uri: str | None
    latest_seen_at: str
    best_avg_rating: float
    best_score: float

    @property
    def link_url(self) -> str | None:
        """Link preferido para entrega: Spotify primeiro, depois fonte editorial."""
        if self.spotify_album_uri:
            return spotify_album_url(self.spotify_album_uri)
        for _, source_url in self.source_urls:
            if source_url:
                return source_url
        return None


def top_album_recommendations(
    db: DB,
    current_week: str,
    weeks: int = 1,
    limit: int = 7,
    source_quality: Mapping[str, SourceQuality] | None = None,
) -> list[AlbumRecommendation]:
    """Carrega menções de uma janela e devolve o top N de álbuns."""
    rows = _load_album_mentions(db, current_week, weeks)
    return rank_album_recommendations(rows, source_quality=source_quality, limit=limit)


def rank_album_recommendations(
    rows: list[AlbumMention],
    source_quality: Mapping[str, SourceQuality] | None = None,
    limit: int = 7,
) -> list[AlbumRecommendation]:
    """Ordena álbuns por consenso, qualidade de source e recência.

    Sources sem score são neutras `(0, 0)`. Isto evita punir sources puramente
    de álbuns enquanto ainda permite que scores existentes desempatem.
    """
    if limit <= 0:
        return []

    quality = source_quality or {}
    buckets: dict[tuple[str, str], _AlbumBucket] = {}
    for row in rows:
        key = (row.artist_key, row.album_key)
        bucket = buckets.get(key)
        if bucket is None:
            bucket = _AlbumBucket(
                artist=row.artist,
                album=row.album,
                artist_key=row.artist_key,
                album_key=row.album_key,
            )
            buckets[key] = bucket
        bucket.add(row, quality)

    ranked = [bucket.to_recommendation(quality) for bucket in buckets.values()]
    ranked.sort(
        key=lambda item: (
            -item.source_count,
            -item.best_avg_rating,
            -item.best_score,
            -_timestamp_sort_value(item.latest_seen_at),
            item.artist.lower(),
            item.album.lower(),
        )
    )
    return ranked[:limit]


def spotify_album_url(spotify_album_uri: str) -> str:
    """Converte `spotify:album:<id>` em URL web, se possível."""
    prefix = "spotify:album:"
    if spotify_album_uri.startswith(prefix):
        return f"https://open.spotify.com/album/{spotify_album_uri[len(prefix) :]}"
    return spotify_album_uri


@dataclass(slots=True)
class _AlbumBucket:
    artist: str
    album: str
    artist_key: str
    album_key: str
    source_ids: set[str] = field(default_factory=set)
    source_urls: dict[str, str | None] = field(default_factory=dict)
    spotify_album_uri: str | None = None
    spotify_seen_ts: float = 0.0
    latest_seen_at: str = ""
    latest_seen_ts: float = 0.0
    best_avg_rating: float = 0.0
    best_score: float = 0.0

    def add(self, row: AlbumMention, quality: Mapping[str, SourceQuality]) -> None:
        seen_ts = _timestamp_sort_value(row.seen_at)
        self.source_ids.add(row.source_id)
        self.source_urls[row.source_id] = row.source_url

        if seen_ts >= self.latest_seen_ts:
            self.artist = row.artist
            self.album = row.album
            self.latest_seen_at = row.seen_at
            self.latest_seen_ts = seen_ts

        avg_rating, score = quality.get(row.source_id, (0.0, 0.0))
        self.best_avg_rating = max(self.best_avg_rating, avg_rating)
        self.best_score = max(self.best_score, score)

        if row.spotify_album_uri and seen_ts >= self.spotify_seen_ts:
            self.spotify_album_uri = row.spotify_album_uri
            self.spotify_seen_ts = seen_ts

    def to_recommendation(
        self,
        quality: Mapping[str, SourceQuality],
    ) -> AlbumRecommendation:
        sources = tuple(
            sorted(
                self.source_ids,
                key=lambda source_id: (
                    -quality.get(source_id, (0.0, 0.0))[0],
                    -quality.get(source_id, (0.0, 0.0))[1],
                    source_id,
                ),
            )
        )
        source_urls = tuple((source_id, self.source_urls.get(source_id)) for source_id in sources)
        return AlbumRecommendation(
            artist=self.artist,
            album=self.album,
            source_count=len(sources),
            sources=sources,
            source_urls=source_urls,
            spotify_album_uri=self.spotify_album_uri,
            latest_seen_at=self.latest_seen_at,
            best_avg_rating=self.best_avg_rating,
            best_score=self.best_score,
        )


def _load_album_mentions(db: DB, current_week: str, weeks: int) -> list[AlbumMention]:
    if weeks < 1:
        raise ValueError("weeks must be >= 1")
    cutoff_week = _cutoff_week(current_week, weeks)
    rows = db.conn.execute(
        """
        SELECT artist, album, artist_key, album_key, source_id, source_url,
               spotify_album_uri, seen_at
        FROM album_mentions
        WHERE added_at_week >= ? AND added_at_week <= ?
        ORDER BY seen_at DESC, source_id ASC, artist COLLATE NOCASE, album COLLATE NOCASE
        """,
        (cutoff_week, current_week),
    ).fetchall()
    return [
        AlbumMention(
            artist=str(row[0]),
            album=str(row[1]),
            artist_key=str(row[2]),
            album_key=str(row[3]),
            source_id=str(row[4]),
            source_url=row[5],
            spotify_album_uri=row[6],
            seen_at=str(row[7]),
        )
        for row in rows
    ]


def _cutoff_week(current_week: str, window: int) -> str:
    year, week = map(int, current_week.split("-W"))
    current_start = datetime.fromisocalendar(year, week, 1)
    cutoff_dt = current_start - timedelta(weeks=window - 1)
    cutoff_year, cutoff_week_num, _ = cutoff_dt.isocalendar()
    return f"{cutoff_year}-W{cutoff_week_num:02d}"


def _timestamp_sort_value(value: str) -> float:
    try:
        return datetime.fromisoformat(value).timestamp()
    except ValueError:
        return 0.0
