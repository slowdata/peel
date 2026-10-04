from __future__ import annotations

import shutil
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from click import unstyle
from typer import rich_utils
from typer.testing import CliRunner

from peel import cli
from peel.config import settings
from peel.db import DB
from peel.models import AlbumQueueItem, ReviewQueueItem
from peel.publication import PublicationPlan, prepare_publication
from peel.site_export import build_site_week_payload, export_site
from peel.spotify_client import SpotifyPlaylistMismatch
from peel.state_sync import StateSyncError, _merge_user_state


@pytest.fixture
def sample(tmp_path, monkeypatch):
    db = DB(str(tmp_path / "peel.db"))
    db.init_schema()
    monkeypatch.setattr(settings, "db_path", db.path)
    monkeypatch.setattr(settings, "peel_playlist_id", "public")
    monkeypatch.setattr(settings, "peel_review_playlist_id", "review")
    queue = []
    for n in range(1, 10):
        uri = f"spotify:track:t{n}"
        db.record_track(uri, "stereogum_new_music", f"Artist {n}", f"Title {n}", None)
        origin = "2026-W35" if n == 9 else "2026-W38"
        db.conn.execute("UPDATE tracks SET added_at_week=? WHERE spotify_uri=?", (origin, uri))
        db.conn.commit()
        db.upsert_feedback(uri, "love" if n % 2 else "like")
        queue.append(
            ReviewQueueItem(
                spotify_uri=uri,
                artist=f"Artist {n}",
                title=f"Title {n}",
                source_id="stereogum_new_music",
                source_url=None,
                source_count=1,
                affinity=0.5,
                is_new=n != 9,
                added_at_week=origin,
                current_week="2026-W38",
            )
        )
    db.replace_review_queue("review", queue)
    albums = [
        AlbumQueueItem(
            week="2026-W38",
            position=n,
            artist=f"Album Artist {n}",
            album=f"Album {n}",
            artist_key=f"album artist {n}",
            album_key=f"album {n}",
            source_ids=("diy_album_reviews",),
            source_count=1,
            listen_url=f"https://open.spotify.com/album/a{n}",
            listen_kind="spotify",
            editorial_url=None,
            is_new=True,
        )
        for n in range(1, 12)
    ]
    db.replace_album_queue("2026-W38", albums)
    for album in albums:
        db.upsert_album_feedback(album.artist, album.album, "like")
    # The keys must be the same normalisation used by feedback lookup.
    selected = [albums[n - 1] for n in (1, 2, 3, 4, 5, 9, 10)]
    plan = PublicationPlan(
        week="2026-W38",
        playlist_id="public",
        review_playlist_id="review",
        track_snapshot_at=db.conn.execute(
            "SELECT confirmed_at FROM review_queue_snapshots"
        ).fetchone()[0],
        album_snapshot_at=db.conn.execute("SELECT created_at FROM album_queue_weeks").fetchone()[0],
        tracks=[queue[n - 1].spotify_uri for n in (1, 3, 5, 6, 7, 8, 9)],
        albums=[(a.artist_key, a.album_key) for a in selected],
        artist_overrides={queue[0].spotify_uri: "Corrected artist"},
    )
    yield db, plan, tmp_path
    db.close()


def test_selection_is_exact_includes_pending_and_preserves_private_queues(sample):
    db, plan, tmp = sample
    private_tracks = db.review_queue("review")
    private_albums = db.album_queue(plan.week)
    payload = prepare_publication(db, plan)
    db.replace_finalized_week_tracks(plan.week, "public", plan.tracks, selection=payload)
    result = build_site_week_payload(db, plan.week, None, finalized_playlist_id="public")
    assert [t["spotify_url"].rsplit("/", 1)[-1] for t in result["tracks"]] == [
        u.rsplit(":", 1)[-1] for u in plan.tracks
    ]
    assert result["tracks"][-1]["discovery_week"] == "2026-W35"
    assert result["tracks"][0]["artist"] == "Corrected artist"
    assert [a["title"] for a in result["albums"]] == [f"Album {n}" for n in (1, 2, 3, 4, 5, 9, 10)]
    assert [a["rank"] for a in result["albums"]] == list(range(1, 8))
    db.upsert_feedback(plan.tracks[0], "skip")
    db.replace_album_queue(plan.week, [])
    db.conn.execute("UPDATE tracks SET artist='Changed later'")
    db.conn.commit()
    assert build_site_week_payload(db, plan.week, None, finalized_playlist_id="public") == result
    assert db.review_queue("review") == private_tracks
    assert len(private_albums) == 11
    exported = export_site(db, tmp / "site", 2, "public", current_week="2026-W39")
    assert [x.week for x in exported] == ["2026-W38"]


