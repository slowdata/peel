from __future__ import annotations

from pathlib import Path

from peel.albums import (
    AlbumMention,
    _album_feedback_quality,
    is_album_url,
    rank_album_recommendations,
    select_album_queue,
    spotify_album_url,
)
from peel.db import DB
from peel.models import AlbumQueueItem
from peel.site_export import build_site_week_payload


def _mention(
    artist: str,
    album: str,
    source_id: str,
    seen_at: str,
    *,
    spotify_album_uri: str | None = None,
    source_url: str | None = None,
) -> AlbumMention:
    return AlbumMention(
        artist=artist,
        album=album,
        artist_key=artist.lower(),
        album_key=album.lower(),
        source_id=source_id,
        source_url=source_url,
        spotify_album_uri=spotify_album_uri,
        seen_at=seen_at,
    )


def test_album_ranking_consensus_beats_single_source_quality() -> None:
    rows = [
        _mention("Artist A", "Album A", "weak-a", "2026-06-10T10:00:00+00:00"),
        _mention("Artist A", "Album A", "weak-b", "2026-06-10T11:00:00+00:00"),
        _mention("Artist B", "Album B", "strong", "2026-06-10T12:00:00+00:00"),
    ]

    ranked = rank_album_recommendations(
        rows,
        source_quality={"strong": (2.0, 100.0), "weak-a": (0.0, 0.0), "weak-b": (0.0, 0.0)},
    )

    assert [(item.artist, item.album) for item in ranked] == [
        ("Artist A", "Album A"),
        ("Artist B", "Album B"),
    ]
    assert ranked[0].source_count == 2


def test_album_ranking_uses_quality_then_recency_as_tie_breakers() -> None:
    rows = [
        _mention("Artist Low", "Album Low", "low", "2026-06-12T12:00:00+00:00"),
        _mention("Artist High", "Album High", "high", "2026-06-11T12:00:00+00:00"),
        _mention("Artist Recent", "Album Recent", "neutral", "2026-06-13T12:00:00+00:00"),
    ]

    ranked = rank_album_recommendations(
        rows,
        source_quality={"high": (1.0, 5.0), "low": (0.0, 0.0), "neutral": (0.0, 0.0)},
    )

    assert [(item.artist, item.album) for item in ranked] == [
        ("Artist High", "Album High"),
        ("Artist Recent", "Album Recent"),
        ("Artist Low", "Album Low"),
    ]


def test_album_ranking_is_deterministic_and_respects_limit() -> None:
    rows = [
        _mention(f"Artist {letter}", f"Album {letter}", "source", "2026-06-10T10:00:00+00:00")
        for letter in "IHGFEDCBA"
    ]

    ranked = rank_album_recommendations(rows, limit=7)

    assert len(ranked) == 7
    assert [(item.artist, item.album) for item in ranked] == [
        ("Artist A", "Album A"),
        ("Artist B", "Album B"),
        ("Artist C", "Album C"),
        ("Artist D", "Album D"),
        ("Artist E", "Album E"),
        ("Artist F", "Album F"),
        ("Artist G", "Album G"),
    ]


def test_album_ranking_propagates_spotify_uri_and_source_urls() -> None:
    rows = [
        _mention(
            "Artist",
            "Album",
            "source-a",
            "2026-06-10T10:00:00+00:00",
            source_url="https://example.com/a",
        ),
        _mention(
            "Artist",
            "Album",
            "source-b",
            "2026-06-10T11:00:00+00:00",
            spotify_album_uri="spotify:album:abc123",
            source_url="https://example.com/b",
        ),
    ]

    item = rank_album_recommendations(rows)[0]

    assert item.spotify_album_uri == "spotify:album:abc123"
    assert item.link_url == "https://open.spotify.com/album/abc123"
    assert item.source_urls == (
        ("source-a", "https://example.com/a"),
        ("source-b", "https://example.com/b"),
    )


def test_legacy_bandcamp_track_url_is_never_ranked_as_album() -> None:
    rows = [
        _mention(
            "Single Artist",
            "Single",
            "bandcamp_label",
            "2026-07-18T10:00:00+00:00",
            source_url="https://label.bandcamp.com/track/single",
        )
    ]
    assert not is_album_url(rows[0].source_url)
    assert rank_album_recommendations(rows) == []


