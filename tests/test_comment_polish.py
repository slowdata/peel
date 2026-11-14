from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

import peel.cli as cli
import peel.comment_soundcheck as ui
from peel.comment_polish import (
    CommentConflict,
    CommentRef,
    FeedbackSession,
    album_comment,
    apply_comments,
    suggest,
    track_comment,
    undo_comments,
)
from peel.db import DB


@pytest.fixture
def db(tmp_path):
    instance = DB(str(tmp_path / "test.db"))
    instance.init_schema()
    instance.conn.execute(
        "INSERT INTO tracks (spotify_uri,source_id,artist,title,added_at,added_at_week) "
        "VALUES ('spotify:track:test','test','PISS','Test Track','2026-11-14','2026-W46')"
    )
    instance.conn.commit()
    yield instance
    instance.close()


def ref(text="Nao gosto desta musica.", **kwargs):
    return CommentRef(
        "track", ("test",), "PISS", "Test Track", 1, "like", "synthetic", text, **kwargs
    )


def match(text, old, new, *, category="TYPOS", choices=None):
    index = text.index(old)
    return {
        "offset": len(text[:index].encode("utf-16-le")) // 2,
        "length": len(old.encode("utf-16-le")) // 2,
        "replacements": [{"value": x} for x in (choices or [new])],
        "rule": {"category": {"id": category}},
    }


def test_session_records_only_this_process_changed_nonempty_comments(db):
    db.upsert_feedback("spotify:track:older", "love", "Comentário antigo")
    db.upsert_feedback("spotify:track:test", "like", "Igual")
    session = FeedbackSession(db)
    session.upsert_feedback("spotify:track:test", "love", "Igual")
    assert not session.comments  # A changed rating is not permission to edit old text.
    session.upsert_feedback("spotify:track:test", "love", "Novo texto")
    session.upsert_album_feedback("Artist", "Album", "meh", "Outro texto")
    assert len(session.comments) == 2
    assert {r.comment for r in session.comments.values()} == {"Novo texto", "Outro texto"}
    assert db.feedback_for_track("spotify:track:older")[2] == "Comentário antigo"


def test_session_latest_comment_restored_or_cleared_is_not_reviewed(db):
    db.upsert_feedback("spotify:track:test", "like", "Original")
    session = FeedbackSession(db)
    session.upsert_feedback("spotify:track:test", "like", "Novo")
    session.upsert_feedback("spotify:track:test", "like", "Original")
    assert not session.comments
    session.upsert_feedback("spotify:track:test", "like", "Outro")
    session.upsert_feedback("spotify:track:test", "like", None)
    assert not session.comments


def test_album_alias_uses_real_stored_key_and_clear_removes_capture(db):
    db.upsert_album_feedback("Fontaines DC", "Album", "like", "Antes")
    session = FeedbackSession(db)
    session.upsert_album_feedback("Fontaines D.C.", "Album", "like", "Novo")
    item = next(iter(session.comments.values()))
    assert item.key == ("fontaines dc", "album")
    assert item.artist == "Fontaines D.C."
    session.upsert_album_feedback("Fontaines D.C.", "Album", "like", None)
    assert not session.comments
    assert db.conn.execute("SELECT COUNT(*) FROM album_feedback").fetchone()[0] == 1


def test_capture_failure_never_prevents_saving_vote(db, monkeypatch):
    session = FeedbackSession(db)
    monkeypatch.setattr(db, "feedback_for_track", MagicMock(side_effect=RuntimeError))
    session.upsert_feedback("spotify:track:test", "hate", "Saved despite failure")
    assert session.capture_failed
    assert db.conn.execute("SELECT label,comment FROM feedback").fetchone() == (
        "hate",
        "Saved despite failure",
    )
    assert not session.comments


@pytest.mark.parametrize("category", ["STYLE", "REDUNDANCY", "SEMANTICS", "FORMAL"])
def test_style_and_formality_rewrites_are_not_applied(category):
    item = ref("Esta merda é boa.")
    assert (
        suggest(item, [match(item.comment, "merda", "música", category=category)]).after
        == item.comment
    )


@pytest.mark.parametrize(
    "term",
    [
        "PISS",
        "Test Track",
        "groove",
        "love",
        "meh",
        "beat",
        "bué",
        "ahahah",
        "!!!",
        "...",
        "talvez",
    ],
)
def test_preserves_names_musical_terms_laughter_and_enthusiasm(term):
    item = ref(f"Gosto de {term} a sério.")
    result = suggest(item, [match(item.comment, term, "outra coisa")])
    assert result.after == item.comment


