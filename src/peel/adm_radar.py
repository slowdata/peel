"""Read-only ADM discovery inventory; never a weekly source or a taste ranker."""

from __future__ import annotations

import json
import math
import re
import sqlite3
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from itertools import zip_longest
from pathlib import Path
from typing import Literal
from urllib.parse import urljoin, urlparse, urlunparse

import httpx
from pydantic import BaseModel, ConfigDict
from selectolax.parser import HTMLParser, Node

from peel.albums import is_archival_album_title, is_compilation_release
from peel.matcher import artist_identity_key, normalize
from peel.releases import MAX_RELEASE_AGE_DAYS, release_date_bounds, release_date_reason

BASE_URL = "http://www.anydecentmusic.com"
LISTINGS = (f"{BASE_URL}/chart/JustOut.aspx", f"{BASE_URL}/chart/Unlisted.aspx")
_ROOT = Path(__file__).resolve().parents[2]
_PROTECTED = (_ROOT / "data", _ROOT / ".git", _ROOT.parent / "peel-sept")
_REVIEW_PATH = re.compile(r"/review/(\d+)/[^/]+\.aspx\Z", re.IGNORECASE)
Fetch = Callable[[str], str]
Status = Literal["recent", "old", "future", "archival", "unconfirmed", "error"]


class RadarError(RuntimeError):
    """ADM could not supply a usable current inventory."""


class Reference(BaseModel):
    model_config = ConfigDict(frozen=True)
    artist: str
    album: str
    url: str
    discovered_via: tuple[str, ...]


class Review(BaseModel):
    model_config = ConfigDict(frozen=True)
    publication: str
    rating_10: float | None = None
    url: str | None = None
    # ADM's data_age is an internal ID, not a publication timestamp.
    published_at: None = None


class RadarAlbum(BaseModel):
    model_config = ConfigDict(frozen=True)
    artist: str
    album: str
    adm_url: str
    adm_urls: tuple[str, ...]
    discovered_via: tuple[str, ...]
    release_uk: str | None = None
    release_us: str | None = None
    release_date: str | None = None
    adm_rating_10: float | None = None
    reviews: tuple[Review, ...] | None = None
    listen_url: str | None = None
    status: Status = "unconfirmed"
    reason: str | None = None
    known_to_peel: bool | None = None
    feedback_label: str | None = None
    local_sources: tuple[str, ...] = ()


class RadarResult(BaseModel):
    model_config = ConfigDict(frozen=True)
    schema_version: Literal[1] = 1
    observed_at: str
    window_days: int = MAX_RELEASE_AGE_DAYS
    listing_urls: tuple[str, ...] = LISTINGS
    discovered_count: int
    detail_limit: int
    details_requested: int
    truncated: bool
    errors: tuple[str, ...]
    albums: tuple[RadarAlbum, ...]
    local_requested: bool = False
    local_available: bool = False
    local_observed_at: str | None = None
    local_error: str | None = None
    coverage: str = "Current Just In/Unlisted pages only; not an exhaustive release catalogue."
    evidence: str = (
        "Dates and scores as indexed by ADM at observed_at. Original articles, "
        "playability, format and first-recording release are not verified. "
        "ADM is not an extra independent publication or a personal taste score."
    )

    @property
    def partial(self) -> bool:
        return bool(self.errors or self.truncated or self.local_error)


def _text(node: Node | None) -> str:
    return node.text(separator=" ", strip=True) if node is not None else ""


def _identity(artist: str, album: str) -> tuple[str, str]:
    return artist_identity_key(artist), normalize(album.translate(str.maketrans("‘’ʼ", "'''")))


def adm_url(value: str, *, base: str = BASE_URL) -> str:
    """Only known ADM listing/detail routes may be fetched, including redirects."""
    try:
        parsed = urlparse(urljoin(base, value))
        if (
            parsed.scheme not in {"http", "https"}
            or parsed.hostname not in {"anydecentmusic.com", "www.anydecentmusic.com"}
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port not in {None, 80, 443}
            or (
                parsed.path not in {"/chart/JustOut.aspx", "/chart/Unlisted.aspx"}
                and not _REVIEW_PATH.fullmatch(parsed.path)
            )
        ):
            raise ValueError("unsupported ADM route")
    except (ValueError, TypeError) as exc:
        raise RadarError("Unsafe or unsupported ADM URL") from exc
    return urlunparse((parsed.scheme, "www.anydecentmusic.com", parsed.path, "", "", ""))


