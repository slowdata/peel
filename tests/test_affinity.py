from __future__ import annotations

from pathlib import Path

from peel.affinity import affinity_score, build_affinity_profile
from peel.db import DB, rank_window_uris


def _db(tmp_path: Path) -> DB:
    db = DB(str(tmp_path / "test.db"))
    db.init_schema()
    return db


class TestAffinityScore:
    def test_static_seed_is_deterministic(self) -> None:
        assert affinity_score("Yussef Dayes") == affinity_score("Yussef Dayes")
        assert affinity_score("Yussef Dayes") > affinity_score("Radiohead")
        assert affinity_score("Radiohead") > affinity_score("Unknown Artist")

    def test_static_genre_prior(self) -> None:
        assert affinity_score("Unknown", ["post-punk"]) > affinity_score("Unknown")
        assert affinity_score("Unknown", ["art pop"]) > affinity_score("Unknown")

    def test_feedback_builds_artist_profile(self, tmp_path: Path) -> None:
        db = _db(tmp_path)
        try:
            db.record_track("spotify:track:love", "s", "Artist Love", "A", None)
            db.record_track("spotify:track:like", "s", "Artist Like", "B", None)
            db.record_track("spotify:track:meh", "s", "Artist Meh", "C", None)
            db.record_track("spotify:track:ban", "s", "Artist Ban", "D", None)
            db.upsert_feedback("spotify:track:love", "love")
            db.upsert_feedback("spotify:track:like", "like")
            db.upsert_feedback("spotify:track:meh", "meh")
            db.upsert_feedback("spotify:track:ban", "ban")

            profile = build_affinity_profile(db)

            assert profile.score("Artist Love") > profile.score("Artist Like")
            assert profile.score("Artist Like") > profile.score("Unknown")
            assert profile.score("Artist Meh") < profile.score("Unknown")
            assert profile.score("Artist Ban") < profile.score("Artist Meh")
        finally:
            db.close()

    def test_feedback_overrides_static_seed(self, tmp_path: Path) -> None:
        db = _db(tmp_path)
        try:
            assert affinity_score("IDLES") > 0.5
            db.record_track("spotify:track:idles", "s", "IDLES", "Bad Fit", None)
            db.upsert_feedback("spotify:track:idles", "ban")

            profile = build_affinity_profile(db)

            assert profile.score("IDLES") < 0.5
        finally:
            db.close()

    def test_cached_genres_are_used_for_unrated_artists(self, tmp_path: Path) -> None:
        db = _db(tmp_path)
        try:
            db.upsert_artist_genres("Mystery Band", ["post-punk", "indie rock"])
            profile = build_affinity_profile(db)

            assert profile.score("Mystery Band") > profile.score("Unknown")
        finally:
            db.close()


class TestAffinityRanking:
    def test_affinity_breaks_ties_after_source_quality(self) -> None:
        rows = [
            ("spotify:track:unknown", "Unknown", "New", "s", "2026-06-12T10:00:00+00:00"),
            ("spotify:track:idles", "IDLES", "Old", "s", "2026-06-01T10:00:00+00:00"),
        ]
        profile = build_affinity_profile()

        assert rank_window_uris(rows, affinity_scorer=profile.score) == [
            "spotify:track:idles",
            "spotify:track:unknown",
        ]

    def test_source_quality_stays_primary_over_affinity(self) -> None:
        rows = [
            ("spotify:track:idles", "IDLES", "High Affinity", "bad", "2026-06-12T10:00:00+00:00"),
            (
                "spotify:track:unknown",
                "Unknown",
                "Low Affinity",
                "good",
                "2026-06-12T10:00:00+00:00",
            ),
        ]
        source_quality = {"good": (1.0, 10.0), "bad": (0.0, 0.0)}
        profile = build_affinity_profile()

        assert rank_window_uris(rows, source_quality, profile.score) == [
            "spotify:track:unknown",
            "spotify:track:idles",
        ]