@pytest.mark.parametrize("label", ["meh", "skip", "ban", None])
def test_nonpositive_track_aborts_before_publication(sample, label):
    db, plan, _ = sample
    if label is None:
        db.conn.execute("DELETE FROM feedback WHERE spotify_uri=?", (plan.tracks[0],))
        db.conn.commit()
    else:
        db.upsert_feedback(plan.tracks[0], label)
    with pytest.raises(ValueError, match="sem avaliação positiva"):
        prepare_publication(db, plan)
    assert db.finalized_week_selection(plan.week, "public") is None


@pytest.mark.parametrize("label", ["meh", "skip", "ban", "unavailable"])
def test_nonpositive_album_aborts(sample, label):
    db, plan, _ = sample
    db.upsert_album_feedback("Album Artist 1", "Album 1", label)
    with pytest.raises(ValueError, match="sem avaliação positiva"):
        prepare_publication(db, plan)


@pytest.mark.parametrize(
    "change",
    [
        "duplicate",
        "duplicate-album",
        "unknown",
        "unknown-album",
        "stale-track",
        "stale-album",
        "bad-link",
    ],
)
def test_invalid_or_changed_plan_fails_closed(sample, change):
    db, plan, _ = sample
    if change == "duplicate":
        plan.tracks[1] = plan.tracks[0]
    elif change == "duplicate-album":
        plan.albums[1] = plan.albums[0]
    elif change == "unknown":
        plan.tracks[0] = "spotify:track:unknown"
    elif change == "unknown-album":
        plan.albums[0] = ("unknown", "unknown")
    elif change == "stale-track":
        plan.track_snapshot_at = "old"
    elif change == "stale-album":
        plan.album_snapshot_at = "old"
    else:
        db.conn.execute("UPDATE album_queue_items SET listen_url='https://example.com/review'")
        db.conn.commit()
    with pytest.raises(ValueError):
        prepare_publication(db, plan)


def test_latest_identity_feedback_wins_over_old_uri_rating(sample):
    db, plan, _ = sample
    db.record_track("spotify:track:alternative", "stereogum_new_music", "Artist 1", "Title 1", None)
    db.upsert_feedback("spotify:track:alternative", "skip")
    with pytest.raises(ValueError, match="sem avaliação positiva"):
        prepare_publication(db, plan)


def test_explicit_underfilled_selection_never_adds_filler(sample):
    db, plan, _ = sample
    plan.tracks = plan.tracks[:2]
    plan.albums = []
    payload = prepare_publication(db, plan)
    assert len(payload["tracks"]) == 2 and payload["albums"] == []


def test_new_week_requires_public_selection(sample):
    db, plan, _ = sample
    with pytest.raises(ValueError, match="Sem selecção pública"):
        build_site_week_payload(db, plan.week, None, finalized_playlist_id="public")


def test_atomic_snapshot_rolls_back_on_full_payload_failure(sample):
    db, plan, _ = sample
    payload = prepare_publication(db, plan)
    db.conn.execute(
        "CREATE TRIGGER fail BEFORE INSERT ON finalized_week_selections "
        "BEGIN SELECT RAISE(ABORT,'failure'); END"
    )
    import sqlite3

    with pytest.raises(sqlite3.IntegrityError):
        db.replace_finalized_week_tracks(plan.week, "public", plan.tracks, selection=payload)
    assert db.finalized_week_uris(plan.week, "public") is None


