"""Testes para o módulo de Telegram."""

from unittest.mock import MagicMock, patch

import pytest

from peel.telegram import MAX_MESSAGE_LENGTH, _format_message, _split_message, send_digest


class TestFormatMessage:
    """Testa a formatação da mensagem HTML do Telegram."""

    def test_format_message_with_tracks_and_albums(self) -> None:
        """Formata mensagem com tracks e álbuns."""
        tracks = [("source-a", "Artist A", "Track 1", None)]  # Tracks não têm URLs no digest
        albums = [("source-b", "Artist B", "Album 1", "http://example.com/album")]
        playlist_id = "spotify:playlist:test123"

        msg = _format_message(tracks, albums, playlist_id)

        # Verifica estrutura básica
        assert "<b>🎵 Peel — Weekly Digest</b>" in msg
        assert "<b>Novas tracks (1)</b>" in msg
        assert "Artist A" in msg
        assert "Track 1" in msg
        assert "(Source A)" in msg
        assert "<b>💿 Álbuns da semana (1)</b>" in msg
        assert "Artist B" in msg
        assert "Album 1" in msg
        assert "(Source B)" in msg
        assert "spotify:playlist:test123" in msg
        assert '<a href="http://example.com/album">' in msg

    def test_format_message_affinity_badge(self, monkeypatch) -> None:
        """Mostra 🎯 quando a afinidade passa o threshold."""
        monkeypatch.setattr("peel.telegram.settings.affinity_badge_threshold", 0.75)
        tracks = [("source", "Artist", "Track", None, 1, 0.9)]

        msg = _format_message(tracks, [], "test_id")

        assert "• 🎯 Artist — Track" in msg

    def test_format_message_empty_tracks(self) -> None:
        """Formata mensagem sem tracks."""
        tracks = []
        albums = [("source", "Artist", "Album", None)]
        playlist_id = "test_id"

        msg = _format_message(tracks, albums, playlist_id)

        assert "Sem tracks novas esta semana" in msg
        assert "💿 Álbuns da semana (1)" in msg

    def test_format_message_empty_albums(self) -> None:
        """Formata mensagem sem álbuns."""
        tracks = [("source", "Artist", "Track", None)]
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
        tracks = [("source", f"Artist {i}", f"Track {i}", None) for i in range(25)]
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
        albums = [("source", f"Artist {i}", f"Album {i}", None) for i in range(20)]
        tracks = []
        playlist_id = "test_id"

        msg = _format_message(tracks, albums, playlist_id)

        # Verifica que mostra os primeiros 15
        assert "💿 Álbuns da semana (20)" in msg
        assert "Album 14" in msg
        assert "Album 19" not in msg

    def test_format_message_html_escaping(self) -> None:
        """Escape de caracteres HTML na mensagem."""
        tracks = [("source&", "Artist <tag>", "Track & Title", None)]
        albums = [("source", 'Artist "quotes"', "Album <script>", None)]
        playlist_id = "test_id"

        msg = _format_message(tracks, albums, playlist_id)

        # Verifica que os caracteres foram escapados
        assert "Artist &lt;tag&gt;" in msg
        assert "Track &amp; Title" in msg
        assert "Artist &quot;quotes&quot;" in msg
        assert "Album &lt;script&gt;" in msg
        assert "Source&amp;" in msg

    def test_format_message_album_with_url(self) -> None:
        """Formata álbum com URL como link."""
        albums = [("source", "Artist", "Album", "https://example.com/album")]
        tracks = []
        playlist_id = "test_id"

        msg = _format_message(tracks, albums, playlist_id)

        # URL deve estar num <a href>
        assert '<a href="https://example.com/album">' in msg
        assert "Artist" in msg

    def test_format_message_album_without_url(self) -> None:
        """Formata álbum sem URL como texto simples."""
        albums = [("source", "Artist", "Album", None)]
        tracks = []
        playlist_id = "test_id"

        msg = _format_message(tracks, albums, playlist_id)

        # Sem <a href>, apenas texto
        assert "• Artist — Album" in msg
        assert "<a href=" not in msg or '<a href="https://open.spotify.com' in msg

    def test_format_message_with_external_entries(self) -> None:
        """Formata escutas externas que não entraram no Spotify."""
        msg = _format_message(
            [],
            [],
            "test_id",
            external_entries=[
                (
                    "stereogum_new_music",
                    "Helado Negro",
                    "Dance To The Music",
                    "https://stereogum.com/example",
                )
            ],
        )

        assert "Escutas externas (1)" in msg
        assert "Helado Negro" in msg
        assert "Dance To The Music" in msg
        assert "Stereogum" in msg
        assert '<a href="https://stereogum.com/example">' in msg

    def test_format_message_marks_consensus_tracks(self) -> None:
        """Tracks com mais de uma source mostram estrela e contagem."""
        msg = _format_message(
            [
                ("source-a", "Artist A", "Shared", None, 2),
                ("source-b", "Artist B", "Single", None),
            ],
            [],
            "test_id",
        )

        assert "⭐ Artist A — Shared" in msg
        assert "Source A, 2 fontes" in msg
        assert "⭐ Artist B — Single" not in msg
        assert "Artist B — Single <i>(Source B)</i>" in msg

    def test_format_message_renders_album_recommendations(self) -> None:
        """7 Álbuns a Ouvir mostra consenso e link preferido."""
        msg = _format_message(
            [],
            [],
            "test_id",
            album_recommendations=[
                (
                    "Wax Machine",
                    "The Sky Unfurls",
                    2,
                    ("aquarium_drunkard", "pitchfork_best_albums"),
                    "https://open.spotify.com/album/abc123",
                ),
                (
                    "No Spotify",
                    "Fallback Album",
                    1,
                    ("guardian_music_albums",),
                    "https://guardian/fallback",
                ),
            ],
        )

        assert "🎧 7 Álbuns a Ouvir (2)" in msg
        assert "⭐ " in msg
        assert "2 fontes: Aquarium Drunkard, Pitchfork" in msg
        assert '<a href="https://open.spotify.com/album/abc123">Wax Machine' in msg
        assert '<a href="https://guardian/fallback">No Spotify' in msg

    def test_format_message_renders_album_source_link(self) -> None:
        """Álbuns podem ter link primário para ouvir e link secundário para fonte."""
        msg = _format_message(
            [],
            [],
            "test_id",
            album_recommendations=[
                (
                    "Goya Gumbani",
                    "Warlord of the Weejuns",
                    1,
                    ("bandcamp_ghostly",),
                    "https://open.spotify.com/album/abc123",
                    "https://goyagumbani.bandcamp.com/album/warlord-of-the-weejuns",
                ),
                (
                    "Khun Narin Electric Phin Band",
                    "III",
                    1,
                    ("thequietus",),
                    "https://open.spotify.com/search/Khun%20Narin%20Electric%20Phin%20Band%20III",
                    "https://thequietus.com/review",
                ),
            ],
        )

        assert '<a href="https://open.spotify.com/album/abc123">Goya Gumbani' in msg
        assert (
            '<a href="https://goyagumbani.bandcamp.com/album/warlord-of-the-weejuns">Bandcamp</a>'
        ) in msg
        assert (
            '<a href="https://open.spotify.com/search/'
            'Khun%20Narin%20Electric%20Phin%20Band%20III">Khun Narin'
        ) in msg
        assert '<a href="https://thequietus.com/review">Review</a>' in msg


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
            tracks = [("source-a", "Artist A", "Track A", None)]
            albums = [("source-b", "Artist B", "Album B", None)]
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
            send_digest([("source-a", "A", "T", None)], [("source-b", "B", "Album", None)], "id")

            # Verifica que log.info foi chamado com "telegram.sent"
            calls = [call[0][0] for call in mock_log.info.call_args_list]
            assert "telegram.sent" in calls


