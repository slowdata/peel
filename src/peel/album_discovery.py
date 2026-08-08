"""Album-only source refresh, deliberately isolated from tracks and delivery."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from peel.config import settings
from peel.db import DB
from peel.models import Track
from peel.sources.base import Source
from peel.sources.registry import active_sources


class AlbumDiscoveryError(RuntimeError):
    """Album sources could not be fetched completely enough for a safe refresh."""


@dataclass(frozen=True, slots=True)
class AlbumDiscoveryResult:
    sources: int
    fetched: int
    fresh: int
    new_albums: int


def discover_album_mentions(
    db: DB,
    *,
    now: datetime | None = None,
    sources: list[Source] | None = None,
) -> AlbumDiscoveryResult:
    """Fetch and persist album sources only, with no Spotify/Telegram/track effects.

    All network fetches complete before the first DB write.  A broken source
    therefore cannot cause a partial queue refresh.
    """
    reference = now or datetime.now(UTC)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=UTC)
    candidates = active_sources() if sources is None else sources
    album_sources = [source for source in candidates if source.kind == "album"]
    collected: list[Track] = []
    fetched_count = 0
    errors: list[str] = []
    for source in album_sources:
        try:
            items = source.fetch()
        except Exception as exc:  # noqa: BLE001 - aggregate before any DB write
            errors.append(f"{source.id}: {exc}")
            continue
        fetched_count += len(items)
        collected.extend(_fresh_items(items, reference))
    if errors:
        raise AlbumDiscoveryError("Falharam album sources: " + "; ".join(errors))

    new_albums = 0
    for item in collected:
        if db.record_album(
            item.artist,
            item.title,
            item.source_id,
            item.source_url,
            spotify_album_uri=item.spotify_album_uri,
        ):
            new_albums += 1
    return AlbumDiscoveryResult(
        sources=len(album_sources),
        fetched=fetched_count,
        fresh=len(collected),
        new_albums=new_albums,
    )


def _fresh_items(items: list[Track], now: datetime) -> list[Track]:
    cutoff = now - timedelta(days=settings.peel_max_source_item_age_days)
    fresh: list[Track] = []
    for item in items:
        published_at = item.published_at
        if published_at is None:
            fresh.append(item)
            continue
        if published_at.tzinfo is None:
            published_at = published_at.replace(tzinfo=UTC)
        if published_at >= cutoff:
            fresh.append(item)
    return fresh
