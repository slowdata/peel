"""Sources Bandcamp filtradas por editora.

A página ``https://<label>.bandcamp.com/music`` é server-rendered e inclui a
lista de lançamentos no atributo ``data-client-items``. A label é o filtro de
género/curadoria; por isso esta source devolve álbuns/contexto, não tracks para
playlist.
"""

from __future__ import annotations

import json
import re
from html import unescape
from urllib.parse import urljoin

import httpx
import structlog

from peel.models import Track
from peel.sources.base import Source

log = structlog.get_logger()

_BROWSER_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


class BandcampLabel(Source):
    """Bandcamp por editora.

    Estratégia: a página ``/music`` traz releases newest-first. Só ``album``
    (e URLs ``/album/``) é elegível: singles ``/track/`` não são álbuns. O cap
    é aplicado *depois* do filtro para não devolver menos álbuns por a página
    começar com singles.
    """

    kind = "album"
    request_headers = {"User-Agent": _BROWSER_UA}

    def __init__(self, source_id: str, name: str, subdomain: str, max_items: int = 5) -> None:
        self.id = source_id
        self.name = name
        self.subdomain = subdomain
        self.max_items = max_items
        self.url = f"https://{subdomain}.bandcamp.com/music"

    def fetch(self) -> list[Track]:
        response = httpx.get(
            self.url,
            headers=self.request_headers,
            follow_redirects=True,
            timeout=20,
        )
        response.raise_for_status()
        return self._parse_music_html(response.text)

    def _parse_music_html(self, html: str) -> list[Track]:
        items = self._client_items(html)
        if items is None:
            log.warning("bandcamp.client_items_not_found", source_id=self.id, url=self.url)
            return []

        tracks: list[Track] = []
        for raw_item in items:
            track = self._parse_item(raw_item)
            if track is None:
                continue
            tracks.append(track)
            if len(tracks) >= self.max_items:
                break
        return tracks

    def _client_items(self, html: str) -> list[dict[str, object]] | None:
        match = re.search(r'data-client-items="([^"]+)"', html)
        if match is None:
            return None

        raw_json = unescape(match.group(1))
        parsed = json.loads(raw_json)
        if not isinstance(parsed, list):
            raise ValueError("Bandcamp data-client-items is not a list")
        return [item for item in parsed if isinstance(item, dict)]

    def _parse_item(self, item: dict[str, object]) -> Track | None:
        release_type = str(item.get("type", "")).strip().lower()
        if release_type != "album":
            log.warning(
                "bandcamp.unsupported_release_type",
                source_id=self.id,
                release_type=release_type,
            )
            return None

        artist = str(item.get("artist", "")).strip()
        title = str(item.get("title", "")).strip()
        page_url = str(item.get("page_url", "")).strip()
        if not artist or not title or not page_url or "/album/" not in page_url:
            log.warning(
                "bandcamp.item_malformed",
                source_id=self.id,
                artist=artist,
                title=title,
                page_url=page_url,
            )
            return None

        return Track(
            source_id=self.id,
            artist=artist,
            title=title,
            source_url=self._resolve_page_url(page_url),
            raw_title=f"{artist} :: {title}",
        )

    def _resolve_page_url(self, page_url: str) -> str:
        if page_url.startswith("http://") or page_url.startswith("https://"):
            return page_url
        return urljoin(f"https://{self.subdomain}.bandcamp.com", page_url)