class TestMessageChunking:
    """Garante que mensagens grandes são partidas para respeitar o limite 4096."""

    def test_split_message_short_returns_single_chunk(self) -> None:
        assert _split_message("curta", MAX_MESSAGE_LENGTH) == ["curta"]

    def test_split_message_long_splits_at_line_boundaries(self) -> None:
        # 3 linhas de 2000 chars cada → 2 chunks (2000 + 2000+1 separador excede 4096).
        line = "x" * 2000
        text = f"{line}\n{line}\n{line}"
        chunks = _split_message(text, MAX_MESSAGE_LENGTH)
        assert len(chunks) == 2
        # Cada chunk respeita o limite.
        assert all(len(c) <= MAX_MESSAGE_LENGTH for c in chunks)
        # Nada perdido: a concatenação com \n repõe o texto.
        assert "\n".join(chunks) == text

    def test_split_message_hard_wraps_pathological_line(self) -> None:
        line = "y" * 9_000
        chunks = _split_message(line, MAX_MESSAGE_LENGTH)
        assert all(len(c) <= MAX_MESSAGE_LENGTH for c in chunks)
        assert "".join(chunks) == line

    def test_send_digest_chunks_long_message_into_multiple_posts(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Uma digest > 4096 deve resultar em vários httpx.post, cada um ≤ 4096."""
        from peel import config as config_module

        monkeypatch.setattr(config_module.settings, "telegram_bot_token", "token")
        monkeypatch.setattr(config_module.settings, "telegram_chat_id", "chat")

        # Força uma mensagem longa sem depender dos caps de display do formatter.
        long_line = "x" * 3000
        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()

        with (
            patch("peel.telegram._format_message", return_value=f"{long_line}\n{long_line}"),
            patch("peel.telegram.httpx.post", return_value=mock_response) as mock_post,
        ):
            send_digest([("source-a", "A", "T", None)], [], "playlist_id")

            assert mock_post.call_count >= 2
            for call in mock_post.call_args_list:
                payload = call[1]["json"]
                assert len(payload["text"]) <= MAX_MESSAGE_LENGTH

    def test_send_digest_abort_remaining_chunks_on_failure(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Se um chunk falha a meio, os restantes não são tentados (best-effort)."""
        from peel import config as config_module

        monkeypatch.setattr(config_module.settings, "telegram_bot_token", "token")
        monkeypatch.setattr(config_module.settings, "telegram_chat_id", "chat")

        long_line = "x" * 3000
        ok = MagicMock()
        ok.raise_for_status = MagicMock()
        with (
            patch("peel.telegram._format_message", return_value=f"{long_line}\n{long_line}"),
            patch("peel.telegram.httpx.post", side_effect=[ok, Exception("boom")]) as mock_post,
        ):
            # Não deve levantar; só o 1º chunk é tentado (depois falha e aborta).
            send_digest([("source-a", "A", "T", None)], [], "playlist_id")

        assert mock_post.call_count == 2
