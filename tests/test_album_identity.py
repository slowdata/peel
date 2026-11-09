"""Album aliases share recommendations and votes without rewriting history."""

from __future__ import annotations

from pathlib import Path

import pytest

from peel.albums import _album_feedback_quality, rank_album_recommendations, select_album_queue
from peel.db import DB
from peel.scoring import build_source_overview

ALBUM = "Dopamine Chamber"
SOURCES = (
    "diy_album_reviews",
    "beats_per_minute_album_reviews",
    "clash_album_reviews",
    "guardian_music_albums",
    "pitchfork_album_reviews",
)


@pytest.fixture
def db() -> DB:
    database = DB(":memory:")
    database.init_schema()
    yield database
    database.close()


def _mention(db: DB, artist: str, source: str, *, week: int = 44) -> None:
    db._record_album_mention(
        artist=artist,
        album=ALBUM,
        source_id=source,
        source_url=f"https://reviews.example/{source}",
        spotify_album_uri=None,
        seen_at=f"2026-10-{'30' if week == 44 else '23'}T10:00:00+00:00",
        added_at_week=f"2026-W{week}",
    )
    db.conn.commit()


def _old_feedback(
    db: DB, artist_key: str, label: str, rated_at: str, comment: str = "Original comment"
) -> None:
    from peel.db import ALBUM_FEEDBACK_RATINGS

    db.conn.execute(
        "INSERT INTO album_feedback VALUES (?, ?, ?, ?, ?, ?)",
        (artist_key, ALBUM.lower(), ALBUM_FEEDBACK_RATINGS[label], label, comment, rated_at),
    )
    db.conn.commit()


def _five_sources(db: DB) -> None:
    for i, source in enumerate(SOURCES):
        _mention(db, "Fontaines D.C." if i < 3 else "Fontaines DC", source)


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Fontaines D.C.", "fontaines dc"),
        ("Fontaines DC", "fontaines dc"),
        ("Fontaines Dc", "fontaines dc"),
        ("R.E.M.", "rem"),
        ("M.I.A", "mia"),
        ("J. Cole", "j. cole"),
        ("Mr. Bungle", "mr. bungle"),
        ("@", "@"),
        ("!!!", "!!!"),
        ("AC/DC", "ac/dc"),
    ],
)
def test_artist_identity_key_only_folds_dotted_initialisms(name: str, expected: str) -> None:
    from peel.matcher import artist_identity_key

    assert artist_identity_key(name) == expected
    assert artist_identity_key(artist_identity_key(name)) == expected


def test_old_mentions_aggregate_five_sources_without_storage_migration(db: DB) -> None:
    from peel.albums import _load_all_album_mentions

    _five_sources(db)
    before = db.conn.execute(
        "SELECT * FROM album_mentions ORDER BY artist_key, source_id"
    ).fetchall()
    rows = _load_all_album_mentions(db, through_week="2026-W44")
    for mentions in (rows, list(reversed(rows))):
        picks = rank_album_recommendations(mentions)
        assert len(picks) == 1
        assert picks[0].artist == "Fontaines D.C."
        assert picks[0].album == ALBUM
        assert picks[0].source_count == 5
        assert set(picks[0].sources) == set(SOURCES)
    assert (
        db.conn.execute("SELECT * FROM album_mentions ORDER BY artist_key, source_id").fetchall()
        == before
    )


def test_old_love_excludes_every_alias_and_preserves_the_row(db: DB) -> None:
    _five_sources(db)
    _old_feedback(db, "fontaines d.c.", "love", "2026-10-31T10:00:00+00:00")
    before = db.conn.execute("SELECT * FROM album_feedback").fetchall()
    for artist in ("Fontaines D.C.", "Fontaines DC", "Fontaines Dc"):
        assert db.album_feedback_for_identity(artist, ALBUM) == (2, "love", "Original comment")
    assert select_album_queue(db, "2026-W44") == []
    assert db.conn.execute("SELECT * FROM album_feedback").fetchall() == before


def test_explicit_rerating_through_alias_reuses_existing_feedback_key(db: DB) -> None:
    _old_feedback(db, "fontaines d.c.", "love", "2026-10-31T10:00:00+00:00")
    db.upsert_album_feedback("Fontaines DC", ALBUM, "like", "Explicit revision")
    rows = db.conn.execute("SELECT artist_key, label, comment FROM album_feedback").fetchall()
    assert rows == [("fontaines d.c.", "like", "Explicit revision")]
    assert db.album_feedback_for_identity("Fontaines D.C.", ALBUM) == (
        1,
        "like",
        "Explicit revision",
    )


def test_aliases_do_not_cross_album_titles_or_symbol_names(db: DB) -> None:
    _old_feedback(db, "fontaines d.c.", "love", "2026-10-31T10:00:00+00:00")
    assert db.album_feedback_for_identity("Fontaines DC", "Another Album") is None
    db.upsert_album_feedback("@", "Shared Title", "hate")
    assert db.album_feedback_for_identity("!!!", "Shared Title") is None


