"""Testes para o módulo de Telegram."""

from unittest.mock import MagicMock, patch

import pytest

from peel.telegram import _format_message, send_digest


class TestFormatMessage:
    """Testa a formatação da mensagem HTML do Telegram."""

    def test_format_message_with_tracks_and_albums(self) -> None:
        """Formata mensagem com tracks e álbuns."""
        tracks = [("Artist A", "Track 1", None)]  # Tracks não têm URLs no digest
        albums = [("Artist B", "Album 1", "http://example.com/album")]
        playlist_id = "spotify:playlist:test123"

        msg = _format_message(tracks, albums, playlist_id)

        # Verifica estrutura básica
        assert "<b>🎵 Peel — Weekly Digest</b>" in msg
        assert "<b>Novas tracks (1)</b>" in msg
        assert "Artist A" in msg
        assert "Track 1" in msg
        assert "<b>💿 Álbuns da semana (1)</b>" in msg
        assert "Artist B" in msg
        assert "Album 1" in msg
        assert "spotify:playlist:test123" in msg
        assert '<a href="http://example.com/album">' in msg

    def test_format_message_empty_tracks(self) -> None:
        """Formata mensagem sem tracks."""
        tracks = []
        albums = [("Artist", "Album", None)]
        playlist_id = "test_id"

        msg = _format_message(tracks, albums, playlist_id)

        assert "Sem tracks novas esta semana" in msg
        assert "💿 Álbuns da semana (1)" in msg

    def test_format_message_empty_albums(self) -> None:
        """Formata mensagem sem álbuns."""
        tracks = [("Artist", "Track", None)]
        albums = []
        playlist_id = "test_id"

        msg = _format_message(tracks, albums, playlist_id)

        assert "Novas tracks (1)" in msg
        assert "Sem álbuns novos esta semana" in msg

    def test_format_message_both_empty(self) -> None:
        """Formata mensagem com tracks e álbuns vazios."""
        tracks = []
        albums = []
        playlist_id = "test_id"

        msg = _format_message(tracks, albums, playlist_id)

        assert "Sem tracks novas esta semana" in msg
        assert "Sem álbuns novos esta semana" in msg

    def test_format_message_tracks_overflow(self) -> None:
        """Formata mensagem com mais de 20 tracks."""
        tracks = [(f"Artist {i}", f"Track {i}", None) for i in range(25)]
        albums = []
        playlist_id = "test_id"

        msg = _format_message(tracks, albums, playlist_id)

        # Conta as tracks exibidas (deve ter 20 + "... e mais 5")
        assert "Novas tracks (25)" in msg
        assert "... e mais 5" in msg
        # Verifica que só mostra os primeiros 20 (0-19)
        assert "Track 19" in msg
        assert "Track 20" not in msg
        assert "Track 24" not in msg

    def test_format_message_albums_overflow(self) -> None:
        """Formata mensagem com mais de 15 álbuns."""
        albums = [(f"Artist {i}", f"Album {i}", None) for i in range(20)]
        tracks = []
        playlist_id = "test_id"

        msg = _format_message(tracks, albums, playlist_id)

        # Verifica que mostra os primeiros 15
        assert "💿 Álbuns da semana (20)" in msg
        assert "Album 14" in msg
        assert "Album 19" not in msg

    def test_format_message_html_escaping(self) -> None:
        """Escape de caracteres HTML na mensagem."""
        tracks = [("Artist <tag>", "Track & Title", None)]
        albums = [('Artist "quotes"', "Album <script>", None)]
        playlist_id = "test_id"

        msg = _format_message(tracks, albums, playlist_id)

        # Verifica que os caracteres foram escapados
        assert "Artist &lt;tag&gt;" in msg
        assert "Track &amp; Title" in msg
        assert "Artist &quot;quotes&quot;" in msg
        assert "Album &lt;script&gt;" in msg

    def test_format_message_album_with_url(self) -> None:
        """Formata álbum com URL como link."""
        albums = [("Artist", "Album", "https://example.com/album")]
        tracks = []
        playlist_id = "test_id"

        msg = _format_message(tracks, albums, playlist_id)

        # URL deve estar num <a href>
        assert '<a href="https://example.com/album">' in msg
        assert "Artist" in msg

    def test_format_message_album_without_url(self) -> None:
        """Formata álbum sem URL como texto simples."""
        albums = [("Artist", "Album", None)]
        tracks = []
        playlist_id = "test_id"

        msg = _format_message(tracks, albums, playlist_id)

        # Sem <a href>, apenas texto
        assert "• Artist — Album" in msg
        assert "<a href=" not in msg or '<a href="https://open.spotify.com' in msg