def test_diversity_uses_combined_quality_not_avg_rating_tier() -> None:
    db = DB(":memory:")
    db.init_schema()
    for artist, album, source, seen_at in (
        ("A1", "One", "editorial_a", "2026-07-18T10:00:00+00:00"),
        ("A2", "Two", "editorial_a", "2026-07-18T10:01:00+00:00"),
        ("B", "Three", "editorial_b", "2026-07-18T10:02:00+00:00"),
    ):
        db.conn.execute(
            """
            INSERT INTO album_mentions
            (artist, album, artist_key, album_key, source_id, source_url, spotify_album_uri,
             seen_at, added_at_week, first_seen_at, first_seen_week, last_seen_at, last_seen_week)
            VALUES (?, ?, ?, ?, ?, NULL, NULL, ?, '2026-W29', ?, '2026-W29', ?, '2026-W29')
            """,
            (
                artist,
                album,
                artist.lower(),
                album.lower(),
                source,
                seen_at,
                seen_at,
                seen_at,
            ),
        )
    db.conn.commit()
    selected = select_album_queue(
        db,
        "2026-W29",
        source_quality={"editorial_a": (1.0, 1.0), "editorial_b": (0.0, 0.0)},
        limit=2,
    )
    assert "editorial_b" in {item.sources[0] for item, _ in selected}
    db.close()


def test_fresh_items_fill_from_unrated_pending_and_exclude_rated(tmp_path: Path) -> None:
    db = DB(str(tmp_path / "peel.db"))
    db.init_schema()
    for artist, album, source, seen_at, week in (
        ("Fresh A", "Fresh A", "fresh-a", "2026-07-14T10:00:00+00:00", "2026-W29"),
        ("Fresh B", "Fresh B", "fresh-b", "2026-07-15T10:00:00+00:00", "2026-W29"),
        ("Pending", "Pending", "pending", "2026-07-08T10:00:00+00:00", "2026-W28"),
        ("Rated", "Rated", "rated", "2026-07-08T11:00:00+00:00", "2026-W28"),
    ):
        db._record_album_mention(
            artist=artist,
            album=album,
            source_id=source,
            source_url="https://review.example/album",
            spotify_album_uri=None,
            seen_at=seen_at,
            added_at_week=week,
        )
    db.conn.commit()
    db.upsert_album_feedback("Rated", "Rated", "like")

    selected = select_album_queue(db, "2026-W29", limit=7)

    assert [(item.album, is_new) for item, is_new in selected] == [
        ("Fresh B", True),
        ("Fresh A", True),
        ("Pending", False),
    ]
    db.close()


def test_diversity_prefers_editorial_then_allows_label_after_repeat_penalty(tmp_path: Path) -> None:
    db = DB(str(tmp_path / "peel.db"))
    db.init_schema()
    for artist, album, source, seen_at in (
        ("Editorial First", "Album", "thequietus", "2026-07-18T12:00:00+00:00"),
        ("Editorial Repeat", "Album", "thequietus", "2026-07-18T11:00:00+00:00"),
        ("Label", "Album", "bandcamp_label", "2026-07-18T10:00:00+00:00"),
    ):
        db._record_album_mention(
            artist=artist,
            album=album,
            source_id=source,
            source_url="https://review.example/album",
            spotify_album_uri=None,
            seen_at=seen_at,
            added_at_week="2026-W29",
        )
    db.conn.commit()

    selected = select_album_queue(db, "2026-W29", limit=3)

    # Equal candidates start with the editorial source. Once represented, its
    # soft repeat penalty lets the label win; it is not a source quota/cap.
    assert [item.artist for item, _ in selected] == ["Editorial First", "Label", "Editorial Repeat"]
    db.close()


def test_two_feeds_from_same_publication_are_not_consensus() -> None:
    rows = [
        _mention(
            "Same Artist",
            "Same Album",
            "pitchfork_best_albums",
            "2026-08-08T10:00:00+00:00",
            source_url="https://pitchfork.com/review",
        ),
        _mention(
            "Same Artist",
            "Same Album",
            "pitchfork_album_reviews",
            "2026-08-08T11:00:00+00:00",
            source_url="https://pitchfork.com/review",
        ),
        _mention(
            "Same Artist",
            "Same Album",
            "guardian_music_albums",
            "2026-08-08T12:00:00+00:00",
            source_url="https://guardian.example/review",
        ),
    ]

    item = rank_album_recommendations(rows, limit=1)[0]

    assert item.sources == (
        "guardian_music_albums",
        "pitchfork_album_reviews",
        "pitchfork_best_albums",
    )
    assert item.source_count == 2


