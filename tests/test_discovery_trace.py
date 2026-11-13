from __future__ import annotations

import hashlib
import json
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from peel import main
from peel.config import settings
from peel.db import DB
from peel.discovery_trace import DiscoveryTrace, capture, observe
from peel.models import AlbumQueueItem, Track

NOW = datetime(2026, 11, 13, 12, tzinfo=UTC)
WEEK = "2026-W46"


def begin(trace):
    trace.record("run.started", started_at=NOW, week=WEEK, config={"track_limit": 28})


def events(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_exclusive_dated_stream_survives_missing_footer_and_never_clobbers(tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_RUN_ID", "123")
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "2")
    monkeypatch.setenv("GITHUB_SHA", "a" * 40)
    traces = []
    for _ in range(2):
        trace = DiscoveryTrace(tmp_path)
        begin(trace)
        trace.record("candidate", artist="PISS", title="Album")
        assert events(trace.path)[-1]["event"] == "candidate"  # flushed before finish
        trace.finish("returned")
        traces.append(trace)
    assert traces[0].path != traces[1].path
    assert traces[0].path.parent.name == WEEK
    assert traces[0].path.name.startswith("run-123-2-")
    lines = events(traces[0].path)
    assert lines[0]["schema_version"] == 1 and lines[0]["code_revision"] == "a" * 40
    assert lines[-1]["trace_complete"] is True


def test_no_trace_context_is_noop_and_context_is_restored(tmp_path):
    capture("before")
    trace = DiscoveryTrace(tmp_path, dry_run=True)
    with observe(trace):
        capture("run.started", started_at=NOW, week=WEEK)
        capture("candidate", value=1)
    count = len(trace.records)
    capture("after")
    assert len(trace.records) == count
    assert not list(tmp_path.rglob("*.jsonl"))


def test_secrets_comments_credentials_and_private_urls_are_not_serialized(tmp_path):
    trace = DiscoveryTrace(tmp_path, dry_run=True)
    begin(trace)
    trace.record(
        "metadata",
        comment="private opinion",
        error="contains credentials",
        authorization="Bearer abc",
        refresh_token="secret",
        headers={"Cookie": "private"},
        source_url="https://review.example/?p=123&token=secret&tracking=abc#frag",
        bad="https://user:password@review.example/path",
        local="http://127.0.0.1/path",
        malformed="https://example.com:invalid/path",
        title="A real album",
    )
    data = trace.records[-1]
    serialized = json.dumps(trace.records)
    assert "private opinion" not in serialized and "Bearer abc" not in serialized
    assert "secret" not in serialized and "password" not in serialized
    assert data["source_url"] == "https://review.example/?p=123"
    assert data["bad"] is None and data["local"] is None and data["malformed"] is None
    assert data["title"] == "A real album"


def test_serializer_does_not_consume_iterators_or_call_arbitrary_repr(tmp_path):
    consumed = []

    def values():
        consumed.append(True)
        yield 1

    class Opaque:
        def __repr__(self):
            raise AssertionError("repr invoked")

    trace = DiscoveryTrace(tmp_path, dry_run=True)
    begin(trace)
    trace.record("metadata", iterator=values(), opaque=Opaque(), nan=float("nan"))
    assert consumed == []
    assert trace.records[-1]["opaque"] == {"unrecorded_type": "Opaque"}
    assert trace.records[-1]["nan"] == {"non_finite": "nan"}


def test_domain_models_and_affinity_dataclasses_preserve_actual_fields(tmp_path):
    trace = DiscoveryTrace(tmp_path, dry_run=True)
    begin(trace)
    item = AlbumQueueItem(
        week=WEEK,
        position=1,
        artist="A",
        album="B",
        artist_key="a",
        album_key="b",
        source_ids=("s",),
        source_count=1,
        listen_url="https://a.bandcamp.com/album/b",
        listen_kind="bandcamp",
        editorial_url=None,
        is_new=True,
    )
    trace.record("selection", albums=[item], metadata=main.ReviewCandidate("s", 2, 1.5, 0.7, 123))
    assert trace.records[-1]["albums"][0]["album"] == "B"
    assert trace.records[-1]["metadata"]["source_score"] == 1.5


@pytest.mark.parametrize("kind", ["events", "bytes", "strings"])
def test_limits_and_truncation_are_explicit_not_silently_complete(tmp_path, kind):
    trace = DiscoveryTrace(
        tmp_path,
        dry_run=True,
        max_events=3 if kind == "events" else 50,
        max_bytes=1200 if kind == "bytes" else 20000,
    )
    begin(trace)
    for i in range(6):
        trace.record("candidate", index=i, title="x" * (6000 if kind == "strings" else 600))
    trace.finish("returned")
    assert trace.records[-1]["trace_complete"] is False
    if kind == "strings":
        assert trace.records[-1]["values_truncated"] > 0
    else:
        assert any(r.get("type") == "limit" for r in trace.records)


def test_pipeline_failure_and_observed_source_failure_are_marked_incomplete(tmp_path):
    for status in ["returned", "raised"]:
        trace = DiscoveryTrace(tmp_path, dry_run=True)
        begin(trace)
        trace.record("source.failed", source_id="broken", error_type="TimeoutError")
        trace.finish(status, error_type="RuntimeError" if status == "raised" else None)
        assert trace.records[-1]["trace_complete"] is False
        assert trace.records[-1]["pipeline_status"] == status


def test_write_failures_never_escape_into_selection(tmp_path, monkeypatch):
    trace = DiscoveryTrace(tmp_path)
    begin(trace)
    trace._stream.close()
    with observe(trace):
        capture("candidate", title="still selected normally")
    trace.finish("returned")
    assert events(trace.path)[0]["type"] == "header"
    assert not any(r.get("type") == "footer" for r in events(trace.path))
    other = DiscoveryTrace(tmp_path, dry_run=True)
    monkeypatch.setattr(
        other, "record", lambda *a, **k: (_ for _ in ()).throw(OSError("full disk"))
    )
    with observe(other):
        capture("candidate", title="no exception")


class Clock(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW if tz is not None else NOW.replace(tzinfo=None)


def configure_clock(monkeypatch):
    import peel.db as db_module
    import peel.scoring as scoring

    monkeypatch.setattr(main, "datetime", Clock)
    monkeypatch.setattr(db_module, "datetime", Clock)
    monkeypatch.setattr(scoring, "datetime", Clock)


def db_rows(path):
    db = DB.open_readonly(path)
    try:
        return {
            name: sorted(db.conn.execute('SELECT * FROM "' + name + '"').fetchall(), key=repr)
            for (name,) in db.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
    finally:
        db.close()


def seed(path):
    db = DB(str(path))
    db.init_schema()
    for uri, artist, title in [
        ("spotify:track:pending", "Pending", "Good"),
        ("spotify:track:oldpending", "Pending", "Old"),
        ("spotify:track:banned", "Blocked", "Ban"),
    ]:
        db.record_track(uri, "source_a", artist, title, None)
    db.conn.execute(
        "UPDATE tracks SET added_at_week='2026-W45',added_at=?",
        ((NOW - timedelta(days=7)).isoformat(),),
    )
    db.conn.commit()
    db.upsert_feedback("spotify:track:banned", "ban", "Original comment")
    db.upsert_album_feedback("Heard Artist", "Heard Album", "like", "Original album comment")
    db.close()


def pipeline_sources():
    sources = [
        SimpleNamespace(id=s, kind=k, name=s, last_raw_entries=1)
        for s, k in [
            ("source_a", "track"),
            ("source_b", "track"),
            ("treble_album_reviews", "album"),
            ("broken", "track"),
        ]
    ]
    tracks = {
        "source_a": [
            Track(source_id="source_a", artist=a, title=t)
            for a, t in [
                ("New A", "Good"),
                ("Blocked", "Ban"),
                ("Old Artist", "Old"),
                ("No Match", "Missing"),
                ("Cap Only", "Unvisited"),
            ]
        ],
        "source_b": [
            Track(source_id="source_b", artist=a, title="Good") for a in ["New B", "New C"]
        ],
        "treble_album_reviews": [
            Track(
                source_id="treble_album_reviews",
                artist=a,
                title=t,
                kind="album",
                source_url="https://review.example/" + a.replace(" ", "-"),
            )
            for a, t in [
                ("Album A", "Unplayable"),
                ("Album B", "Old"),
                ("Album C", "Good C"),
                ("Album D", "Good D"),
                ("Album E", "Never Resolved"),
                ("Heard Artist", "Heard Album"),
                ("Archive Artist", "Album (Remaster)"),
            ]
        ],
    }
    return sources, tracks


def client():
    sp = MagicMock()

    def search(artist, title, limit=5):
        if artist == "No Match":
            return []
        return [
            {
                "uri": "spotify:track:" + artist.replace(" ", ""),
                "name": title,
                "artists": [artist],
                "release_date": "2020-01-01" if artist == "Old Artist" else "2026-11-12",
            }
        ]

    def search_album(artist, title):
        if artist == "Album A":
            return []
        return [
            {
                "uri": "spotify:album:" + artist.replace(" ", ""),
                "url": "https://open.spotify.com/album/" + artist.replace(" ", ""),
                "name": title,
                "artists": [artist],
            }
        ]

    def album(album_id):
        return {
            "name": album_id,
            "album_type": "album",
            "total_tracks": 12,
            "release_date": "2020-01-01" if album_id == "AlbumB" else "2026-11-06",
        }

    def track(uri):
        return {
            "name": "Pending",
            "album": {"release_date": "2020-01-01" if uri.endswith("oldpending") else "2026-11-06"},
        }

    sp.search_track.side_effect = search
    sp.search_album.side_effect = search_album
    sp.sp.album.side_effect = album
    sp.sp.track.side_effect = track
    return sp


@pytest.mark.parametrize("track_limit", [2, 4])
def test_observation_and_disk_failure_leave_both_queues_db_and_external_calls_identical(
    tmp_path, monkeypatch, track_limit
):
    configure_clock(monkeypatch)
    base = tmp_path / "base.db"
    seed(base)
    sources, items = pipeline_sources()

    def fetch(_db, source):
        if source.id == "broken":
            raise RuntimeError("DO_NOT_CAPTURE_SECRET")
        return items[source.id]

    monkeypatch.setattr(main, "active_sources", lambda: sources)
    monkeypatch.setattr(main, "fetch_source", fetch)
    monkeypatch.setattr(settings, "peel_playlist_id", "public")
    monkeypatch.setattr(settings, "peel_review_playlist_id", "review")
    monkeypatch.setattr(settings, "peel_max_tracks_per_run", track_limit)
    monkeypatch.setattr(settings, "peel_max_tracks_per_source", 4)
    monkeypatch.setattr(main, "MAX_ALBUM_RESOLUTION_CANDIDATES", 6)
    from peel.novelty import NoveltyCheck

    outcomes = []
    for mode in ["off", "on", "disk_failure"]:
        path = tmp_path / mode / "state.db"
        path.parent.mkdir()
        shutil.copy2(base, path)
        monkeypatch.setattr(settings, "db_path", str(path))
        monkeypatch.setenv("PEEL_CAPTURE_DISCOVERY", "0" if mode == "off" else "1")
        if mode == "disk_failure":
            (path.parent / "reports").write_text("not a directory")
        sp = client()
        digest = MagicMock()
        mb = MagicMock(return_value={"artists": []})
        monkeypatch.setattr(
            main,
            "NoveltyCheck",
            lambda mb=mb, **kw: NoveltyCheck(**kw, artist_search=mb, sleep=lambda _: None),
        )
        monkeypatch.setattr(main, "SpotifyClient", lambda sp=sp: sp)
        monkeypatch.setattr(main, "send_digest", digest)
        main.run()
        outcomes.append(
            (
                db_rows(path),
                sp.search_track.call_args_list,
                sp.search_album.call_args_list,
                sp.sp.album.call_args_list,
                sp.sp.track.call_args_list,
                sp.replace_playlist_items.call_args_list,
                digest.call_args_list,
                mb.call_args_list,
            )
        )
        snapshots = list(path.parent.rglob("*.jsonl"))
        if mode == "on":
            assert len(snapshots) == 1
            raw = snapshots[0].read_text()
            assert "DO_NOT_CAPTURE_SECRET" not in raw
            observed = events(snapshots[0])
            codes = {r.get("event") for r in observed}
            assert {
                "source.collected",
                "source.failed",
                "tracks.source_cap",
                "tracks.search",
                "tracks.rotation_pool",
                "albums.base_ranking",
                "albums.feedback_check",
                "albums.ranking_score",
                "albums.spotify_metadata",
                "albums.queue_decision",
                "selection.planned",
                "delivery.spotify_verified",
                "delivery.persisted",
            } <= codes
            assert observed[-1]["pipeline_status"] == "returned"
            assert observed[-1]["trace_complete"] is False  # broken source, not hidden
            assert any(
                r.get("decision") == "archival_title" or r.get("reason") == "archival_title"
                for r in observed
            )
            assert any(r.get("decision") == "already_rated_or_control" for r in observed)
            assert not any(c.args[0] == "Cap Only" for c in sp.search_track.call_args_list)
        else:
            assert snapshots == []
    assert outcomes[0] == outcomes[1] == outcomes[2]


def test_enabled_dry_run_never_creates_a_snapshot_or_changes_real_state(tmp_path, monkeypatch):
    configure_clock(monkeypatch)
    path = tmp_path / "state.db"
    seed(path)
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    monkeypatch.setattr(settings, "db_path", str(path))
    monkeypatch.setenv("PEEL_CAPTURE_DISCOVERY", "1")
    monkeypatch.setattr(main, "active_sources", lambda: [])
    sp = client()
    digest = MagicMock()
    monkeypatch.setattr(main, "SpotifyClient", lambda: sp)
    monkeypatch.setattr(main, "send_digest", digest)
    main.run(dry_run=True)
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before
    assert not (tmp_path / "reports").exists()
    sp.replace_playlist_items.assert_not_called()
    digest.assert_not_called()


def test_failed_pipeline_keeps_trace_but_does_not_claim_delivery(tmp_path, monkeypatch):
    configure_clock(monkeypatch)
    path = tmp_path / "state.db"
    seed(path)
    monkeypatch.setattr(settings, "db_path", str(path))
    monkeypatch.setenv("PEEL_CAPTURE_DISCOVERY", "1")
    monkeypatch.setattr(
        main, "SpotifyClient", lambda: (_ for _ in ()).throw(RuntimeError("no credentials"))
    )
    with pytest.raises(RuntimeError, match="no credentials"):
        main.run()
    snapshot = next(tmp_path.rglob("*.jsonl"))
    data = events(snapshot)
    assert data[-1]["pipeline_status"] == "raised" and data[-1]["trace_complete"] is False
    assert not any(r.get("event") == "delivery.spotify_verified" for r in data)
    assert "no credentials" not in snapshot.read_text()


@pytest.mark.parametrize(
    "new_count,expected_calls,reason",
    [(2, 0, "not_evaluated_queue_full"), (0, 14, "not_evaluated_lookup_budget")],
)
def test_pending_caps_do_not_probe_or_invent_rejections_for_unexamined_items(
    tmp_path, new_count, expected_calls, reason
):
    ranked = [f"spotify:track:{n}" for n in range(30)]
    lookup = MagicMock(return_value=None)
    trace = DiscoveryTrace(tmp_path, dry_run=True)
    begin(trace)
    with observe(trace):
        result = main.select_review_playlist_uris(
            ranked,
            ranked,
            ranked[:new_count],
            lambda _: True,
            limit=2,
            candidate_metadata=None,
            pending_release_filter=lookup,
        )
    assert len(result) == 2 and lookup.call_count == expected_calls
    assert any(r.get("decision") == reason for r in trace.records)
    assert not any(r.get("decision") == "release_held" for r in trace.records)


def test_album_unresolved_old_selected_and_unexamined_are_distinct_without_extra_lookups(tmp_path):
    from peel.albums import AlbumRecommendation

    def item(name, uri):
        return AlbumRecommendation(
            name,
            name,
            1,
            ("source",),
            (("source", "https://review.example/album"),),
            uri,
            NOW.isoformat(),
            0.0,
            0.0,
            name.lower(),
            name.lower(),
        )

    pool = [
        (item("No Link", None), True),
        (item("Old", "spotify:album:old"), True),
        (item("Good", "spotify:album:good"), True),
        (item("Unvisited", "spotify:album:unvisited"), True),
    ]
    resolver = MagicMock(return_value=None)
    novelty = MagicMock(side_effect=lambda artist, *_: "old edition" if artist == "Old" else None)
    trace = DiscoveryTrace(tmp_path, dry_run=True)
    begin(trace)
    with observe(trace):
        chosen = main._album_queue_snapshot_items(WEEK, pool, resolver, limit=1, novelty=novelty)
    assert [a.artist for a in chosen] == ["Good"]
    assert resolver.call_count == 1 and novelty.call_count == 2
    assert any(r.get("decision") == "no_direct_listen_link" for r in trace.records)
    assert any(r.get("reason") == "old edition" for r in trace.records)
    assert any(r.get("event") == "albums.not_evaluated" for r in trace.records)


def test_bandcamp_inspection_caps_are_visible_without_fetching_more_details(tmp_path, monkeypatch):
    from peel.sources.bandcamp import BandcampLabel, ReleaseDetails

    source = BandcampLabel("bandcamp_test", "Test", "label", max_items=1, max_candidates=4)
    candidates = [
        Track(
            source_id=source.id,
            artist="Artist",
            title=str(i),
            source_url=f"https://artist.bandcamp.com/album/{i}",
        )
        for i in range(4)
    ]
    details = MagicMock(
        side_effect=[
            ReleaseDetails(10, "Lead", NOW + timedelta(days=1), False),
            ReleaseDetails(1, "Single", NOW - timedelta(days=1), False),
        ]
    )
    monkeypatch.setattr(source, "_catalogue_items", lambda _: list(range(4)))
    monkeypatch.setattr(source, "_parse_items", lambda *args: candidates)
    monkeypatch.setattr(source, "_release_details", details)
    monkeypatch.setattr(
        "peel.sources.bandcamp.httpx.get",
        lambda *a, **kw: SimpleNamespace(text="html", raise_for_status=lambda: None),
    )
    trace = DiscoveryTrace(tmp_path, dry_run=True)
    begin(trace)
    with observe(trace):
        chosen = source.fetch(now=NOW)
    assert len(chosen) == 1 and chosen[0].kind == "track" and details.call_count == 2
    end = next(r for r in trace.records if r.get("event") == "bandcamp.inspection_completed")
    assert len(end["not_evaluated"]) == 2
    assert any(r.get("reason") == "routed_to_track" for r in trace.records)


def test_search_failures_are_distinct_from_genuine_empty_results_without_exposing_errors(tmp_path):
    from peel.spotify_client import SpotifyClient

    client = object.__new__(SpotifyClient)
    client.sp = MagicMock()
    client.sp.search.side_effect = RuntimeError("DO_NOT_STORE_CREDENTIAL")
    trace = DiscoveryTrace(tmp_path, dry_run=True)
    begin(trace)
    with observe(trace):
        assert client._do_search("Artist Song", 5) == []
        assert client.search_album("Artist", "Album") == []
    assert client.sp.search.call_count == 2
    assert {r.get("event") for r in trace.records} >= {
        "tracks.spotify_search_failed",
        "albums.spotify_search_failed",
    }
    assert "DO_NOT_STORE_CREDENTIAL" not in json.dumps(trace.records)


def test_profile_names_are_not_mistaken_for_secret_field_names(tmp_path):
    from peel.affinity import AffinityProfile

    trace = DiscoveryTrace(tmp_path, dry_run=True)
    begin(trace)
    trace.record(
        "profile",
        profile=AffinityProfile(
            artist_scores={"sleep token": 0.3, "secret": 0.7},
            artist_genres={"sleep token": ("metal",)},
        ),
    )
    profile = trace.records[-1]["profile"]
    assert dict(profile["artist_scores"]) == {"sleep token": 0.3, "secret": 0.7}
    assert dict(profile["artist_genres"])["sleep token"] == ["metal"]


def test_spotify_failure_keeps_planned_pool_but_never_claims_confirmed_delivery(
    tmp_path, monkeypatch
):
    configure_clock(monkeypatch)
    path = tmp_path / "state.db"
    seed(path)
    monkeypatch.setattr(settings, "db_path", str(path))
    monkeypatch.setenv("PEEL_CAPTURE_DISCOVERY", "1")
    monkeypatch.setattr(settings, "peel_playlist_id", "public")
    monkeypatch.setattr(settings, "peel_review_playlist_id", "review")
    monkeypatch.setattr(main, "active_sources", lambda: [])
    sp = client()
    sp.replace_playlist_items.side_effect = RuntimeError("write uncertain")
    digest = MagicMock()
    monkeypatch.setattr(main, "SpotifyClient", lambda: sp)
    monkeypatch.setattr(main, "send_digest", digest)
    with pytest.raises(RuntimeError, match="write uncertain"):
        main.run()
    data = events(next(tmp_path.rglob("*.jsonl")))
    codes = {r.get("event") for r in data}
    assert "selection.planned" in codes and "delivery.spotify_started" in codes
    assert "delivery.spotify_verified" not in codes and "delivery.persisted" not in codes
    assert data[-1]["trace_complete"] is False
    digest.assert_not_called()


def test_weekly_enables_capture_and_retains_only_current_attempt_even_on_failure():
    workflow = (Path(__file__).parents[1] / ".github/workflows/weekly.yml").read_text()

    def step(name):
        return workflow.split(f"      - name: {name}\n", 1)[1].split("\n      - name:", 1)[0]

    run, upload = step("run peel"), step("upload discovery trace")
    assert 'PEEL_CAPTURE_DISCOVERY: "1"' in run
    assert "if: ${{ always() }}" in upload and "continue-on-error: true" in upload
    assert "retention-days: 7" in upload
    path = next(line for line in upload.splitlines() if line.strip().startswith("path:"))
    assert "github.run_id" in path and "github.run_attempt" in path
    assert ".peel-backups" not in path
    assert "data/reports/" in step("commit state")
