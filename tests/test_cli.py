from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from typer.testing import CliRunner

import peel.cli as cli
from peel.db import DB, iso_week

runner = CliRunner()


def _settings(db_path: Path) -> SimpleNamespace:
    return SimpleNamespace(
        db_path=str(db_path),
        spotify_client_id="client-id",
        spotify_client_secret="client-secret",
        spotify_refresh_token="refresh-token",
        peel_playlist_id="playlist-id",
    )


def _insert_week_track(
    db: DB,
    spotify_uri: str,
    artist: str,
    title: str,
    week: str,
) -> None:
    order = int(spotify_uri.rsplit(":", 1)[-1]) if spotify_uri.rsplit(":", 1)[-1].isdigit() else 1
    db.conn.execute(
        """
        INSERT INTO tracks
        (spotify_uri, source_id, artist, title, source_url, added_at, added_at_week)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            spotify_uri,
            "source-a",
            artist,
            title,
            None,
            f"2026-05-0{order}T10:00:00+00:00",
            week,
        ),
    )
    db.conn.commit()


class TestCliRun:
    def test_run_command_calls_pipeline(self, monkeypatch) -> None:
        mock_run = MagicMock()
        monkeypatch.setattr(cli, "run_pipeline", mock_run)

        result = runner.invoke(cli.app, ["run"])

        assert result.exit_code == 0
        mock_run.assert_called_once_with()


class TestCliStatus:
    def test_status_shows_counts_and_sources(self, tmp_path: Path, monkeypatch) -> None:
        db_path = tmp_path / "peel.db"
        db = DB(str(db_path))
        db.init_schema()
        db.record_track("spotify:track:1", "source-a", "Artist A", "Track A", None)
        db.record_track("spotify:track:1", "source-b", "Artist A", "Track A", None)
        db.record_track("spotify:track:2", "source-a", "Artist B", "Track B", None)
        db.update_source_state("source-a", "ok")
        db.update_source_state("source-b", "error", "timeout")
        db.record_unmatched("source-a", "Ghost", "Song")

        monkeypatch.setattr(cli, "settings", _settings(db_path))

        result = runner.invoke(cli.app, ["status"])

        assert result.exit_code == 0
        assert "DB path" in result.stdout
        assert str(db_path) in result.stdout
        assert "Unique tracks" in result.stdout
        assert "2" in result.stdout
        assert "Sources" in result.stdout
        assert "Unmatched" in result.stdout
        assert "source-a" in result.stdout
        assert "source-b" in result.stdout


class TestCliTracks:
    def test_tracks_lists_aggregated_rows(self, tmp_path: Path, monkeypatch) -> None:
        db_path = tmp_path / "peel.db"
        db = DB(str(db_path))
        db.init_schema()
        db.record_track("spotify:track:1", "source-a", "Artist A", "Track A", "https://a")
        db.record_track("spotify:track:1", "source-b", "Artist A", "Track A", "https://b")
        db.record_track("spotify:track:2", "source-c", "Artist B", "Track B", None)
        db.upsert_feedback("spotify:track:1", "like", "bom groove")

        monkeypatch.setattr(cli, "settings", _settings(db_path))

        result = runner.invoke(cli.app, ["tracks"])

        assert result.exit_code == 0
        assert "Artist A" in result.stdout
        assert "Track A" in result.stdout
        assert "2" in result.stdout
        assert "like" in result.stdout
        assert "Artist B" in result.stdout

    def test_tracks_sources_shows_source_details(self, tmp_path: Path, monkeypatch) -> None:
        db_path = tmp_path / "peel.db"
        db = DB(str(db_path))
        db.init_schema()
        db.record_track("spotify:track:1", "source-a", "Artist A", "Track A", "https://a")
        db.record_track("spotify:track:1", "source-b", "Artist A", "Track A", "https://b")

        monkeypatch.setattr(cli, "settings", _settings(db_path))

        result = runner.invoke(cli.app, ["tracks", "--sources"])

        assert result.exit_code == 0
        assert "source-a" in result.stdout
        assert "source-b" in result.stdout
        assert "https://a" in result.stdout
        assert "https://b" in result.stdout


class TestCliFeedback:
    def test_feedback_non_interactive_upserts_rating(self, tmp_path: Path, monkeypatch) -> None:
        db_path = tmp_path / "peel.db"
        db = DB(str(db_path))
        db.init_schema()
        db.record_track("spotify:track:1", "source-a", "Artist A", "Track A", None)

        monkeypatch.setattr(cli, "settings", _settings(db_path))

        result = runner.invoke(
            cli.app,
            [
                "feedback",
                "--uri",
                "spotify:track:1",
                "--rating",
                "love",
                "--comment",
                "grande baixo",
            ],
        )

        assert result.exit_code == 0
        assert "Saved feedback" in result.stdout

        db2 = DB(str(db_path))
        db2.init_schema()
        assert db2.feedback_for_track("spotify:track:1") == (2, "love", "grande baixo")
        db2.close()

    def test_feedback_interactive_prompts_and_saves(self, tmp_path: Path, monkeypatch) -> None:
        db_path = tmp_path / "peel.db"
        db = DB(str(db_path))
        db.init_schema()
        db.record_track("spotify:track:1", "source-a", "Artist A", "Track A", None)

        monkeypatch.setattr(cli, "settings", _settings(db_path))

        result = runner.invoke(cli.app, ["feedback", "--limit", "1"], input="like\nbom groove\n")

        assert result.exit_code == 0
        assert "Feedback" in result.stdout

        db2 = DB(str(db_path))
        db2.init_schema()
        assert db2.feedback_for_track("spotify:track:1") == (1, "like", "bom groove")
        db2.close()


class TestCliReport:
    def test_report_command_writes_markdown(self, tmp_path: Path, monkeypatch) -> None:
        db_path = tmp_path / "peel.db"
        db = DB(str(db_path))
        db.init_schema()
        db.conn.execute(
            """
            INSERT INTO tracks
            (spotify_uri, source_id, artist, title, source_url, added_at, added_at_week)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "spotify:track:1",
                "source-a",
                "Artist A",
                "Track A",
                None,
                "2026-05-01T10:00:00+00:00",
                "2026-W18",
            ),
        )
        db.conn.commit()
        db.close()

        monkeypatch.setattr(cli, "settings", _settings(db_path))
        output_dir = tmp_path / "reports"

        result = runner.invoke(
            cli.app,
            ["report", "--week", "2026-W18", "--output-dir", str(output_dir)],
        )

        assert result.exit_code == 0
        assert "Report written" in result.stdout
        assert (output_dir / "2026-W18.md").exists()