def test_unavailable_feedback_is_not_musical_source_quality(tmp_path: Path) -> None:
    db = DB(str(tmp_path / "peel.db"))
    db.init_schema()
    db._record_album_mention(
        artist="Unavailable Artist",
        album="Unavailable Album",
        source_id="source-a",
        source_url="https://review.example/album",
        spotify_album_uri=None,
        seen_at="2026-08-08T10:00:00+00:00",
        added_at_week="2026-W32",
    )
    db.conn.commit()
    db.upsert_album_feedback(
        "Unavailable Artist", "Unavailable Album", "unavailable", "not streaming"
    )

    quality = _album_feedback_quality(db, {"source-a": (1.0, 2.0)})

    assert db.album_feedback_for_identity("Unavailable Artist", "Unavailable Album") == (
        0,
        "unavailable",
        "not streaming",
    )
    assert quality["source-a"] == (1.0, 2.0)
    db.close()


def test_explicit_reissue_title_is_not_queue_eligible() -> None:
    rows = [
        _mention(
            "Museum Of Love",
            "Museum Of Love (10th Anniversary Expanded Edition)",
            "bandcamp_dfa",
            "2026-08-08T10:00:00+00:00",
            source_url="https://museumoflove.bandcamp.com/album/museum-of-love",
        ),
        _mention(
            "Current Artist",
            "Current Album",
            "bandcamp_label",
            "2026-08-08T11:00:00+00:00",
            source_url="https://current.bandcamp.com/album/current-album",
        ),
    ]

    ranked = rank_album_recommendations(rows, limit=7)

    assert [(item.artist, item.album) for item in ranked] == [("Current Artist", "Current Album")]


def test_spotify_album_url_passes_through_non_uri_values() -> None:
    assert spotify_album_url("https://example.com/album") == "https://example.com/album"


def test_repeated_source_keeps_first_seen_and_new_source_is_fresh(tmp_path: Path) -> None:
    db = DB(str(tmp_path / "peel.db"))
    db.init_schema()
    # A static source was first seen in W25 and polled again in W29.
    db._record_album_mention(
        artist="Old",
        album="Static",
        source_id="label",
        source_url="https://x.bandcamp.com/album/a",
        spotify_album_uri=None,
        seen_at="2026-06-15T10:00:00+00:00",
        added_at_week="2026-W25",
    )
    db._record_album_mention(
        artist="Old",
        album="Static",
        source_id="label",
        source_url="https://x.bandcamp.com/album/a",
        spotify_album_uri=None,
        seen_at="2026-07-18T10:00:00+00:00",
        added_at_week="2026-W29",
    )
    # An old album can still be relevant when an independent source newly cites it.
    db._record_album_mention(
        artist="Old",
        album="Static",
        source_id="review",
        source_url="https://review.example/a",
        spotify_album_uri=None,
        seen_at="2026-07-18T11:00:00+00:00",
        added_at_week="2026-W29",
    )
    db.conn.commit()

    row = db.conn.execute(
        "SELECT first_seen_week, last_seen_week FROM album_mentions WHERE source_id = 'label'"
    ).fetchone()
    assert row == ("2026-W25", "2026-W29")
    selected = select_album_queue(db, "2026-W29")
    assert [(item.album, is_new) for item, is_new in selected] == [("Static", True)]
    db.close()


def test_freshness_migration_recovers_canonical_first_observation(tmp_path: Path) -> None:
    db = DB(str(tmp_path / "peel.db"))
    db.init_schema()
    db.conn.execute(
        """
        INSERT INTO albums (artist, album, source_id, source_url, seen_at, added_at_week)
        VALUES ('Artist', 'Album', 'source', NULL, '2026-06-15T10:00:00+00:00', '2026-W25')
        """
    )
    # Legacy SQLite PK is case-sensitive; choose the oldest same-source
    # canonical evidence rather than multiplying rows in a case-insensitive JOIN.
    db.conn.execute(
        """
        INSERT INTO albums (artist, album, source_id, source_url, seen_at, added_at_week)
        VALUES ('artist', 'album', 'source', NULL, '2026-06-08T10:00:00+00:00', '2026-W24')
        """
    )
    db.conn.execute(
        """
        INSERT INTO album_mentions
        (artist, album, artist_key, album_key, source_id, source_url, spotify_album_uri,
         seen_at, added_at_week, first_seen_at, first_seen_week, last_seen_at, last_seen_week)
        VALUES ('Artist', 'Album', 'artist', 'album', 'source', NULL, NULL,
                '2026-07-18T10:00:00+00:00', '2026-W29', NULL, NULL, NULL, NULL)
        """
    )
    db.conn.commit()
    db.init_schema()
    assert db.conn.execute(
        "SELECT first_seen_week, last_seen_week FROM album_mentions"
    ).fetchone() == ("2026-W24", "2026-W29")
    db.close()