def _public_link(value: str | None) -> str | None:
    if not value:
        return None
    try:
        parsed = urlparse(value)
        if (
            parsed.scheme in {"http", "https"}
            and parsed.hostname
            and not parsed.username
            and not parsed.password
        ):
            return value
    except ValueError:
        pass
    return None


def parse_listing(html: str, url: str) -> list[Reference]:
    chart = HTMLParser(html).css_first(".article ol.main_chart")
    if chart is None:
        raise RadarError("ADM listing markup is missing")
    result: list[Reference] = []
    for card in chart.css("li"):
        artist = _text(card.css_first(".album_info h4"))
        album = _text(card.css_first(".album_info h5"))
        link = card.css_first(".album_info h5 a[href]")
        if not artist or not album or link is None:
            continue
        target = adm_url(link.attributes.get("href", ""), base=url)
        if not _REVIEW_PATH.fullmatch(urlparse(target).path):
            continue
        result.append(Reference(artist=artist, album=album, url=target, discovered_via=(url,)))
    if chart.css("li") and not result:
        raise RadarError("ADM listing contains no parseable album references")
    return result


def _rating(value: str) -> float | None:
    try:
        number = float(value)
        return number if math.isfinite(number) and 0 <= number <= 10 else None
    except ValueError:
        return None


def _release_date(value: str) -> str | None:
    if not re.fullmatch(r"\d{2}/\d{2}/\d{4}", value):
        return None
    try:
        return datetime.strptime(value, "%d/%m/%Y").date().isoformat()
    except ValueError:
        return None


def _status(
    artist: str, album: str, released: str | None, now: datetime
) -> tuple[Status, str | None]:
    if is_archival_album_title(album) or is_compilation_release(artist, album):
        return "archival", "edição de arquivo/compilação identificada no título"
    reason = release_date_reason(released, now.date())
    bounds = release_date_bounds(released)
    if bounds is None:
        return "unconfirmed", reason
    if bounds[0] > now.date():
        return "future", reason
    if reason:
        return "old", reason
    return "recent", None


def parse_album(html: str, reference: Reference, now: datetime) -> RadarAlbum:
    article = HTMLParser(html).css_first(".article.reviewpage")
    if article is None:
        raise RadarError("ADM album markup is missing")
    artist = _text(article.css_first(".review_head h2"))
    album = _text(article.css_first(".review_head h3"))
    if (
        not artist
        or not album
        or _identity(artist, album) != _identity(reference.artist, reference.album)
    ):
        raise RadarError("ADM album identity does not match its listing")
    fields: dict[str, str] = {}
    for term in article.css(".review_album_info dt"):
        value = term.next
        while value is not None and value.tag != "dd":
            value = value.next
        fields[_text(term).casefold()] = _text(value)
    uk = _release_date(fields.get("uk release date", ""))
    us = _release_date(fields.get("us release date", ""))
    released = min(x for x in (uk, us) if x) if uk or us else None
    reviews: list[Review] | None = None
    container = article.css_first("#album_reviews_list")
    if container is not None:
        reviews = []
        seen: set[tuple[str, str | None]] = set()
        for row in container.css("li.review_item"):
            publication = _text(row.css_first("h4 span"))
            if not publication:
                continue
            link = row.css_first("a[href]")
            original = _public_link(link.attributes.get("href") if link is not None else None)
            key = (normalize(publication), original)
            if key in seen:
                continue
            seen.add(key)
            reviews.append(
                Review(
                    publication=publication,
                    rating_10=_rating(_text(row.css_first(".data_rating"))),
                    url=original,
                )
            )
    listen = None
    for link in article.css(".listen_at a[href]"):
        candidate = _public_link(link.attributes.get("href"))
        parsed = urlparse(candidate or "")
        if (parsed.hostname == "open.spotify.com" and parsed.path.startswith("/album/")) or (
            (parsed.hostname or "").endswith(".bandcamp.com") and parsed.path.startswith("/album/")
        ):
            listen = urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", "", ""))
            break
    status, reason = _status(artist, album, released, now)
    return RadarAlbum(
        artist=artist,
        album=album,
        adm_url=reference.url,
        adm_urls=(reference.url,),
        discovered_via=reference.discovered_via,
        release_uk=uk,
        release_us=us,
        release_date=released,
        adm_rating_10=_rating(_text(article.css_first(".review_album_info .score"))),
        reviews=tuple(reviews) if reviews is not None else None,
        listen_url=listen,
        status=status,
        reason=reason,
    )


