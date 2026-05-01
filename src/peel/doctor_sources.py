"""Doctor de sources para validar feeds antes de as adicionar ao Peel."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

import feedparser
import httpx

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SOURCE_REGISTRY_PATH = PROJECT_ROOT / "music_sources" / "music_sources_for_agent.json"
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
REQUEST_TIMEOUT = 10.0


@dataclass(slots=True, frozen=True)
class SourceDoctorSpec:
    source_id: str
    name: str
    type: str
    url: str


@dataclass(slots=True)
class SourceDoctorResult:
    source_id: str
    name: str
    type: str
    url: str
    http_status: int
    entries: int
    ok: bool
    note: str

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def load_source_registry(path: Path | None = None) -> list[SourceDoctorSpec]:
    """Carrega a registry de fontes usada pelo doctor."""
    registry_path = path or SOURCE_REGISTRY_PATH
    data = json.loads(registry_path.read_text(encoding="utf-8"))

    specs: list[SourceDoctorSpec] = []
    for section_name, section in data.items():
        if not section_name.startswith("tier_") or not isinstance(section, list):
            continue
        for item in section:
            if not isinstance(item, dict):
                continue
            url = item.get("feed_url") or item.get("url")
            if not url:
                continue
            specs.append(
                SourceDoctorSpec(
                    source_id=str(item.get("id") or item.get("name") or "unknown"),
                    name=str(item.get("name") or item.get("id") or "unknown"),
                    type=str(item.get("type") or "rss"),
                    url=str(url),
                )
            )
    return specs


def inspect_registered_sources(
    specs: list[SourceDoctorSpec] | None = None,
) -> list[SourceDoctorResult]:
    """Valida a registry de fontes com HTTP + parsing apropriado por tipo."""
    source_specs = specs if specs is not None else load_source_registry()
    return [inspect_source(spec) for spec in source_specs]


def inspect_source(spec: SourceDoctorSpec) -> SourceDoctorResult:
    """Valida uma source individual."""
    try:
        response = httpx.get(
            spec.url,
            headers={"User-Agent": USER_AGENT},
            follow_redirects=True,
            timeout=REQUEST_TIMEOUT,
        )
    except httpx.HTTPError as exc:
        return SourceDoctorResult(
            source_id=spec.source_id,
            name=spec.name,
            type=spec.type,
            url=spec.url,
            http_status=0,
            entries=0,
            ok=False,
            note=str(exc),
        )

    if spec.type in {"rss", "podcast_rss"}:
        return _inspect_rss(spec, response)
    if spec.type == "api_json":
        return _inspect_json(spec, response)
    return _inspect_scrape(spec, response)


def _inspect_rss(spec: SourceDoctorSpec, response: httpx.Response) -> SourceDoctorResult:
    feed = feedparser.parse(response.content)
    entries = len(feed.entries)
    note = ""

    if response.status_code >= 400:
        note = f"HTTP {response.status_code}"
    elif entries == 0:
        note = _rss_empty_note(response, feed)
    elif getattr(feed, "bozo", False) and getattr(feed, "bozo_exception", None):
        note = f"bozo: {feed.bozo_exception}"

    ok = response.status_code < 400 and entries > 0
    return _result(spec, response.status_code, entries, ok, note)


def _inspect_json(spec: SourceDoctorSpec, response: httpx.Response) -> SourceDoctorResult:
    note = ""
    entries = 0
    if response.status_code < 400:
        try:
            payload = response.json()
            entries = _count_json_entries(payload)
            if entries == 0:
                note = "empty JSON"
        except Exception as exc:  # pragma: no cover - defensive
            note = f"invalid JSON: {exc}"

    if response.status_code >= 400:
        note = f"HTTP {response.status_code}"

    ok = response.status_code < 400 and entries > 0 and not note.startswith("invalid JSON")
    return _result(spec, response.status_code, entries, ok, note)


def _inspect_scrape(spec: SourceDoctorSpec, response: httpx.Response) -> SourceDoctorResult:
    note = "scrape target"
    if response.status_code >= 400:
        note = f"HTTP {response.status_code}"
    ok = response.status_code < 400
    return _result(spec, response.status_code, 0, ok, note)


def _result(
    spec: SourceDoctorSpec,
    http_status: int,
    entries: int,
    ok: bool,
    note: str,
) -> SourceDoctorResult:
    return SourceDoctorResult(
        source_id=spec.source_id,
        name=spec.name,
        type=spec.type,
        url=spec.url,
        http_status=http_status,
        entries=entries,
        ok=ok,
        note=note,
    )


def _rss_empty_note(response: httpx.Response, feed: feedparser.FeedParserDict) -> str:
    content_type = response.headers.get("content-type", "").lower()
    body = response.text.lstrip().lower()
    if "html" in content_type or body.startswith("<!doctype html") or body.startswith("<html"):
        return "HTML, not feed"
    if getattr(feed, "bozo", False) and getattr(feed, "bozo_exception", None):
        return f"bozo: {feed.bozo_exception}"
    return "0 entries"


def _count_json_entries(payload: object) -> int:
    if isinstance(payload, list):
        return len(payload)
    if isinstance(payload, dict):
        for key in ("results", "items", "data"):
            value = payload.get(key)
            if isinstance(value, list):
                return len(value)
    return 0