def test_cli_dry_run_never_syncs_or_mutates_real_state(sample, monkeypatch):
    db, plan, tmp = sample
    path = tmp / "plan.json"
    path.write_text(plan.model_dump_json())
    db.conn.execute("DROP TABLE finalized_week_selections")
    db.conn.commit()
    before = Path(db.path).read_bytes()
    sync = MagicMock(side_effect=AssertionError("unexpected sync"))
    spotify = MagicMock(side_effect=AssertionError("unexpected Spotify"))
    monkeypatch.setattr(cli, "_auto_sync_state", sync)
    monkeypatch.setattr(cli, "SpotifyClient", spotify)
    result = CliRunner().invoke(
        cli.app,
        ["finalize", "--selection", str(path), "--dry-run", "--site-dir", str(tmp / "site")],
    )
    assert result.exit_code == 0, result.output
    assert "Dry-run" in result.output
    assert Path(db.path).read_bytes() == before
    assert not (tmp / "site").exists()
    sync.assert_not_called()
    spotify.assert_not_called()


@pytest.mark.parametrize("failure", [False, True])
def test_cli_only_exports_selected_week_after_spotify_confirmation(sample, monkeypatch, failure):
    db, plan, tmp = sample
    path = tmp / "plan.json"
    path.write_text(plan.model_dump_json())
    client = MagicMock()
    if failure:
        client.replace_playlist_items.side_effect = SpotifyPlaylistMismatch("mismatch")
    monkeypatch.setattr(cli, "_auto_sync_state", lambda _: None)
    monkeypatch.setattr(cli, "SpotifyClient", lambda: client)
    export = MagicMock()
    monkeypatch.setattr(cli, "export_site", export)
    result = CliRunner().invoke(cli.app, ["finalize", "--selection", str(path)])
    client.replace_playlist_items.assert_called_once_with("public", plan.tracks)
    if failure:
        assert result.exit_code != 0
        assert db.finalized_week_uris(plan.week, "public") is None
        assert db.finalized_week_selection(plan.week, "public") is None
        export.assert_not_called()
    else:
        assert result.exit_code == 0, result.output
        assert db.finalized_week_selection(plan.week, "public")["track_uris"] == plan.tracks
        assert export.call_args.kwargs["weeks"] == 1
        assert export.call_args.kwargs["current_week"] == plan.week
        assert len(db.album_queue(plan.week)) == 11


@pytest.mark.parametrize("force_color", [False, True], ids=["plain", "ansi"])
def test_cli_rejects_missing_plan_and_changed_finalized_choices(sample, monkeypatch, force_color):
    db, plan, tmp = sample
    client = MagicMock()
    monkeypatch.setattr(cli, "_auto_sync_state", lambda _: None)
    monkeypatch.setattr(cli, "SpotifyClient", client)
    # Typer enables Rich colour on GitHub Actions even without a TTY.
    # Exercise that rendering locally, not just the runner's default mode.
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setattr(rich_utils, "FORCE_TERMINAL", force_color)
    result = CliRunner().invoke(cli.app, ["finalize", "--week", plan.week], color=force_color)
    assert result.exit_code == 2
    assert ("\x1b[" in result.output) is force_color
    assert "Indica --selection" in unstyle(result.output)
    assert db.finalized_week_uris(plan.week, "public") is None
    payload = prepare_publication(db, plan)
    db.replace_finalized_week_tracks(plan.week, "public", plan.tracks, selection=payload)
    plan.tracks.reverse()
    path = tmp / "changed.json"
    path.write_text(plan.model_dump_json())
    result = CliRunner().invoke(cli.app, ["finalize", "--selection", str(path)], color=force_color)
    assert result.exit_code == 2
    assert ("\x1b[" in result.output) is force_color
    assert "usa --refresh" in unstyle(result.output)
    client.assert_not_called()
    assert db.finalized_week_selection(plan.week, "public") == payload


