"""Testes de integração para main.py."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from peel.main import _retry_unmatched, run
from peel.models import Track
from peel.sources.rss import GorillaVsBear, PitchforkBNT, StereogumNewMusic, TheQuietus


class TestMainIntegration:
    """Testes de integração end-to-end."""

    def test_run_end_to_end_with_mocked_spotify(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Teste E2E: fetch, match, add com SpotifyClient mockado.

        Fluxo:
        1. Mocking SpotifyClient inteiro
        2. Run processa o feed fixture do Pitchfork
        3. Verifica que alguns tracks foram adicionados ao DB
        4. Verifica que alguns unmatched foram registados
        """
        # Configura DB temporário
        db_path = tmp_path / "test.db"
        monkeypatch.setenv("PEEL_PLAYLIST_ID", "spotify:playlist:test")
        monkeypatch.setenv("SPOTIFY_CLIENT_ID", "test_id")
        monkeypatch.setenv("SPOTIFY_CLIENT_SECRET", "test_secret")
        monkeypatch.setenv("SPOTIFY_REFRESH_TOKEN", "test_token")

        # Poda settings para usar DB tmp
        from peel import config as config_module

        monkeypatch.setattr(config_module.settings, "db_path", str(db_path))

        # Mock do SpotifyClient: encontra sempre algo
        mock_sp = MagicMock()

        def mock_search_track(artist, title, limit=5):
            """Mock simples: sempre devolve um candidato."""
            artist_slug = artist.lower().replace(" ", "_")
            title_slug = title.lower().replace(" ", "_")
            return [
                {
                    "uri": f"spotify:track:{artist_slug}_{title_slug}",
                    "name": title,
                    "artists": [artist],
                }
            ]

        mock_sp.search_track = mock_search_track
        mock_sp.replace_playlist_items = MagicMock()

        # Patcha o URL do Pitchfork RSS para apontar ao fixture
        fixture_path = Path(__file__).parent / "fixtures" / "pitchfork_feed.xml"
        fixture_url = fixture_path.as_uri()

        with (
            patch("peel.sources.rss.PitchforkBNT.url", fixture_url),
            patch("peel.main.SpotifyClient", return_value=mock_sp),
            patch("peel.main.send_digest"),  # Mocka Telegram
        ):
            # Executa a run
            run()

        # Verifica estado do DB
        from peel.db import DB

        db = DB(str(db_path))
        db.init_schema()

        # Verifica que ALGUNS tracks foram adicionados
        cursor = db.conn.execute("SELECT COUNT(*) FROM tracks")
        track_count = cursor.fetchone()[0]
        assert track_count > 0, "Deve ter adicionado pelo menos um track"

        # Verifica que o SpotifyClient.replace_playlist_items foi chamado (rotação)
        assert mock_sp.replace_playlist_items.called

        db.close()

    def test_run_idempotent_second_execution(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Teste: segunda run com mesma DB não adiciona duplicados.

        Garante idempotência: a run é segura correr 2x.
        """
        db_path = tmp_path / "test.db"
        monkeypatch.setenv("PEEL_PLAYLIST_ID", "spotify:playlist:test")
        monkeypatch.setenv("SPOTIFY_CLIENT_ID", "test_id")
        monkeypatch.setenv("SPOTIFY_CLIENT_SECRET", "test_secret")
        monkeypatch.setenv("SPOTIFY_REFRESH_TOKEN", "test_token")

        from peel import config as config_module

        monkeypatch.setattr(config_module.settings, "db_path", str(db_path))

        mock_sp = MagicMock()

        def mock_search_track(artist, title, limit=5):
            """Mock simples: encontra sempre algo."""
            return [
                {
                    "uri": f"spotify:track:{artist.lower()}_{title.lower()}",
                    "name": title,
                    "artists": [artist],
                }
            ]

        mock_sp.search_track = mock_search_track
        mock_sp.replace_playlist_items = MagicMock()

        fixture_path = Path(__file__).parent / "fixtures" / "pitchfork_feed.xml"
        fixture_url = fixture_path.as_uri()

        with (
            patch("peel.sources.rss.PitchforkBNT.url", fixture_url),
            patch("peel.main.SpotifyClient", return_value=mock_sp),
            patch("peel.main.send_digest"),  # Mocka Telegram
        ):
            # Primeira run
            run()

            # Conta tracks adicionadas
            from peel.db import DB

            db = DB(str(db_path))
            db.init_schema()
            cursor = db.conn.execute("SELECT COUNT(*) FROM tracks")
            count_after_first = cursor.fetchone()[0]
            db.close()

            # Segunda run (mesma DB)
            run()

            # Conta novamente
            db = DB(str(db_path))
            db.init_schema()
            cursor = db.conn.execute("SELECT COUNT(*) FROM tracks")
            count_after_second = cursor.fetchone()[0]
            db.close()

        # Deve ter o mesmo número (nenhum duplicado adicionado)
        assert count_after_first == count_after_second
        assert count_after_first > 0  # Mas tem algo

    def test_run_handles_source_failure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Teste: falha de uma source não para a run.

        Verifica que o error handling funciona:
        - Source crasha (RuntimeError simulado)
        - Run completa normalmente (try/except)
        - sources_state regista a falha com mensagem
        """
        db_path = tmp_path / "test.db"
        monkeypatch.setenv("PEEL_PLAYLIST_ID", "spotify:playlist:test")
        monkeypatch.setenv("SPOTIFY_CLIENT_ID", "test_id")
        monkeypatch.setenv("SPOTIFY_CLIENT_SECRET", "test_secret")
        monkeypatch.setenv("SPOTIFY_REFRESH_TOKEN", "test_token")

        from peel import config as config_module

        monkeypatch.setattr(config_module.settings, "db_path", str(db_path))

        mock_sp = MagicMock()
        mock_sp.search_track = MagicMock(return_value=[])
        mock_sp.replace_playlist_items = MagicMock()

        def mock_fetch(self):
            """Simula crash da source."""
            raise RuntimeError("simulated source crash: network timeout")

        with (
            patch.object(PitchforkBNT, "fetch", mock_fetch),
            patch("peel.main.SpotifyClient", return_value=mock_sp),
            patch("peel.main.send_digest"),  # Mocka Telegram
        ):
            # Executa a run — não deve falhar globalmente
            run()

        # Verifica que sources_state registou o erro
        from peel.db import DB

        db = DB(str(db_path))
        db.init_schema()

        cursor = db.conn.execute(
            "SELECT last_status, last_error FROM sources_state WHERE source_id='pitchfork_bnt'"
        )
        row = cursor.fetchone()
        assert row is not None, "sources_state deve ter um registo para pitchfork_bnt"

        status, error = row
        assert status == "error", f"Status deve ser 'error', obtive '{status}'"
        assert "simulated source crash" in error, (
            f"Error deve conter 'simulated source crash', obtive '{error}'"
        )

        db.close()


class TestPlaylistSafetyCaps:
    def test_run_limits_new_tracks_per_source(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        db_path = tmp_path / "test.db"
        monkeypatch.setenv("PEEL_PLAYLIST_ID", "spotify:playlist:test")
        monkeypatch.setenv("SPOTIFY_CLIENT_ID", "test_id")
        monkeypatch.setenv("SPOTIFY_CLIENT_SECRET", "test_secret")
        monkeypatch.setenv("SPOTIFY_REFRESH_TOKEN", "test_token")

        from peel import config as config_module

        monkeypatch.setattr(config_module.settings, "db_path", str(db_path))
        monkeypatch.setattr(config_module.settings, "peel_max_tracks_per_source", 2)
        monkeypatch.setattr(config_module.settings, "peel_max_tracks_per_run", 10)

        tracks = [
            Track(source_id="pitchfork_bnt", artist="Artist", title=f"Track {idx}")
            for idx in range(1, 4)
        ]

        mock_sp = MagicMock()
        mock_sp.search_track.side_effect = [
            [{"uri": f"spotify:track:{idx}", "name": f"Track {idx}", "artists": ["Artist"]}]
            for idx in range(1, 4)
        ]
        mock_sp.replace_playlist_items = MagicMock()

        with (
            patch.object(PitchforkBNT, "fetch", return_value=tracks),
            patch.object(StereogumNewMusic, "fetch", return_value=[]),
            patch.object(TheQuietus, "fetch", return_value=[]),
            patch.object(GorillaVsBear, "fetch", return_value=[]),
            patch("peel.main.SpotifyClient", return_value=mock_sp),
            patch("peel.main.send_digest"),
        ):
            run()

        from peel.db import DB

        db = DB(str(db_path))
        db.init_schema()
        count = db.conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0]
        assert count == 2
        called_uris = mock_sp.replace_playlist_items.call_args.args[1]
        assert set(called_uris) == {"spotify:track:1", "spotify:track:2"}
        db.close()

    def test_run_limits_new_tracks_globally(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        db_path = tmp_path / "test.db"
        monkeypatch.setenv("PEEL_PLAYLIST_ID", "spotify:playlist:test")
        monkeypatch.setenv("SPOTIFY_CLIENT_ID", "test_id")
        monkeypatch.setenv("SPOTIFY_CLIENT_SECRET", "test_secret")
        monkeypatch.setenv("SPOTIFY_REFRESH_TOKEN", "test_token")

        from peel import config as config_module

        monkeypatch.setattr(config_module.settings, "db_path", str(db_path))
        monkeypatch.setattr(config_module.settings, "peel_max_tracks_per_source", 10)
        monkeypatch.setattr(config_module.settings, "peel_max_tracks_per_run", 2)

        def track(source_id: str, idx: int) -> Track:
            return Track(source_id=source_id, artist=f"Artist {idx}", title=f"Track {idx}")

        mock_sp = MagicMock()
        mock_sp.search_track.side_effect = [
            [
                {
                    "uri": f"spotify:track:{idx}",
                    "name": f"Track {idx}",
                    "artists": [f"Artist {idx}"],
                }
            ]
            for idx in range(1, 5)
        ]
        mock_sp.replace_playlist_items = MagicMock()

        with (
            patch.object(PitchforkBNT, "fetch", return_value=[track("pitchfork_bnt", 1)]),
            patch.object(
                StereogumNewMusic,
                "fetch",
                return_value=[track("stereogum_new_music", 2)],
            ),
            patch.object(TheQuietus, "fetch", return_value=[track("thequietus", 3)]),
            patch.object(GorillaVsBear, "fetch", return_value=[track("gorillavsbear", 4)]),
            patch("peel.main.SpotifyClient", return_value=mock_sp),
            patch("peel.main.send_digest"),
        ):
            run()

        from peel.db import DB

        db = DB(str(db_path))
        db.init_schema()
        count = db.conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0]
        assert count == 2
        called_uris = mock_sp.replace_playlist_items.call_args.args[1]
        assert set(called_uris) == {"spotify:track:1", "spotify:track:2"}
        db.close()

    def test_run_skips_non_track_non_album_sources(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        db_path = tmp_path / "test.db"
        monkeypatch.setenv("PEEL_PLAYLIST_ID", "spotify:playlist:test")
        monkeypatch.setenv("SPOTIFY_CLIENT_ID", "test_id")
        monkeypatch.setenv("SPOTIFY_CLIENT_SECRET", "test_secret")
        monkeypatch.setenv("SPOTIFY_REFRESH_TOKEN", "test_token")

        from peel import config as config_module

        monkeypatch.setattr(config_module.settings, "db_path", str(db_path))

        context_track = Track(
            source_id="pitchfork_bnt",
            artist="Context Artist",
            title="Context Item",
        )
        mock_sp = MagicMock()
        mock_sp.search_track = MagicMock()
        mock_sp.replace_playlist_items = MagicMock()

        with (
            patch.object(PitchforkBNT, "kind", "context", create=True),
            patch.object(PitchforkBNT, "fetch", return_value=[context_track]),
            patch.object(StereogumNewMusic, "fetch", return_value=[]),
            patch.object(TheQuietus, "fetch", return_value=[]),
            patch.object(GorillaVsBear, "fetch", return_value=[]),
            patch("peel.main.SpotifyClient", return_value=mock_sp),
            patch("peel.main.send_digest"),
        ):
            run()

        mock_sp.search_track.assert_not_called()
        called_uris = mock_sp.replace_playlist_items.call_args.args[1]
        assert called_uris == []


class TestRetryUnmatched:
    """Testa o fluxo de retry de unmatched (fix #2)."""

    def test_retry_promotes_matched_and_cleans_unmatched(self, tmp_path: Path) -> None:
        """Track que antes falhou é promovida quando Spotify agora devolve hit."""
        from peel.db import DB

        db = DB(str(tmp_path / "test.db"))
        db.init_schema()
        db.record_unmatched("pitchfork_bnt", "Claire Rousay", "Hey Eleanor")

        mock_sp = MagicMock()
        mock_sp.search_track = MagicMock(
            return_value=[
                {
                    "uri": "spotify:track:abc",
                    "name": "Hey Eleanor",
                    "artists": ["Claire Rousay"],
                }
            ]
        )

        digest_entries: list = []
        total, matched = _retry_unmatched(db, mock_sp, digest_entries)

        assert total == 1
        assert matched == 1
        # Unmatched foi limpo
        assert db.list_unmatched(30) == []
        # Track foi registada e atribuída à source original
        row = db.conn.execute(
            "SELECT spotify_uri FROM tracks WHERE spotify_uri='spotify:track:abc'"
        ).fetchone()
        assert row is not None
        assert db.track_sources("spotify:track:abc") == [("pitchfork_bnt", None)]
        # Entrou no digest do Telegram
        assert digest_entries == [("Claire Rousay", "Hey Eleanor", None)]
        db.close()

    def test_retry_keeps_source_attribution_for_existing_uri(self, tmp_path: Path) -> None:
        """Se a URI já existir, retry regista a nova source mas não duplica digest."""
        from peel.db import DB

        db = DB(str(tmp_path / "test.db"))
        db.init_schema()

        uri = "spotify:track:shared"
        db.record_track(uri, "source-a", "Artist", "Title", None)
        db.record_unmatched("source-b", "Artist", "Title")

        mock_sp = MagicMock()
        mock_sp.search_track = MagicMock(
            return_value=[
                {
                    "uri": uri,
                    "name": "Title",
                    "artists": ["Artist"],
                }
            ]
        )

        digest_entries: list = []
        total, matched = _retry_unmatched(db, mock_sp, digest_entries)

        assert total == 1
        assert matched == 1
        assert db.list_unmatched(30) == []
        assert db.track_sources(uri) == [("source-a", None), ("source-b", None)]
        assert digest_entries == []
        db.close()

    def test_retry_respects_new_track_cap(self, tmp_path: Path) -> None:
        """Retry não deve promover backlog inteiro quando o cap foi atingido."""
        from peel.db import DB

        db = DB(str(tmp_path / "test.db"))
        db.init_schema()
        db.record_unmatched("source-a", "Artist A", "Track A")
        db.record_unmatched("source-b", "Artist B", "Track B")

        mock_sp = MagicMock()
        mock_sp.search_track.side_effect = [
            [{"uri": "spotify:track:a", "name": "Track A", "artists": ["Artist A"]}],
            [{"uri": "spotify:track:b", "name": "Track B", "artists": ["Artist B"]}],
        ]

        digest_entries: list = []
        total, matched = _retry_unmatched(db, mock_sp, digest_entries, max_new_tracks=1)

        assert total == 2
        assert matched == 1
        assert digest_entries == [("Artist A", "Track A", None)]
        assert db.already_added("spotify:track:a") is True
        assert db.already_added("spotify:track:b") is False
        assert db.list_unmatched(30) == [("source-b", "Artist B", "Track B")]
        db.close()

    def test_retry_handles_empty_table(self, tmp_path: Path) -> None:
        """Sem rows unmatched, retorna (0, 0) sem chamar Spotify."""
        from peel.db import DB

        db = DB(str(tmp_path / "test.db"))
        db.init_schema()

        mock_sp = MagicMock()
        total, matched = _retry_unmatched(db, mock_sp, [])
        assert (total, matched) == (0, 0)
        mock_sp.search_track.assert_not_called()
        db.close()


class TestConsensusAttribution:
    """Tracks iguais vindas de múltiplas sources devem ser atribuídas, não ignoradas."""

    def test_run_records_same_uri_from_multiple_sources_once_in_playlist(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        db_path = tmp_path / "test.db"
        monkeypatch.setenv("PEEL_PLAYLIST_ID", "spotify:playlist:test")
        monkeypatch.setenv("SPOTIFY_CLIENT_ID", "test_id")
        monkeypatch.setenv("SPOTIFY_CLIENT_SECRET", "test_secret")
        monkeypatch.setenv("SPOTIFY_REFRESH_TOKEN", "test_token")

        from peel import config as config_module

        monkeypatch.setattr(config_module.settings, "db_path", str(db_path))

        mock_sp = MagicMock()
        shared_uri = "spotify:track:shared"
        mock_sp.search_track = MagicMock(
            return_value=[
                {
                    "uri": shared_uri,
                    "name": "Shared Track",
                    "artists": ["Shared Artist"],
                }
            ]
        )
        mock_sp.replace_playlist_items = MagicMock()

        shared_track = Track(
            source_id="pitchfork_bnt",
            artist="Shared Artist",
            title="Shared Track",
            source_url="https://example.com/pitchfork",
        )
        shared_track_2 = Track(
            source_id="stereogum_new_music",
            artist="Shared Artist",
            title="Shared Track",
            source_url="https://example.com/stereogum",
        )
        shared_track_3 = Track(
            source_id="thequietus",
            artist="Shared Artist",
            title="Shared Track",
            source_url="https://example.com/quietus",
        )
        shared_track_4 = Track(
            source_id="gorillavsbear",
            artist="Shared Artist",
            title="Shared Track",
            source_url="https://example.com/gvb",
        )

        with (
            patch.object(PitchforkBNT, "fetch", return_value=[shared_track]),
            patch.object(StereogumNewMusic, "fetch", return_value=[shared_track_2]),
            patch.object(TheQuietus, "fetch", return_value=[shared_track_3]),
            patch.object(GorillaVsBear, "fetch", return_value=[shared_track_4]),
            patch("peel.main.SpotifyClient", return_value=mock_sp),
            patch("peel.main.send_digest"),
        ):
            run()

        from peel.db import DB

        db = DB(str(db_path))
        db.init_schema()

        cursor = db.conn.execute(
            "SELECT COUNT(*) FROM tracks WHERE spotify_uri = ?",
            (shared_uri,),
        )
        count = cursor.fetchone()[0]
        assert count == 4
        assert db.track_sources(shared_uri) == [
            ("pitchfork_bnt", "https://example.com/pitchfork"),
            ("stereogum_new_music", "https://example.com/stereogum"),
            ("thequietus", "https://example.com/quietus"),
            ("gorillavsbear", "https://example.com/gvb"),
        ]

        called_uris = mock_sp.replace_playlist_items.call_args.args[1]
        assert called_uris == [shared_uri]

        db.close()
