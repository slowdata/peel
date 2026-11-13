from __future__ import annotations

import json
import shutil
import sqlite3
from datetime import UTC, date, datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

from peel import cli
from peel.config import settings
from peel.db import DB
from peel.models import AlbumQueueItem, ReviewQueueItem
from peel.publication import (
    AdditionalAlbum,
    PublicationPlan,
    additional_album_candidates,
    album_candidates,
    build_plan,
    prepare_publication,
    track_candidates,
    verify_additional_releases,
)
from peel.site_export import build_site_week_payload
from peel.state_sync import _merge_user_state

WEEK = "2026-W45"


@pytest.fixture
def state(tmp_path, monkeypatch):
    db = DB(str(tmp_path / "state.db"))
    db.init_schema()
    monkeypatch.setattr(settings, "db_path", db.path)
    monkeypatch.setattr(settings, "peel_playlist_id", "public")
    monkeypatch.setattr(settings, "peel_review_playlist_id", "review")
    db.record_track("spotify:track:t1", "stereogum_new_music", "Track Artist", "Track", None)
    db.replace_review_queue(
        "review",
        [
            ReviewQueueItem(
                spotify_uri="spotify:track:t1",
                artist="Track Artist",
                title="Track",
                source_id="stereogum_new_music",
                source_url=None,
                source_count=1,
                affinity=0.5,
                is_new=True,
                added_at_week=WEEK,
                current_week=WEEK,
            )
        ],
    )
    db.upsert_feedback("spotify:track:t1", "love")
    db.replace_album_queue(
        WEEK,
        [
            AlbumQueueItem(
                week=WEEK,
                position=n,
                artist=f"Queue Artist {n}",
                album=f"Queue Album {n}",
                artist_key=f"queue artist {n}",
                album_key=f"queue album {n}",
                source_ids=("diy_album_reviews",),
                source_count=1,
                listen_url=f"https://open.spotify.com/album/{100 + n:022d}",
                listen_kind="spotify",
                editorial_url=None,
                is_new=True,
            )
            for n in range(1, 8)
        ],
    )
    for n in range(1, 8):
        db.upsert_album_feedback(f"Queue Artist {n}", f"Queue Album {n}", "meh")
    yield db, tmp_path
    db.close()


def extra(db, n=1, label="like", **updates):
    item = AdditionalAlbum(
        artist=f"Extra Artist {n}",
        album=f"Extra Album {n}",
        listen_url=f"https://open.spotify.com/album/{n:022d}",
        listen_kind="spotify",
    )
    if updates:
        item = AdditionalAlbum.model_validate({**item.model_dump(), **updates})
    if label is not None:
        db.upsert_album_feedback(item.artist, item.album, label, "Original comment")
    return item


def plan_for(db, additions, picks=None):
    candidates = additional_album_candidates(db, WEEK, additions)
    return build_plan(
        db,
        week=WEEK,
        playlist_id="public",
        review_playlist_id="review",
        tracks=track_candidates(db, "review", WEEK),
        albums=picks if picks is not None else candidates,
        additional_albums=additions,
    )


def mention(db, item, source):
    db.conn.execute(
        "INSERT INTO album_mentions (artist,album,artist_key,album_key,source_id,source_url,"
        "seen_at,added_at_week) VALUES (?,?,?,?,?,?,?,?)",
        (
            item.artist,
            item.album,
            *item.key,
            source,
            "https://review.example/album",
            "2026-11-10T12:00:00+00:00",
            WEEK,
        ),
    )
    db.conn.commit()


def test_extra_requires_explicit_catalogue_and_never_changes_private_queue(state):
    db, _ = state
    item = extra(db)
    before = Path(db.path).read_bytes()
    queue = db.album_queue(WEEK)
    assert album_candidates(db, WEEK) == []
    assert additional_album_candidates(db, WEEK, []) == []
    candidates = additional_album_candidates(db, WEEK, [item])
    assert candidates[0].position == 8
    assert candidates[0].origin == "outside_queue"
    payload = prepare_publication(db, plan_for(db, [item]))
    assert payload["album_queue_positions"] == [None]
    assert payload["albums"][0]["selection_origin"] == "outside_queue"
    assert payload["albums"][0]["source"] == "Personal pick"
    assert payload["albums"][0]["source_count"] == 0  # no extra ADM family vote
    assert db.album_queue(WEEK) == queue
    assert Path(db.path).read_bytes() == before
    assert db.finalized_week_selection(WEEK, "public") is None


