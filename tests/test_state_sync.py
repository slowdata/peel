from __future__ import annotations

from pathlib import Path

import httpx
import pytest

import peel.state_sync as state_sync
from peel.db import DB
from peel.models import AlbumQueueItem


def _make_db(path: Path, week: str, *, title: str = "Track") -> None:
    db = DB(str(path))
    db.init_schema()
    db.conn.execute(
        """
        INSERT INTO tracks
        (spotify_uri, source_id, artist, title, source_url, added_at, added_at_week)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            f"spotify:track:{week}",
            "source-a",
            "Artist",
            title,
            None,
            "2026-08-08T10:00:00+00:00",
            week,
        ),
    )
    db.conn.commit()
    db.close()


def _queue_item(artist: str, album: str) -> AlbumQueueItem:
    return AlbumQueueItem(
        week="2026-W31",
        position=1,
        artist=artist,
        album=album,
        artist_key=artist.lower(),
        album_key=album.lower(),
        source_ids=("source-a",),
        source_count=1,
        listen_url=f"https://{artist.lower().replace(' ', '-')}.bandcamp.com/album/item",
        listen_kind="bandcamp",
        editorial_url=None,
        is_new=True,
    )


def _remote_get(db_path: Path):
    content = db_path.read_bytes()
    blob_sha = state_sync.git_blob_sha(db_path)

    def get(url: str, **_: object) -> httpx.Response:
        request = httpx.Request("GET", url)
        if url == state_sync.REMOTE_STATE_API_URL:
            return httpx.Response(
                200,
                request=request,
                json={"sha": blob_sha, "download_url": "https://download.example/peel.db"},
            )
        if url == "https://download.example/peel.db":
            return httpx.Response(200, request=request, content=content)
        raise AssertionError(url)

    return get, blob_sha


def test_sync_updates_atomically_and_keeps_backup(tmp_path: Path) -> None:
    local = tmp_path / "data" / "peel.db"
    local.parent.mkdir()
    remote = tmp_path / "remote.db"
    _make_db(local, "2026-W31")
    _make_db(remote, "2026-W32", title="Remote")
    state_sync.mark_local_state_synced(local)
    getter, _ = _remote_get(remote)

    result = state_sync.sync_remote_state(local, tmp_path, http_get=getter)

    assert result.status == "updated"
    assert result.previous_week == "2026-W31"
    assert result.local_week == "2026-W32"
    assert result.backup_path is not None and result.backup_path.exists()
    assert state_sync.file_sha256(local) == state_sync.file_sha256(remote)
    assert state_sync.state_has_local_changes(local) is False


def test_sync_never_overwrites_local_feedback_when_remote_advanced(tmp_path: Path) -> None:
    local = tmp_path / "data" / "peel.db"
    local.parent.mkdir()
    remote = tmp_path / "remote.db"
    _make_db(local, "2026-W31")
    state_sync.mark_local_state_synced(local)
    before_feedback = local.read_bytes()
    with local.open("ab") as handle:
        handle.write(b"local feedback marker")
    local_bytes = local.read_bytes()
    assert local_bytes != before_feedback
    _make_db(remote, "2026-W32", title="Remote")
    getter, _ = _remote_get(remote)

    with pytest.raises(state_sync.LocalStateConflict, match="alterações por sincronizar"):
        state_sync.sync_remote_state(local, tmp_path, http_get=getter)

    assert local.read_bytes() == local_bytes


def test_sync_allows_local_changes_while_remote_is_unchanged(tmp_path: Path) -> None:
    local = tmp_path / "data" / "peel.db"
    local.parent.mkdir()
    _make_db(local, "2026-W32")
    getter, remote_sha = _remote_get(local)
    state_sync.mark_local_state_synced(local, remote_blob_sha=remote_sha)
    db = DB(str(local))
    db.init_schema()
    db.upsert_feedback("spotify:track:2026-W32", "like", None)
    db.close()

    result = state_sync.sync_remote_state(local, tmp_path, http_get=getter)

    assert result.status == "local_changes"
    assert state_sync.state_has_local_changes(local) is True


def test_merge_for_push_preserves_remote_week_and_local_feedback(tmp_path: Path) -> None:
    local = tmp_path / "data" / "peel.db"
    local.parent.mkdir()
    remote = tmp_path / "remote.db"
    _make_db(local, "2026-W31")
    local_db = DB(str(local))
    local_db.init_schema()
    local_db.replace_album_queue("2026-W31", [_queue_item("Local Queue", "Old")])
    local_db.close()
    state_sync.mark_local_state_synced(local)
    local_db = DB(str(local))
    local_db.init_schema()
    local_db.upsert_feedback("spotify:track:2026-W31", "love", "local")
    local_db.close()

    _make_db(remote, "2026-W32", title="Remote")
    remote_db = DB(str(remote))
    remote_db.init_schema()
    remote_db.replace_album_queue("2026-W31", [_queue_item("Remote Queue", "Newer")])
    remote_db.close()
    getter, remote_sha = _remote_get(remote)

    result = state_sync.merge_remote_state_for_push(local, http_get=getter)

    merged = DB(str(local))
    merged.init_schema()
    assert state_sync.latest_state_week(local) == "2026-W32"
    assert merged.feedback_for_track("spotify:track:2026-W31") == (2, "love", "local")
    assert (
        merged.conn.execute(
            "SELECT title FROM tracks WHERE spotify_uri = 'spotify:track:2026-W32'"
        ).fetchone()[0]
        == "Remote"
    )
    assert merged.album_queue("2026-W31")[0].artist == "Remote Queue"  # type: ignore[index]
    merged.close()
    assert result.remote_blob_sha == remote_sha
    assert result.backup_path is not None and result.backup_path.exists()
    assert state_sync.state_has_local_changes(local) is True


def test_merge_for_push_keeps_explicit_queue_refresh_after_marker(tmp_path: Path) -> None:
    local = tmp_path / "data" / "peel.db"
    local.parent.mkdir()
    remote = tmp_path / "remote.db"
    _make_db(local, "2026-W31")
    state_sync.mark_local_state_synced(local)
    local_db = DB(str(local))
    local_db.init_schema()
    local_db.replace_album_queue("2026-W31", [_queue_item("Local Refresh", "Chosen")])
    local_db.close()
    _make_db(remote, "2026-W32")
    remote_db = DB(str(remote))
    remote_db.init_schema()
    remote_db.replace_album_queue("2026-W31", [_queue_item("Remote Queue", "Older")])
    remote_db.close()
    getter, _ = _remote_get(remote)

    state_sync.merge_remote_state_for_push(local, http_get=getter)

    merged = DB(str(local))
    assert merged.album_queue("2026-W31")[0].artist == "Local Refresh"  # type: ignore[index]
    merged.close()


def test_invalid_download_does_not_replace_local_db(tmp_path: Path) -> None:
    local = tmp_path / "data" / "peel.db"
    local.parent.mkdir()
    _make_db(local, "2026-W31")
    state_sync.mark_local_state_synced(local)
    original = local.read_bytes()

    def get(url: str, **_: object) -> httpx.Response:
        request = httpx.Request("GET", url)
        if url == state_sync.REMOTE_STATE_API_URL:
            return httpx.Response(
                200,
                request=request,
                json={"sha": "different", "download_url": "https://download.example/bad"},
            )
        return httpx.Response(200, request=request, content=b"not sqlite")

    with pytest.raises(state_sync.StateSyncError, match="não é uma DB SQLite"):
        state_sync.sync_remote_state(local, tmp_path, http_get=get)

    assert local.read_bytes() == original


def test_clean_checkout_bootstraps_marker_without_download(tmp_path: Path, monkeypatch) -> None:
    local = tmp_path / "data" / "peel.db"
    local.parent.mkdir()
    _make_db(local, "2026-W32")
    getter, _ = _remote_get(local)
    monkeypatch.setattr(state_sync, "git_db_is_clean", lambda *_: True)

    result = state_sync.sync_remote_state(local, tmp_path, http_get=getter)

    assert result.status == "current"
    assert state_sync.marker_path(local).exists()