def test_punctuation_can_change_around_unchanged_protected_terms():
    item = ref("É love mas esta merda é boa.")
    assert (
        suggest(item, [match(item.comment, "love mas", "love, mas")]).after
        == "É love, mas esta merda é boa."
    )
    assert suggest(item, [match(item.comment, "love", "lovely")]).after == item.comment


@pytest.mark.parametrize(
    ("before", "old", "after"),
    [
        ("Gosto disto.", "Gosto", "Não gosto"),
        ("Não gosto disto.", "Não gosto", "Gosto"),
        ("Nunca gostei disto.", "Nunca gostei", "Gostei"),
        ("Sem grande brilho.", "Sem", "Com"),
    ],
)
def test_never_infers_or_changes_negation(before, old, after):
    result = suggest(ref(before), [match(before, old, after)])
    assert result.after == before
    assert result.needs_review


def test_accent_repair_of_negation_is_not_a_semantic_change():
    text = "Nao gosto da musica."
    result = suggest(ref(text), [match(text, "Nao", "Não"), match(text, "musica", "música")])
    assert result.after == "Não gosto da música."
    assert not result.needs_review


@pytest.mark.parametrize("text", ["n�faz-me agitar", "gosto \ud800 disto", "a" * 8001])
def test_corrupted_or_too_long_text_requires_manual_review(text):
    result = suggest(ref(text), [])
    assert result.after == text
    assert result.needs_review


def test_utf16_offsets_after_emoji_and_invalid_surrogate_boundary():
    text = "😄 boa musica"
    assert suggest(ref(text), [match(text, "musica", "música")]).after == "😄 boa música"
    invalid = {"offset": 1, "length": 1, "replacements": [{"value": "x"}]}
    assert suggest(ref(text), [invalid]).after == text


def test_alternative_words_need_individual_confirmation_but_accents_do_not():
    text = "Otimo som."
    assert not suggest(
        ref(text), [match(text, "Otimo", "Ótimo", choices=["Ótimo", "Óptimo"])]
    ).needs_review
    text = "Belo soom."
    assert suggest(ref(text), [match(text, "soom", "som", choices=["som", "sono"])]).needs_review


def test_overlapping_or_invalid_edits_are_not_composed():
    text = "Boa musica."
    result = suggest(ref(text), [match(text, "musica", "música"), match(text, "musica", "músicas")])
    assert result.after == "Boa música."
    assert result.needs_review
    assert suggest(ref(text), [{"offset": True, "length": 1}]).after == text


def test_zero_length_punctuation_and_protected_word_interior():
    text = "Boa musica"
    insertion = {"offset": len(text), "length": 0, "replacements": [{"value": "."}]}
    assert suggest(ref(text), [insertion]).after == text + "."
    text = "Bom groove"
    insertion["offset"] = 6
    assert suggest(ref(text), [insertion]).after == text


def test_comment_only_update_and_undo_preserve_every_other_column_and_table(db):
    db.upsert_feedback("spotify:track:test", "love", "Boa musica!!")
    db.upsert_album_feedback("Artist", "Album", "dislike", "Nao gostei.")
    db.conn.execute("UPDATE feedback SET rated_at='2026-11-14T01:02:03.123456+00:00'")
    db.conn.commit()
    track = track_comment(db, "spotify:track:test")
    album = album_comment(db, "Artist", "Album")
    schema = db.conn.execute("SELECT type,name,sql FROM sqlite_master").fetchall()
    tracks = db.conn.execute("SELECT * FROM tracks").fetchall()
    journal = apply_comments(Path(db.path), [(track, "Boa música!!"), (album, "Não gostei.")])
    assert json.loads(journal.read_text())["changes"][0]["original"]["comment"] == "Boa musica!!"
    assert track_comment(db, "spotify:track:test") == replace(track, comment="Boa música!!")
    assert album_comment(db, "Artist", "Album") == replace(album, comment="Não gostei.")
    assert db.conn.execute("SELECT type,name,sql FROM sqlite_master").fetchall() == schema
    assert db.conn.execute("SELECT * FROM tracks").fetchall() == tracks
    assert journal.stat().st_mode & 0o777 == 0o600
    reverse = undo_comments(Path(db.path), journal)
    assert reverse.exists()
    assert track_comment(db, "spotify:track:test") == track
    assert album_comment(db, "Artist", "Album") == album