def test_mixed_selection_order_and_only_selected_metadata_are_frozen(state):
    db, _ = state
    additions = [extra(db, n) for n in (1, 2, 3)]
    db.upsert_album_feedback("Queue Artist 2", "Queue Album 2", "love")
    extras = additional_album_candidates(db, WEEK, additions)
    queued = album_candidates(db, WEEK)[0]
    plan = plan_for(db, additions, [extras[1], queued, extras[0]])
    assert [x.key for x in plan.additional_albums] == [additions[1].key, additions[0].key]
    result = prepare_publication(db, plan)
    assert [a["artist"] for a in result["albums"]] == [
        "Extra Artist 2",
        "Queue Artist 2",
        "Extra Artist 1",
    ]
    assert [a["rank"] for a in result["albums"]] == [1, 2, 3]
    assert result["album_queue_positions"] == [None, 2, None]


def test_publication_metadata_stays_frozen_and_publication_families_are_not_doubled(state):
    db, _ = state
    item = extra(db)
    mention(db, item, "pitchfork_album_reviews")
    mention(db, item, "pitchfork_best_albums")
    plan = plan_for(db, [item])
    before = prepare_publication(db, plan)
    assert before["albums"][0]["source"] == "Pitchfork"
    assert before["albums"][0]["source_count"] == 1
    assert plan.additional_albums[0].metadata_captured_at
    mention(db, item, "treble_album_reviews")
    assert prepare_publication(db, plan) == before
    assert plan_for(db, plan.additional_albums).additional_albums == plan.additional_albums


@pytest.mark.parametrize("label", [None, "meh", "dislike", "hate", "ban", "unavailable", "skip"])
def test_unrated_negative_and_controls_are_not_public_candidates(state, label):
    db, _ = state
    item = extra(db, label=label)
    assert additional_album_candidates(db, WEEK, [item]) == []
    plan = plan_for(db, [item]).model_copy(
        update={"albums": [item.key], "additional_albums": [item]}
    )
    with pytest.raises(ValueError, match="avaliação positiva"):
        prepare_publication(db, plan)


def test_vote_change_after_preview_blocks_publication(state):
    db, _ = state
    item = extra(db, label="love")
    plan = plan_for(db, [item])
    db.upsert_album_feedback(item.artist, item.album, "dislike")
    with pytest.raises(ValueError, match="avaliação positiva"):
        prepare_publication(db, plan)


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/album/x",
        "https://open.spotify.com/search/x",
        "https://user:password@open.spotify.com/album/0000000000000000000001",
        "http://open.spotify.com/album/0000000000000000000001",
        "https://open.spotify.com:444/album/0000000000000000000001",
        "https://open.spotify.com/album/short",
    ],
)
def test_bad_listening_links_are_rejected(state, url):
    db, _ = state
    with pytest.raises(ValueError, match="link directo"):
        additional_album_candidates(db, WEEK, [extra(db, listen_url=url)])


@pytest.mark.parametrize(
    "title", ["Album (2026 Remaster)", "Album (Deluxe Edition)", "Greatest Hits"]
)
def test_archival_and_compilation_extras_are_rejected(state, title):
    db, _ = state
    with pytest.raises(ValueError, match="arquivo/compilação"):
        additional_album_candidates(db, WEEK, [extra(db, album=title)])


def test_artist_alias_and_link_duplicates_are_rejected(state):
    db, _ = state
    a = extra(db, artist="R.E.M.")
    alias = extra(db, 2, artist="REM", album=a.album)
    with pytest.raises(ValueError, match="repetido"):
        additional_album_candidates(db, WEEK, [a, alias])
    duplicate_link = extra(db, 3, listen_url=a.listen_url + "?si=tracking#fragment")
    with pytest.raises(ValueError, match="Link.*repetido"):
        additional_album_candidates(db, WEEK, [a, duplicate_link])
    queued = extra(db, 4, artist="Queue Artist 1", album="Queue Album 1")
    with pytest.raises(ValueError, match="já pertence"):
        additional_album_candidates(db, WEEK, [queued])