def _get_page(client: httpx.Client, url: str) -> str:
    for _ in range(3):
        url = adm_url(url)
        response = client.get(url, follow_redirects=False)
        if response.is_redirect:
            location = response.headers.get("location")
            if not location:
                raise RadarError("ADM returned a redirect without a destination")
            url = adm_url(location, base=url)  # validate before another HTTP request
            continue
        response.raise_for_status()
        return response.text
    raise RadarError("ADM exceeded the redirect budget")


def collect_radar(
    *, limit: int = 30, now: datetime | None = None, fetch: Fetch | None = None
) -> RadarResult:
    """Bounded public GETs; no original review, listening-link or personal-data requests."""
    if not 1 <= limit <= 50:
        raise ValueError("limit must be between 1 and 50")
    reference = now or datetime.now(UTC)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=UTC)
    if fetch is None:
        with httpx.Client(
            timeout=10, headers={"User-Agent": "Peel/0.1 (read-only album radar)"}
        ) as client:
            return collect_radar(
                limit=limit, now=reference, fetch=lambda url: _get_page(client, url)
            )
    errors: list[str] = []
    lists: list[list[Reference]] = []
    for url in LISTINGS:
        try:
            lists.append(parse_listing(fetch(url), url))
        except (httpx.HTTPError, RadarError, ValueError) as exc:
            lists.append([])
            errors.append(f"{url}: {exc}")
    if len(errors) == len(LISTINGS):
        raise RadarError("Não foi possível ler as listagens ADM: " + "; ".join(errors))
    # Interleave discovery pages so a bounded request budget cannot hide Unlisted.
    refs: dict[str, Reference] = {}
    for pair in zip_longest(*lists):
        for item in pair:
            if item is None:
                continue
            album_id = _REVIEW_PATH.fullmatch(urlparse(item.url).path)[1]
            previous = refs.get(album_id)
            refs[album_id] = (
                previous.model_copy(
                    update={
                        "discovered_via": tuple(
                            dict.fromkeys(previous.discovered_via + item.discovered_via)
                        )
                    }
                )
                if previous
                else item
            )
    observations: list[RadarAlbum] = []
    attempted = 0
    consecutive_errors = 0
    for item in list(refs.values())[:limit]:
        attempted += 1
        try:
            observations.append(parse_album(fetch(item.url), item, reference))
            consecutive_errors = 0
        except (httpx.HTTPError, RadarError, ValueError) as exc:
            consecutive_errors += 1
            message = f"{item.url}: {exc}"
            errors.append(message)
            observations.append(
                RadarAlbum(
                    artist=item.artist,
                    album=item.album,
                    adm_url=item.url,
                    adm_urls=(item.url,),
                    discovered_via=item.discovered_via,
                    status="error",
                    reason=str(exc),
                )
            )
            throttled = isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 429
            if throttled or consecutive_errors >= 3:
                errors.append(
                    "Consulta interrompida: limitação de pedidos ou três falhas consecutivas"
                )
                break
    # Artist aliases share one observation; explicit archive editions remain separate.
    albums: dict[tuple, RadarAlbum] = {}
    for item in observations:
        key = (
            *_identity(item.artist, item.album),
            is_archival_album_title(item.album),
            is_compilation_release(item.artist, item.album),
        )
        previous = albums.get(key)
        if previous is not None:
            # Conservatively retain the earliest known release, never rejuvenate an alias.
            primary = min((previous, item), key=lambda x: x.release_date or "9999")
            albums[key] = primary.model_copy(
                update={
                    "adm_urls": tuple(dict.fromkeys(previous.adm_urls + item.adm_urls)),
                    "discovered_via": tuple(
                        dict.fromkeys(previous.discovered_via + item.discovered_via)
                    ),
                }
            )
        else:
            albums[key] = item
    return RadarResult(
        observed_at=reference.isoformat(),
        discovered_count=len(refs),
        detail_limit=limit,
        details_requested=attempted,
        truncated=len(refs) > attempted,
        errors=tuple(errors),
        albums=tuple(albums.values()),
    )


