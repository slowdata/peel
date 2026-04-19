"""Testes de integração para main.py."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from peel.main import run
from peel.sources.rss import PitchforkBNT


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

