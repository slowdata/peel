"""Sources Bandcamp filtradas por editora.

Read visible catalogue cards before the serialized continuation: neither is
necessarily ordered by release date. Inspect a bounded number of release pages
and apply the output cap only after date/format checks (``data-tralbum``):

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
from urllib.parse import urljoin, urlparse

import httpx
import structlog
from selectolax.parser import HTMLParser

from peel.albums import is_archival_album_title, is_compilation_release
from peel.discovery_trace import capture
from peel.matcher import artist_identity_key, normalize
from peel.models import Track
from peel.releases import MAX_RELEASE_AGE_DAYS, MIN_ALBUM_TRACKS, release_date_reason
from peel.sources.base import Source

log = structlog.get_logger()


@dataclass(frozen=True, slots=True)
class ReleaseDetails:
    track_count: int
    lead_track: str | None
    released_at: datetime | None
    preorder: bool
    archival_reason: str | None = None


_BROWSER_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


class BandcampLabel(Source):
    """Bandcamp por editora: álbuns para a fila, singles para a triagem.

    ``max_items`` limits admitted releases; ``max_candidates`` bounds inspection.
    Skipped archives, dates, preorders and failures do not fill the output cap.
    """

    kind = "album"
    request_headers = {"User-Agent": _BROWSER_UA}

    def __init__(
        self,
        source_id: str,
        name: str,
        subdomain: str,
        max_items: int = 5,
        *,
        max_candidates: int = 20,
    ) -> None:
        if not 1 <= max_items <= max_candidates:
            raise ValueError("Require 1 <= max_items <= max_candidates")
        self.id = source_id
        self.name = name
        self.subdomain = subdomain
        self.max_items = max_items
        self.max_candidates = max_candidates
        self.url = f"https://{subdomain}.bandcamp.com/music"

    def fetch(self, *, now: datetime | None = None) -> list[Track]:
        reference = now or datetime.now(UTC)
        if reference.tzinfo is None:
            reference = reference.replace(tzinfo=UTC)
        response = httpx.get(
            self.url,
            headers=self.request_headers,
            follow_redirects=True,
            timeout=20,
        )
        response.raise_for_status()
        items = self._catalogue_items(response.text)
        self.last_raw_entries = len(items)
        releases: list[Track] = []
        inspected = 0
        candidates = self._parse_items(items, self.max_candidates)
        capture(
            "bandcamp.inspection_pool",
            source_id=self.id,
            items=candidates,
            catalogue_size=len(items),
            candidate_budget=self.max_candidates,
            output_limit=self.max_items,
        )
        for candidate in candidates:
            inspected += 1
            release = self._with_details(candidate, now=reference)
            if release is not None:
                releases.append(release)
                if len(releases) >= self.max_items:
                    break
        capture(
            "bandcamp.inspection_completed",
            source_id=self.id,
            inspected=inspected,
            admitted=len(releases),
            not_evaluated=candidates[inspected:],
        )
        log.info(
            "bandcamp.fetched",
            source_id=self.id,
            raw=len(items),
            inspected=inspected,
            admitted=len(releases),
            budget=self.max_candidates,
        )
        return releases

    def _with_details(self, candidate: Track, *, now: datetime) -> Track | None:
        """Classifica um lançamento pela sua página; falhas não inventam dados."""
        try:
            details = self._release_details(str(candidate.source_url))
        except (httpx.HTTPError, ValueError) as exc:
            capture(
                "bandcamp.lookup_failed",
                source_id=self.id,
                artist=candidate.artist,
                title=candidate.title,
                error_type=type(exc).__name__,
            )
            log.warning(
                "bandcamp.release_details_failed",
                source_id=self.id,
                url=candidate.source_url,
                error=str(exc),
            )
            return None
        capture(
            "bandcamp.release_metadata",
            source_id=self.id,
            artist=candidate.artist,
            title=candidate.title,
            source_url=candidate.source_url,
            metadata=details,
        )
        if details.archival_reason:
            capture(
                "bandcamp.decision",
                source_id=self.id,
                artist=candidate.artist,
                title=candidate.title,
                reason=details.archival_reason,
            )
            log.info(
                "bandcamp.archive_skipped",
                source_id=self.id,
                title=candidate.title,
                reason=details.archival_reason,
            )
            return None
        if details.preorder:
            capture(
                "bandcamp.decision",
                source_id=self.id,
                artist=candidate.artist,
                title=candidate.title,
                reason="preorder",
            )
            log.info("bandcamp.preorder_skipped", source_id=self.id, title=candidate.title)
            return None
        if details.track_count < 1 or details.released_at is None:
            capture(
                "bandcamp.decision",
                source_id=self.id,
                artist=candidate.artist,
                title=candidate.title,
                reason="incomplete_metadata",
            )
            log.warning("bandcamp.incomplete_release", source_id=self.id, title=candidate.title)
            return None
        # Match the outer editorial-age filter too: an old release must not
        # consume one of the five slots only to be discarded by the caller.
        from peel.config import settings

        reason = release_date_reason(
            details.released_at.date().isoformat(),
            now.date(),
            max_age_days=min(MAX_RELEASE_AGE_DAYS, settings.peel_max_source_item_age_days),
        )
        if reason or details.released_at > now:
            capture(
                "bandcamp.decision",
                source_id=self.id,
                artist=candidate.artist,
                title=candidate.title,
                reason=reason or "future_release_timestamp",
            )
            log.info(
                "bandcamp.date_skipped",
                source_id=self.id,
                title=candidate.title,
                reason=reason or "release timestamp is in the future",
            )
            return None
        if candidate.kind == "track" or details.track_count < MIN_ALBUM_TRACKS:
            if not details.lead_track:
                capture(
                    "bandcamp.decision",
                    source_id=self.id,
                    artist=candidate.artist,
                    title=candidate.title,
                    reason="no_lead_track",
                )
                return None
            capture(
                "bandcamp.decision",
                source_id=self.id,
                artist=candidate.artist,
                title=candidate.title,
                lead_track=details.lead_track,
                reason="routed_to_track",
            )
            return candidate.model_copy(
                update={
                    "kind": "track",
                    "title": details.lead_track or candidate.title,
                    "published_at": details.released_at,
                }
            )
        if is_archival_album_title(candidate.title):
            capture(
                "bandcamp.decision",
                source_id=self.id,
                artist=candidate.artist,
                title=candidate.title,
                reason="archival_title",
            )
            log.info("bandcamp.archive_skipped", source_id=self.id, title=candidate.title)
            return None
        if is_compilation_release(candidate.artist, candidate.title):
            capture(
                "bandcamp.decision",
                source_id=self.id,
                artist=candidate.artist,
                title=candidate.title,
                reason="compilation",
            )
            return None
        capture(
            "bandcamp.decision",
            source_id=self.id,
            artist=candidate.artist,
            title=candidate.title,
            reason="routed_to_album",
        )
        return candidate.model_copy(update={"kind": "album", "published_at": details.released_at})

    def _release_details(self, url: str) -> ReleaseDetails:
        return fetch_release_details(url)

    def _catalogue_items(self, html: str) -> list[dict[str, object]]:
        visible: list[dict[str, object]] = []
        for card in HTMLParser(html).css("#music-grid > li"):
            link = card.css_first("a[href]")
            title = card.css_first(".title")
            artist_node = card.css_first(".artist-override, .artist")
            if link is None or title is None or artist_node is None:
                continue
            artist = artist_node.text(separator=" ", strip=True)
            artist_node.decompose()
            page_url = link.attributes.get("href", "")
            try:
                path = urlparse(page_url).path
            except ValueError:
                continue
            release_type = "album" if path.startswith("/album/") else "track"
            visible.append(
                {
                    "artist": artist,
                    "title": title.text(separator=" ", strip=True),
                    "type": release_type,
                    "page_url": page_url,
                }
            )
        try:
            continuation = self._client_items(html)
        except ValueError as exc:
            if not visible:
                raise
            log.warning("bandcamp.continuation_failed", source_id=self.id, error=str(exc))
            continuation = None
        return visible + (continuation or [])

    def _parse_music_html(self, html: str) -> list[Track]:
        return self._parse_items(self._catalogue_items(html), self.max_items)

    def _parse_items(self, items: list[dict[str, object]], limit: int) -> list[Track]:
        tracks: list[Track] = []
        urls: set[tuple[str, str]] = set()
        identities: set[tuple[str, str, str | None]] = set()
        for raw_item in items:
            track = self._parse_item(raw_item)
            if track is None:
                continue
            parsed = urlparse(str(track.source_url))
            url_key = (parsed.hostname or "", parsed.path.rstrip("/"))
            identity = (artist_identity_key(track.artist), normalize(track.title), track.kind)
            if url_key in urls or identity in identities:
                continue
            urls.add(url_key)
            identities.add(identity)
            tracks.append(track)
            if len(tracks) >= limit:
                break
        return tracks

    def _client_items(self, html: str) -> list[dict[str, object]] | None:
        grid = HTMLParser(html).css_first("#music-grid")
        raw_json = grid.attributes.get("data-client-items") if grid is not None else None
        if raw_json is None:
            return None
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

        artist, title, page_url = (item.get(key) for key in ("artist", "title", "page_url"))
        if not all(isinstance(value, str) for value in (artist, title, page_url)):
            log.warning("bandcamp.item_malformed", source_id=self.id)
            return None
        artist, title, page_url = artist.strip(), title.strip(), page_url.strip()
        resolved_url = self._resolve_page_url(page_url)
        try:
            parsed = urlparse(resolved_url)
        except ValueError:
            return None
        if (
            not artist
            or not title
            or not page_url
            or parsed.scheme not in {"http", "https"}
            or not (parsed.hostname or "").endswith(".bandcamp.com")
            or parsed.username is not None
            or parsed.password is not None
            or not parsed.path.startswith(f"/{release_type}/")
        ):
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
            source_url=resolved_url,
            raw_title=f"{artist} :: {title}",
            kind="album" if release_type == "album" else "track",
        )

    def _resolve_page_url(self, page_url: str) -> str:
        if page_url.startswith("http://") or page_url.startswith("https://"):
            return page_url
        return urljoin(f"https://{self.subdomain}.bandcamp.com", page_url)


def fetch_release_details(url: str) -> ReleaseDetails:
    """Read a single release page without modifying any external service."""
    response = httpx.get(
        url, headers=BandcampLabel.request_headers, follow_redirects=True, timeout=20
    )
    response.raise_for_status()
    return parse_release_details(response.text)


def parse_release_details(html: str) -> ReleaseDetails:
    """Lê ``data-tralbum`` de uma página de álbum/faixa Bandcamp."""
    node = HTMLParser(html).css_first("[data-tralbum]")
    if node is None:
        raise ValueError("Bandcamp release page without data-tralbum")
    data = json.loads(node.attributes["data-tralbum"])
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
        archival_reason=_archival_description_reason(current.get("about")),
    )


def _archival_description_reason(value: object) -> str | None:
    """Use explicit vault provenance, not incidental dates in an artist biography."""
    if not isinstance(value, str):
        return None
    opening = re.split(r"\n\s*\n", value.replace("\r\n", "\n"), maxsplit=1)[0]
    if re.search(
        r"\bcollection\s+of\s+(?:previously\s+)?unreleased\s+(?:material|recordings)\b",
        opening,
        re.IGNORECASE,
    ) and re.search(
        r"\bfrom\s+(?:(?:the|their|his|her|our)\s+)?(?:personal\s+)?(?:vaults?|archives?)\b",
        opening,
        re.IGNORECASE,
    ):
        return "gravações de arquivo segundo a descrição Bandcamp"
    return None


def _parse_bandcamp_date(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.strptime(value.strip(), "%d %b %Y %H:%M:%S GMT").replace(tzinfo=UTC)
    except ValueError:
        return None
