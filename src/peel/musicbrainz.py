"""Cliente mínimo MusicBrainz para cache local de géneros/tags.

Usado apenas por comando CLI explícito; a weekly não faz rede. A API do
MusicBrainz pede User-Agent identificável e uso moderado (~1 req/s), por isso o
throttle fica no caller.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

from peel.matcher import normalize

MUSICBRAINZ_ARTIST_SEARCH_URL = "https://musicbrainz.org/ws/2/artist/"
MUSICBRAINZ_USER_AGENT = "Peel/0.1 (https://github.com/slowdata/peel)"
NOISY_TAGS = {
    "seen live",
    "favorites",
    "favourites",
    "favorite",
    "favourite",
    "spotify",
    "lastfm",
    "last.fm",
    "albums i own",
    "all",
    "female",
    "male",
    "uk",
    "european",
    "swedish",
    "indian",
    "2010s",
    "2020s",
}


@dataclass(frozen=True, slots=True)
class MusicBrainzArtistGenres:
    """Resultado normalizado do lookup MusicBrainz."""

    name: str
    mbid: str
    genres: tuple[str, ...]


def fetch_musicbrainz_artist_genres(
    artist: str,
    *,
    limit: int = 5,
    min_tag_count: int = 1,
    timeout: float = 10.0,
) -> MusicBrainzArtistGenres | None:
    """Procura um artista no MusicBrainz e devolve géneros/tags utilizáveis.

    Conservador: só aceita match exacto normalizado. Prefere `genres` oficiais
    quando existem; caso contrário usa `tags` com count mínimo.
    """
    response = httpx.get(
        MUSICBRAINZ_ARTIST_SEARCH_URL,
        params={"query": f'artist:"{artist}"', "fmt": "json", "limit": str(limit)},
        headers={"User-Agent": MUSICBRAINZ_USER_AGENT, "Accept": "application/json"},
        timeout=timeout,
    )
    response.raise_for_status()
    return parse_musicbrainz_artist_genres(
        artist,
        response.json(),
        min_tag_count=min_tag_count,
    )


def parse_musicbrainz_artist_genres(
    artist: str,
    payload: dict[str, Any],
    *,
    min_tag_count: int = 1,
) -> MusicBrainzArtistGenres | None:
    """Extrai resultado MusicBrainz de payload JSON, sem rede."""
    target = normalize(artist)
    artists = payload.get("artists") or []
    if not isinstance(artists, list):
        return None

    for item in artists:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "")
        if normalize(name) != target:
            continue
        mbid = str(item.get("id") or "")
        genres = _extract_musicbrainz_genres(item, min_tag_count=min_tag_count)
        if not mbid or not genres:
            return None
        return MusicBrainzArtistGenres(name=name, mbid=mbid, genres=tuple(genres))
    return None


def _extract_musicbrainz_genres(item: dict[str, Any], *, min_tag_count: int) -> list[str]:
    official = _tag_names(item.get("genres"), min_count=0)
    if official:
        return official
    return _tag_names(item.get("tags"), min_count=min_tag_count)


def _tag_names(raw_tags: Any, *, min_count: int) -> list[str]:
    if not isinstance(raw_tags, list):
        return []

    names: list[str] = []
    seen: set[str] = set()
    for tag in raw_tags:
        if not isinstance(tag, dict):
            continue
        name = str(tag.get("name") or "").strip()
        key = normalize(name)
        if not key or key in seen or key in NOISY_TAGS:
            continue
        try:
            count = int(tag.get("count") or 0)
        except (TypeError, ValueError):
            count = 0
        if count < min_count:
            continue
        seen.add(key)
        names.append(name)
    return names
