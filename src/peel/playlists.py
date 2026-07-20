"""Helpers para identificar playlists Spotify de forma estável."""

from __future__ import annotations

from urllib.parse import urlparse


def canonical_playlist_id(value: str) -> str:
    """Converte ID, URI ou URL Spotify numa chave canónica de playlist.

    Queries de partilha (``?si=...``) não fazem parte da identidade. Valores que
    não são URI/URL Spotify são tratados como IDs raw para manter compatibilidade
    com IDs locais/mocks usados pelo cliente.
    """
    raw = value.strip()
    prefix = "spotify:playlist:"
    if raw.startswith(prefix):
        return raw[len(prefix) :].split("?", maxsplit=1)[0]

    parsed = urlparse(raw)
    if parsed.scheme in {"http", "https"} and parsed.netloc.lower() == "open.spotify.com":
        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) >= 2 and parts[0] == "playlist":
            return parts[1]
    return raw
