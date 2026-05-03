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
        assert source_a.score == 21.0

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
        assert source_c.score == -11.0

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

        result = runner.invoke(cli.app, ["sources", "--weeks", "4", "--min-tracks", "2"])

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

        result = runner.invoke(cli.app, ["sources", "--weeks", "4"])

        assert result.exit_code == 0
        assert "Source scores" in result.stdout
        assert "source-a" in result.stdout
        assert "Found" in result.stdout
        assert "22.0" in result.stdout


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