@pytest.mark.parametrize(
    "url",
    [
        "https://OPEN.SPOTIFY.COM/album/0000000000000000000001/",
        "https://open.spotify.com:443/album/0000000000000000000001",
    ],
)
def test_equivalent_links_cannot_publish_the_same_album_twice(state, url):
    db, _ = state
    a = extra(db)
    b = extra(db, 2, listen_url=url)
    with pytest.raises(ValueError, match="Link.*repetido"):
        additional_album_candidates(db, WEEK, [a, b])


def test_equivalent_link_already_in_queue_cannot_be_added_under_a_different_name(state):
    db, _ = state
    queued = db.album_queue(WEEK)[0]
    item = extra(
        db, listen_url=queued.listen_url.replace("open.spotify.com", "OPEN.SPOTIFY.COM:443") + "/"
    )
    with pytest.raises(ValueError, match="Link.*repetido"):
        additional_album_candidates(db, WEEK, [item])


def test_extra_metadata_must_be_selected_and_frozen(state):
    db, _ = state
    a, b = extra(db, 1), extra(db, 2)
    plan = plan_for(db, [a])
    with pytest.raises(ValueError, match="não congelados"):
        prepare_publication(db, plan.model_copy(update={"additional_albums": [a]}))
    with pytest.raises(ValueError, match="fora da selecção"):
        prepare_publication(
            db, plan.model_copy(update={"additional_albums": plan.additional_albums + [b]})
        )


def test_seven_album_cap_applies_to_queue_plus_extras(state):
    db, _ = state
    additions = [extra(db, n) for n in range(1, 9)]
    with pytest.raises(ValueError, match="sete|7"):
        additional_album_candidates(db, WEEK, additions)
    for n in range(1, 8):
        db.upsert_album_feedback(f"Queue Artist {n}", f"Queue Album {n}", "like")
    with pytest.raises(ValueError):
        plan_for(
            db,
            additions[:1],
            album_candidates(db, WEEK) + additional_album_candidates(db, WEEK, additions[:1]),
        )


def test_finalized_payload_keeps_extras_after_feedback_changes_and_sync(state):
    db, tmp = state
    item = extra(db)
    remote = tmp / "remote.db"
    shutil.copy2(db.path, remote)
    plan = plan_for(db, [item])
    payload = prepare_publication(db, plan)
    queue = db.album_queue(WEEK)
    db.replace_finalized_week_tracks(WEEK, "public", plan.tracks, selection=payload)
    db.upsert_album_feedback(item.artist, item.album, "hate")
    exported = build_site_week_payload(db, WEEK, None, finalized_playlist_id="public")
    assert exported["albums"] == payload["albums"]
    assert db.album_queue(WEEK) == queue
    _merge_user_state(remote, Path(db.path), changed_since="2020-01-01T00:00:00+00:00")
    other = DB.open_readonly(remote)
    try:
        assert other.finalized_week_selection(WEEK, "public") == payload
        assert other.album_queue(WEEK) == queue
    finally:
        other.close()


def spotify_data(item, **changes):
    return {
        "name": item.album,
        "artists": [{"name": item.artist}],
        "album_type": "album",
        "total_tracks": 12,
        "release_date": "2026-10-30",
        **changes,
    }


@pytest.mark.parametrize(
    "changes",
    [
        {"release_date": "2020-01-01"},
        {"release_date": "2027-01-01"},
        {"release_date": None},
        {"album_type": "compilation"},
        {"name": "Different Album"},
        {"artists": [{"name": "Namesake"}]},
        {"total_tracks": 2, "album_type": "single"},
    ],
)
def test_live_metadata_failure_blocks_new_admission(state, changes):
    db, _ = state
    item = extra(db)
    plan = plan_for(db, [item])
    with pytest.raises(ValueError, match="não elegível"):
        verify_additional_releases(
            plan,
            None,
            spotify_album=lambda _: spotify_data(item, **changes),
            bandcamp_release=MagicMock(),
            reference_date=date(2026, 11, 13),
        )