def annotate_local(result: RadarResult, path: Path, *, now: datetime | None = None) -> RadarResult:
    """Read current retained opinions locally; never initialize/migrate/sync SQLite."""
    known: set[tuple[str, str]] = set()
    sources: dict[tuple[str, str], set[str]] = {}
    feedback: dict[tuple[str, str], tuple[datetime, bool, str]] = {}
    try:
        connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
        try:
            connection.execute("PRAGMA query_only=ON")
            connection.execute("BEGIN")
            for artist, album, source in connection.execute(
                "SELECT artist, album, source_id FROM albums"
            ):
                key = _identity(artist, album)
                known.add(key)
                sources.setdefault(key, set()).add(source)
            for artist, album, source in connection.execute(
                "SELECT artist_key, album_key, source_id FROM album_mentions"
            ):
                key = _identity(artist, album)
                known.add(key)
                sources.setdefault(key, set()).add(source)
            for artist, album, label, rated_at in connection.execute(
                "SELECT artist_key, album_key, label, rated_at FROM album_feedback"
            ):
                key = _identity(artist, album)
                at = datetime.fromisoformat(rated_at.replace("Z", "+00:00"))
                at = at.replace(tzinfo=UTC) if at.tzinfo is None else at
                row = (at, label == "ban", label)
                if key not in feedback or row > feedback[key]:
                    feedback[key] = row
                known.add(key)
        finally:
            connection.close()
    except (OSError, sqlite3.Error, ValueError) as exc:
        unknown = tuple(
            item.model_copy(
                update={
                    "known_to_peel": None,
                    "feedback_label": None,
                    "local_sources": (),
                }
            )
            for item in result.albums
        )
        return result.model_copy(
            update={
                "albums": unknown,
                "local_requested": True,
                "local_available": False,
                "local_observed_at": None,
                "local_error": str(exc),
            }
        )
    items = tuple(
        item.model_copy(
            update={
                "known_to_peel": _identity(item.artist, item.album) in known,
                "feedback_label": (
                    feedback.get(_identity(item.artist, item.album)) or (None, None, None)
                )[2],
                "local_sources": tuple(sorted(sources.get(_identity(item.artist, item.album), ()))),
            }
        )
        for item in result.albums
    )
    return result.model_copy(
        update={
            "albums": items,
            "local_requested": True,
            "local_available": True,
            "local_observed_at": (now or datetime.now(UTC)).isoformat(),
            "local_error": None,
        }
    )


def snapshot_destination(
    path: Path, *, protected_roots: Sequence[Path] = (), protected_files: Sequence[Path] = ()
) -> Path:
    """Validate before network access; canonical state and existing files are off limits."""
    resolved = path.resolve()
    if resolved.suffix.lower() != ".json":
        raise ValueError("A snapshot deve ter extensão .json")
    if any(
        resolved.is_relative_to(root.resolve()) for root in (*_PROTECTED, *protected_roots)
    ) or any(resolved == item.resolve() for item in protected_files):
        raise ValueError("Não é permitido guardar snapshots em estado canónico")
    if path.exists() or path.is_symlink():
        raise FileExistsError("A snapshot já existe; escolhe outro ficheiro")
    if not resolved.parent.is_dir():
        raise ValueError("A pasta de destino da snapshot não existe")
    return resolved


def save_snapshot(
    result: RadarResult,
    path: Path,
    *,
    protected_roots: Sequence[Path] = (),
    protected_files: Sequence[Path] = (),
) -> None:
    target = snapshot_destination(
        path, protected_roots=protected_roots, protected_files=protected_files
    )
    content = (
        json.dumps(
            {**result.model_dump(mode="json"), "partial": result.partial},
            ensure_ascii=False,
            indent=2,
        )
        + "\n"
    )
    with target.open("x", encoding="utf-8") as handle:
        handle.write(content)