@pytest.mark.parametrize(
    "column,value",
    [("comment", "Changed elsewhere"), ("label", "meh"), ("rating", 0), ("rated_at", "later")],
)
def test_concurrent_change_refuses_whole_batch(db, column, value):
    db.upsert_feedback("spotify:track:test", "like", "Antes")
    db.upsert_album_feedback("Artist", "Album", "meh", "Outro")
    first, second = track_comment(db, "spotify:track:test"), album_comment(db, "Artist", "Album")
    db.conn.execute(f"UPDATE feedback SET {column} = ?", (value,))
    db.conn.commit()
    with pytest.raises(CommentConflict):
        apply_comments(Path(db.path), [(second, "Alterado"), (first, "Depois")])
    assert album_comment(db, "Artist", "Album") == second


def test_journal_failure_cannot_change_comment(db, monkeypatch):
    db.upsert_feedback("spotify:track:test", "like", "Antes")
    item = track_comment(db, "spotify:track:test")
    monkeypatch.setattr("peel.comment_polish.tempfile.mkdtemp", MagicMock(side_effect=OSError))
    with pytest.raises(OSError):
        apply_comments(Path(db.path), [(item, "Depois")])
    assert track_comment(db, "spotify:track:test") == item


@pytest.mark.parametrize(
    "label,rating",
    [
        ("love", 2),
        ("like", 1),
        ("meh", 0),
        ("hate", -2),
        ("ban", -2),
        ("unavailable", 0),
        ("skip", -1),
    ],
)
def test_polish_preserves_legacy_and_control_semantics(db, label, rating):
    db.conn.execute(
        "INSERT INTO feedback VALUES (?,?,?,?,?)",
        ("spotify:track:test", rating, label, "Boa musica", "original-time"),
    )
    db.conn.commit()
    item = track_comment(db, "spotify:track:test")
    apply_comments(Path(db.path), [(item, "Boa música")])
    assert track_comment(db, "spotify:track:test") == replace(item, comment="Boa música")


def test_one_spelling_guess_is_not_bulk_safe_and_noop_creates_nothing(tmp_path):
    text = "Belo soom."
    assert suggest(ref(text), [match(text, "soom", "som")]).needs_review
    assert apply_comments(tmp_path / "missing.db", []) is None
    assert not list(tmp_path.iterdir())


def test_concurrent_readback_is_not_captured_as_our_comment(db, monkeypatch):
    session = FeedbackSession(db)
    fake = CommentRef(
        "track", ("spotify:track:test",), "PISS", "Test Track", 1, "like", "later", "Another writer"
    )
    monkeypatch.setattr("peel.comment_polish.track_comment", lambda *args: fake)
    session.upsert_feedback("spotify:track:test", "like", "Our comment")
    assert session.capture_failed and not session.comments
    assert db.feedback_for_track("spotify:track:test")[2] == "Our comment"


def test_missing_database_is_never_created(tmp_path):
    missing = tmp_path / "missing.db"
    with pytest.raises(FileNotFoundError):
        apply_comments(missing, [(ref(), "Depois")])
    assert not missing.exists()


def test_undo_refuses_later_user_edit_and_other_database(db, tmp_path):
    db.upsert_feedback("spotify:track:test", "like", "Antes")
    journal = apply_comments(Path(db.path), [(track_comment(db, "spotify:track:test"), "Depois")])
    db.upsert_feedback("spotify:track:test", "love", "New opinion")
    with pytest.raises(CommentConflict):
        undo_comments(Path(db.path), journal)
    other = tmp_path / "other.db"
    other.touch()
    with pytest.raises(ValueError):
        undo_comments(other, journal)


def _cli(monkeypatch, db):
    # Tests use disposable state only, no sync, playback, Java, or real network.
    monkeypatch.setattr(
        cli,
        "settings",
        SimpleNamespace(
            db_path=db.path, peel_review_playlist_id="review", peel_playlist_id="public"
        ),
    )
    monkeypatch.setattr(cli, "_auto_sync_state", lambda *args: None)

    def rate(session, week):
        session.upsert_feedback("spotify:track:test", "like", "Nao gosto da musica.")
        return 1

    monkeypatch.setattr(cli, "_rate_unrated", rate)
    pushed = []

    def push():
        pushed.append(db.feedback_for_track("spotify:track:test"))
        return True

    monkeypatch.setattr(cli, "_push_state", push)
    return pushed


