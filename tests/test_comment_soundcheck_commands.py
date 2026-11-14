from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

import peel.cli as cli
from peel.comment_polish import FeedbackSession, album_comment, track_comment, week_comments
from peel.db import DB


@pytest.fixture
def state(tmp_path, monkeypatch):
    path = tmp_path / "state.db"
    db = DB(str(path))
    db.init_schema()
    db.upsert_feedback("selected", "love", "Selected comment")
    db.upsert_feedback("other", "meh", "Unrelated comment")
    db.upsert_album_feedback("Artist", "Album", "dislike", "Album comment")
    monkeypatch.setattr(
        cli,
        "settings",
        SimpleNamespace(
            db_path=str(path), peel_review_playlist_id="review", peel_playlist_id="public"
        ),
    )
    yield db, path
    db.close()


def test_explicit_week_review_deduplicates_and_excludes_unrelated_opinions(state, monkeypatch):
    db, _ = state
    monkeypatch.setattr(
        db,
        "review_queue_snapshot",
        lambda playlist, week: [
            SimpleNamespace(spotify_uri="selected"),
            SimpleNamespace(spotify_uri="selected"),
        ],
    )
    monkeypatch.setattr(
        db, "album_queue", lambda week: [SimpleNamespace(artist="Artist", album="Album")]
    )
    refs = week_comments(db, "review", "2026-W46")
    assert len(refs) == 2
    assert {r.comment for r in refs} == {"Selected comment", "Album comment"}


def test_polish_command_uses_readonly_existing_state_without_sync(state, monkeypatch):
    db, path = state
    monkeypatch.setattr(cli, "_comment_interactive", lambda: True)
    monkeypatch.setattr(cli, "_auto_sync_state", MagicMock(side_effect=AssertionError))
    monkeypatch.setattr(cli, "_push_state", MagicMock(side_effect=AssertionError))
    seen = []

    def select(opened, playlist, week):
        assert opened.conn.execute("PRAGMA query_only").fetchone()[0] == 1
        assert week == "2026-W46" and playlist == "review"
        return [track_comment(opened, "selected")]

    monkeypatch.setattr(cli.comment_polish, "week_comments", select)
    monkeypatch.setattr(cli, "_comment_soundcheck", lambda *args, **kwargs: seen.append(args) or 0)
    before = path.read_bytes()
    result = CliRunner().invoke(cli.app, ["comments", "polish", "--week", "2026-W46"])
    assert result.exit_code == 0, result.output
    assert len(seen) == 1 and seen[0][1][0].comment == "Selected comment"
    assert path.read_bytes() == before
    cli._push_state.assert_not_called()


def test_polish_does_not_create_database_even_with_explicit_week(tmp_path, monkeypatch):
    path = tmp_path / "missing.db"
    monkeypatch.setattr(cli, "settings", SimpleNamespace(db_path=str(path)))
    monkeypatch.setattr(cli, "_comment_interactive", lambda: True)
    result = CliRunner().invoke(cli.app, ["comments", "polish", "--week", "2026-W46"])
    assert result.exit_code == 2
    assert not path.exists()


def test_no_pending_comments_means_no_soundcheck_or_push(state, monkeypatch):
    db, path = state
    monkeypatch.setattr(cli, "_auto_sync_state", lambda *a: None)
    monkeypatch.setattr(cli, "_rate_unrated", lambda session, week: 0)
    monkeypatch.setattr(cli, "_comment_soundcheck", MagicMock(side_effect=AssertionError))
    monkeypatch.setattr(cli, "_push_state", MagicMock(side_effect=AssertionError))
    before = db.conn.execute("SELECT * FROM feedback").fetchall()
    result = CliRunner().invoke(cli.app, ["ouvir"])
    assert result.exit_code == 0
    cli._comment_soundcheck.assert_not_called()
    cli._push_state.assert_not_called()
    assert db.conn.execute("SELECT * FROM feedback").fetchall() == before


def test_album_rating_only_revision_is_not_permission_to_polish_old_comment(state):
    db, _ = state
    session = FeedbackSession(db)
    session.upsert_album_feedback("Artist", "Album", "like", "Album comment")
    assert not session.comments
    assert album_comment(db, "Artist", "Album").label == "like"


def test_setup_opt_out_never_downloads_or_opens_database(tmp_path, monkeypatch):
    monkeypatch.setattr(cli.language_tool, "available", lambda: False)
    monkeypatch.setattr(cli.shutil, "which", lambda *a: "/synthetic/java")
    monkeypatch.setattr(cli.language_tool, "install", MagicMock(side_effect=AssertionError))
    result = CliRunner().invoke(cli.app, ["comments", "setup"], input="n\n")
    assert result.exit_code == 0, result.output
    cli.language_tool.install.assert_not_called()


def test_setup_without_java_is_explicit_and_does_not_download(monkeypatch):
    monkeypatch.setattr(cli.language_tool, "available", lambda: False)
    monkeypatch.setattr(cli.shutil, "which", lambda *a: None)
    monkeypatch.setattr(cli.language_tool, "install", MagicMock(side_effect=AssertionError))
    result = CliRunner().invoke(cli.app, ["comments", "setup", "--yes"])
    assert result.exit_code == 2
    cli.language_tool.install.assert_not_called()


def test_undo_requires_interactive_explicit_confirmation(state, tmp_path, monkeypatch):
    backup = tmp_path / "comments.json"
    backup.write_text("{}")
    monkeypatch.setattr(cli, "_comment_interactive", lambda: False)
    monkeypatch.setattr(cli.comment_polish, "undo_comments", MagicMock(side_effect=AssertionError))
    result = CliRunner().invoke(cli.app, ["comments", "undo", str(backup)])
    assert result.exit_code == 2
    monkeypatch.setattr(cli, "_comment_interactive", lambda: True)
    result = CliRunner().invoke(cli.app, ["comments", "undo", str(backup)], input="n\n")
    assert result.exit_code == 0
    cli.comment_polish.undo_comments.assert_not_called()