def test_live_checks_pass_without_mutating_plan_or_state_and_do_not_expire_published_extras(state):
    db, _ = state
    item = extra(db)
    plan = plan_for(db, [item])
    before = Path(db.path).read_bytes()
    getter = MagicMock(return_value=spotify_data(item))
    checks = verify_additional_releases(
        plan,
        None,
        spotify_album=getter,
        bandcamp_release=MagicMock(),
        reference_date=date(2026, 11, 13),
    )
    assert len(checks) == 1 and checks[0]["listen_url"] == item.listen_url
    getter.assert_called_once_with("0000000000000000000001")
    getter.reset_mock()
    assert (
        verify_additional_releases(
            plan,
            plan,
            spotify_album=getter,
            bandcamp_release=MagicMock(),
            reference_date=date(2027, 11, 13),
        )
        == []
    )
    getter.assert_not_called()
    assert Path(db.path).read_bytes() == before


def test_readonly_db_does_not_create_missing_files_and_refuses_writes(state):
    db, tmp = state
    missing = tmp / "missing" / "state.db"
    with pytest.raises(sqlite3.OperationalError):
        DB.open_readonly(missing)
    assert not missing.parent.exists()
    readonly = DB.open_readonly(db.path)
    try:
        assert readonly.feedback_for_track("spotify:track:t1")[1] == "love"
        with pytest.raises(sqlite3.OperationalError):
            readonly.conn.execute("DELETE FROM feedback")
    finally:
        readonly.close()


def catalogue(tmp, items):
    file = tmp / "extras.json"
    file.write_text(json.dumps([a.model_dump(mode="json") for a in items]))
    return file


