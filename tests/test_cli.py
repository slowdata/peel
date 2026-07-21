from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import structlog
from typer.testing import CliRunner

import peel.cli as cli
from peel.db import DB, iso_week
from peel.models import AlbumQueueItem, ReviewQueueItem

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


def _queue_item(
    spotify_uri: str,
    artist: str = "Queue Artist",
    title: str = "Queue Track",
) -> ReviewQueueItem:
    return ReviewQueueItem(
        source_id="source-a",
        artist=artist,
        title=title,
        spotify_uri=spotify_uri,
        source_url=None,
        source_count=1,
        affinity=0.5,
        is_new=False,
        added_at_week="2026-W28",
        current_week="2026-W28",
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


class TestAlbumsCLI:
    def _db_with_album_queue(self, path: Path) -> DB:
        db = DB(str(path))
        db.init_schema()
        db.replace_album_queue(
            "2026-W29",
            [
                AlbumQueueItem(
                    week="2026-W29",
                    position=1,
                    artist="Album Artist",
                    album="Album Name",
                    artist_key="album artist",
                    album_key="album name",
                    source_ids=("source-a",),
                    source_count=1,
                    listen_url="https://listen.example/album",
                    listen_kind="spotify",
                    editorial_url="https://review.example/album",
                    is_new=True,
                )
            ],
        )
        return db

    def test_albums_lists_unrated_and_opens_rank(self, tmp_path: Path, monkeypatch) -> None:
        db_path = tmp_path / "albums.db"
        db = self._db_with_album_queue(db_path)
        db.close()
        monkeypatch.setattr(cli, "settings", _settings(db_path))
        opened = MagicMock()
        monkeypatch.setattr(cli.webbrowser, "open", opened)

        result = runner.invoke(cli.app, ["albums", "--unrated"])
        assert result.exit_code == 0
        assert "Album Artist" in result.output
        result = runner.invoke(cli.app, ["albums", "--open", "1"])
        assert result.exit_code == 0
        opened.assert_called_once_with("https://listen.example/album")
        assert runner.invoke(cli.app, ["albums", "--open", "2"]).exit_code == 2

    def test_refresh_dry_run_is_side_effect_free_and_skips_spotify(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        db_path = tmp_path / "albums.db"
        db = self._db_with_album_queue(db_path)
        db.close()
        before = db_path.read_bytes()
        monkeypatch.setattr(cli, "settings", _settings(db_path))
        spotify = MagicMock()
        monkeypatch.setattr(cli, "SpotifyClient", spotify)

        result = runner.invoke(cli.app, ["albums", "refresh", "--week", "2026-W29", "--dry-run"])

        assert result.exit_code == 0
        assert db_path.read_bytes() == before
        spotify.assert_not_called()

    def test_refresh_no_eligible_keeps_existing_snapshot(self, tmp_path: Path, monkeypatch) -> None:
        db_path = tmp_path / "albums.db"
        db = self._db_with_album_queue(db_path)
        db.close()
        monkeypatch.setattr(cli, "settings", _settings(db_path))
        result = runner.invoke(cli.app, ["albums", "refresh", "--week", "2026-W29"])
        assert result.exit_code == 0
        assert "snapshot existente preservada" in result.output
        db = DB(str(db_path))
        assert db.album_queue("2026-W29")[0].album == "Album Name"  # type: ignore[index]
        db.close()

    def test_refresh_rejects_impossible_iso_week(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr(cli, "settings", _settings(tmp_path / "albums.db"))
        assert (
            runner.invoke(
                cli.app, ["albums", "refresh", "--week", "2025-W53", "--dry-run"]
            ).exit_code
            == 2
        )

    def test_albums_reports_confirmed_empty_queue(self, tmp_path: Path, monkeypatch) -> None:
        db_path = tmp_path / "albums.db"
        db = DB(str(db_path))
        db.init_schema()
        db.replace_album_queue("2026-W29", [])
        db.close()
        monkeypatch.setattr(cli, "settings", _settings(db_path))

        result = runner.invoke(cli.app, ["albums"])

        assert result.exit_code == 0
        assert "confirmada mais recente está vazia" in result.output

    def test_albums_feedback_quit_does_not_write(self, tmp_path: Path, monkeypatch) -> None:
        db_path = tmp_path / "albums.db"
        db = self._db_with_album_queue(db_path)
        db.close()
        monkeypatch.setattr(cli, "settings", _settings(db_path))

        result = runner.invoke(cli.app, ["albums", "feedback"], input="q\n")
        assert result.exit_code == 0
        db = DB(str(db_path))
        assert db.album_feedback_for_identity("Album Artist", "Album Name") is None
        db.close()


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

    def test_run_keeps_structured_debug_logging(self, monkeypatch) -> None:
        def fake_run(*, dry_run: bool) -> None:
            structlog.get_logger().debug("pipeline.debug", dry_run=dry_run)

        monkeypatch.setattr(cli, "run_pipeline", fake_run)

        result = runner.invoke(cli.app, ["run"])

        assert result.exit_code == 0
        assert '"event": "pipeline.debug"' in result.stdout


class TestCliFinalize:
    def _db_with_keeper(self, tmp_path: Path) -> Path:
        db_path = tmp_path / "peel.db"
        db = DB(str(db_path))
        db.init_schema()
        _insert_week_track(db, "spotify:track:keeper", "Keeper Artist", "Keeper Track", "2026-W24")
        db.upsert_feedback("spotify:track:keeper", "like")
        db.close()
        return db_path

    def test_finalize_persists_confirmed_snapshot_before_export(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        db_path = self._db_with_keeper(tmp_path)
        mock_spotify = MagicMock()
        observed_snapshots: list[list[str] | None] = []
        observed_export_weeks: list[str | None] = []

        def fake_export(db, *_args, **kwargs):  # noqa: ANN001, ANN002, ANN003
            observed_snapshots.append(db.finalized_week_uris("2026-W24", "playlist-id"))
            observed_export_weeks.append(kwargs.get("current_week"))
            return []

        monkeypatch.setattr(cli, "settings", _settings(db_path))
        monkeypatch.setattr(cli, "SpotifyClient", lambda: mock_spotify)
        monkeypatch.setattr(cli, "export_site", fake_export)

        result = runner.invoke(
            cli.app,
            ["finalize", "--week", "2026-W24", "--site-dir", str(tmp_path / "site")],
        )

        assert result.exit_code == 0
        mock_spotify.replace_playlist_items.assert_called_once_with(
            "playlist-id", ["spotify:track:keeper"]
        )
        assert observed_snapshots == [["spotify:track:keeper"]]
        assert observed_export_weeks == ["2026-W24"]
        assert "Corre uv run peel sync push." in result.stdout
        db = DB(str(db_path))
        assert db.finalized_week_uris("2026-W24", "playlist-id") == ["spotify:track:keeper"]
        db.close()

    def test_finalize_spotify_failure_leaves_no_snapshot_or_export(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        db_path = self._db_with_keeper(tmp_path)
        mock_spotify = MagicMock()
        mock_spotify.replace_playlist_items.side_effect = RuntimeError("Spotify unavailable")
        mock_export = MagicMock()
        monkeypatch.setattr(cli, "settings", _settings(db_path))
        monkeypatch.setattr(cli, "SpotifyClient", lambda: mock_spotify)
        monkeypatch.setattr(cli, "export_site", mock_export)

        result = runner.invoke(cli.app, ["finalize", "--week", "2026-W24"])

        assert result.exit_code != 0
        mock_export.assert_not_called()
        db = DB(str(db_path))
        assert db.finalized_week_uris("2026-W24", "playlist-id") is None
        db.close()

    def test_finalize_export_failure_keeps_confirmed_snapshot_for_retry(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        db_path = self._db_with_keeper(tmp_path)
        mock_spotify = MagicMock()
        monkeypatch.setattr(cli, "settings", _settings(db_path))
        monkeypatch.setattr(cli, "SpotifyClient", lambda: mock_spotify)
        monkeypatch.setattr(cli, "export_site", MagicMock(side_effect=RuntimeError("site failed")))

        result = runner.invoke(cli.app, ["finalize", "--week", "2026-W24"])

        assert result.exit_code != 0
        assert "snapshot ficou guardado" in result.stdout
        assert "peel site export" in result.stdout
        mock_spotify.replace_playlist_items.assert_called_once_with(
            "playlist-id", ["spotify:track:keeper"]
        )
        db = DB(str(db_path))
        assert db.finalized_week_uris("2026-W24", "playlist-id") == ["spotify:track:keeper"]
        db.close()

    def test_finalize_no_export_still_persists_confirmed_snapshot(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        db_path = self._db_with_keeper(tmp_path)
        mock_spotify = MagicMock()
        mock_export = MagicMock()
        monkeypatch.setattr(cli, "settings", _settings(db_path))
        monkeypatch.setattr(cli, "SpotifyClient", lambda: mock_spotify)
        monkeypatch.setattr(cli, "export_site", mock_export)

        result = runner.invoke(cli.app, ["finalize", "--week", "2026-W24", "--no-export"])

        assert result.exit_code == 0
        assert "Corre uv run peel sync push." in result.stdout
        mock_export.assert_not_called()
        db = DB(str(db_path))
        assert db.finalized_week_uris("2026-W24", "playlist-id") == ["spotify:track:keeper"]
        db.close()

    def test_finalize_rejects_invalid_week_before_spotify_or_snapshot(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        db_path = self._db_with_keeper(tmp_path)
        mock_spotify = MagicMock()
        monkeypatch.setattr(cli, "settings", _settings(db_path))
        monkeypatch.setattr(cli, "SpotifyClient", lambda: mock_spotify)

        result = runner.invoke(cli.app, ["finalize", "--week", "bad-week", "--no-export"])

        assert result.exit_code != 0
        mock_spotify.replace_playlist_items.assert_not_called()
        db = DB(str(db_path))
        assert db.finalized_week_uris("2026-W24", "playlist-id") is None
        db.close()


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
        item = _queue_item("spotify:track:1", "Artist A", "Track A")
        db.record_track(item.spotify_uri, item.source_id, item.artist, item.title, None)
        db.replace_review_queue("playlist-id", [item])
        db.close()

        monkeypatch.setattr(cli, "settings", _settings(db_path))

        result = runner.invoke(cli.app, ["feedback", "--limit", "1"], input="like\nbom groove\n")

        assert result.exit_code == 0
        assert "Artist A" in result.stdout
        assert "Triagem activa completa" in result.stdout
        assert "Corre uv run peel sync push." in result.stdout

        db2 = DB(str(db_path))
        db2.init_schema()
        assert db2.feedback_for_track("spotify:track:1") == (1, "like", "bom groove")
        db2.close()

    def test_feedback_default_only_evaluates_active_triage_queue(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        db_path = tmp_path / "peel.db"
        db = DB(str(db_path))
        db.init_schema()
        active = _queue_item("spotify:track:active", "Active Artist", "Active Track")
        db.record_track(active.spotify_uri, active.source_id, active.artist, active.title, None)
        db.record_track(
            "spotify:track:historic", "source-a", "Historic Artist", "Historic Track", None
        )
        db.replace_review_queue("playlist-id", [active])
        db.close()
        monkeypatch.setattr(cli, "settings", _settings(db_path))

        result = runner.invoke(cli.app, ["feedback"], input="like\n\n")

        assert result.exit_code == 0
        assert "Active Artist" in result.stdout
        assert "Historic Artist" not in result.stdout
        assert "db.connected" not in result.stdout
        db = DB(str(db_path))
        assert db.feedback_for_track(active.spotify_uri) is not None
        assert db.feedback_for_track("spotify:track:historic") is None
        db.close()

    def test_feedback_quits_without_saving_current_active_track(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        db_path = tmp_path / "peel.db"
        db = DB(str(db_path))
        db.init_schema()
        active = _queue_item("spotify:track:active", "Active Artist", "Active Track")
        db.record_track(active.spotify_uri, active.source_id, active.artist, active.title, None)
        db.replace_review_queue("playlist-id", [active])
        db.close()
        monkeypatch.setattr(cli, "settings", _settings(db_path))

        result = runner.invoke(cli.app, ["feedback"], input="q\n")

        assert result.exit_code == 0
        assert "Sessão interrompida" in result.stdout
        assert "Corre uv run peel sync push." not in result.stdout
        db = DB(str(db_path))
        assert db.feedback_for_track(active.spotify_uri) is None
        db.close()

    def test_feedback_history_evaluates_historical_backlog(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        db_path = tmp_path / "peel.db"
        db = DB(str(db_path))
        db.init_schema()
        db.record_track(
            "spotify:track:historic", "source-a", "Historic Artist", "Historic Track", None
        )
        db.close()
        monkeypatch.setattr(cli, "settings", _settings(db_path))

        result = runner.invoke(cli.app, ["feedback", "--history"], input="love\n\n")

        assert result.exit_code == 0
        assert "Historic Artist" in result.stdout
        assert "Corre uv run peel sync push." in result.stdout
        db = DB(str(db_path))
        assert db.feedback_for_track("spotify:track:historic") == (2, "love", None)
        db.close()

    def test_feedback_history_excludes_active_identities_and_uri_variants(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        db_path = tmp_path / "peel.db"
        db = DB(str(db_path))
        db.init_schema()
        active = _queue_item("spotify:track:active", "Active Artist", "Active Track")
        db.record_track(active.spotify_uri, active.source_id, active.artist, active.title, None)
        # URI diferente e capitalização diferente: a identidade continua activa.
        db.record_track(
            "spotify:track:active-variant",
            "source-b",
            "ACTIVE ARTIST",
            "ACTIVE TRACK",
            None,
        )
        db.record_track(
            "spotify:track:historic", "source-a", "Historic Artist", "Historic Track", None
        )
        db.replace_review_queue("playlist-id", [active])
        db.close()
        monkeypatch.setattr(cli, "settings", _settings(db_path))

        result = runner.invoke(cli.app, ["feedback", "--history"], input="like\n\n")

        assert result.exit_code == 0
        assert "Historic Artist" in result.stdout
        assert "Active Artist" not in result.stdout
        db = DB(str(db_path))
        assert db.feedback_for_track("spotify:track:historic") is not None
        assert db.feedback_for_track(active.spotify_uri) is None
        assert db.feedback_for_track("spotify:track:active-variant") is None
        db.close()

    def test_feedback_week_requires_history_without_writing(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        db_path = tmp_path / "peel.db"
        db = DB(str(db_path))
        db.init_schema()
        db.record_track(
            "spotify:track:historic", "source-a", "Historic Artist", "Historic Track", None
        )
        db.close()
        monkeypatch.setattr(cli, "settings", _settings(db_path))

        result = runner.invoke(cli.app, ["feedback", "--week", "2026-W28"])

        assert result.exit_code == 2
        assert "--week requer --history" in result.stderr
        db = DB(str(db_path))
        assert db.feedback_for_track("spotify:track:historic") is None
        db.close()

    def test_feedback_history_rejects_invalid_week_without_writing(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        db_path = tmp_path / "peel.db"
        db = DB(str(db_path))
        db.init_schema()
        db.record_track(
            "spotify:track:historic", "source-a", "Historic Artist", "Historic Track", None
        )
        db.close()
        monkeypatch.setattr(cli, "settings", _settings(db_path))

        result = runner.invoke(cli.app, ["feedback", "--history", "--week", "not-a-week"])

        assert result.exit_code == 2
        assert "Invalid ISO week" in result.stderr
        db = DB(str(db_path))
        assert db.feedback_for_track("spotify:track:historic") is None
        db.close()

    def test_feedback_reports_complete_active_queue_without_offering_history(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        db_path = tmp_path / "peel.db"
        db = DB(str(db_path))
        db.init_schema()
        active = _queue_item("spotify:track:active", "Active Artist", "Active Track")
        db.record_track(active.spotify_uri, active.source_id, active.artist, active.title, None)
        db.upsert_feedback(active.spotify_uri, "like")
        db.record_track(
            "spotify:track:historic", "source-a", "Historic Artist", "Historic Track", None
        )
        db.replace_review_queue("playlist-id", [active])
        db.close()
        monkeypatch.setattr(cli, "settings", _settings(db_path))

        result = runner.invoke(cli.app, ["feedback"])

        assert result.exit_code == 0
        assert "Triagem activa completa" in result.stdout
        assert "Historic Artist" not in result.stdout
        assert "Corre uv run peel sync push." not in result.stdout

    def test_triage_feedback_is_compatible_alias_for_active_queue(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        db_path = tmp_path / "peel.db"
        db = DB(str(db_path))
        db.init_schema()
        active = _queue_item("spotify:track:active", "Active Artist", "Active Track")
        db.record_track(active.spotify_uri, active.source_id, active.artist, active.title, None)
        db.record_track(
            "spotify:track:historic", "source-a", "Historic Artist", "Historic Track", None
        )
        db.replace_review_queue("playlist-id", [active])
        db.close()
        monkeypatch.setattr(cli, "settings", _settings(db_path))

        result = runner.invoke(cli.app, ["triage", "feedback"], input="skip\n\n")

        assert result.exit_code == 0
        assert "Active Artist" in result.stdout
        assert "Historic Artist" not in result.stdout
        db = DB(str(db_path))
        assert db.feedback_for_track(active.spotify_uri) == (-1, "skip", None)
        assert db.feedback_for_track("spotify:track:historic") is None
        db.close()

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
        db.replace_album_queue(iso_week(datetime.now(UTC)), [])
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

    def test_human_triage_hides_internal_info_logs(self, tmp_path: Path, monkeypatch) -> None:
        db_path = tmp_path / "test.db"
        db = DB(str(db_path))
        db.init_schema()
        item = _queue_item("spotify:track:active", "Active Artist", "Active Track")
        db.record_track(item.spotify_uri, item.source_id, item.artist, item.title, None)
        db.replace_review_queue("playlist-id", [item])
        db.close()
        monkeypatch.setattr(cli, "settings", _settings(db_path))

        result = runner.invoke(cli.app, ["triage"])

        assert result.exit_code == 0
        assert "Active Artist" in result.stdout
        assert "db.connected" not in result.stdout
        assert "schema_initialized" not in result.stdout

    def test_verbose_human_command_shows_debug_but_normal_hides_it(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        db_path = tmp_path / "test.db"
        db = DB(str(db_path))
        db.init_schema()
        item = _queue_item("spotify:track:active", "Active Artist", "Active Track")
        db.record_track(item.spotify_uri, item.source_id, item.artist, item.title, None)
        db.replace_review_queue("playlist-id", [item])
        db.close()
        monkeypatch.setattr(cli, "settings", _settings(db_path))
        original_review_queue = DB.review_queue

        def review_queue_with_debug(self, playlist_id):  # noqa: ANN001
            structlog.get_logger().debug("human.debug")
            return original_review_queue(self, playlist_id)

        monkeypatch.setattr(DB, "review_queue", review_queue_with_debug)

        normal = runner.invoke(cli.app, ["triage"])
        verbose = runner.invoke(cli.app, ["--verbose", "triage"])

        assert '"event": "human.debug"' not in normal.stdout
        assert '"event": "human.debug"' in verbose.stdout

    def test_triage_shows_feedback_and_unrated_aliases_hide_rated(
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
        unrated = runner.invoke(cli.app, ["triage", "--unrated"])
        pending = runner.invoke(cli.app, ["triage", "--pending"])

        assert result.exit_code == 0
        assert "✓ love" in result.output
        assert unrated.exit_code == 0
        assert pending.exit_code == 0
        assert "Sem tracks activas sem avaliação" in unrated.output
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
