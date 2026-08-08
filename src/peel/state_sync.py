"""Safe synchronisation of Peel's canonical SQLite state.

The weekly pipeline writes ``data/peel.db`` on GitHub.  Interactive commands
run against a local checkout, so code and state must be synchronised
independently: a dirty source tree must not make the CLI read an old queue.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import httpx

STATE_REPOSITORY = "slowdata/peel"
STATE_CLONE_URL = f"https://github.com/{STATE_REPOSITORY}.git"
REMOTE_STATE_API_URL = (
    f"https://api.github.com/repos/{STATE_REPOSITORY}/contents/data/peel.db?ref=main"
)
_REQUIRED_TABLES = {"tracks", "feedback", "review_queue", "album_queue_weeks"}


class StateSyncError(RuntimeError):
    """The canonical state could not be obtained without risking local data."""


class LocalStateConflict(StateSyncError):
    """Both local and remote state changed since the last safe synchronisation."""


@dataclass(frozen=True, slots=True)
class RemoteState:
    blob_sha: str
    download_url: str


@dataclass(frozen=True, slots=True)
class SyncMarker:
    remote_blob_sha: str
    db_sha256: str
    synced_at: str


@dataclass(frozen=True, slots=True)
class SyncResult:
    status: Literal["current", "updated", "local_changes"]
    local_week: str | None
    previous_week: str | None = None
    backup_path: Path | None = None
    remote_blob_sha: str | None = None


HttpGet = Callable[..., httpx.Response]


def marker_path(db_path: Path) -> Path:
    return db_path.parent / ".peel-state.json"


def backup_dir(db_path: Path) -> Path:
    return db_path.parent / ".peel-backups"


def sync_remote_state(
    db_path: Path,
    project_root: Path,
    *,
    http_get: HttpGet | None = None,
) -> SyncResult:
    """Update only the canonical DB, never the source checkout.

    A marker records the exact local bytes last known to be remote.  If those
    bytes changed locally and GitHub also advanced, the operation fails instead
    of overwriting feedback.
    """
    getter = http_get or httpx.get
    remote = fetch_remote_state(getter)
    marker = load_marker(marker_path(db_path))
    local_hash = file_sha256(db_path) if db_path.exists() else None
    previous_week = latest_state_week(db_path) if db_path.exists() else None

    if marker is not None and local_hash != marker.db_sha256:
        if remote.blob_sha != marker.remote_blob_sha:
            raise LocalStateConflict(
                "A DB local tem alterações por sincronizar e o estado remoto avançou. "
                "Corre `uv run peel sync push` antes de voltar a tentar."
            )
        return SyncResult("local_changes", previous_week, remote_blob_sha=remote.blob_sha)

    if marker is None and db_path.exists():
        if not git_db_is_clean(project_root, db_path):
            raise LocalStateConflict(
                "A DB local tem alterações sem marcador de sincronização. "
                "Corre `uv run peel sync push` ou usa `--offline`; não foi sobrescrita."
            )
        # A clean checkout already at the remote blob only needs bootstrapping.
        if git_blob_sha(db_path) == remote.blob_sha:
            mark_local_state_synced(db_path, remote_blob_sha=remote.blob_sha)
            return SyncResult("current", previous_week, remote_blob_sha=remote.blob_sha)

    if (
        marker is not None
        and marker.remote_blob_sha == remote.blob_sha
        and local_hash == marker.db_sha256
    ):
        return SyncResult("current", previous_week, remote_blob_sha=remote.blob_sha)

    downloaded = _download_and_validate(remote, db_path.parent, getter)
    backup: Path | None = None
    try:
        if db_path.exists():
            backup = _backup_db(db_path)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        os.replace(downloaded, db_path)
    finally:
        downloaded.unlink(missing_ok=True)

    mark_local_state_synced(db_path, remote_blob_sha=remote.blob_sha)
    return SyncResult(
        "updated",
        latest_state_week(db_path),
        previous_week=previous_week,
        backup_path=backup,
        remote_blob_sha=remote.blob_sha,
    )


def fetch_remote_state(http_get: HttpGet | None = None) -> RemoteState:
    getter = http_get or httpx.get
    try:
        response = getter(
            REMOTE_STATE_API_URL,
            headers={"Accept": "application/vnd.github+json"},
            timeout=20,
        )
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, ValueError, TypeError) as exc:
        raise StateSyncError(f"Não foi possível consultar o estado remoto: {exc}") from exc

    blob_sha = payload.get("sha") if isinstance(payload, dict) else None
    download_url = payload.get("download_url") if isinstance(payload, dict) else None
    if not isinstance(blob_sha, str) or not blob_sha:
        raise StateSyncError("Resposta remota sem SHA da DB.")
    if not isinstance(download_url, str) or not download_url.startswith("https://"):
        raise StateSyncError("Resposta remota sem URL segura para a DB.")
    return RemoteState(blob_sha=blob_sha, download_url=download_url)


def mark_local_state_synced(db_path: Path, *, remote_blob_sha: str | None = None) -> None:
    """Record that the current DB bytes have been successfully pushed/pulled."""
    if not db_path.exists():
        raise StateSyncError(f"DB local inexistente: {db_path}")
    _write_marker(
        db_path,
        remote_blob_sha=remote_blob_sha or git_blob_sha(db_path),
        synced_db_sha256=file_sha256(db_path),
    )


def merge_remote_state_for_push(
    db_path: Path,
    *,
    http_get: HttpGet | None = None,
) -> SyncResult:
    """Rebase local user-owned state onto the latest remote DB for a safe push."""
    if not db_path.exists():
        raise StateSyncError(f"DB local inexistente: {db_path}")
    getter = http_get or httpx.get
    remote = fetch_remote_state(getter)
    downloaded = _download_and_validate(remote, db_path.parent, getter)
    remote_base_hash = file_sha256(downloaded)
    previous_week = latest_state_week(db_path)
    marker = load_marker(marker_path(db_path))
    backup: Path | None = None
    try:
        _merge_user_state(
            downloaded,
            db_path,
            changed_since=marker.synced_at if marker else None,
        )
        _validate_peel_db(downloaded)
        backup = _backup_db(db_path)
        os.replace(downloaded, db_path)
    finally:
        downloaded.unlink(missing_ok=True)
    # The local bytes now contain unpushed overlays.  Keep the pristine remote
    # hash in the marker so another remote advance still detects divergence.
    _write_marker(
        db_path,
        remote_blob_sha=remote.blob_sha,
        synced_db_sha256=remote_base_hash,
    )
    return SyncResult(
        "local_changes",
        latest_state_week(db_path),
        previous_week=previous_week,
        backup_path=backup,
        remote_blob_sha=remote.blob_sha,
    )


def _write_marker(
    db_path: Path,
    *,
    remote_blob_sha: str,
    synced_db_sha256: str,
) -> None:
    marker = SyncMarker(
        remote_blob_sha=remote_blob_sha,
        db_sha256=synced_db_sha256,
        synced_at=datetime.now(UTC).isoformat(),
    )
    _atomic_json_write(marker_path(db_path), asdict(marker))


def load_marker(path: Path) -> SyncMarker | None:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return SyncMarker(
            remote_blob_sha=str(payload["remote_blob_sha"]),
            db_sha256=str(payload["db_sha256"]),
            synced_at=str(payload["synced_at"]),
        )
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise StateSyncError(
            f"Marcador de sincronização inválido em {path}; não foi sobrescrito."
        ) from exc


def state_has_local_changes(db_path: Path) -> bool | None:
    """Return marker-relative dirtiness, or ``None`` before bootstrap."""
    marker = load_marker(marker_path(db_path))
    if marker is None or not db_path.exists():
        return None
    return file_sha256(db_path) != marker.db_sha256


def latest_state_week(db_path: Path) -> str | None:
    if not db_path.exists():
        return None
    try:
        with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
            rows = conn.execute(
                """
                SELECT MAX(week) FROM (
                    SELECT week FROM album_queue_weeks
                    UNION ALL
                    SELECT current_week AS week FROM review_queue
                    UNION ALL
                    SELECT added_at_week AS week FROM tracks
                )
                """
            ).fetchone()
    except sqlite3.Error:
        return None
    return str(rows[0]) if rows and rows[0] else None


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def git_blob_sha(path: Path) -> str:
    data = path.read_bytes()
    return hashlib.sha1(f"blob {len(data)}\0".encode() + data).hexdigest()  # noqa: S324


def git_db_is_clean(project_root: Path, db_path: Path) -> bool:
    """Check only the DB in worktree and index; unrelated code may be dirty."""
    try:
        relative = db_path.resolve().relative_to(project_root.resolve())
    except ValueError:
        return False
    for args in (
        ["git", "diff", "--quiet", "--", str(relative)],
        ["git", "diff", "--cached", "--quiet", "--", str(relative)],
    ):
        result = subprocess.run(args, cwd=project_root, check=False)
        if result.returncode != 0:
            return False
    return True


def _merge_user_state(
    remote_path: Path,
    local_path: Path,
    *,
    changed_since: str | None,
) -> None:
    """Overlay user-owned tables; remote weekly discovery remains authoritative."""
    try:
        with sqlite3.connect(remote_path) as conn:
            conn.execute("ATTACH DATABASE ? AS local_state", (str(local_path),))
            _merge_feedback_table(conn, "feedback", ("spotify_uri",))
            _merge_feedback_table(conn, "album_feedback", ("artist_key", "album_key"))
            _merge_timestamped_table(
                conn,
                "artist_genres",
                key_columns=("artist",),
                timestamp_column="fetched_at",
            )
            _copy_table(conn, "albums", mode="IGNORE")
            _copy_table(conn, "album_mentions", mode="IGNORE")

            if changed_since is not None:
                local_weeks = [
                    str(row[0])
                    for row in conn.execute(
                        "SELECT week FROM local_state.album_queue_weeks WHERE created_at > ?",
                        (changed_since,),
                    )
                ]
                for week in local_weeks:
                    conn.execute("DELETE FROM main.album_queue_items WHERE week = ?", (week,))
                    _copy_table_where(
                        conn,
                        "album_queue_weeks",
                        mode="REPLACE",
                        where="week = ?",
                        params=(week,),
                    )
                    _copy_table_where(
                        conn,
                        "album_queue_items",
                        mode="REPLACE",
                        where="week = ?",
                        params=(week,),
                    )

                finalized = [
                    (str(row[0]), str(row[1]))
                    for row in conn.execute(
                        "SELECT week, playlist_id FROM local_state.finalized_weeks "
                        "WHERE finalized_at > ?",
                        (changed_since,),
                    )
                ]
                for week, playlist_id in finalized:
                    conn.execute(
                        "DELETE FROM main.finalized_week_tracks WHERE week = ? AND playlist_id = ?",
                        (week, playlist_id),
                    )
                    _copy_table_where(
                        conn,
                        "finalized_weeks",
                        mode="REPLACE",
                        where="week = ? AND playlist_id = ?",
                        params=(week, playlist_id),
                    )
                    _copy_table_where(
                        conn,
                        "finalized_week_tracks",
                        mode="REPLACE",
                        where="week = ? AND playlist_id = ?",
                        params=(week, playlist_id),
                    )
            conn.commit()
    except sqlite3.Error as exc:
        raise StateSyncError(f"Falhou o merge seguro do estado local: {exc}") from exc


def _merge_feedback_table(
    conn: sqlite3.Connection,
    table: str,
    key_columns: tuple[str, ...],
) -> None:
    columns = _shared_columns(conn, table)
    if not columns or "rated_at" not in columns:
        return
    quoted = ", ".join(f'"{column}"' for column in columns)
    conflict = ", ".join(f'"{column}"' for column in key_columns)
    updates = ", ".join(
        f'"{column}" = excluded."{column}"' for column in columns if column not in key_columns
    )
    conn.execute(
        f'INSERT INTO main."{table}" ({quoted}) '
        f'SELECT {quoted} FROM local_state."{table}" WHERE 1 '
        f"ON CONFLICT ({conflict}) DO UPDATE SET {updates} "
        'WHERE excluded."rated_at" >= "rated_at"'
    )


def _merge_timestamped_table(
    conn: sqlite3.Connection,
    table: str,
    *,
    key_columns: tuple[str, ...],
    timestamp_column: str,
) -> None:
    columns = _shared_columns(conn, table)
    if not columns or timestamp_column not in columns:
        return
    quoted = ", ".join(f'"{column}"' for column in columns)
    conflict = ", ".join(f'"{column}"' for column in key_columns)
    updates = ", ".join(
        f'"{column}" = excluded."{column}"' for column in columns if column not in key_columns
    )
    conn.execute(
        f'INSERT INTO main."{table}" ({quoted}) '
        f'SELECT {quoted} FROM local_state."{table}" WHERE 1 '
        f"ON CONFLICT ({conflict}) DO UPDATE SET {updates} "
        f'WHERE excluded."{timestamp_column}" >= "{timestamp_column}"'
    )


def _copy_table(conn: sqlite3.Connection, table: str, *, mode: str) -> None:
    if mode not in {"IGNORE", "REPLACE"}:
        raise ValueError(mode)
    columns = _shared_columns(conn, table)
    if not columns:
        return
    quoted = ", ".join(f'"{column}"' for column in columns)
    conn.execute(
        f'INSERT OR {mode} INTO main."{table}" ({quoted}) '
        f'SELECT {quoted} FROM local_state."{table}"'
    )


def _copy_table_where(
    conn: sqlite3.Connection,
    table: str,
    *,
    mode: str,
    where: str,
    params: tuple[object, ...],
) -> None:
    if mode not in {"IGNORE", "REPLACE"}:
        raise ValueError(mode)
    columns = _shared_columns(conn, table)
    if not columns:
        return
    quoted = ", ".join(f'"{column}"' for column in columns)
    conn.execute(
        f'INSERT OR {mode} INTO main."{table}" ({quoted}) '
        f'SELECT {quoted} FROM local_state."{table}" WHERE {where}',
        params,
    )


def _shared_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    main_columns = [str(row[1]) for row in conn.execute(f'PRAGMA main.table_info("{table}")')]
    local_columns = {
        str(row[1]) for row in conn.execute(f'PRAGMA local_state.table_info("{table}")')
    }
    return [column for column in main_columns if column in local_columns]


def _download_and_validate(remote: RemoteState, directory: Path, http_get: HttpGet) -> Path:
    try:
        response = http_get(remote.download_url, timeout=30)
        response.raise_for_status()
        content = response.content
    except httpx.HTTPError as exc:
        raise StateSyncError(f"Falhou o download da DB remota: {exc}") from exc
    if not content.startswith(b"SQLite format 3\x00"):
        raise StateSyncError("Download remoto não é uma DB SQLite válida.")

    directory.mkdir(parents=True, exist_ok=True)
    fd, raw_path = tempfile.mkstemp(prefix=".peel-state-", suffix=".db", dir=directory)
    os.close(fd)
    path = Path(raw_path)
    try:
        path.write_bytes(content)
        _validate_peel_db(path)
        if git_blob_sha(path) != remote.blob_sha:
            raise StateSyncError("Download remoto não corresponde ao SHA publicado pelo GitHub.")
    except Exception:
        path.unlink(missing_ok=True)
        raise
    return path


def _validate_peel_db(path: Path) -> None:
    try:
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as conn:
            check = conn.execute("PRAGMA quick_check").fetchone()
            tables = {
                str(row[0])
                for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
    except sqlite3.Error as exc:
        raise StateSyncError(f"DB remota SQLite inválida: {exc}") from exc
    if not check or check[0] != "ok":
        raise StateSyncError("DB remota falhou PRAGMA quick_check.")
    missing = sorted(_REQUIRED_TABLES - tables)
    if missing:
        raise StateSyncError(f"DB remota não parece ser Peel; faltam tabelas: {', '.join(missing)}")


def _backup_db(db_path: Path) -> Path:
    target_dir = backup_dir(db_path)
    target_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    target = target_dir / f"peel-before-state-sync-{stamp}.db"
    counter = 1
    while target.exists():
        target = target_dir / f"peel-before-state-sync-{stamp}-{counter}.db"
        counter += 1
    shutil.copy2(db_path, target)
    return target


def _atomic_json_write(path: Path, payload: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, raw_path = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)
    os.close(fd)
    temporary = Path(raw_path)
    try:
        temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
