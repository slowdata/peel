from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

from pytest import MonkeyPatch
from typer.testing import CliRunner

import peel.cli as cli
from peel.db import DB, iso_week
from peel.scoring import build_source_scores

runner = CliRunner()


class TestBuildSourceScores:
    def test_build_source_scores_calculates_metrics(self, tmp_path: Path) -> None:
        db = DB(str(tmp_path / "peel.db"))
        db.init_schema()

        _insert_track(
            db,
            uri="spotify:track:shared",
            source_id="source-a",
            artist="Artist A",
            title="Track A",
            added_at="2026-05-01T10:00:00+00:00",
        )
        _insert_track(
            db,
            uri="spotify:track:shared",
            source_id="source-b",
            artist="Artist A",
            title="Track A",
            added_at="2026-05-01T11:00:00+00:00",
        )
        _insert_track(
            db,
            uri="spotify:track:unique-a",
            source_id="source-a",
            artist="Artist B",
            title="Track B",
            added_at="2026-05-01T12:00:00+00:00",
        )
        _insert_track(
            db,
            uri="spotify:track:unique-c",
            source_id="source-c",
            artist="Artist C",
            title="Track C",
            added_at="2026-05-01T13:00:00+00:00",
        )
        _insert_unmatched(
            db,
            source_id="source-a",
            artist="Ghost Artist",
            title="Ghost Song",
            seen_at="2026-05-01T15:00:00+00:00",
        )
        _insert_unmatched(
            db,
            source_id="source-c",
            artist="Missing Artist",
            title="Missing Song",
            seen_at="2026-05-01T16:00:00+00:00",
        )

        db.upsert_feedback("spotify:track:shared", "like", None)
        db.upsert_feedback("spotify:track:unique-a", "love", None)
        db.upsert_feedback("spotify:track:unique-c", "skip", None)

        scores = build_source_scores(
            db,
            weeks=4,
            reference_dt=datetime(2026, 5, 1, tzinfo=UTC),
        )

        assert [row.source_id for row in scores] == ["source-a", "source-b", "source-c"]

        source_a = scores[0]
        assert source_a.tracks_found == 3
        assert source_a.tracks_matched == 2
        assert source_a.new_unique_tracks == 2
        assert source_a.duplicate_mentions == 1
        assert source_a.consensus_hits == 1
        assert source_a.unmatched_count == 1
        assert source_a.liked_count == 2
        assert source_a.skipped_count == 0
        assert source_a.avg_rating == 1.5
        assert source_a.score == 18.166666666666668

        source_b = scores[1]
        assert source_b.tracks_found == 1
        assert source_b.tracks_matched == 1
        assert source_b.new_unique_tracks == 0
        assert source_b.duplicate_mentions == 1
        assert source_b.consensus_hits == 1
        assert source_b.unmatched_count == 0
        assert source_b.liked_count == 1
        assert source_b.skipped_count == 0
        assert source_b.avg_rating == 1.0
        assert source_b.score == 13.0

        source_c = scores[2]
        assert source_c.tracks_found == 2
        assert source_c.tracks_matched == 1
        assert source_c.new_unique_tracks == 1
        assert source_c.duplicate_mentions == 0
        assert source_c.consensus_hits == 0
        assert source_c.unmatched_count == 1
        assert source_c.liked_count == 0
        assert source_c.skipped_count == 1
        assert source_c.avg_rating == -1.0
        assert source_c.score == -10.5

        db.close()

    def test_source_score_normalizes_equivalent_quality_across_volumes(self) -> None:
        """Volume bruto não pode tornar uma source equivalente dominante."""
        from peel.scoring import SourceScore

        high_volume = SourceScore(
            source_id="high-volume",
            tracks_matched=100,
            new_unique_tracks=75,
            consensus_hits=25,
            unmatched_count=25,
            rating_total=100,
            rating_count=100,
            skipped_count=25,
        )
        low_volume = SourceScore(
            source_id="low-volume",
            tracks_matched=4,
            new_unique_tracks=3,
            consensus_hits=1,
            unmatched_count=1,
            rating_total=4,
            rating_count=4,
            skipped_count=1,
        )

        # Mesmas taxas, dentro da granularidade dos contadores pequenos.
        assert high_volume.score == low_volume.score

    def test_build_source_scores_adds_source_run_metrics(self, tmp_path: Path) -> None:
        db = DB(str(tmp_path / "peel.db"))
        db.init_schema()

        _insert_track(
            db,
            uri="spotify:track:1",
            source_id="source-a",
            artist="Artist A",
            title="Track A",
            added_at="2026-05-01T10:00:00+00:00",
        )
        db.record_source_run(
            source_id="source-a",
            run_at="2026-05-01T09:00:00+00:00",
            fetched_count=10,
            fresh_count=8,
            processed_count=5,
            matched_count=4,
            new_unique_count=3,
            unmatched_count=1,
            album_count=0,
            skipped_stale_count=2,
            skipped_cap_count=3,
            status="ok",
            error=None,
        )
        db.record_source_run(
            source_id="source-a",
            run_at="2026-05-02T09:00:00+00:00",
            fetched_count=7,
            fresh_count=6,
            processed_count=4,
            matched_count=2,
            new_unique_count=1,
            unmatched_count=2,
            album_count=0,
            skipped_stale_count=1,
            skipped_cap_count=0,
            status="error",
            error="boom",
        )
        db.record_source_run(
            source_id="album-only",
            run_at="2026-05-01T09:00:00+00:00",
            fetched_count=5,
            fresh_count=5,
            processed_count=5,
            matched_count=0,
            new_unique_count=0,
            unmatched_count=0,
            album_count=5,
            skipped_stale_count=0,
            skipped_cap_count=0,
            status="ok",
            error=None,
        )

        scores = build_source_scores(
            db,
            weeks=4,
            reference_dt=datetime(2026, 5, 1, tzinfo=UTC),
        )

        assert [row.source_id for row in scores] == ["source-a"]
        source_a = scores[0]
        assert source_a.run_count == 2
        assert source_a.fetched_count == 17
        assert source_a.fresh_count == 14
        assert source_a.processed_count == 9
        assert source_a.skipped_stale_count == 3
        assert source_a.skipped_cap_count == 3
        assert source_a.error_count == 1
        db.close()

    def test_build_source_scores_uses_window_and_global_first_source(
        self,
        tmp_path: Path,
    ) -> None:
        db = DB(str(tmp_path / "peel.db"))
        db.init_schema()

        _insert_track(
            db,
            uri="spotify:track:old-consensus",
            source_id="source-old",
            artist="Artist A",
            title="Track A",
            added_at="2026-04-24T10:00:00+00:00",
        )
        _insert_track(
            db,
            uri="spotify:track:old-consensus",
            source_id="source-new",
            artist="Artist A",
            title="Track A",
            added_at="2026-05-01T10:00:00+00:00",
        )
        _insert_track(
            db,
            uri="spotify:track:previous-week",
            source_id="source-previous",
            artist="Artist B",
            title="Track B",
            added_at="2026-04-24T11:00:00+00:00",
        )
        _insert_unmatched(
            db,
            source_id="source-previous",
            artist="Old Missing",
            title="Old Song",
            seen_at="2026-04-24T12:00:00+00:00",
        )

        scores = build_source_scores(
            db,
            weeks=1,
            reference_dt=datetime(2026, 5, 1, tzinfo=UTC),
        )

        assert [row.source_id for row in scores] == ["source-new"]
        source_new = scores[0]
        assert source_new.tracks_found == 1
        assert source_new.tracks_matched == 1
        assert source_new.new_unique_tracks == 0
        assert source_new.consensus_hits == 1
        assert source_new.duplicate_mentions == 1

        db.close()


