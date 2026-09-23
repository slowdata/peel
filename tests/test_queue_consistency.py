from __future__ import annotations

from datetime import UTC, datetime
from importlib import import_module
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from selectolax.parser import HTMLParser

from peel.albums import select_album_queue
from peel.config import settings
from peel.db import DB, iso_week
from peel.listening import archive_report_files
from peel.models import ReviewQueueItem, Track
from peel.report import build_weekly_html_report, build_weekly_report
from peel.site_export import build_site_week_payload, export_site
from peel.spotify_client import SpotifyClient, SpotifyPlaylistMismatch
from peel.state_sync import StateSyncError, _merge_user_state

main = import_module("peel.main")


@pytest.fixture
def db(tmp_path):
    db = DB(str(tmp_path / "test.db"))
    db.init_schema()
    yield db
    db.close()


def item(name, week="2026-W37", *, new=True, origin=None):
    return ReviewQueueItem(
        spotify_uri=f"spotify:track:{name}",
        artist=f"Artist {name}",
        title=f"Title {name}",
        source_id="stereogum_new_music",
        source_url="https://review.example/item",
        source_count=2,
        affinity=0.5,
        is_new=new,
        added_at_week=origin or week,
        current_week=week,
    )


def track(db, name, week, *, source="stereogum_new_music", uri=None):
    db.conn.execute(
        "INSERT INTO tracks VALUES (?, ?, ?, ?, NULL, ?, ?)",
        (
            uri or f"spotify:track:{name}",
            source,
            f"Artist {name}",
            f"Title {name}",
            "2026-09-07T10:00:00+00:00",
            week,
        ),
    )
    db.conn.commit()


def mention(db, name, week, source="clash_album_reviews"):
    db.record_album(f"Artist {name}", f"Album {name}", source, "https://review.example/album")
    db.conn.execute(
        "UPDATE album_mentions SET first_seen_week=?, added_at_week=?, last_seen_week=? "
        "WHERE album_key=? AND source_id=?",
        (week, week, week, f"album {name}", source),
    )
    db.conn.commit()


def test_history_survives_rotation_and_empty_snapshot(db):
    first = [item("b"), item("a", new=False, origin="2026-W35")]
    db.replace_review_queue("review", first)
    db.replace_review_queue("review", [], week="2026-W38")
    assert db.review_queue("review") == []
    assert db.review_queue_snapshot("review", "2026-W37") == first
    assert db.review_queue_snapshot("review", "2026-W38") == []
    assert db.review_queue_snapshot("review", "2026-W36") is None


def test_invalid_queue_does_not_destroy_either_snapshot(db):
    prior = [item("a")]
    db.replace_review_queue("review", prior)
    with pytest.raises(ValueError, match="mixed weeks"):
        db.replace_review_queue("review", [item("a"), item("b", week="2026-W38")])
    with pytest.raises(ValueError, match="duplicate"):
        db.replace_review_queue("review", [item("b"), item("b")])
    assert db.review_queue("review") == prior
    assert db.review_queue_snapshot("review", "2026-W37") == prior


def test_snapshot_and_active_write_rollback_together(db):
    prior = [item("a")]
    db.replace_review_queue("review", prior)
    db.conn.execute(
        "CREATE TRIGGER fail_snapshot BEFORE UPDATE ON review_queue_snapshots "
        "BEGIN SELECT RAISE(ABORT, 'snapshot error'); END"
    )
    import sqlite3

    with pytest.raises(sqlite3.IntegrityError, match="snapshot error"):
        db.replace_review_queue("review", [item("b")])
    assert db.review_queue("review") == prior
    assert db.review_queue_snapshot("review", "2026-W37") == prior


@pytest.mark.parametrize("empty", [True, False])
def test_markdown_html_match_frozen_queue_not_discovery_order(db, monkeypatch, empty):
    monkeypatch.setattr(settings, "peel_review_playlist_id", "review")
    queue = [] if empty else [item("b"), item("a", new=False, origin="2026-W35")]
    for name in ("a", "b", "c"):
        track(db, name, "2026-W37")
    db.replace_album_queue("2026-W37", [])
    db.replace_review_queue("review", queue, week="2026-W37")
    md = build_weekly_report(db, "2026-W37")
    html = build_weekly_html_report(db, "2026-W37")
    triage = md.split("## Triagem", 1)[1].split("## Tracks", 1)[0]
    assert f"{len(queue)} faixas" in triage
    assert "Title c" not in triage
    tree = HTMLParser(html)
    assert [node.attributes["data-uri"] for node in tree.css("article[data-uri]")] == [
        entry.spotify_uri for entry in queue
    ]
    if queue:
        assert triage.index("Title b") < triage.index("Title a")
        assert "pendente 2026-W35" in triage
        assert "1 novas · 1 pendentes" in html
    # Rotating now cannot rebuild a different historical playlist next week.
    db.replace_review_queue("review", [item("c", week="2026-W38")])
    assert build_weekly_report(db, "2026-W37") == md
    assert build_weekly_html_report(db, "2026-W37") == html