def test_sync_preserves_full_selection_and_refuses_old_schema(sample):
    db, plan, tmp = sample
    remote = tmp / "remote.db"
    shutil.copy2(db.path, remote)
    payload = prepare_publication(db, plan)
    db.replace_finalized_week_tracks(plan.week, "public", plan.tracks, selection=payload)
    _merge_user_state(remote, Path(db.path), changed_since="2020-01-01T00:00:00Z")
    other = DB(str(remote))
    assert other.finalized_week_selection(plan.week, "public") == payload
    other.conn.execute("DROP TABLE finalized_week_selections")
    other.conn.commit()
    other.close()
    with pytest.raises(StateSyncError, match="novo schema"):
        _merge_user_state(remote, Path(db.path), changed_since="2020-01-01T00:00:00Z")


# --- peel ouvir / peel publicar ---------------------------------------------

from peel.publication import (  # noqa: E402
    album_candidates,
    default_picks,
    parse_positions,
    track_candidates,
)


def _no_sync(monkeypatch):
    monkeypatch.setattr(cli, "_auto_sync_state", lambda _: None)


def test_proposal_prefers_love_keeps_queue_order_and_never_pads(sample):
    db, plan, _ = sample
    candidates = track_candidates(db, "review", plan.week)
    assert [c.position for c in candidates] == list(range(1, 10))
    picks = default_picks(candidates)
    assert [c.position for c in picks] == [1, 2, 3, 4, 5, 7, 9]  # 5 loves + 2 likes
    assert default_picks(candidates[:3]) == candidates[:3]


def test_album_candidates_drop_archival_and_unplayable(sample):
    db, plan, _ = sample
    db.conn.execute(
        "UPDATE album_queue_items SET album='Album 2 (Deluxe Edition)' WHERE position=2"
    )
    db.conn.execute(
        "UPDATE album_queue_items SET listen_url='https://example.com/review' WHERE position=3"
    )
    db.conn.commit()
    db.upsert_album_feedback("Album Artist 2", "Album 2 (Deluxe Edition)", "love")
    positions = [c.position for c in album_candidates(db, plan.week)]
    assert 2 not in positions and 3 not in positions
    assert positions == [1, 4, 5, 6, 7, 8, 9, 10, 11]


def test_typed_positions_define_public_order(sample):
    db, plan, _ = sample
    candidates = track_candidates(db, "review", plan.week)
    assert [c.position for c in parse_positions("9 1, #3", candidates)] == [9, 1, 3]
    for bad in ("1 1", "42", "x", "1 2 3 4 5 6 7 8"):
        with pytest.raises(ValueError):
            parse_positions(bad, candidates)


def test_numeric_rating_shortcuts(monkeypatch):
    answers = iter(["1", "6"])
    monkeypatch.setattr(cli.typer, "prompt", lambda *a, **k: next(answers))
    assert cli._prompt_rating(default="like") == "love"
    from peel.db import ALBUM_FEEDBACK_RATINGS

    assert cli._prompt_rating(default="like", ratings=ALBUM_FEEDBACK_RATINGS) == "unavailable"


def test_ouvir_rates_tracks_then_albums_and_pushes_once(sample, monkeypatch):
    db, plan, _ = sample
    db.conn.execute(
        "DELETE FROM feedback WHERE spotify_uri IN ('spotify:track:t2','spotify:track:t4')"
    )
    db.conn.execute("DELETE FROM album_feedback WHERE album_key='album 1'")
    db.conn.commit()
    _no_sync(monkeypatch)
    push = MagicMock(return_value=True)
    monkeypatch.setattr(cli, "_push_state", push)
    result = CliRunner().invoke(cli.app, ["ouvir"], input="1\n\n3\n\ny\n2\n\n")
    assert result.exit_code == 0, result.output
    assert db.feedback_for_track_identity("spotify:track:t2")[1] == "love"
    assert db.feedback_for_track_identity("spotify:track:t4")[1] == "meh"
    assert db.album_feedback_for_identity("Album Artist 1", "Album 1")[1] == "like"
    push.assert_called_once()
    assert "sync push" not in unstyle(result.output)


def test_ouvir_without_new_ratings_does_not_push(sample, monkeypatch):
    _no_sync(monkeypatch)
    push = MagicMock()
    monkeypatch.setattr(cli, "_push_state", push)
    result = CliRunner().invoke(cli.app, ["ouvir"], input="y\n")
    assert result.exit_code == 0, result.output
    assert "Nada novo" in unstyle(result.output)
    push.assert_not_called()


