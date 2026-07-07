"""Snapshot helper for Spotify Release Radar.

Spotify personalised playlists such as Release Radar can be visible in the web
player while returning 404 via the public Web API. This module deliberately uses
only the server-rendered public playlist page as a lightweight fallback so the
weekly run stays independent from Spotify recommendation APIs.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any

import httpx
from selectolax.parser import HTMLParser, Node

DEFAULT_RELEASE_RADAR_URL = (
    "https://open.spotify.com/playlist/37i9dQZEVXbdMFQMlaz9jo?si=53d60675596e47b4"
)
RELEASE_RADAR_SOURCE_ID = "spotify_release_radar"

# Spotify serves the useful server-rendered mobile page to a generic UA. A full
# desktop Chrome UA currently returns a tiny shell without track rows.
_BROWSER_HEADERS = {"User-Agent": "Mozilla/5.0"}


@dataclass(frozen=True, slots=True)
class ReleaseRadarTrack:
    """Track extracted from the Spotify web playlist page."""

    position: int
    spotify_uri: str
    title: str
    artists: list[str]

    @property
    def artist(self) -> str:
        return ", ".join(self.artists)

    @property
    def spotify_url(self) -> str:
        return self.spotify_uri.replace("spotify:track:", "https://open.spotify.com/track/")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self) | {"artist": self.artist, "spotify_url": self.spotify_url}


def fetch_release_radar(
    url: str = DEFAULT_RELEASE_RADAR_URL,
    *,
    timeout: float = 20.0,
) -> list[ReleaseRadarTrack]:
    """Fetch and parse a Release Radar web page snapshot.

    This is a fallback for personalised Spotify playlists that cannot be read via
    the official API. It is intentionally not used by the weekly pipeline.
    """
    response = httpx.get(
        url,
        headers=_BROWSER_HEADERS,
        follow_redirects=True,
        timeout=timeout,
    )
    response.raise_for_status()
    return parse_release_radar_html(response.text)


def parse_release_radar_html(html: str) -> list[ReleaseRadarTrack]:
    """Parse tracks from Spotify's server-rendered playlist rows."""
    parser = HTMLParser(html)
    rows = parser.css('[data-testid="track-row"]')
    tracks: list[ReleaseRadarTrack] = []

    for row in rows:
        spotify_uri = _extract_track_uri(row)
        title = (row.attributes.get("aria-label") or "").strip()
        artists = _extract_artists(row)
        if not spotify_uri or not title:
            continue
        tracks.append(
            ReleaseRadarTrack(
                position=len(tracks) + 1,
                spotify_uri=spotify_uri,
                title=title,
                artists=artists,
            )
        )

    return tracks


def release_radar_snapshot_payload(
    tracks: list[ReleaseRadarTrack],
    *,
    week: str,
    url: str = DEFAULT_RELEASE_RADAR_URL,
    fetched_at: datetime | None = None,
) -> dict[str, Any]:
    """JSON-serialisable snapshot payload."""
    timestamp = fetched_at or datetime.now(UTC)
    return {
        "week": week,
        "source_id": RELEASE_RADAR_SOURCE_ID,
        "url": url,
        "fetched_at": timestamp.isoformat(),
        "track_count": len(tracks),
        "tracks": [track.to_dict() for track in tracks],
    }


def tracks_from_snapshot(payload: dict[str, Any]) -> list[ReleaseRadarTrack]:
    """Load ReleaseRadarTrack objects from a saved snapshot payload."""
    tracks: list[ReleaseRadarTrack] = []
    for item in payload.get("tracks", []):
        if not isinstance(item, dict):
            continue
        spotify_uri = str(item.get("spotify_uri") or "").strip()
        title = str(item.get("title") or "").strip()
        artists_raw = item.get("artists") or []
        artists = [str(artist).strip() for artist in artists_raw if str(artist).strip()]
        if not spotify_uri or not title:
            continue
        tracks.append(
            ReleaseRadarTrack(
                position=int(item.get("position") or len(tracks) + 1),
                spotify_uri=spotify_uri,
                title=title,
                artists=artists,
            )
        )
    return tracks


def _extract_track_uri(row: Node) -> str | None:
    labelled = row.attributes.get("aria-labelledby") or ""
    match = re.search(r"track-(spotify:track:[A-Za-z0-9]+)-", labelled)
    if match is not None:
        return match.group(1)

    link = row.css_first('a[href^="/track/"]')
    if link is None:
        return None
    href = link.attributes.get("href") or ""
    match = re.search(r"/track/([A-Za-z0-9]+)", href)
    if match is None:
        return None
    return f"spotify:track:{match.group(1)}"


def _extract_artists(row: Node) -> list[str]:
    artists: list[str] = []
    for link in row.css('[data-testid="internal-artist-link"] a'):
        artist = link.text(strip=True)
        if artist and artist not in artists:
            artists.append(artist)
    return artists