def test_discovery_audit_keeps_queue_positions_with_gaps_aliases_and_extras(db, monkeypatch):
    monkeypatch.setattr(settings, "peel_review_playlist_id", "review")
    track(db, "a", "2026-W37", uri="spotify:track:alternate")
    track(db, "b", "2026-W37")
    track(db, "c", "2026-W37")  # discovery deliberately absent from the playlist
    queue = [item("b"), item("pending", new=False, origin="2026-W36"), item("a")]
    db.replace_review_queue("review", queue)
    db.replace_album_queue("2026-W37", [])
    md = build_weekly_report(db, "2026-W37")
    audit = md.split("## Tracks / Descobertas", 1)[1].split("## Albums", 1)[0]
    assert audit.index("**#1** Artist b") < audit.index("**#3** Artist a")
    assert audit.index("Artist a") < audit.index("### Fora da triagem") < audit.index("Artist c")
    assert "**#2**" not in audit
    assert "**#4**" not in audit
    tree = HTMLParser(build_weekly_html_report(db, "2026-W37"))
    rows = tree.css("article[data-discovery-uri]")
    assert [node.attributes["data-discovery-uri"] for node in rows] == [
        "spotify:track:b",
        "spotify:track:alternate",
        "spotify:track:c",
    ]
    assert [node.css_first(".number").text() for node in rows] == ["01", "03", "—"]
    assert "Fora da triagem" in tree.css_first("details").text()


def test_empty_queue_does_not_invent_audit_positions(db, monkeypatch):
    monkeypatch.setattr(settings, "peel_review_playlist_id", "review")
    track(db, "outside", "2026-W37")
    db.replace_review_queue("review", [], week="2026-W37")
    db.replace_album_queue("2026-W37", [])
    md = build_weekly_report(db, "2026-W37")
    assert "### Fora da triagem" in md
    assert "**#1**" not in md
    tree = HTMLParser(build_weekly_html_report(db, "2026-W37"))
    assert tree.css_first("article[data-discovery-uri] .number").text() == "—"


def test_new_report_fails_without_verified_snapshot(db, monkeypatch):
    monkeypatch.setattr(settings, "peel_review_playlist_id", "review")
    db.replace_album_queue("2026-W37", [])
    with pytest.raises(ValueError, match="triagem confirmada"):
        build_weekly_report(db, "2026-W37")
    with pytest.raises(ValueError, match="triagem confirmada"):
        build_weekly_html_report(db, "2026-W37")


def test_archive_preserves_feedback_discoveries_and_blocks_resurrection(db):
    track(db, "old", "2026-W36")
    track(db, "old", "2026-W37", source="pitchfork_news")
    track(db, "old", "2026-W37", source="clash_first_take", uri="spotify:track:variant")
    track(db, "fresh", "2026-W37")
    db.upsert_feedback("spotify:track:old", "love", "preserve")
    mention(db, "old", "2026-W36")
    mention(db, "old", "2026-W37", "pitchfork_album_reviews")
    mention(db, "fresh", "2026-W37")
    db.replace_review_queue("review", [item("old", week="2026-W36")])
    before = {
        table: db.conn.execute(f"SELECT * FROM {table}").fetchall()
        for table in ("tracks", "feedback", "albums", "album_mentions", "album_feedback")
    }
    assert db.archive_listening_weeks("2026-W32", "2026-W36") == "2026-W37"
    assert db.archive_listening_weeks("2026-W32", "2026-W36") == "2026-W37"
    for table, rows in before.items():
        assert db.conn.execute(f"SELECT * FROM {table}").fetchall() == rows
    assert db.conn.execute("SELECT COUNT(*) FROM listening_resets").fetchone()[0] == 1
    assert db.review_queue("review") == []
    assert db.review_queue_snapshot("review", "2026-W36") is not None
    assert db.ranked_tracks_in_window("2026-W37", 4) == ["spotify:track:fresh"]
    # An archived queue is not an album dislike: new consensus may recover the LP.
    assert [x.album for x, _ in select_album_queue(db, "2026-W37")] == ["Album old", "Album fresh"]
    # Point-in-time archive does not rewrite earlier ranking.
    assert db.listening_floor("2026-W36") is None
    assert db.ranked_tracks_in_window("2026-W36", 1) == ["spotify:track:old"]


def test_reset_never_falls_back_to_an_older_album_queue(db):
    db.replace_album_queue("2026-W31", [])
    db.replace_album_queue("2026-W36", [])
    db.archive_listening_weeks("2026-W32", "2026-W36")
    assert db.latest_album_queue() is None
    assert db.album_queue("2026-W31") == []


