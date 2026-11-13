"""Bounded, passive discovery observations. Never a selection input or network client."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import math
import os
import re
from contextlib import contextmanager, suppress
from contextvars import ContextVar
from dataclasses import fields, is_dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from uuid import uuid4

import structlog

from peel.models import AlbumQueueItem, ReviewQueueItem, Track

log = structlog.get_logger()
_ACTIVE: ContextVar[DiscoveryTrace | None] = ContextVar("discovery_trace", default=None)
_REDACT = {
    "comment",
    "comments",
    "error",
    "headers",
    "authorization",
    "token",
    "secret",
    "refresh_token",
    "access_token",
    "password",
    "raw_title",
    "raw_response",
}


def _public_url(value: str) -> str | None:
    try:
        return _checked_public_url(value)
    except ValueError:
        return None


def _checked_public_url(value: str) -> str | None:
    parsed = urlsplit(value)
    host = parsed.hostname or ""
    if parsed.username is not None or parsed.password is not None or not host:
        return None
    if host == "localhost" or host.endswith((".local", ".internal")):
        return None
    try:
        if not ipaddress.ip_address(host).is_global:
            return None
    except ValueError:
        pass
    if parsed.port not in (None, 80, 443):
        return None
    query = [
        (k, v)
        for k, v in parse_qsl(parsed.query)
        if k in {"p", "page", "year", "month"} and v.isdigit() and len(v) <= 10
    ]
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), ""))


class DiscoveryTrace:
    """One exclusively-created JSONL stream; a missing footer means incomplete.

    Source-parser output is the collection boundary, not the whole catalogue or
    internet. Caps and unexamined candidates stay explicit. No raw HTTP payloads,
    exception messages, credentials or listening comments are collected.
    """

    def __init__(
        self,
        directory: Path,
        *,
        dry_run: bool = False,
        max_events: int = 50_000,
        max_bytes: int = 20 * 1024 * 1024,
    ):
        self.directory = directory
        self.dry_run = dry_run
        self.max_events = max_events
        self.max_bytes = max_bytes
        self.path: Path | None = None
        self.records: list[dict] = []  # dry-run/test only
        self._stream = None
        self._events = 0
        self._bytes = 0
        self._dropped = 0
        self._trimmed = 0
        self._errors = 0
        self._disabled = False
        self._started = False
        self._limited = False
        self._pipeline_issues = 0

    def _clean(self, value: Any, depth: int = 0) -> Any:
        if depth > 12:
            self._trimmed += 1
            return "[depth_limit]"
        if value is None or isinstance(value, (bool, int)):
            return value
        if isinstance(value, float):
            return value if math.isfinite(value) else {"non_finite": str(value)}
        if isinstance(value, (datetime, date)):
            return value.isoformat()
        if isinstance(value, str):
            if value.casefold().startswith("file://"):
                return None
            if value.casefold().startswith(("http://", "https://")):
                value = _public_url(value)
                if value is None:
                    return None
            if len(value) > 4096:
                self._trimmed += 1
                return value[:4096] + "[truncated]"
            return value
        if isinstance(value, (AlbumQueueItem, ReviewQueueItem, Track)):
            value = value.model_dump(mode="python")
        if is_dataclass(value) and not isinstance(value, type):
            value = {f.name: getattr(value, f.name) for f in fields(value)}
            # Artist/genre names are values, not credential field names.
            # Keep keyed profile inputs as pairs (e.g. the band Sleep Token).
            for name in ("artist_scores", "genre_scores", "artist_genres"):
                if isinstance(value.get(name), dict):
                    value[name] = list(value[name].items())
        if isinstance(value, dict):
            return {
                str(k): self._clean(v, depth + 1)
                for k, v in value.items()
                if str(k).lower() not in _REDACT
                and not any(
                    s in str(k).lower() for s in ("secret", "token", "password", "authorization")
                )
            }
        if isinstance(value, (list, tuple)):
            return [self._clean(v, depth + 1) for v in value]
        if isinstance(value, (set, frozenset)):
            return [self._clean(v, depth + 1) for v in sorted(value, key=str)]
        # Never invoke repr, iterators, arbitrary serialization or properties.
        self._trimmed += 1
        return {"unrecorded_type": type(value).__name__}

    def _append(self, record: dict, *, force: bool = False) -> None:
        encoded = json.dumps(self._clean(record), ensure_ascii=False, allow_nan=False) + "\n"
        size = len(encoded.encode("utf-8"))
        if not force and (self._events >= self.max_events or self._bytes + size > self.max_bytes):
            self._dropped += 1
            if not self._limited:
                self._limited = True
                self._append({"type": "limit", "reason": "capture_budget_exhausted"}, force=True)
            return
        self._events += 1
        self._bytes += size
        if self.dry_run:
            self.records.append(json.loads(encoded))
        elif self._stream is not None:
            self._stream.write(encoded)
            self._stream.flush()  # completed lines survive a later pipeline crash

    def record(self, event: str, **data: Any) -> None:
        if self._disabled:
            return
        try:
            if event.endswith(("_failed", ".failed")):
                self._pipeline_issues += 1
            if not self._started:
                if event != "run.started":
                    return
                started = data["started_at"]
                week = data["week"]
                if not re.fullmatch(r"\d{4}-W\d{2}", week):
                    raise ValueError("invalid observation week")
                run_id = os.getenv("GITHUB_RUN_ID", "")
                attempt = os.getenv("GITHUB_RUN_ATTEMPT", "")
                run_id = run_id if run_id.isdigit() else "local"
                attempt = attempt if attempt.isdigit() else "1"
                revision = os.getenv("GITHUB_SHA", "")
                revision = revision if re.fullmatch(r"[a-fA-F0-9]{40}", revision) else None
                digest = None
                db_path = data.pop("db_path", None)
                if db_path and Path(db_path).is_file():
                    digest = hashlib.sha256(Path(db_path).read_bytes()).hexdigest()
                if not self.dry_run:
                    parent = self.directory / week
                    parent.mkdir(parents=True, exist_ok=True)
                    self.path = parent / (
                        f"run-{run_id}-{attempt}-"
                        f"{started.strftime('%Y%m%dT%H%M%S%fZ')}-{uuid4().hex[:12]}.jsonl"
                    )
                    self._stream = self.path.open("x", encoding="utf-8")
                self._started = True
                self._append(
                    {
                        "type": "header",
                        "schema_version": 1,
                        "week": week,
                        "started_at": started,
                        "run_id": run_id,
                        "run_attempt": attempt,
                        "code_revision": revision,
                        "input_db_sha256": digest,
                        "mode": "dry_run" if self.dry_run else "live",
                        "boundary": "parsed_items_bandcamp_inspections_local_pools",
                        "limits": {"max_events": self.max_events, "max_bytes": self.max_bytes},
                        "config": data.get("config", {}),
                    },
                    force=True,
                )
            self._append(
                {
                    "type": "event",
                    "sequence": self._events,
                    "observed_at": datetime.now(UTC),
                    "event": event,
                    **data,
                }
            )
        except Exception as exc:
            self._errors += 1
            self._disabled = True
            log.warning("discovery.capture_failed", error_type=type(exc).__name__)

    def finish(self, status: str, *, error_type: str | None = None) -> None:
        try:
            if self._started and not self._disabled:
                self._append(
                    {
                        "type": "footer",
                        "finished_at": datetime.now(UTC),
                        "pipeline_status": status,
                        "pipeline_error_type": error_type,
                        "trace_complete": status == "returned"
                        and not (
                            self._dropped or self._trimmed or self._errors or self._pipeline_issues
                        ),
                        "observed_pipeline_issues": self._pipeline_issues,
                        "events_written": self._events,
                        "events_dropped": self._dropped,
                        "values_truncated": self._trimmed,
                        "capture_errors": self._errors,
                    },
                    force=True,
                )
        except Exception as exc:
            log.warning("discovery.capture_finish_failed", error_type=type(exc).__name__)
        finally:
            if self._stream is not None:
                with suppress(Exception):
                    self._stream.close()
            log.info(
                "discovery.capture_finished",
                path=str(self.path) if self.path else None,
                dry_run=self.dry_run,
                records=self._events,
                dropped=self._dropped,
                capture_errors=self._errors,
            )


@contextmanager
def observe(trace: DiscoveryTrace):
    token = _ACTIVE.set(trace)
    try:
        yield trace
    finally:
        _ACTIVE.reset(token)


def capture(event: str, **data: Any) -> None:
    """Non-throwing observation: no values are ever returned to the ranker."""
    try:
        trace = _ACTIVE.get()
        if trace is not None:
            trace.record(event, **data)
    except Exception as exc:
        log.warning("discovery.capture_failed", error_type=type(exc).__name__)