def forbid_services(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Preview attempted a schema/sync/service write")

    for name in (
        "_auto_sync_state",
        "SpotifyClient",
        "finalize",
        "_prepare_site_checkout",
        "_publish_site",
        "_push_state",
    ):
        monkeypatch.setattr(cli, name, forbidden)
    monkeypatch.setattr(DB, "init_schema", forbidden)


def test_cli_dry_run_reads_canonical_db_without_sync_schema_or_services(state, monkeypatch):
    db, tmp = state
    file = catalogue(tmp, [extra(db, 1, "love"), extra(db, 2)])
    before = Path(db.path).read_bytes()
    forbid_services(monkeypatch)
    result = CliRunner().invoke(
        cli.app, ["publicar", "--dry-run", "--album-extras", str(file)], input="\n\n"
    )
    assert result.exit_code == 0, result.output
    assert "Outside queue" in result.output and "Extra Album 2" in result.output
    assert "Dry-run" in result.output
    assert Path(db.path).read_bytes() == before
    assert db.finalized_week_selection(WEEK, "public") is None


@pytest.mark.parametrize("kind", ["duplicate", "invalid-json", "internal-provenance"])
def test_cli_invalid_input_is_rejected_before_sync(state, monkeypatch, kind):
    db, tmp = state
    item = extra(db)
    if kind == "duplicate":
        file = catalogue(tmp, [item, item])
    elif kind == "invalid-json":
        file = tmp / "extras.json"
        file.write_text("not JSON")
    else:
        file = catalogue(tmp, [item.model_copy(update={"source_ids": ("invented",)})])
    forbid_services(monkeypatch)
    assert CliRunner().invoke(cli.app, ["publicar", "--album-extras", str(file)]).exit_code == 2


def test_cli_explicit_choice_is_frozen_and_refazer_reuses_stored_extra_metadata(state, monkeypatch):
    db, tmp = state
    items = [extra(db, 1, "love"), extra(db, 2)]
    file = catalogue(tmp, items)
    monkeypatch.setattr(cli, "_auto_sync_state", lambda _: None)
    for name in ("_prepare_site_checkout", "_publish_site", "_push_state"):
        monkeypatch.setattr(cli, name, MagicMock())
    captured = []

    def capture(**kwargs):
        captured.append(PublicationPlan.model_validate_json(kwargs["selection"].read_text()))

    monkeypatch.setattr(cli, "finalize", capture)
    result = CliRunner().invoke(
        cli.app, ["publicar", "--album-extras", str(file)], input="\n9 8\ny\n"
    )
    assert result.exit_code == 0, result.output
    plan = captured[-1]
    assert plan.albums == [items[1].key, items[0].key]
    payload = prepare_publication(db, plan)
    db.replace_finalized_week_tracks(WEEK, "public", plan.tracks, selection=payload)
    db.upsert_album_feedback(items[0].artist, items[0].album, "dislike")
    result = CliRunner().invoke(cli.app, ["publicar", "--refazer"], input="\n\ny\n")
    assert result.exit_code == 0, result.output
    assert captured[-1].albums == [items[1].key]
    assert captured[-1].additional_albums == [plan.additional_albums[0]]


@pytest.mark.parametrize("failure", [False, True])
def test_finalize_verified_extras_are_frozen_only_after_spotify_confirmation(
    state, monkeypatch, failure
):
    from peel.spotify_client import SpotifyPlaylistMismatch

    db, tmp = state
    item = extra(db)
    plan = plan_for(db, [item])
    file = tmp / "plan.json"
    file.write_text(plan.model_dump_json())
    monkeypatch.setattr(cli, "_auto_sync_state", lambda _: None)
    client = MagicMock()
    client.sp.album.return_value = spotify_data(
        item, release_date=datetime.now(UTC).date().isoformat()
    )
    if failure:
        client.replace_playlist_items.side_effect = SpotifyPlaylistMismatch("verification failed")
    monkeypatch.setattr(cli, "SpotifyClient", lambda: client)
    export = MagicMock()
    monkeypatch.setattr(cli, "export_site", export)
    result = CliRunner().invoke(cli.app, ["finalize", "--selection", str(file)])
    client.sp.album.assert_called_once()
    client.replace_playlist_items.assert_called_once_with("public", plan.tracks)
    saved = db.finalized_week_selection(WEEK, "public")
    if failure:
        assert result.exit_code != 0
        assert saved is None
        export.assert_not_called()
    else:
        assert result.exit_code == 0, result.output
        assert saved["albums"][0]["selection_origin"] == "outside_queue"
        assert len(saved["additional_album_checks"]) == 1
        export.assert_called_once()


@pytest.mark.parametrize("bad", ["short", "preorder", "archive"])
def test_bandcamp_extras_share_final_release_admission_checks(state, bad):
    from peel.sources.bandcamp import ReleaseDetails

    db, _ = state
    item = extra(db, listen_url="https://artist.bandcamp.com/album/new", listen_kind="bandcamp")
    plan = plan_for(db, [item])
    details = ReleaseDetails(
        track_count=2 if bad == "short" else 12,
        lead_track="Lead",
        released_at=datetime(2026, 10, 30, tzinfo=UTC),
        preorder=bad == "preorder",
        archival_reason="vault collection" if bad == "archive" else None,
    )
    with pytest.raises(ValueError, match="não elegível"):
        verify_additional_releases(
            plan,
            None,
            spotify_album=MagicMock(),
            bandcamp_release=lambda _: details,
            reference_date=date(2026, 11, 13),
        )


def test_legacy_plan_without_extras_still_compares_equal_when_finalizing(state, monkeypatch):
    db, tmp = state
    plan = plan_for(db, [])
    payload = prepare_publication(db, plan)
    payload["plan"].pop("additional_albums")
    db.replace_finalized_week_tracks(WEEK, "public", plan.tracks, selection=payload)
    file = tmp / "legacy.json"
    file.write_text(json.dumps(payload["plan"]))
    monkeypatch.setattr(cli, "_auto_sync_state", lambda _: None)
    client = MagicMock()
    monkeypatch.setattr(cli, "SpotifyClient", lambda: client)
    result = CliRunner().invoke(cli.app, ["finalize", "--selection", str(file), "--no-export"])
    assert result.exit_code == 0, result.output
    assert db.finalized_week_selection(WEEK, "public") == payload


def test_finalize_checks_extra_metadata_before_any_spotify_write(state, monkeypatch):
    db, tmp = state
    item = extra(db)
    plan = plan_for(db, [item])
    file = tmp / "plan.json"
    file.write_text(plan.model_dump_json())
    monkeypatch.setattr(cli, "_auto_sync_state", lambda _: None)
    client = MagicMock()
    client.sp.album.return_value = spotify_data(item, name="Wrong Album")
    monkeypatch.setattr(cli, "SpotifyClient", lambda: client)
    export = MagicMock()
    monkeypatch.setattr(cli, "export_site", export)
    result = CliRunner().invoke(cli.app, ["finalize", "--selection", str(file)])
    assert result.exit_code == 2, result.output
    client.replace_playlist_items.assert_not_called()
    export.assert_not_called()
    assert db.finalized_week_selection(WEEK, "public") is None