def test_alias_conflicts_use_latest_opinion_without_deleting_either_row(db: DB) -> None:
    _five_sources(db)
    _old_feedback(db, "fontaines d.c.", "love", "2026-10-30T10:00:00+00:00")
    _old_feedback(db, "fontaines dc", "dislike", "2026-10-31T10:00:00+00:00", "Later opinion")
    before = db.conn.execute("SELECT * FROM album_feedback ORDER BY artist_key").fetchall()
    for artist in ("Fontaines D.C.", "Fontaines DC"):
        assert db.album_feedback_for_identity(artist, ALBUM) == (-1, "dislike", "Later opinion")
    quality = _album_feedback_quality(db, {})
    assert {source: quality[source][0] for source in SOURCES} == dict.fromkeys(SOURCES, -0.25)
    assert db.conn.execute("SELECT * FROM album_feedback ORDER BY artist_key").fetchall() == before


@pytest.mark.parametrize("label", ["ban", "unavailable"])
def test_latest_control_excludes_all_aliases_from_musical_quality(db: DB, label: str) -> None:
    _five_sources(db)
    _old_feedback(db, "fontaines d.c.", "love", "2026-10-30T10:00:00+00:00")
    _old_feedback(db, "fontaines dc", label, "2026-10-31T10:00:00+00:00")
    assert _album_feedback_quality(db, {}) == {}
    assert select_album_queue(db, "2026-W44") == []


def test_alias_feedback_credits_every_source_once_and_keeps_window(db: DB) -> None:
    _five_sources(db)
    # Legacy duplicate of one source under another spelling must not double its vote.
    db.conn.execute(
        "INSERT INTO album_mentions SELECT 'Fontaines DC', album, 'fontaines dc', album_key, "
        "source_id, source_url, spotify_album_uri, seen_at, added_at_week, "
        "first_seen_at, first_seen_week, last_seen_at, last_seen_week, first_seen_reliable "
        "FROM album_mentions WHERE source_id = ?",
        (SOURCES[0],),
    )
    db.conn.commit()
    _old_feedback(db, "fontaines d.c.", "love", "2026-10-31T10:00:00+00:00")
    quality = _album_feedback_quality(db, {})
    assert {source: quality[source][0] for source in SOURCES} == dict.fromkeys(SOURCES, 0.5)
    from datetime import UTC, datetime

    overview = build_source_overview(
        db,
        [(source, source, "album") for source in SOURCES],
        weeks=1,
        reference_dt=datetime(2026, 10, 30, tzinfo=UTC),
    )
    assert [(row.rated, row.positive) for row in overview] == [(1, 1)] * 5
    previous = build_source_overview(
        db,
        [(SOURCES[0], "DIY", "album")],
        weeks=1,
        reference_dt=datetime(2026, 10, 23, tzinfo=UTC),
    )
    assert previous[0].rated == 0


def test_alias_poll_does_not_create_new_discovery_or_reset_freshness(db: DB) -> None:
    assert db.record_album("Fontaines D.C.", ALBUM, SOURCES[0], None)
    first = db.conn.execute("SELECT first_seen_at, first_seen_week FROM album_mentions").fetchone()
    assert not db.record_album("Fontaines DC", ALBUM, SOURCES[0], None)
    db.init_schema()
    assert db.conn.execute("SELECT COUNT(*) FROM albums").fetchone()[0] == 1
    assert db.conn.execute("SELECT COUNT(*) FROM album_mentions").fetchone()[0] == 1
    assert (
        db.conn.execute("SELECT first_seen_at, first_seen_week FROM album_mentions").fetchone()
        == first
    )


def test_legacy_alias_duplicate_does_not_make_old_source_fresh(db: DB) -> None:
    _mention(db, "Fontaines D.C.", SOURCES[0], week=43)
    # Reproduce old storage directly: new ingestion now reuses the first key.
    db.conn.execute(
        "INSERT INTO album_mentions "
        "(artist, album, artist_key, album_key, source_id, seen_at, added_at_week, "
        "first_seen_at, first_seen_week, last_seen_at, last_seen_week, first_seen_reliable) "
        "VALUES ('Fontaines DC', ?, 'fontaines dc', ?, ?, "
        "'2026-10-30T10:00:00+00:00', '2026-W44', "
        "'2026-10-30T10:00:00+00:00', '2026-W44', "
        "'2026-10-30T10:00:00+00:00', '2026-W44', 1)",
        (ALBUM, ALBUM.lower(), SOURCES[0]),
    )
    db.conn.commit()
    selected = select_album_queue(db, "2026-W44")
    assert len(selected) == 1
    assert selected[0][1] is False
    assert selected[0][0].newest_source_week == "2026-W43"
    _old_feedback(db, "fontaines d.c.", "love", "2026-10-31T10:00:00+00:00")
    assert db.album_source_feedback(start="2026-10-26T00:00:00+00:00") == []


