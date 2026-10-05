"""Sources Bandcamp filtradas por editora.

A página ``https://<label>.bandcamp.com/music`` é server-rendered e inclui a
lista de lançamentos no atributo ``data-client-items``, mas sem datas e nem
sempre do mais recente para o mais antigo. Por isso cada candidato é lido na
sua própria página (``data-tralbum``):

- pré-vendas ficam de fora até saírem;
- a data de lançamento alimenta o filtro normal de novidade;
- edições com menos de ``MIN_ALBUM_TRACKS`` faixas são singles: seguem para a
  triagem de faixas (pela faixa principal), não para a fila de álbuns.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from html import unescape
from urllib.parse import urljoin

import httpx
import structlog

from peel.models import Track
from peel.sources.base import Source

log = structlog.get_logger()

# Uma Bandcamp ``/album/`` com 1–3 faixas é um single/EP curto, não um álbum.
MIN_ALBUM_TRACKS = 4


@dataclass(frozen=True, slots=True)
class ReleaseDetails:
    track_count: int
    lead_track: str | None
    released_at: datetime | None
    preorder: bool


_BROWSER_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


class BandcampLabel(Source):
    """Bandcamp por editora: álbuns para a fila, singles para a triagem.

    O cap ``max_items`` aplica-se aos lançamentos válidos (merch e itens
    malformados não contam). Cada um custa um pedido extra à sua página.
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
        items = self._client_items(response.text)
        self.last_raw_entries = len(items) if items is not None else 0
        releases: list[Track] = []
        for candidate in self._parse_music_html(response.text):
            release = self._with_details(candidate)
            if release is not None:
                releases.append(release)
        return releases

    def _with_details(self, candidate: Track) -> Track | None:
        """Classifica um lançamento pela sua página; falhas não inventam dados."""
        try:
            details = self._release_details(str(candidate.source_url))
        except (httpx.HTTPError, ValueError) as exc:
            log.warning(
                "bandcamp.release_details_failed",
                source_id=self.id,
                url=candidate.source_url,
                error=str(exc),
            )
            return None
        if details.preorder:
            log.info("bandcamp.preorder_skipped", source_id=self.id, title=candidate.title)
            return None
        if candidate.kind == "track" or details.track_count < MIN_ALBUM_TRACKS:
            return candidate.model_copy(
                update={
                    "kind": "track",
                    "title": details.lead_track or candidate.title,
                    "published_at": details.released_at,
                }
            )
        return candidate.model_copy(update={"kind": "album", "published_at": details.released_at})

    def _release_details(self, url: str) -> ReleaseDetails:
        response = httpx.get(url, headers=self.request_headers, follow_redirects=True, timeout=20)
        response.raise_for_status()
        return parse_release_details(response.text)

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
        if release_type not in {"album", "track"}:
            log.warning(
                "bandcamp.unsupported_release_type",
                source_id=self.id,
                release_type=release_type,
            )
            return None

        artist = str(item.get("artist", "")).strip()
        title = str(item.get("title", "")).strip()
        page_url = str(item.get("page_url", "")).strip()
        if not artist or not title or not page_url or f"/{release_type}/" not in page_url:
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
            kind="album" if release_type == "album" else "track",
        )

    def _resolve_page_url(self, page_url: str) -> str:
        if page_url.startswith("http://") or page_url.startswith("https://"):
            return page_url
        return urljoin(f"https://{self.subdomain}.bandcamp.com", page_url)


def parse_release_details(html: str) -> ReleaseDetails:
    """Lê ``data-tralbum`` de uma página de álbum/faixa Bandcamp."""
    match = re.search(r'data-tralbum="([^"]+)"', html)
    if match is None:
        raise ValueError("Bandcamp release page without data-tralbum")
    data = json.loads(unescape(match.group(1)))
    if not isinstance(data, dict):
        raise ValueError("Bandcamp data-tralbum is not an object")
    tracks = [item for item in data.get("trackinfo") or [] if isinstance(item, dict)]
    current = data.get("current") if isinstance(data.get("current"), dict) else {}
    lead = next((str(t.get("title", "")).strip() for t in tracks if t.get("title")), None)
    return ReleaseDetails(
        track_count=len(tracks),
        lead_track=lead or None,
        released_at=_parse_bandcamp_date(
            data.get("album_release_date") or current.get("release_date")
        ),
        preorder=bool(data.get("album_is_preorder") or data.get("is_preorder")),
    )


def _parse_bandcamp_date(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.strptime(value.strip(), "%d %b %Y %H:%M:%S GMT").replace(tzinfo=UTC)
    except ValueError:
        return None
