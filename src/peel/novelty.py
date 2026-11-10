"""Read-only release admission after matching or resolving a listening link.

Spotify/Bandcamp metadata must confirm a recent, already-out release and album
format. Explicit archival editions and long-ended artists are excluded. Artist
ambiguity remains best-effort so namesakes do not hide otherwise eligible music.
Missing providers are allowed only for explicitly incomplete offline previews.
Edition dates cannot prove the original release of every unmarked recording.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

import httpx
import structlog

from peel.albums import is_archival_album_title
from peel.matcher import normalize
from peel.musicbrainz import MUSICBRAINZ_ARTIST_SEARCH_URL, MUSICBRAINZ_USER_AGENT
from peel.releases import (
    MAX_RELEASE_AGE_DAYS as MAX_RELEASE_AGE_DAYS,
)
from peel.releases import (
    MIN_ALBUM_TRACKS,
    release_date_bounds,
    release_date_reason,
)

if TYPE_CHECKING:
    from peel.sources.bandcamp import ReleaseDetails

log = structlog.get_logger()

ARTIST_ENDED_GRACE_YEARS = 2
MUSICBRAINZ_MIN_SCORE = 90
MUSICBRAINZ_INTERVAL_SECONDS = 1.5
MUSICBRAINZ_RETRY_SECONDS = 3.0

ArtistSearch = Callable[[str], dict[str, Any]]
SpotifyAlbumLookup = Callable[[str], dict[str, Any]]
TrackReleaseFilter = Callable[[str], str | None]
BandcampReleaseLookup = Callable[[str], "ReleaseDetails"]
# (artist, album, listen_url, week) -> reason it is not a new release, or None
NoveltyFilter = Callable[[str, str, str | None, str], str | None]


def parse_release_date(value: str | None) -> date | None:
    """Spotify dates come as YYYY, YYYY-MM or YYYY-MM-DD."""
    bounds = release_date_bounds(value)
    return bounds[0] if bounds else None


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


_TRACK_ARCHIVE_SUFFIX = re.compile(
    r"(?:\(|[-–—])[^)]*\b(?:remaster(?:ed)?|reissue|anniversary)\b", re.IGNORECASE
)


@dataclass(frozen=True, slots=True)
class TrackReleaseCheck:
    reason: str | None = None
    retryable: bool = False


def check_track_release(
    candidate: dict[str, Any],
    candidates: list[dict[str, Any]],
    reference: date,
) -> TrackReleaseCheck:
    """Validate search metadata without extra API calls or changing matching.

    Same-ISRC editions can reveal an old recording behind a new compilation.
    A deluxe parent alone does not reject genuinely new bonus tracks.
    """
    if _TRACK_ARCHIVE_SUFFIX.search(str(candidate.get("name") or "")):
        return TrackReleaseCheck("versão de arquivo/remasterização")
    reason = release_date_reason(candidate.get("release_date"), reference)
    if reason:
        bounds = release_date_bounds(candidate.get("release_date"))
        retryable = bounds is None or bounds[1] >= reference - timedelta(days=MAX_RELEASE_AGE_DAYS)
        return TrackReleaseCheck(reason, retryable=retryable)
    isrc = candidate.get("isrc")
    if isinstance(isrc, str) and isrc.strip():
        cutoff = reference - timedelta(days=MAX_RELEASE_AGE_DAYS)
        for other in candidates:
            other_isrc = other.get("isrc")
            if (
                not isinstance(other_isrc, str)
                or other_isrc.strip().casefold() != isrc.strip().casefold()
            ):
                continue
            bounds = release_date_bounds(other.get("release_date"))
            if bounds and bounds[1] < cutoff:
                return TrackReleaseCheck(
                    f"mesma gravação (ISRC) já editada em {other['release_date']}"
                )
    return TrackReleaseCheck()


def track_release_reason(
    candidate: dict[str, Any],
    candidates: list[dict[str, Any]],
    reference: date,
) -> str | None:
    """Convenience interface for read-only release diagnostics."""
    return check_track_release(candidate, candidates, reference).reason


def make_track_release_filter(
    lookup: Callable[[str], dict[str, Any]],
    reference: date,
) -> TrackReleaseFilter:
    """Cache read-only metadata checks for the bounded pending backfill pool."""
    cache: dict[str, str | None] = {}

    def check(uri: str) -> str | None:
        if uri not in cache:
            try:
                data = lookup(uri)
                if not isinstance(data, dict):
                    raise ValueError("invalid track metadata")
                album = data.get("album") or {}
                candidate = {
                    "name": data.get("name"),
                    "release_date": album.get("release_date"),
                    "isrc": (data.get("external_ids") or {}).get("isrc"),
                }
                cache[uri] = track_release_reason(candidate, [candidate], reference)
            except Exception as exc:  # noqa: BLE001 - hold backfill, preserve historical state
                log.warning("novelty.track_failed", uri=uri, error=str(exc))
                cache[uri] = "metadados de lançamento da faixa não confirmados"
        return cache[uri]

    return check


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
    bandcamp_release: BandcampReleaseLookup | None = None
    reference_date: date | None = None
    # Looked up at construction so tests can patch the module-level search.
    artist_search: ArtistSearch | None = field(default_factory=lambda: _search_musicbrainz)
    sleep: Callable[[float], None] = time.sleep
    _artist_cache: dict[str, date | None] = field(default_factory=dict)
    _last_mb_call: float = 0.0

    def __call__(self, artist: str, album: str, listen_url: str | None, week: str) -> str | None:
        week_start = datetime.fromisocalendar(int(week[:4]), int(week[-2:]), 1).date()
        reference = self.reference_date or week_start + timedelta(days=6)
        return (
            self._spotify_reason(listen_url, reference)
            or self._bandcamp_reason(listen_url, reference)
            or self._artist_reason(artist, week_start)
        )

    def _spotify_reason(self, listen_url: str | None, reference: date) -> str | None:
        album_id = spotify_album_id(listen_url)
        if self.spotify_album is None or album_id is None:
            return None
        try:
            data = self.spotify_album(album_id)
        except Exception as exc:  # noqa: BLE001 - novelty is best-effort
            log.warning("novelty.spotify_failed", album_id=album_id, error=str(exc))
            return "metadados de lançamento Spotify não confirmados"
        if not isinstance(data, dict):
            return "metadados de lançamento Spotify não confirmados"
        name = str(data.get("name") or "")
        if is_archival_album_title(name):
            return f"edição de arquivo no Spotify ({name})"
        count = data.get("total_tracks")
        if not isinstance(count, int) or isinstance(count, bool):
            return "formato do lançamento Spotify não confirmado"
        if count < 1:
            return "lançamento Spotify sem faixas"
        if count < MIN_ALBUM_TRACKS and data.get("album_type") != "album":
            return f"single/EP curto no Spotify ({count} faixas)"
        if data.get("album_type") == "compilation":
            return "compilação no Spotify"
        return release_date_reason(data.get("release_date"), reference)

    def _bandcamp_reason(self, listen_url: str | None, reference: date) -> str | None:
        if self.bandcamp_release is None or not listen_url:
            return None
        parsed = urlparse(listen_url)
        if not (parsed.hostname or "").endswith(".bandcamp.com"):
            return None
        try:
            details = self.bandcamp_release(listen_url)
        except Exception as exc:  # noqa: BLE001 - withhold, never fabricate a release date
            log.warning("novelty.bandcamp_failed", url=listen_url, error=str(exc))
            return "metadados de lançamento Bandcamp não confirmados"
        if details.archival_reason:
            return details.archival_reason
        if details.preorder:
            return "pré-venda Bandcamp"
        if details.track_count < MIN_ALBUM_TRACKS:
            return f"single/EP curto no Bandcamp ({details.track_count} faixas)"
        released = details.released_at.date().isoformat() if details.released_at else None
        return release_date_reason(released, reference)

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