def test_publicar_dry_run_writes_nothing(sample, monkeypatch):
    db, plan, _ = sample
    _no_sync(monkeypatch)
    finalize = MagicMock()
    monkeypatch.setattr(cli, "finalize", finalize)
    before = Path(db.path).read_bytes()
    result = CliRunner().invoke(cli.app, ["publicar", "--dry-run"], input="\n\n")
    assert result.exit_code == 0, result.output
    assert "Dry-run" in unstyle(result.output)
    finalize.assert_not_called()
    assert Path(db.path).read_bytes() == before


def test_publicar_freezes_typed_choice_then_publishes_site_and_state(sample, monkeypatch):
    db, plan, tmp = sample
    _no_sync(monkeypatch)
    captured = {}

    def fake_finalize(**kwargs):
        captured["plan"] = PublicationPlan.model_validate_json(kwargs["selection"].read_text())
        captured["kwargs"] = kwargs

    monkeypatch.setattr(cli, "finalize", fake_finalize)
    prepare_site, publish_site, push = MagicMock(), MagicMock(), MagicMock()
    monkeypatch.setattr(cli, "_prepare_site_checkout", prepare_site)
    monkeypatch.setattr(cli, "_publish_site", publish_site)
    monkeypatch.setattr(cli, "_push_state", push)
    result = CliRunner().invoke(cli.app, ["publicar"], input="9 1 3\n\ny\n")
    assert result.exit_code == 0, result.output
    assert captured["plan"].tracks == ["spotify:track:t9", "spotify:track:t1", "spotify:track:t3"]
    assert [key[1] for key in captured["plan"].albums] == [f"album {n}" for n in range(1, 8)]
    assert captured["kwargs"]["export"] is True
    prepare_site.assert_called_once()
    publish_site.assert_called_once()
    assert publish_site.call_args.args[1] == plan.week
    push.assert_called_once()


def test_publicar_refuses_already_published_week(sample, monkeypatch):
    db, plan, _ = sample
    _no_sync(monkeypatch)
    payload = prepare_publication(db, plan)
    db.replace_finalized_week_tracks(plan.week, "public", plan.tracks, selection=payload)
    finalize = MagicMock()
    monkeypatch.setattr(cli, "finalize", finalize)
    result = CliRunner().invoke(cli.app, ["publicar"])
    assert result.exit_code == 0
    assert "já está publicada" in unstyle(result.output)
    finalize.assert_not_called()


def test_publish_site_commits_only_the_week_file(tmp_path, monkeypatch):
    import subprocess

    for key, value in {
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
    }.items():
        monkeypatch.setenv(key, value)
    remote, site = tmp_path / "remote.git", tmp_path / "site"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(remote)], check=True)
    subprocess.run(["git", "clone", "-q", str(remote), str(site)], check=True)
    weeks = site / "src/data/weeks"
    weeks.mkdir(parents=True)
    (weeks / "2026-W31.json").write_text("{}\n")
    subprocess.run(["git", "-C", str(site), "add", "."], check=True)
    subprocess.run(["git", "-C", str(site), "commit", "-qm", "init"], check=True)
    subprocess.run(["git", "-C", str(site), "push", "-q", "origin", "HEAD:main"], check=True)
    subprocess.run(["git", "-C", str(site), "branch", "-q", "-u", "origin/main"], check=True)
    cli._prepare_site_checkout(site)
    (weeks / "2026-W38.json").write_text('{"week": "2026-W38"}\n')
    cli._publish_site(site, "2026-W38")
    log = subprocess.run(
        ["git", "--git-dir", str(remote), "log", "--name-only", "--format=%s", "-1"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert "feat(data): publish 2026-W38" in log and "src/data/weeks/2026-W38.json" in log
    (weeks / "stray.json").write_text("{}")
    (weeks / "2026-W39.json").write_text("{}")
    with pytest.raises(cli.typer.Exit):
        cli._publish_site(site, "2026-W39")
    with pytest.raises(cli.typer.Exit):
        cli._prepare_site_checkout(site)