class TestSendDigest:
    """Testa a função send_digest."""

    def test_send_digest_skips_without_credentials(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """send_digest() com credentials em falta não faz HTTP."""
        # Desabilita Telegram
        from peel import config as config_module

        monkeypatch.setattr(config_module.settings, "telegram_bot_token", None)
        monkeypatch.setattr(config_module.settings, "telegram_chat_id", None)

        # Mocka httpx para garantir que não é chamado
        with patch("peel.telegram.httpx.post") as mock_post:
            send_digest([], [], "test_id")
            mock_post.assert_not_called()

    def test_send_digest_skips_without_token(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """send_digest() sem token não faz HTTP."""
        from peel import config as config_module

        monkeypatch.setattr(config_module.settings, "telegram_bot_token", None)
        monkeypatch.setattr(config_module.settings, "telegram_chat_id", "chat123")

        with patch("peel.telegram.httpx.post") as mock_post:
            send_digest([], [], "test_id")
            mock_post.assert_not_called()

    def test_send_digest_skips_without_chat_id(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """send_digest() sem chat_id não faz HTTP."""
        from peel import config as config_module

        monkeypatch.setattr(config_module.settings, "telegram_bot_token", "token123")
        monkeypatch.setattr(config_module.settings, "telegram_chat_id", None)

        with patch("peel.telegram.httpx.post") as mock_post:
            send_digest([], [], "test_id")
            mock_post.assert_not_called()

    def test_send_digest_calls_http_with_correct_payload(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """send_digest() com credentials faz HTTP com payload correto."""
        from peel import config as config_module

        token = "bot_token_123"
        chat_id = "chat_456"
        monkeypatch.setattr(config_module.settings, "telegram_bot_token", token)
        monkeypatch.setattr(config_module.settings, "telegram_chat_id", chat_id)

        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()

        with patch("peel.telegram.httpx.post", return_value=mock_response) as mock_post:
            tracks = [("Artist A", "Track A", None)]
            albums = [("Artist B", "Album B", None)]
            playlist_id = "playlist123"

            send_digest(tracks, albums, playlist_id)

            # Verifica que httpx.post foi chamado
            assert mock_post.called
            call_args = mock_post.call_args

            # Verifica URL
            url = call_args[0][0]
            assert f"bot{token}/sendMessage" in url

            # Verifica payload
            payload = call_args[1]["json"]
            assert payload["chat_id"] == chat_id
            assert "HTML" in payload["parse_mode"]
            assert "text" in payload

    def test_send_digest_handles_http_failure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """send_digest() com falha HTTP loga mas não levanta."""
        from peel import config as config_module

        monkeypatch.setattr(config_module.settings, "telegram_bot_token", "token")
        monkeypatch.setattr(config_module.settings, "telegram_chat_id", "chat")

        # Simula erro de HTTP
        with patch("peel.telegram.httpx.post", side_effect=Exception("Network error")):
            # Não deve levantar
            send_digest([], [], "test_id")

    def test_send_digest_logs_on_success(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """send_digest() loga sucesso ao enviar."""
        from peel import config as config_module

        monkeypatch.setattr(config_module.settings, "telegram_bot_token", "token")
        monkeypatch.setattr(config_module.settings, "telegram_chat_id", "chat")

        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()

        with (
            patch("peel.telegram.httpx.post", return_value=mock_response),
            patch("peel.telegram.log") as mock_log,
        ):
            send_digest([("A", "T", None)], [("B", "Album", None)], "id")

            # Verifica que log.info foi chamado com "telegram.sent"
            calls = [call[0][0] for call in mock_log.info.call_args_list]
            assert "telegram.sent" in calls
