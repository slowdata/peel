"""Optional comment-only edits: capture a session, suggest, preview, then compare-and-swap.

No schema migrations, rating upserts, networking or automatic historical rewrites.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import tempfile
import unicodedata
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote

from peel.db import DB
from peel.matcher import normalize

Kind = Literal["track", "album"]
MAX_COMMENT_LENGTH = 8_000
# These are the listener's vocabulary, not spelling mistakes or taste rules.
PROTECTED_WORDS = (
    "love",
    "like",
    "meh",
    "dislike",
    "hate",
    "groove",
    "beat",
    "beats",
    "noise",
    "folk",
    "hip-hop",
    "hip hop",
    "synth",
    "synths",
    "pop",
    "rap",
    "rock",
    "jazz",
    "nice",
    "bué",
    "tá",
    "ahah",
    "lol",
    "wanna be",
    "wannabe",
    "acho",
    "talvez",
    "parece",
    "penso",
    "sei",
)


@dataclass(frozen=True)
class CommentRef:
    kind: Kind
    key: tuple[str, ...]
    artist: str
    title: str
    rating: int
    label: str
    rated_at: str
    comment: str


@dataclass(frozen=True)
class Suggestion:
    ref: CommentRef
    after: str
    needs_review: bool = False
    reason: str = ""


class CommentConflict(RuntimeError):
    """The opinion changed since it was displayed. Do not overwrite it."""


def _where(kind: Kind) -> tuple[str, str]:
    if kind == "track":
        return "feedback", "spotify_uri = ?"
    if kind == "album":
        return "album_feedback", "artist_key = ? AND album_key = ?"
    raise ValueError("Unknown comment kind")


def _opinion(conn: sqlite3.Connection, ref: CommentRef) -> tuple | None:
    table, where = _where(ref.kind)
    return conn.execute(
        f"SELECT rating, label, rated_at, comment FROM {table} WHERE {where}", ref.key
    ).fetchone()


def track_comment(db: DB, uri: str) -> CommentRef | None:
    row = db.conn.execute(
        "SELECT f.rating, f.label, f.rated_at, f.comment, t.artist, t.title "
        "FROM feedback f LEFT JOIN tracks t ON t.spotify_uri = f.spotify_uri "
        "WHERE f.spotify_uri = ?",
        (uri,),
    ).fetchone()
    if row is None or not row[3] or not row[3].strip():
        return None
    return CommentRef("track", (uri,), row[4] or "", row[5] or uri, *row[:4])


def album_comment(db: DB, artist: str, album: str) -> CommentRef | None:
    latest = db._album_feedback_row(artist, album)
    if latest is None:
        return None
    key = (str(latest[0]), normalize(album))
    row = db.conn.execute(
        "SELECT rating, label, rated_at, comment FROM album_feedback "
        "WHERE artist_key = ? AND album_key = ?",
        key,
    ).fetchone()
    if row is None or not row[3] or not row[3].strip():
        return None
    return CommentRef("album", key, artist, album, *row)


class FeedbackSession:
    """DB proxy that observes only writes made by this listening session.

    Existing upserts still save originals immediately. Capture failures cannot
    prevent a vote being saved; no global hooks or extra DB writes are installed.
    """

    def __init__(self, db: DB):
        self.db = db
        self.comments: dict[tuple, CommentRef] = {}
        self._originals: dict[tuple, str | None] = {}
        self.capture_failed = False

    def __getattr__(self, name: str) -> Any:
        return getattr(self.db, name)

    def _remember(self, ref: CommentRef | None, identity: tuple, before: str | None) -> None:
        self._originals.setdefault(identity, before)
        if ref is None or ref.comment == self._originals[identity]:
            self.comments.pop(identity, None)
        else:
            self.comments[identity] = ref

    def upsert_feedback(self, uri: str, label: str, comment: str | None = None) -> None:
        before = None
        try:
            previous = self.db.feedback_for_track(uri)
            before = previous[2] if previous else None
        except Exception:
            self.capture_failed = True
        self.db.upsert_feedback(uri, label, comment)
        if self.capture_failed:
            self.comments.clear()
            return
        try:
            ref = track_comment(self.db, uri)
            if ref and (ref.comment != comment or ref.label != label.strip().lower()):
                self.capture_failed = True
                self.comments.clear()
                return
            self._remember(ref, ("track", uri), before)
        except Exception:
            self.capture_failed = True

    def upsert_album_feedback(
        self, artist: str, album: str, label: str, comment: str | None = None
    ) -> None:
        before = None
        try:
            previous = self.db.album_feedback_for_identity(artist, album)
            before = previous[2] if previous else None
        except Exception:
            self.capture_failed = True
        self.db.upsert_album_feedback(artist, album, label, comment)
        if self.capture_failed:
            self.comments.clear()
            return
        try:
            ref = album_comment(self.db, artist, album)
            latest = self.db._album_feedback_row(artist, album)
            key = (str(latest[0]), normalize(album))
            if ref and (ref.comment != comment or ref.label != label.strip().lower()):
                self.capture_failed = True
                self.comments.clear()
                return
            self._remember(ref, ("album", *key), before)
        except Exception:
            self.capture_failed = True


def week_comments(db: DB, playlist_id: str, week: str) -> list[CommentRef]:
    """Explicit bounded review of current opinions linked to a confirmed week."""
    refs: dict[tuple, CommentRef] = {}
    for item in db.review_queue_snapshot(playlist_id, week) or []:
        ref = track_comment(db, item.spotify_uri)
        if ref:
            refs[(ref.kind, *ref.key)] = ref
    for item in db.album_queue(week) or []:
        ref = album_comment(db, item.artist, item.album)
        if ref:
            refs[(ref.kind, *ref.key)] = ref
    return list(refs.values())


def _fold(value: str) -> str:
    text = unicodedata.normalize("NFD", value.casefold())
    return "".join(c for c in text if unicodedata.category(c) != "Mn")


def _negations(value: str) -> list[str]:
    return re.findall(r"\b(?:nao|nunca|jamais|nem|sem)\b", _fold(value))


def _utf16_indices(text: str) -> dict[int, int]:
    # LanguageTool offsets use Java UTF-16 code units, not Python code points.
    offsets, units = {0: 0}, 0
    for index, char in enumerate(text, 1):
        units += 2 if ord(char) > 0xFFFF else 1
        offsets[units] = index
    return offsets


def _protected_spans(ref: CommentRef) -> list[tuple[int, int]]:
    values = [ref.artist, ref.title, *PROTECTED_WORDS]
    spans = []
    for word in filter(None, values):
        for match in re.finditer(r"(?<!\w)" + re.escape(word) + r"(?!\w)", ref.comment, re.I):
            spans.append(match.span())
    spans.extend(
        m.span() for m in re.finditer(r"[!?…\.]{2,}|\b(?:a?ha){2,}h?\b", ref.comment, re.I)
    )
    return spans


def suggest(ref: CommentRef, matches: list[dict]) -> Suggestion:
    """Conservative mechanical edits, never a stylistic rewrite or inferred opinion."""
    text = ref.comment
    if "\ufffd" in text or any(unicodedata.category(c) == "Cs" for c in text):
        return Suggestion(
            ref, text, True, "Há caracteres danificados; não vou adivinhar o sentido."
        )
    if len(text) > MAX_COMMENT_LENGTH:
        return Suggestion(ref, text, True, "Comentário longo: revê ou edita manualmente.")
    indices, protected = _utf16_indices(text), _protected_spans(ref)
    edits, ambiguous = [], False
    for match in matches[:500]:
        rule = match.get("rule") or {}
        category = (rule.get("category") or {}).get("id", "")
        if (
            category in {"STYLE", "REDUNDANCY", "SEMANTICS", "FORMAL"}
            or rule.get("issueType") == "style"
        ):
            continue
        offset, length = match.get("offset"), match.get("length")
        if type(offset) is not int or type(length) is not int or length < 0:
            continue
        start, end = indices.get(offset), indices.get(offset + length)
        if start is None or end is None:
            continue
        replacements = match.get("replacements") or []
        choices = [
            r["value"]
            for r in replacements
            if isinstance(r, dict) and isinstance(r.get("value"), str)
        ]
        if not choices or choices[0] == text[start:end]:
            continue
        replacement = choices[0]
        if len(replacement) > 500 or "\ufffd" in replacement:
            continue
        unsafe = False
        for a, b in protected:
            if start == end and a < start < b:
                unsafe = True
            elif start < b and end > a:
                token = text[a:b]
                # Allow punctuation around an unchanged term ("love, mas"),
                # never a replacement/translation or a partial edit inside it.
                pattern = (r"(?<!\w)" if token[0].isalnum() else "") + re.escape(token)
                pattern += r"(?!\w)" if token[-1].isalnum() else ""
                unsafe |= not (
                    start <= a
                    and end >= b
                    and len(re.findall(pattern, replacement))
                    == len(re.findall(pattern, text[start:end]))
                )
        if unsafe:
            continue
        # Spelling alternatives are not all equally certain. They may be shown
        # individually, but never included in bulk 'apply safe'.
        lexical_change = _fold(replacement) != _fold(text[start:end])
        ambiguous |= lexical_change and (
            len(set(choices)) > 1
            or (category == "TYPOS" and any(c.isalpha() for c in text[start:end]))
        )
        if any(start < b and end > a or start == a for a, b, _ in edits):
            ambiguous = True
            continue
        edits.append((start, end, replacement))
    after = text
    for start, end, replacement in sorted(edits, reverse=True):
        after = after[:start] + replacement + after[end:]
    if _negations(text) != _negations(after):
        return Suggestion(
            ref, text, True, "A sugestão mexia numa negação; confirma o texto manualmente."
        )
    return Suggestion(
        ref,
        after,
        ambiguous,
        "Há alternativas possíveis; confirma esta sugestão." if ambiguous else "",
    )


def apply_comments(db_path: Path, changes: list[tuple[CommentRef, str]]) -> Path | None:
    """Atomic comment-only updates with an exclusive original/undo journal first."""
    changes = [(ref, after) for ref, after in changes if after != ref.comment]
    if not changes:
        return None
    if len({(ref.kind, *ref.key) for ref, _ in changes}) != len(changes):
        raise ValueError("Duplicate comment identity")
    path = db_path.resolve(strict=True)
    conn = sqlite3.connect(f"file:{quote(str(path))}?mode=rw", uri=True, timeout=2)
    backup = None
    try:
        conn.execute("BEGIN IMMEDIATE")
        for ref, after in changes:
            if not isinstance(after, str) or not after.strip():
                raise ValueError("A replacement comment cannot be empty")
            if _opinion(conn, ref) != (ref.rating, ref.label, ref.rated_at, ref.comment):
                raise CommentConflict("A avaliação ou o comentário mudou durante a revisão.")
        directory = path.parent / ".peel-backups"
        directory.mkdir(mode=0o700, exist_ok=True)
        folder = Path(
            tempfile.mkdtemp(
                prefix="comment-soundcheck-" + datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-",
                dir=directory,
            )
        )
        backup = folder / "comments.json"
        payload = {
            "schema_version": 1,
            "db_path": str(path),
            "created_at": datetime.now(UTC).isoformat(),
            "changes": [{"original": asdict(ref), "after": after} for ref, after in changes],
        }
        fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        for ref, after in changes:
            table, where = _where(ref.kind)
            conn.execute(f"UPDATE {table} SET comment = ? WHERE {where}", (after, *ref.key))
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()
    return backup


def undo_comments(db_path: Path, backup: Path) -> Path | None:
    data = json.loads(backup.read_text(encoding="utf-8"))
    if data.get("schema_version") != 1 or data.get("db_path") != str(db_path.resolve(strict=True)):
        raise ValueError("Backup does not match this database")
    changes = []
    for entry in data["changes"]:
        values = dict(entry["original"])
        original = values.pop("comment")
        values["key"] = tuple(values["key"])
        ref = CommentRef(comment=entry["after"], **values)
        changes.append((ref, original))
    return apply_comments(db_path, changes)