class TestCliSite:
    def test_site_export_writes_week_json(self, tmp_path: Path, monkeypatch) -> None:
        db_path = tmp_path / "peel.db"
        db = DB(str(db_path))
        db.init_schema()
        db.close()
        site_dir = tmp_path / "peel-sept"
        current_week = iso_week(datetime.now(UTC))

        monkeypatch.setattr(cli, "settings", _settings(db_path))

        result = runner.invoke(
            cli.app,
            ["site", "export", "--site-dir", str(site_dir), "--weeks", "1"],
        )

        assert result.exit_code == 0
        assert f"Exported {current_week}" in result.stdout
        assert (site_dir / "src" / "data" / "weeks" / f"{current_week}.json").exists()


class TestCliPlaylist:
    def test_playlist_fill_week_dry_run_lists_tracks(
        self,
        tmp_path: Path,
        monkeypatch,
    ) -> None:
        db_path = tmp_path / "peel.db"
        db = DB(str(db_path))
        db.init_schema()
        _insert_week_track(db, "spotify:track:1", "Artist A", "Track A", "2026-W22")
        db.close()
        monkeypatch.setattr(cli, "settings", _settings(db_path))

        result = runner.invoke(
            cli.app,
            ["playlist", "fill-week", "2026-w22", "--playlist-id", "playlist-id", "--dry-run"],
        )

        assert result.exit_code == 0
        assert "Artist A" in result.stdout
        assert "Track A" in result.stdout
        assert "Dry run" in result.stdout

    def test_playlist_fill_week_replaces_playlist_items(
        self,
        tmp_path: Path,
        monkeypatch,
    ) -> None:
        db_path = tmp_path / "peel.db"
        db = DB(str(db_path))
        db.init_schema()
        _insert_week_track(db, "spotify:track:1", "Artist A", "Track A", "2026-W22")
        _insert_week_track(db, "spotify:track:2", "Artist B", "Track B", "2026-W22")
        db.close()

        mock_client = MagicMock()
        monkeypatch.setattr(cli, "settings", _settings(db_path))
        monkeypatch.setattr(cli, "SpotifyClient", lambda: mock_client)

        result = runner.invoke(
            cli.app,
            ["playlist", "fill-week", "2026-W22", "--playlist-id", "playlist-id"],
        )

        assert result.exit_code == 0
        mock_client.replace_playlist_items.assert_called_once_with(
            "playlist-id",
            ["spotify:track:2", "spotify:track:1"],
        )

    def test_playlist_fill_week_unrated_only_filters_known_feedback(
        self,
        tmp_path: Path,
        monkeypatch,
    ) -> None:
        db_path = tmp_path / "peel.db"
        db = DB(str(db_path))
        db.init_schema()
        _insert_week_track(db, "spotify:track:old", "Artist A", "Track A", "2026-W20")
        _insert_week_track(db, "spotify:track:new", "Artist A", "Track A", "2026-W22")
        _insert_week_track(db, "spotify:track:2", "Artist B", "Track B", "2026-W22")
        db.upsert_feedback("spotify:track:old", "love", None)
        db.close()

        mock_client = MagicMock()
        monkeypatch.setattr(cli, "settings", _settings(db_path))
        monkeypatch.setattr(cli, "SpotifyClient", lambda: mock_client)

        result = runner.invoke(
            cli.app,
            [
                "playlist",
                "fill-week",
                "2026-W22",
                "--playlist-id",
                "playlist-id",
                "--unrated-only",
            ],
        )

        assert result.exit_code == 0
        mock_client.replace_playlist_items.assert_called_once_with(
            "playlist-id",
            ["spotify:track:2"],
        )


class TestCliDoctor:
    def test_doctor_checks_project_root(self, tmp_path: Path, monkeypatch) -> None:
        db_path = tmp_path / "peel.db"
        db = DB(str(db_path))
        db.init_schema()

        (tmp_path / ".env").write_text("PEEL=1\n", encoding="utf-8")
        (tmp_path / "pyproject.toml").write_text(
            """
[project]
name = "peel"
[project.scripts]
peel = "peel.cli:app"
""".strip()
            + "\n",
            encoding="utf-8",
        )

        monkeypatch.setattr(cli, "PROJECT_ROOT", tmp_path)
        monkeypatch.setattr(cli, "settings", _settings(db_path))

        result = runner.invoke(cli.app, ["doctor"])

        assert result.exit_code == 0
        assert ".env" in result.stdout
        assert "yes" in result.stdout
        assert "peel.cli:app" in result.stdout