def test_archive_moves_reports_byte_for_byte_and_refuses_conflicts(db, tmp_path):
    reports = tmp_path / "reports"
    (reports / ".html").mkdir(parents=True)
    (reports / "2026-W31.md").write_bytes(b"published\n")
    (reports / "2026-W36.md").write_bytes(b"historical\n")
    (reports / ".html/2026-W36.html").write_bytes(b"preview\n")
    db.archive_listening_weeks("2026-W32", "2026-W36")
    moved = archive_report_files(db, reports)
    assert len(moved) == 2
    assert (reports / "archive/2026-W36.md").read_bytes() == b"historical\n"
    assert (reports / "archive/.html/2026-W36.html").read_bytes() == b"preview\n"
    assert (reports / "2026-W31.md").read_bytes() == b"published\n"
    assert archive_report_files(db, reports) == []
    (reports / "2026-W36.md").write_bytes(b"different\n")
    with pytest.raises(ValueError, match="Archive conflict"):
        archive_report_files(db, reports)
    assert (reports / "2026-W36.md").read_bytes() == b"different\n"
    assert (reports / "archive/2026-W36.md").read_bytes() == b"historical\n"


def test_archive_cannot_touch_finalized_weeks(db):
    db.replace_finalized_week_tracks("2026-W31", "weekly", [])
    with pytest.raises(ValueError, match="finalized"):
        db.archive_listening_weeks("2026-W31", "2026-W36")
    assert db.conn.execute("SELECT COUNT(*) FROM listening_resets").fetchone()[0] == 0


def test_archived_weeks_are_not_published(db, tmp_path):
    db.archive_listening_weeks("2026-W32", "2026-W36")
    assert export_site(db, tmp_path, 5, "weekly", current_week="2026-W36") == []
    with pytest.raises(ValueError, match="arquivada"):
        build_site_week_payload(db, "2026-W36", None)


def test_remote_merge_cannot_silently_drop_local_reset_or_pipeline(db, tmp_path):
    remote = tmp_path / "remote.db"
    other = DB(str(remote))
    other.init_schema()
    other.close()
    before = remote.read_bytes()
    db.archive_listening_weeks("2026-W32", "2026-W36")
    with pytest.raises(StateSyncError, match="run/reset local"):
        _merge_user_state(remote, Path(db.path), changed_since=None)
    assert remote.read_bytes() == before


@pytest.mark.parametrize("drop_first", [False, True])
def test_real_client_readback_gates_db_history_telegram_and_reports(db, monkeypatch, drop_first):
    week = iso_week(datetime.now(UTC))
    monkeypatch.setattr(settings, "db_path", db.path)
    monkeypatch.setattr(settings, "peel_review_playlist_id", "review")
    monkeypatch.setattr("peel.spotify_client.time.sleep", lambda _: None)
    client = SpotifyClient.__new__(SpotifyClient)
    client.sp = MagicMock()
    stored = []

    def replace(_pid, uris):
        stored[:] = uris[1:] if drop_first else uris

    client.sp.playlist_replace_items.side_effect = replace
    client.sp.playlist_items.side_effect = lambda *_a, **_k: {
        "items": [{"item": {"uri": uri}} for uri in stored],
        "total": len(stored),
        "next": None,
    }
    client.sp.search.return_value = {
        "tracks": {
            "items": [
                {"uri": "spotify:track:fresh", "name": "New", "artists": [{"name": "Artist"}]}
            ]
        }
    }
    source = SimpleNamespace(
        id="stereogum_new_music",
        kind="track",
        fetch=lambda: [Track(source_id="stereogum_new_music", artist="Artist", title="New")],
    )
    monkeypatch.setattr(main, "active_sources", lambda: [source])
    monkeypatch.setattr(main, "SpotifyClient", lambda: client)
    monkeypatch.setattr(main, "select_album_queue", lambda *_a, **_k: [])
    digest = MagicMock()
    monkeypatch.setattr(main, "send_digest", digest)
    if drop_first:
        with pytest.raises(SpotifyPlaylistMismatch):
            main.run()
        digest.assert_not_called()
        assert db.review_queue("review") == []
        assert db.review_queue_snapshot("review", week) is None
        assert db.album_queue(week) is None
    else:
        main.run()
        snapshot = db.review_queue_snapshot("review", week)
        assert (
            stored
            == [x.spotify_uri for x in snapshot]
            == [x.spotify_uri for x in digest.call_args.args[0]]
        )
        html = HTMLParser(build_weekly_html_report(db, week))
        assert stored == [n.attributes["data-uri"] for n in html.css("article[data-uri]")]
    client.sp.playlist_replace_items.assert_called_once()


def test_selection_failure_after_ready_flag_never_clears_spotify(db, monkeypatch):
    monkeypatch.setattr(settings, "db_path", db.path)
    monkeypatch.setattr(settings, "peel_review_playlist_id", "review")
    db.replace_review_queue("review", [item("preserved")])
    client, digest, logger = MagicMock(), MagicMock(), MagicMock()
    monkeypatch.setattr(main, "active_sources", lambda: [])
    monkeypatch.setattr(main, "SpotifyClient", lambda: client)
    monkeypatch.setattr(main, "send_digest", digest)
    monkeypatch.setattr(main, "log", logger)

    def fail_distribution(event, **kwargs):
        if event == "playlist.triage_source_distribution":
            raise RuntimeError("selection/logging failed after ready flag")

    logger.info.side_effect = fail_distribution
    with pytest.raises(RuntimeError, match="Triagem não seleccionada"):
        main.run()
    client.replace_playlist_items.assert_not_called()
    digest.assert_not_called()
    assert db.review_queue("review") == [item("preserved")]
