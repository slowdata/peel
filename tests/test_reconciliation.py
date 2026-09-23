from pathlib import Path

import pytest

from peel.db import DB
from peel.main import SourceRunStats
from peel.reconciliation import merge_discovery_history

EARLY = "2026-09-07T17:00:00+00:00"
LATE = "2026-09-18T20:00:00+00:00"


@pytest.fixture
def pair(tmp_path):
    local, candidate = (DB(str(tmp_path / f"{name}.db")) for name in ("local", "candidate"))
    for db in (local, candidate):
        db.init_schema()
    yield local, candidate
    local.close()
    candidate.close()


def add_track(db, uri, at, week):
    db.conn.execute(
        "INSERT INTO tracks VALUES (?, 'source', 'Artist', 'Title', NULL, ?, ?)",
        (uri, at, week),
    )
    db.conn.commit()


def test_union_keeps_earliest_track_and_both_independent_runs(pair):
    local, candidate = pair
    add_track(local, "shared", EARLY, "2026-W37")
    add_track(local, "local-only", EARLY, "2026-W37")
    add_track(candidate, "shared", LATE, "2026-W38")
    add_track(candidate, "remote-only", LATE, "2026-W38")
    SourceRunStats("source", EARLY).record(local, "ok")
    SourceRunStats("source", LATE).record(candidate, "ok")
    before = Path(local.path).read_bytes()
    audit = merge_discovery_history(candidate, Path(local.path))
    rows = dict(candidate.conn.execute("SELECT spotify_uri,added_at_week FROM tracks"))
    assert rows == {"shared": "2026-W37", "local-only": "2026-W37", "remote-only": "2026-W38"}
    assert candidate.conn.execute("SELECT COUNT(*) FROM source_runs").fetchone()[0] == 2
    assert audit["counts"]["tracks"] == {"inserted": 1, "updated": 1}
    assert audit["conflicts"][0]["remote"]["added_at"] == LATE
    assert Path(local.path).read_bytes() == before
    second = merge_discovery_history(candidate, Path(local.path))
    assert all(c == {"inserted": 0, "updated": 0} for c in second["counts"].values())


def test_album_first_and_last_are_independent_and_feedback_is_preserved(pair):
    local, candidate = pair
    for db, at, week in ((local, EARLY, "2026-W37"), (candidate, LATE, "2026-W38")):
        db.record_album("Artist", "Album", "source", "https://example.com/review")
        db.conn.execute("UPDATE albums SET seen_at=?,added_at_week=?", (at, week))
        db.conn.execute(
            "UPDATE album_mentions SET seen_at=?, added_at_week=?, first_seen_at=?, "
            "first_seen_week=?, last_seen_at=?, last_seen_week=?",
            (at, week, at, week, at, week),
        )
        db.upsert_feedback("track", "love" if db is local else "like")
        db.conn.execute("UPDATE feedback SET rated_at=?", (LATE if db is local else EARLY,))
        db.conn.commit()
    merge_discovery_history(candidate, Path(local.path))
    assert candidate.conn.execute(
        "SELECT first_seen_at,first_seen_week,last_seen_at,last_seen_week FROM album_mentions"
    ).fetchone() == (EARLY, "2026-W37", LATE, "2026-W38")
    assert candidate.feedback_for_track("track")[1] == "love"
    before = Path(candidate.path).read_bytes()
    candidate.init_schema()
    assert Path(candidate.path).read_bytes() == before  # migration does not undo earliest evidence


def test_conflict_rolls_back_every_table_and_does_not_choose_a_queue(pair):
    local, candidate = pair
    add_track(local, "local-only", EARLY, "2026-W37")
    local.replace_review_queue("playlist", [], week="2026-W37")
    for db, rating in ((local, "love"), (candidate, "skip")):
        db.upsert_feedback("track", rating)
        db.conn.execute("UPDATE feedback SET rated_at=?", (EARLY,))
        db.conn.commit()
    with pytest.raises(ValueError, match="same timestamp"):
        merge_discovery_history(candidate, Path(local.path))
    assert candidate.conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0] == 0
    assert candidate.feedback_for_track("track")[1] == "skip"
    assert candidate.review_queue_snapshot("playlist", "2026-W37") is None


def test_published_recovery_does_not_block_later_feedback_sync(pair):
    from datetime import UTC, datetime

    from peel.state_sync import _merge_user_state

    local, remote = pair
    local.archive_listening_weeks("2026-W32", "2026-W35")
    local.replace_review_queue("playlist", [], week="2026-W38")
    local.conn.execute(
        "INSERT INTO queue_recoveries VALUES (?, ?, ?, ?)",
        ("2026-W38", EARLY, "Reconciled", "{}"),
    )
    local.conn.commit()
    local.conn.backup(remote.conn)
    marker_time = datetime.now(UTC).isoformat()
    remote.replace_review_queue("playlist", [], week="2026-W39")
    local.upsert_feedback("track", "love", "unsent feedback")
    _merge_user_state(Path(remote.path), Path(local.path), changed_since=marker_time)
    assert remote.active_review_week("playlist") == "2026-W39"
    assert remote.feedback_for_track("track") == (2, "love", "unsent feedback")
    assert remote.review_queue_snapshot("playlist", "2026-W38") == []
    assert remote.queue_recovery_note("2026-W38") == "Reconciled"


def test_same_path_is_rejected(pair):
    local, _ = pair
    with pytest.raises(ValueError, match="separate disposable"):
        merge_discovery_history(local, Path(local.path))
