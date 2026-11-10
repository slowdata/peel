"""Track release checks use existing search metadata, not extra API requests."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

from peel.db import DB
from peel.main import (
    SourceRunStats,
    _process_track_items,
    _retry_unmatched,
    select_review_playlist_uris,
)
from peel.models import Track
from peel.novelty import check_track_release, make_track_release_filter, track_release_reason

NOW = datetime(2026, 11, 10, tzinfo=UTC).date()


def _candidate(title="Song", released="2026-11-06", isrc="ISRC1"):
    return {
        "uri": f"spotify:track:{title}",
        "name": title,
        "artists": ["Artist"],
        "release_date": released,
        "isrc": isrc,
    }


def test_track_date_and_explicit_archive_version_are_checked():
    for candidate in [
        _candidate(released="2025-01-01"),
        _candidate(released="2026-11-11"),
        _candidate(released=None),
        _candidate(title="Song - 2026 Remaster"),
    ]:
        assert track_release_reason(candidate, [candidate], NOW)
    fresh = _candidate()
    assert track_release_reason(fresh, [fresh], NOW) is None


def test_same_isrc_prevents_a_new_compilation_from_redating_an_old_recording():
    new_edition = _candidate()
    old_edition = _candidate(released="2020-01-01", isrc="isrc1")
    assert "ISRC" in track_release_reason(new_edition, [new_edition, old_edition], NOW)
    different_recording = _candidate(released="2020-01-01", isrc="different")
    assert track_release_reason(new_edition, [new_edition, different_recording], NOW) is None


def test_a_new_bonus_track_is_not_rejected_only_because_its_parent_is_deluxe():
    fresh = {**_candidate(), "album_name": "Album (Deluxe Edition)"}
    assert track_release_reason(fresh, [fresh], NOW) is None


def test_old_future_and_unconfirmed_tracks_cannot_fill_new_slots(tmp_path):
    db = DB(str(tmp_path / "admission.db"))
    db.init_schema()
    today = datetime.now(UTC).date()
    dates = {
        "Old": "2020-01-01",
        "Future": (today + timedelta(days=1)).isoformat(),
        "Unknown": None,
        "Fresh": today.isoformat(),
    }
    client = MagicMock()
    client.search_track.side_effect = lambda artist, title, limit=5: (
        [_candidate(title, dates[title])] if title in dates else []
    )
    items = [Track(source_id="fixture", artist="Artist", title=t) for t in [*dates, "Missing"]]
    stats = SourceRunStats(source_id="fixture", run_at=datetime.now(UTC).isoformat())
    uris, entries = [], []
    result = _process_track_items(
        db,
        client,
        SimpleNamespace(id="fixture", kind="track"),
        items,
        stats,
        source_slot_caps={"fixture": 10},
        banned_track_keys=set(),
        playlist_slots_used=0,
        new_track_uris=uris,
        new_track_entries=entries,
    )
    assert result == (1, 1, 1)
    assert uris == ["spotify:track:Fresh"]
    assert db.conn.execute("SELECT title FROM tracks").fetchall() == [("Fresh",)]
    assert db.conn.execute("SELECT count(*) FROM feedback").fetchone()[0] == 0
    assert stats.unmatched_count == 1  # run counter: only the missing fuzzy match
    assert len(db.list_unmatched_with_urls(30)) == 3  # future, unknown date, and no match
    assert [call[0] for call in client.method_calls] == ["search_track"] * 5
    db.close()


def test_only_future_or_unconfirmed_releases_are_retryable():
    for released in [None, "2026-11-11", "2026-11"]:
        candidate = _candidate(released=released)
        assert check_track_release(candidate, [candidate], NOW).retryable
    old = _candidate(released="2020-01-01")
    assert not check_track_release(old, [old], NOW).retryable
    archive = _candidate(title="Song - Remastered")
    assert not check_track_release(archive, [archive], NOW).retryable


def test_recent_source_publication_does_not_make_future_article_eligible():
    from peel.main import _filter_fresh_source_items

    now = datetime(2026, 11, 10, 12, tzinfo=UTC)
    item = Track(
        source_id="source", artist="Artist", title="Song", published_at=now + timedelta(days=1)
    )
    assert _filter_fresh_source_items("source", [item], now) == []


def test_pending_metadata_is_read_only_cached_and_fail_closed():
    lookup = MagicMock(
        side_effect=lambda uri: {
            "name": "Song",
            "album": {
                "release_date": {
                    "fresh": "2026-11-06",
                    "old": "2020-01-01",
                    "unknown": None,
                }[uri]
            },
        }
    )
    check = make_track_release_filter(lookup, NOW)
    assert check("fresh") is None and check("fresh") is None
    assert check("old") and check("unknown")
    assert lookup.call_count == 3


def test_backfill_does_not_pad_with_old_or_unconfirmed_pending_tracks():
    def check(uri):
        return {"old": "old recording", "unknown": "no date"}.get(uri)

    selected = select_review_playlist_uris(
        ["old", "new", "fresh", "unknown"],
        ["new"],
        ["new"],
        lambda _uri: True,
        limit=3,
        pending_release_filter=check,
    )
    assert selected == ["new", "fresh"]


def test_pending_lookup_budget_is_bounded_and_zero_when_new_tracks_fill_queue():
    check = MagicMock(return_value="old")
    assert (
        select_review_playlist_uris(
            [str(i) for i in range(100)],
            [],
            [],
            lambda _uri: True,
            limit=1,
            pending_release_filter=check,
        )
        == []
    )
    assert check.call_count == 14
    check.reset_mock()
    assert select_review_playlist_uris(
        ["new", "pending"],
        ["new"],
        ["new"],
        lambda _uri: True,
        limit=1,
        pending_release_filter=check,
    ) == ["new"]
    check.assert_not_called()


def test_retry_cannot_bypass_release_admission(tmp_path):
    db = DB(str(tmp_path / "retry.db"))
    db.init_schema()
    today = datetime.now(UTC).date()
    dates = {
        "Old": "2020-01-01",
        "Future": (today + timedelta(days=1)).isoformat(),
        "Fresh": today.isoformat(),
    }
    for title in dates:
        db.record_unmatched("fixture", "Artist", title, None)
    client = MagicMock()
    client.search_track.side_effect = lambda artist, title, limit=5: [
        _candidate(title, dates[title])
    ]
    entries, uris = [], []
    total, matched = _retry_unmatched(db, client, entries, new_track_uris=uris)
    assert (total, matched) == (3, 1)
    assert uris == ["spotify:track:Fresh"]
    assert len(db.list_unmatched_with_urls(30)) == 2
    # When provider metadata confirms release, the pending item can enter normally.
    dates["Future"] = today.isoformat()
    assert _retry_unmatched(db, client, entries, new_track_uris=uris) == (2, 1)
    assert uris == ["spotify:track:Fresh", "spotify:track:Future"]
    assert db.conn.execute("SELECT count(*) FROM feedback").fetchone()[0] == 0
    db.close()
