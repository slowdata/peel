from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from typer.testing import CliRunner

import peel.cli as cli
from peel.db import DB, iso_week
from peel.models import ReviewQueueItem

runner = CliRunner()


def _settings(db_path: Path) -> SimpleNamespace:
    return SimpleNamespace(
        db_path=str(db_path),
        spotify_client_id="client-id",
        spotify_client_secret="client-secret",
        spotify_refresh_token="refresh-token",
        peel_playlist_id="playlist-id",
        peel_review_playlist_id="",
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


class TestSelectArtistSearchResult:
    def test_exact_normalized_match(self) -> None:
        result = {"artists": {"items": [{"name": "Cécile McLorin Salvant", "genres": []}]}}

        item = cli._select_artist_search_result("Cecile McLorin Salvant", result)

        assert item == {"name": "Cécile McLorin Salvant", "genres": []}

    def test_no_exact_match_returns_none(self) -> None:
        result = {"artists": {"items": [{"name": "Drake", "genres": ["rap"]}]}}

        assert cli._select_artist_search_result("Drake Sexyy Red", result) is None


class TestLookupArtistGenres:
    def test_musicbrainz_lookup(self, monkeypatch) -> None:
        monkeypatch.setattr(
            cli,
            "fetch_musicbrainz_artist_genres",
            lambda artist, **kwargs: SimpleNamespace(
                genres=("post-punk", "art punk"),
                mbid="mbid-1",
            ),
        )

        result = cli._lookup_artist_genres(
            "IDLES",
            source_name="musicbrainz",
            spotify_client=None,
            min_tag_count=2,
        )

        assert result == (["post-punk", "art punk"], "mbid-1")

    def test_spotify_lookup_reuses_exact_match_guard(self) -> None:
        spotify = SimpleNamespace(
            sp=SimpleNamespace(
                search=lambda **_: {"artists": {"items": [{"name": "Drake", "genres": ["rap"]}]}}
            )
        )

        result = cli._lookup_artist_genres(
            "Drake Sexyy Red",
            source_name="spotify",
            spotify_client=spotify,
            min_tag_count=1,
        )

        assert result is None


class TestCliRun:
    def test_run_command_calls_pipeline(self, monkeypatch) -> None:
        mock_run = MagicMock()
        monkeypatch.setattr(cli, "run_pipeline", mock_run)

        result = runner.invoke(cli.app, ["run"])

        assert result.exit_code == 0
        mock_run.assert_called_once_with(dry_run=False)

    def test_run_command_dry_run_passes_flag(self, monkeypatch) -> None:
        mock_run = MagicMock()
        monkeypatch.setattr(cli, "run_pipeline", mock_run)

        result = runner.invoke(cli.app, ["run", "--dry-run"])

        assert result.exit_code == 0
        mock_run.assert_called_once_with(dry_run=True)


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

    def test_prompt_comment_retries_after_unicode_decode_error(self, monkeypatch) -> None:
        calls = 0

        def fake_prompt(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
            nonlocal calls
            calls += 1
            if calls == 1:
                raise UnicodeDecodeError("utf-8", b"\xc2x", 0, 1, "invalid continuation byte")
            return "comentário recuperado"

        monkeypatch.setattr(cli.typer, "prompt", fake_prompt)

        assert cli._prompt_comment(default="") == "comentário recuperado"
        assert calls == 2


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
        # Semana corrente precisa de pelo menos uma faixa, senão o export
        # (corretamente) salta semanas vazias e não escreve ficheiro.
        db.record_track("spotify:track:cli1", "stereogum_new_music", "Snag", "Unarrest Me", None)
        db.close()
        site_dir = tmp_path / "peel-sept"
        current_week = iso_week(datetime.now(UTC))

        monkeypatch.setattr(cli, "settings", _settings(db_path))

        result = runner.invoke(
            cli.app,
            ["site", "export", "--site-dir", str(site_dir), "--weeks", "1", "--no-resolve-albums"],
        )

        assert result.exit_code == 0
        assert f"Exported {current_week}" in result.stdout
        assert (site_dir / "src" / "data" / "weeks" / f"{current_week}.json").exists()


class TestCliTriage:
    def test_triage_lists_confirmed_queue(self, tmp_path: Path, monkeypatch) -> None:
        db_path = tmp_path / "test.db"
        db = DB(str(db_path))
        db.init_schema()
        db.replace_review_queue(
            "playlist-id",
            [
                ReviewQueueItem(
                    source_id="kexp_in_our_headphones",
                    artist="Modern Woman",
                    title="Dashboard Mary",
                    spotify_uri="spotify:track:modern-woman",
                    source_url=None,
                    source_count=1,
                    affinity=0.5,
                    is_new=True,
                    added_at_week="2026-W28",
                    current_week="2026-W28",
                )
            ],
        )
        db.close()
        monkeypatch.setattr(cli, "settings", _settings(db_path))

        result = runner.invoke(cli.app, ["triage"])

        assert result.exit_code == 0
        assert "Triagem confirmada (1)" in result.output
        assert "Modern Woman" in result.output
        assert "🆕 2026-W28" in result.output

    def test_triage_shows_feedback_and_pending_hides_rated(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        db_path = tmp_path / "test.db"
        db = DB(str(db_path))
        db.init_schema()
        item = ReviewQueueItem(
            source_id="kexp_in_our_headphones",
            artist="Modern Woman",
            title="Dashboard Mary",
            spotify_uri="spotify:track:modern-woman",
            source_url=None,
            source_count=1,
            affinity=0.5,
            is_new=False,
            added_at_week="2026-W27",
            current_week="2026-W28",
        )
        db.replace_review_queue("playlist-id", [item])
        db.record_track(item.spotify_uri, item.source_id, item.artist, item.title, None)
        db.upsert_feedback(item.spotify_uri, "love")
        db.close()
        monkeypatch.setattr(cli, "settings", _settings(db_path))

        result = runner.invoke(cli.app, ["triage"])
        pending = runner.invoke(cli.app, ["triage", "--pending"])

        assert result.exit_code == 0
        assert "✓ love" in result.output
        assert pending.exit_code == 0
        assert "Sem tracks activas sem avaliação" in pending.output


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
