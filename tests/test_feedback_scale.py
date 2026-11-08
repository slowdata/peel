"""Taste votes are not playback controls; legacy rows remain unmodified."""

from __future__ import annotations

import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest

from peel import cli
from peel.affinity import build_affinity_profile
from peel.db import ALBUM_FEEDBACK_RATINGS, DB, FEEDBACK_RATINGS
from peel.scoring import build_source_overview, build_source_scores
from peel.site_export import _banned_uris
from peel.state_sync import _merge_user_state


@pytest.mark.parametrize("ratings", [FEEDBACK_RATINGS, ALBUM_FEEDBACK_RATINGS])
def test_five_numeric_votes_have_the_same_meaning_for_tracks_and_albums(monkeypatch, ratings):
    answers = iter(["1", "2", "3", "4", "5"])
    monkeypatch.setattr(cli.typer, "prompt", lambda *a, **k: next(answers))
    assert [cli._prompt_rating(ratings=ratings) for _ in range(5)] == [
        "love",
        "like",
        "meh",
        "dislike",
        "hate",
    ]
    assert [ratings[x] for x in cli.RATING_ORDER] == [2, 1, 0, -1, -2]


@pytest.mark.parametrize("value", ["", "s", "skip", " SKIP "])
def test_skip_is_no_vote_even_when_reviewing_an_existing_like(monkeypatch, value):
    def prompt(*args, **kwargs):
        assert kwargs["default"] == ""  # Enter must never imply Like
        return value

    monkeypatch.setattr(cli.typer, "prompt", prompt)
    assert cli._prompt_rating(default="like") is None


@pytest.mark.parametrize("confirmed", [True, False])
def test_ban_requires_confirmation_and_is_not_vote_five(monkeypatch, confirmed):
    answers = iter(["b"] if confirmed else ["ban", "s"])
    monkeypatch.setattr(cli.typer, "prompt", lambda *a, **k: next(answers))

    def confirm(*args, **kwargs):
        assert kwargs["default"] is False
        return confirmed

    monkeypatch.setattr(cli.typer, "confirm", confirm)
    assert cli._prompt_rating() == ("ban" if confirmed else None)


@pytest.mark.parametrize("ratings", [FEEDBACK_RATINGS, ALBUM_FEEDBACK_RATINGS])
def test_unavailable_is_separate_from_the_scale(monkeypatch, ratings):
    monkeypatch.setattr(cli.typer, "prompt", lambda *a, **k: "u")
    assert cli._prompt_rating(ratings=ratings) == "unavailable"


def test_hate_does_not_ban_but_a_legacy_ban_still_does(tmp_path):
    db = DB(str(tmp_path / "feedback.db"))
    db.init_schema()
    try:
        for label in ("dislike", "hate", "skip", "ban"):
            db.record_track(f"spotify:track:{label}", "source", f"Artist {label}", "Song", None)
            db.upsert_feedback(f"spotify:track:{label}", label)
        before = db.conn.execute("SELECT * FROM feedback ORDER BY spotify_uri").fetchall()
        assert db.feedback_for_track("spotify:track:hate")[:2] == (-2, "hate")
        assert not db.is_banned_uri("spotify:track:hate")
        assert db.is_banned_uri("spotify:track:ban")
        assert db._banned_uris() == _banned_uris(db) == {"spotify:track:ban"}
        assert db.banned_track_keys() == {("artist ban", "song")}
        assert db.feedback_for_track("spotify:track:skip")[:2] == (-1, "skip")
        assert db.conn.execute("SELECT * FROM feedback ORDER BY spotify_uri").fetchall() == before
    finally:
        db.close()


def test_affinity_has_a_neutral_middle_and_does_not_treat_blocks_as_dislikes(tmp_path):
    db = DB(str(tmp_path / "affinity.db"))
    db.init_schema()
    try:
        labels = ("love", "like", "meh", "dislike", "hate", "skip", "ban", "unavailable")
        for label in labels:
            db.record_track(f"spotify:track:{label}", "source", label, "Song", None)
            db.upsert_feedback(f"spotify:track:{label}", label)
        # Neutral/control data must not erase an existing artist prior either.
        db.record_track("spotify:track:idles", "source", "IDLES", "Song", None)
        db.upsert_feedback("spotify:track:idles", "meh")
        db.record_track("spotify:track:radiohead", "source", "Radiohead", "Song", None)
        db.upsert_feedback("spotify:track:radiohead", "ban")
        p = build_affinity_profile(db)
        empty = build_affinity_profile()
        assert p.score("love") > p.score("like") > p.score("meh")
        assert p.score("meh") == p.score("unknown") == p.score("unavailable") == p.score("ban")
        assert p.score("meh") > p.score("dislike") > p.score("hate")
        assert p.score("skip") == p.score("dislike")  # historical negative vote
        assert p.score("IDLES") == empty.score("IDLES")
        assert p.score("Radiohead") == empty.score("Radiohead")
    finally:
        db.close()


def test_new_labels_sync_without_rewriting_old_votes(tmp_path):
    local = tmp_path / "local.db"
    remote = tmp_path / "remote.db"
    db = DB(str(local))
    db.init_schema()
    db.upsert_feedback("spotify:track:old", "skip", "original comment")
    old = db.conn.execute("SELECT * FROM feedback").fetchone()
    shutil.copyfile(local, remote)
    db.upsert_feedback("spotify:track:new", "hate", "new opinion")
    db.upsert_album_feedback("Artist", "Album", "dislike")
    _merge_user_state(remote, Path(db.path), changed_since=None)
    other = DB(str(remote))
    try:
        assert other.feedback_for_track("spotify:track:new") == (-2, "hate", "new opinion")
        assert other.album_feedback_for_identity("Artist", "Album")[:2] == (-1, "dislike")
        assert (
            other.conn.execute(
                "SELECT * FROM feedback WHERE spotify_uri='spotify:track:old'"
            ).fetchone()
            == old
        )
        assert not other.is_banned_uri("spotify:track:new")
    finally:
        other.close()
        db.close()


def test_source_quality_ignores_controls_and_keeps_legacy_negative_votes(tmp_path):
    db = DB(str(tmp_path / "sources.db"))
    db.init_schema()
    try:
        for label in ("love", "like", "meh", "dislike", "hate", "skip", "ban", "unavailable"):
            db.record_track(f"spotify:track:{label}", label, "Artist", label, None)
            db.upsert_feedback(f"spotify:track:{label}", label)
            db.record_album("Artist", label, label, f"https://review.example/{label}")
            db.upsert_album_feedback("Artist", label, label)
        scores = {s.source_id: s for s in build_source_scores(db, reference_dt=datetime.now(UTC))}
        for label in ("ban", "unavailable"):
            assert scores[label].rating_count == 0
            assert scores[label].rating_total == 0
            assert scores[label].skipped_count == 0
        for label, numeric in (("meh", 0), ("dislike", -1), ("hate", -2), ("skip", -1)):
            assert scores[label].rating_count == 1
            assert scores[label].avg_rating == numeric
        assert scores["hate"].skipped_count == scores["dislike"].skipped_count == 1
        # Album and track summaries use the same distinction.
        for kind in ("album", "track"):
            overview = {
                s.source_id: s
                for s in build_source_overview(
                    db, [(label, label, kind) for label in ("ban", "unavailable", "hate")]
                )
            }
            assert overview["ban"].rated == overview["unavailable"].rated == 0
            assert overview["hate"].rated == 1
    finally:
        db.close()