class Engine:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def check(self, text):
        return [match(text, "Nao", "Não"), match(text, "musica", "música")]


def test_ouvir_original_is_saved_before_check_then_accepted_before_single_push(db, monkeypatch):
    pushed = _cli(monkeypatch, db)
    monkeypatch.setattr(ui, "interactive", lambda: True)

    class SavedFirst(Engine):
        def check(self, text):
            assert db.feedback_for_track("spotify:track:test")[2] == text
            self.before = track_comment(db, "spotify:track:test")
            return super().check(text)

    monkeypatch.setattr(ui, "LocalLanguageTool", SavedFirst)
    result = CliRunner().invoke(cli.app, ["ouvir"], input="y\na\n")
    assert result.exit_code == 0, result.output
    assert len(pushed) == 1
    assert pushed[0][2] == "Não gosto da música."
    assert "Original:" in result.output and "Suggestion:" in result.output
    assert "Ortografia afinada" in result.output


@pytest.mark.parametrize("answers", ["n\n", "y\nk\n", "y\nr\nk\n", "y\nr\nq\n"])
def test_opt_out_keep_and_finish_still_push_originals(db, monkeypatch, answers):
    pushed = _cli(monkeypatch, db)
    monkeypatch.setattr(ui, "interactive", lambda: True)
    monkeypatch.setattr(ui, "LocalLanguageTool", Engine)
    result = CliRunner().invoke(cli.app, ["ouvir"], input=answers)
    assert result.exit_code == 0, result.output
    assert pushed[0][2] == "Nao gosto da musica."


@pytest.mark.parametrize("mode", ["non_tty", "disabled", "failure", "cancel"])
def test_missing_backend_cancel_and_non_tty_do_not_block_saved_votes(db, monkeypatch, mode):
    pushed = _cli(monkeypatch, db)
    monkeypatch.setattr(ui, "interactive", lambda: mode != "non_tty")
    engine = MagicMock(side_effect=RuntimeError if mode != "cancel" else KeyboardInterrupt)
    monkeypatch.setattr(ui, "LocalLanguageTool", engine)
    args = ["ouvir"] + (["--no-polish"] if mode == "disabled" else [])
    result = CliRunner().invoke(cli.app, args, input="y\n")
    assert result.exit_code == 0, result.output
    assert len(pushed) == 1 and pushed[0][2] == "Nao gosto da musica."
    if mode in {"non_tty", "disabled"}:
        engine.assert_not_called()


def test_apply_safe_requires_individual_confirmation_for_ambiguous_comment(db, monkeypatch):
    _cli(monkeypatch, db)
    monkeypatch.setattr(ui, "interactive", lambda: True)

    class Ambiguous(Engine):
        def check(self, text):
            return [match(text, "musica", "melodia", choices=["melodia", "canção"])]

    monkeypatch.setattr(ui, "LocalLanguageTool", Ambiguous)
    result = CliRunner().invoke(cli.app, ["ouvir"], input="y\na\nk\n")
    assert result.exit_code == 0, result.output
    assert db.feedback_for_track("spotify:track:test")[2] == "Nao gosto da musica."
    assert "Apply [a] / Edit [e]" in result.output


def test_manual_edit_is_explicit_and_preserves_vote(db, monkeypatch):
    _cli(monkeypatch, db)
    monkeypatch.setattr(ui, "interactive", lambda: True)
    monkeypatch.setattr(ui, "LocalLanguageTool", Engine)
    result = CliRunner().invoke(cli.app, ["ouvir"], input="y\nr\ne\nNão é love, mas é bom!\n")
    assert result.exit_code == 0, result.output
    assert db.feedback_for_track("spotify:track:test") == (1, "like", "Não é love, mas é bom!")


def test_week_command_requires_explicit_scope_and_never_creates_missing_db(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "settings", SimpleNamespace(db_path=str(tmp_path / "missing.db")))
    result = CliRunner().invoke(cli.app, ["comments", "polish"])
    assert result.exit_code == 2
    assert not (tmp_path / "missing.db").exists()