def test_unknown_legacy_secondary_source_is_not_fresh(tmp_path: Path) -> None:
    db = DB(str(tmp_path / "peel.db"))
    db.init_schema()
    # This source row was invented by legacy backfill in W29; only the first
    # source has exact historical evidence, so the secondary is uncertain.
    db.conn.execute(
        """INSERT INTO albums (artist, album, source_id, source_url, seen_at, added_at_week)
           VALUES ('Artist', 'Album', 'known', NULL, '2026-06-15T00:00:00+00:00', '2026-W25')"""
    )
    for source in ("known", "unknown"):
        db.conn.execute(
            """INSERT INTO album_mentions
            (artist, album, artist_key, album_key, source_id, source_url, spotify_album_uri,
             seen_at, added_at_week, first_seen_reliable)
            VALUES ('Artist', 'Album', 'artist', 'album', ?, 'https://review.example/a', NULL,
                    '2026-07-18T00:00:00+00:00', '2026-W29', 0)""",
            (source,),
        )
    db.conn.commit()
    db.init_schema()
    selected = select_album_queue(db, "2026-W29")
    assert [(item.album, fresh) for item, fresh in selected] == [("Album", False)]
    db.close()


def test_freshness_migration_proof_repairs_too_early_and_converges(tmp_path: Path) -> None:
    db = DB(str(tmp_path / "peel.db"))
    db.init_schema()
    db.conn.execute(
        """INSERT INTO albums (artist, album, source_id, source_url, seen_at, added_at_week)
           VALUES ('Artist', 'Album', 'source', NULL, '2026-06-15T00:00:00+00:00', '2026-W25')"""
    )
    db.conn.execute(
        """INSERT INTO album_mentions
        (artist, album, artist_key, album_key, source_id, source_url, spotify_album_uri,
         seen_at, added_at_week, first_seen_at, first_seen_week, first_seen_reliable)
        VALUES ('Artist', 'Album', 'artist', 'album', 'source', NULL, NULL,
                '2026-07-18T00:00:00+00:00', '2026-W29',
                '2026-06-01T00:00:00+00:00', '2026-W23', 1)"""
    )
    db.conn.commit()
    db.init_schema()
    assert db.conn.execute(
        "SELECT first_seen_week, first_seen_reliable FROM album_mentions"
    ).fetchone() == ("2026-W25", 1)
    before = db.conn.total_changes
    db.init_schema()
    assert db.conn.total_changes == before
    db.close()


def test_post_migration_new_source_is_reliably_fresh(tmp_path: Path) -> None:
    db = DB(str(tmp_path / "peel.db"))
    db.init_schema()
    db._record_album_mention(
        artist="Old",
        album="Album",
        source_id="old",
        source_url="https://x/old",
        spotify_album_uri=None,
        seen_at="2026-06-15T00:00:00+00:00",
        added_at_week="2026-W25",
    )
    db._record_album_mention(
        artist="Old",
        album="Album",
        source_id="new",
        source_url="https://x/new",
        spotify_album_uri=None,
        seen_at="2026-07-18T00:00:00+00:00",
        added_at_week="2026-W29",
    )
    db.conn.commit()
    assert [(item.album, fresh) for item, fresh in select_album_queue(db, "2026-W29")] == [
        ("Album", True)
    ]
    db.close()


def test_future_album_observation_cannot_enter_historical_selection(tmp_path: Path) -> None:
    db = DB(str(tmp_path / "peel.db"))
    db.init_schema()
    db._record_album_mention(
        artist="Future",
        album="Album",
        source_id="source",
        source_url="https://x/a",
        spotify_album_uri=None,
        seen_at="2026-07-25T00:00:00+00:00",
        added_at_week="2026-W30",
    )
    db.conn.commit()
    assert select_album_queue(db, "2026-W29") == []
    db.close()


def test_album_snapshot_freezes_site_links(tmp_path: Path) -> None:
    db = DB(str(tmp_path / "peel.db"))
    db.init_schema()
    db.replace_album_queue(
        "2026-W29",
        [
            AlbumQueueItem(
                week="2026-W29",
                position=1,
                artist="Artist",
                album="Album",
                artist_key="artist",
                album_key="album",
                source_ids=("source",),
                source_count=1,
                listen_url="https://open.spotify.com/album/frozen",
                listen_kind="spotify",
                editorial_url="https://review.example/a",
                is_new=True,
            )
        ],
    )
    payload = build_site_week_payload(db, "2026-W29", None, source_quality={})
    assert payload["albums"][0]["listen_url"] == "https://open.spotify.com/album/frozen"
    assert payload["albums"][0]["link"] == "https://review.example/a"
    db.close()