class TestSourcesCli:
    def test_sources_command_renders_json(self, tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
        db_path = tmp_path / "peel.db"
        db = DB(str(db_path))
        db.init_schema()
        now = datetime.now(UTC)
        _insert_track(
            db,
            uri="spotify:track:1",
            source_id="source-a",
            artist="Artist A",
            title="Track A",
            added_at=now.isoformat(),
        )
        db.upsert_feedback("spotify:track:1", "love", None)
        db.close()

        monkeypatch.setattr(cli, "settings", _settings(db_path))

        result = runner.invoke(cli.app, ["sources", "--weeks", "4", "--json"])

        assert result.exit_code == 0
        payload = json.loads(result.stdout)
        assert payload[0]["source_id"] == "source-a"
        assert payload[0]["tracks_matched"] == 1
        assert payload[0]["run_count"] == 0
        assert payload[0]["fetched_count"] == 0
        assert payload[0]["score"] == 22.0

    def test_sources_command_min_tracks_filters_by_tracks_matched(
        self,
        tmp_path: Path,
        monkeypatch: MonkeyPatch,
    ) -> None:
        db_path = tmp_path / "peel.db"
        db = DB(str(db_path))
        db.init_schema()
        now = datetime.now(UTC).isoformat()
        _insert_track(
            db,
            uri="spotify:track:1",
            source_id="source-a",
            artist="Artist A",
            title="Track A",
            added_at=now,
        )
        _insert_track(
            db,
            uri="spotify:track:2",
            source_id="source-b",
            artist="Artist B",
            title="Track B",
            added_at=now,
        )
        _insert_track(
            db,
            uri="spotify:track:3",
            source_id="source-b",
            artist="Artist C",
            title="Track C",
            added_at=now,
        )
        db.close()

        monkeypatch.setattr(cli, "settings", _settings(db_path))

        result = runner.invoke(
            cli.app, ["sources", "--weeks", "4", "--min-tracks", "2", "--detalhe"]
        )

        assert result.exit_code == 0
        assert "source-b" in result.stdout
        assert "source-a" not in result.stdout

    def test_sources_command_renders_table(self, tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
        db_path = tmp_path / "peel.db"
        db = DB(str(db_path))
        db.init_schema()
        now = datetime.now(UTC)
        _insert_track(
            db,
            uri="spotify:track:1",
            source_id="source-a",
            artist="Artist A",
            title="Track A",
            added_at=now.isoformat(),
        )
        db.upsert_feedback("spotify:track:1", "love", None)
        db.close()

        monkeypatch.setattr(cli, "settings", _settings(db_path))

        result = runner.invoke(
            cli.app, ["sources", "--weeks", "4", "--min-data-tracks", "1", "--detalhe"]
        )

        assert result.exit_code == 0
        assert "Source scores" in result.stdout
        assert "source-a" in result.stdout
        assert "Fnd" in result.stdout
        assert "ok" in result.stdout
        assert "22.0" in result.stdout

    def test_sources_command_marks_insufficient_data(
        self, tmp_path: Path, monkeypatch: MonkeyPatch
    ) -> None:
        db_path = tmp_path / "peel.db"
        db = DB(str(db_path))
        db.init_schema()
        now = datetime.now(UTC)
        _insert_track(
            db,
            uri="spotify:track:1",
            source_id="source-a",
            artist="Artist A",
            title="Track A",
            added_at=now.isoformat(),
        )
        db.upsert_feedback("spotify:track:1", "love", None)
        db.close()

        monkeypatch.setattr(cli, "settings", _settings(db_path))

        result = runner.invoke(cli.app, ["sources", "--weeks", "4", "--detalhe"])

        assert result.exit_code == 0
        assert "source-a" in result.stdout
        assert "insufficient data" in result.stdout
        assert "22.0" not in result.stdout


def _insert_track(
    db: DB,
    *,
    uri: str,
    source_id: str,
    artist: str,
    title: str,
    added_at: str,
) -> None:
    week = iso_week(datetime.fromisoformat(added_at))
    db.conn.execute(
        """
        INSERT INTO tracks
        (spotify_uri, source_id, artist, title, source_url, added_at, added_at_week)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (uri, source_id, artist, title, None, added_at, week),
    )
    db.conn.commit()


def _insert_unmatched(
    db: DB,
    *,
    source_id: str,
    artist: str,
    title: str,
    seen_at: str,
) -> None:
    db.conn.execute(
        """
        INSERT INTO unmatched
        (source_id, artist, title, seen_at)
        VALUES (?, ?, ?, ?)
        """,
        (source_id, artist, title, seen_at),
    )
    db.conn.commit()


def _settings(db_path: Path) -> SimpleNamespace:
    return SimpleNamespace(
        db_path=str(db_path),
        spotify_client_id="client-id",
        spotify_client_secret="client-secret",
        spotify_refresh_token="refresh-token",
        peel_playlist_id="playlist-id",
    )


def test_source_overview_reports_volume_hit_rate_and_health(tmp_path: Path) -> None:
    from peel.scoring import build_source_overview

    db = DB(str(tmp_path / "peel.db"))
    db.init_schema()
    now = datetime.now(UTC)
    for i in range(3):
        _insert_track(
            db,
            uri=f"spotify:track:{i}",
            source_id="volume",
            artist=f"A{i}",
            title=f"T{i}",
            added_at=now.isoformat(),
        )
    db.upsert_feedback("spotify:track:0", "love")
    db.upsert_feedback("spotify:track:1", "meh")
    runs = [
        ("volume", 10, 3, 0, "ok"),
        ("volume", 9, 3, 0, "ok"),
        ("tiny", 4, 0, 0, "ok"),
        ("dead", 0, 0, 0, "ok"),
        ("broken", 5, 1, 0, "error"),
        ("albums", 6, 0, 2, "ok"),
    ]
    for source_id, fetched, new, albums, status in runs:
        db.conn.execute(
            "INSERT INTO source_runs (source_id, run_at, fetched_count, fresh_count,"
            " processed_count, matched_count, new_unique_count, unmatched_count, album_count,"
            " skipped_stale_count, skipped_cap_count, status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                source_id,
                now.isoformat(),
                fetched,
                fetched,
                fetched,
                new,
                new,
                0,
                albums,
                0,
                0,
                status,
            ),
        )
    db._record_album_mention(
        artist="Band",
        album="LP",
        source_id="albums",
        source_url=None,
        spotify_album_uri=None,
        seen_at=now.isoformat(),
        added_at_week=iso_week(now),
    )
    db._record_album_mention(
        artist="Other",
        album="EP",
        source_id="albums",
        source_url=None,
        spotify_album_uri=None,
        seen_at=now.isoformat(),
        added_at_week=iso_week(now),
    )
    db.conn.commit()
    db.upsert_album_feedback("Band", "LP", "like")
    db.upsert_album_feedback("Other", "EP", "unavailable")
    rows = {
        row.source_id: row
        for row in build_source_overview(
            db,
            [
                ("volume", "Volume", "track"),
                ("tiny", "Tiny", "track"),
                ("dead", "Dead", "track"),
                ("broken", "Broken", "track"),
                ("new", "New", "track"),
                ("albums", "Albums", "album"),
            ],
            weeks=4,
        )
    }
    assert rows["volume"].per_week == 3.0 and rows["volume"].status == "ok"
    assert (rows["volume"].rated, rows["volume"].positive) == (2, 1)
    assert rows["tiny"].status == "pouco volume"
    assert rows["dead"].status == "sem resultados"
    assert rows["broken"].status == "erro"
    assert rows["new"].status == "sem execuções"
    assert rows["albums"].per_week == 2.0
    # 'unavailable' is not a musical judgement and is not counted.
    assert (rows["albums"].rated, rows["albums"].positive) == (1, 1)
    db.close()


def test_sources_command_defaults_to_the_plain_overview(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    db_path = tmp_path / "peel.db"
    DB(str(db_path)).init_schema()
    monkeypatch.setattr(cli, "settings", _settings(db_path))
    result = runner.invoke(cli.app, ["sources"])
    assert result.exit_code == 0, result.output
    assert "Faixas — últimas 12 semanas" in result.stdout
    assert "Álbuns — últimas 12 semanas" in result.stdout
    assert "Stereogum — 5 Best Songs" in result.stdout
    assert "Fnd" not in result.stdout