def test_state_merge_keeps_alias_votes_and_uses_latest(tmp_path: Path) -> None:
    from peel.state_sync import _merge_user_state

    remote_path, local_path = tmp_path / "remote.db", tmp_path / "local.db"
    remote, local = DB(str(remote_path)), DB(str(local_path))
    for database in (remote, local):
        database.init_schema()
    _five_sources(remote)
    _old_feedback(remote, "fontaines d.c.", "love", "2026-10-30T10:00:00+00:00")
    _old_feedback(local, "fontaines dc", "like", "2026-10-31T10:00:00+00:00", "Local revision")
    remote.close()
    local.close()
    _merge_user_state(remote_path, local_path, changed_since=None)
    merged = DB(str(remote_path))
    assert merged.conn.execute("SELECT COUNT(*) FROM album_feedback").fetchone()[0] == 2
    assert merged.album_feedback_for_identity("Fontaines D.C.", ALBUM) == (
        1,
        "like",
        "Local revision",
    )
    assert select_album_queue(merged, "2026-W44") == []
    merged.close()


def test_reading_aliases_does_not_change_frozen_album_queue(db: DB) -> None:
    from peel.models import AlbumQueueItem

    frozen = AlbumQueueItem(
        week="2026-W42",
        position=1,
        artist="Fontaines D.C.",
        album=ALBUM,
        artist_key="fontaines d.c.",
        album_key=ALBUM.lower(),
        source_ids=SOURCES[:3],
        source_count=3,
        listen_url="https://open.spotify.com/album/frozen",
        listen_kind="spotify",
        editorial_url="https://reviews.example/original",
        is_new=True,
    )
    db.replace_album_queue("2026-W42", [frozen])
    _five_sources(db)
    _old_feedback(db, "fontaines d.c.", "love", "2026-10-31T10:00:00+00:00")
    assert select_album_queue(db, "2026-W44") == []
    _album_feedback_quality(db, {})
    assert db.album_queue("2026-W42") == [frozen]


def test_alias_recommendation_reuses_legacy_cached_listen_url(db: DB) -> None:
    from peel.main import _album_queue_snapshot_items

    _five_sources(db)
    selected = select_album_queue(db, "2026-W44")

    def no_resolution(*_: str) -> None:
        raise AssertionError("A name alias must not force a Spotify lookup")

    items = _album_queue_snapshot_items(
        "2026-W44",
        selected,
        no_resolution,
        cached_listen_urls={
            ("fontaines d.c.", ALBUM.lower()): ("https://open.spotify.com/album/frozen", "spotify")
        },
    )
    assert len(items) == 1
    assert items[0].listen_url == "https://open.spotify.com/album/frozen"
    assert items[0].source_count == 5


def test_artist_alias_uses_existing_track_feedback_and_genre_cache(db: DB) -> None:
    from peel.affinity import build_affinity_profile

    db.conn.execute(
        "INSERT INTO tracks (spotify_uri, source_id, artist, title, added_at, added_at_week) "
        "VALUES ('spotify:track:test', 'source', 'Fontaines D.C.', 'Track', "
        "'2026-10-23T10:00:00+00:00', '2026-W43')"
    )
    db.conn.commit()
    db.upsert_feedback("spotify:track:test", "love", "Preserved")
    db.upsert_artist_genres("Fontaines D.C.", ["post-punk"])
    db.upsert_artist_genres("Fontaines DC", ["indie rock"])
    profile = build_affinity_profile(db)
    assert profile.score("Fontaines DC") == profile.score("Fontaines D.C.")
    assert profile.score("Fontaines DC") > 0.7
    assert set(profile.artist_genres["fontaines dc"]) == {"post-punk", "indie rock"}
    assert db.feedback_for_track("spotify:track:test") == (2, "love", "Preserved")


def test_equal_timestamp_alias_block_is_deterministic_but_hate_is_not_a_block(db: DB) -> None:
    date = "2026-10-31T10:00:00+00:00"
    _old_feedback(db, "fontaines d.c.", "love", date)
    _old_feedback(db, "fontaines dc", "ban", date)
    assert db.album_feedback_for_identity("Fontaines D.C.", ALBUM)[1] == "ban"
    db.conn.execute("UPDATE album_feedback SET label = 'hate' WHERE artist_key = 'fontaines dc'")
    db.conn.commit()
    assert db.album_feedback_for_identity("Fontaines D.C.", ALBUM)[1] != "ban"


def test_later_fractional_second_vote_supersedes_an_older_alias_block(db: DB) -> None:
    _five_sources(db)
    # SQLite's julianday rounds fractions; the original UTC timestamp must
    # break that tie before any block preference is applied.
    _old_feedback(db, "fontaines dc", "ban", "2026-10-31T10:00:00.000100+00:00")
    _old_feedback(db, "fontaines d.c.", "like", "2026-10-31T10:00:00.000200+00:00")
    assert db.album_feedback_for_identity("Fontaines DC", ALBUM)[1] == "like"
    assert {value[0] for value in _album_feedback_quality(db, {}).values()} == {0.25}
