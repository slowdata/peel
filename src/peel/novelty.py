"""Keep the album queue to genuinely new releases.

Two read-only checks run on each queue candidate, after its listen link is
known:

* the Spotify album we would link to is a remaster/deluxe edition or was
  released more than ``MAX_RELEASE_AGE_DAYS`` before the queue week;
* the artist ended (died or disbanded, per MusicBrainz) more than
  ``ARTIST_ENDED_GRACE_YEARS`` before the week: a "new" release is then archival
  (e.g. John Coltrane's The Tiberi Tapes, 2026).

Any lookup failure or ambiguity keeps the album: a missed reissue costs one
slot, a false exclusion hides a new record.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any
from urllib.parse import urlparse

import httpx
import structlog

from peel.albums import is_archival_album_title
from peel.matcher import normalize
from peel.musicbrainz import MUSICBRAINZ_ARTIST_SEARCH_URL, MUSICBRAINZ_USER_AGENT

log = structlog.get_logger()

MAX_RELEASE_AGE_DAYS = 180
ARTIST_ENDED_GRACE_YEARS = 2
MUSICBRAINZ_MIN_SCORE = 90
MUSICBRAINZ_INTERVAL_SECONDS = 1.5
MUSICBRAINZ_RETRY_SECONDS = 3.0

ArtistSearch = Callable[[str], dict[str, Any]]
SpotifyAlbumLookup = Callable[[str], dict[str, Any]]
# (artist, album, listen_url, week) -> reason it is not a new release, or None
NoveltyFilter = Callable[[str, str, str | None, str], str | None]


def parse_release_date(value: str | None) -> date | None:
    """Spotify dates come as YYYY, YYYY-MM or YYYY-MM-DD."""
    if not value:
        return None
    parts = value.split("-")
    try:
        return date(
            int(parts[0]),
            int(parts[1]) if len(parts) > 1 else 1,
            int(parts[2]) if len(parts) > 2 else 1,
        )
    except (ValueError, IndexError):
        return None


def spotify_album_id(url: str | None) -> str | None:
    if not url:
        return None
    if url.startswith("spotify:album:"):
        return url.rsplit(":", 1)[-1]
    parsed = urlparse(url)
    if (parsed.hostname or "").lower() != "open.spotify.com":
        return None
    parts = [part for part in parsed.path.split("/") if part]
    return parts[-1] if len(parts) >= 2 and parts[-2] == "album" else None


def artist_ended_on(artist: str, payload: dict[str, Any]) -> date | None:
    """End date when every confident exact-name match has ended; else None."""
    target = normalize(artist)
    matches = [
        item
        for item in payload.get("artists") or []
        if isinstance(item, dict)
        and normalize(str(item.get("name") or "")) == target
        and int(item.get("score") or 0) >= MUSICBRAINZ_MIN_SCORE
    ]
    if not matches:
        return None
    ends: list[date] = []
    for item in matches:
        span = item.get("life-span") or {}
        end = parse_release_date(str(span.get("end") or "")) if span.get("ended") else None
        if end is None:
            return None  # an active namesake: cannot call it archival
        ends.append(end)
    return max(ends)


def _search_musicbrainz(artist: str) -> dict[str, Any]:
    """One artist search; a 503 (MusicBrainz rate limit) is retried once."""
    for attempt in range(2):
        response = httpx.get(
            MUSICBRAINZ_ARTIST_SEARCH_URL,
            params={"query": f'artist:"{artist}"', "fmt": "json", "limit": "5"},
            headers={"User-Agent": MUSICBRAINZ_USER_AGENT, "Accept": "application/json"},
            timeout=10,
        )
        if response.status_code == 503 and attempt == 0:
            time.sleep(MUSICBRAINZ_RETRY_SECONDS)
            continue
        response.raise_for_status()
        return response.json()
    raise RuntimeError("unreachable")


@dataclass
class NoveltyCheck:
    """Callable ``(artist, album, listen_url, week) -> reason | None``."""

    spotify_album: SpotifyAlbumLookup | None = None
    # Looked up at construction so tests can patch the module-level search.
    artist_search: ArtistSearch | None = field(default_factory=lambda: _search_musicbrainz)
    sleep: Callable[[float], None] = time.sleep
    _artist_cache: dict[str, date | None] = field(default_factory=dict)
    _last_mb_call: float = 0.0

    def __call__(self, artist: str, album: str, listen_url: str | None, week: str) -> str | None:
        week_start = datetime.fromisocalendar(int(week[:4]), int(week[-2:]), 1).date()
        return self._spotify_reason(listen_url, week_start) or self._artist_reason(
            artist, week_start
        )

    def _spotify_reason(self, listen_url: str | None, week_start: date) -> str | None:
        album_id = spotify_album_id(listen_url)
        if self.spotify_album is None or album_id is None:
            return None
        try:
            data = self.spotify_album(album_id)
        except Exception as exc:  # noqa: BLE001 - novelty is best-effort
            log.warning("novelty.spotify_failed", album_id=album_id, error=str(exc))
            return None
        name = str(data.get("name") or "")
        if is_archival_album_title(name):
            return f"edição de arquivo no Spotify ({name})"
        released = parse_release_date(data.get("release_date"))
        if released and (week_start - released).days > MAX_RELEASE_AGE_DAYS:
            return f"lançado em {released.isoformat()}"
        return None

    def _artist_reason(self, artist: str, week_start: date) -> str | None:
        if self.artist_search is None:
            return None
        key = normalize(artist)
        if key not in self._artist_cache:
            wait = MUSICBRAINZ_INTERVAL_SECONDS - (time.monotonic() - self._last_mb_call)
            if self._last_mb_call and wait > 0:
                self.sleep(wait)
            self._last_mb_call = time.monotonic()
            try:
                self._artist_cache[key] = artist_ended_on(artist, self.artist_search(artist))
            except Exception as exc:  # noqa: BLE001 - novelty is best-effort
                log.warning("novelty.musicbrainz_failed", artist=artist, error=str(exc))
                self._artist_cache[key] = None
        ended = self._artist_cache[key]
        if ended and (week_start.year - ended.year) > ARTIST_ENDED_GRACE_YEARS:
            return f"artista terminou em {ended.year} (gravação de arquivo)"
        return None
