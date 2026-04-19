"""Testes para o cliente Spotify."""

from unittest.mock import MagicMock, call, patch

import pytest

from peel.spotify_client import SpotifyClient


@pytest.fixture
def mock_spotify_client():
    """Fixture: mocka spotipy.Spotify."""
    with patch("peel.spotify_client.SpotifyOAuth") as mock_auth:
        # Setup do mock auth
        mock_auth_instance = MagicMock()
        mock_auth_instance.refresh_access_token.return_value = {"access_token": "test_token"}
        mock_auth.return_value = mock_auth_instance

        with patch("peel.spotify_client.spotipy.Spotify") as mock_sp:
            mock_sp_instance = MagicMock()
            mock_sp.return_value = mock_sp_instance

            client = SpotifyClient()
            yield client, mock_sp_instance


class TestReplacePlaylistItems:
    """Testa o método replace_playlist_items."""

    def test_replace_playlist_items_empty_list(self, mock_spotify_client):
        """replace_playlist_items com lista vazia limpa a playlist."""
        client, mock_sp = mock_spotify_client

        client.replace_playlist_items("playlist:123", [])

        # Deve chamar playlist_replace_items com lista vazia
        mock_sp.playlist_replace_items.assert_called_once_with("playlist:123", [])
        # Não deve chamar playlist_add_items
        mock_sp.playlist_add_items.assert_not_called()

    def test_replace_playlist_items_50_uris(self, mock_spotify_client):
        """replace_playlist_items com 50 URIs (< 100)."""
        client, mock_sp = mock_spotify_client
        uris = [f"spotify:track:{i}" for i in range(50)]

        client.replace_playlist_items("playlist:123", uris)

        # Deve chamar playlist_replace_items com os 50
        mock_sp.playlist_replace_items.assert_called_once_with("playlist:123", uris)
        # Não deve chamar playlist_add_items
        mock_sp.playlist_add_items.assert_not_called()

    def test_replace_playlist_items_100_uris(self, mock_spotify_client):
        """replace_playlist_items com exatamente 100 URIs."""
        client, mock_sp = mock_spotify_client
        uris = [f"spotify:track:{i}" for i in range(100)]

        client.replace_playlist_items("playlist:123", uris)

        # Deve chamar playlist_replace_items com os 100
        mock_sp.playlist_replace_items.assert_called_once_with("playlist:123", uris)
        # Não deve chamar playlist_add_items
        mock_sp.playlist_add_items.assert_not_called()

    def test_replace_playlist_items_250_uris(self, mock_spotify_client):
        """replace_playlist_items com 250 URIs (> 100)."""
        client, mock_sp = mock_spotify_client
        uris = [f"spotify:track:{i}" for i in range(250)]

        client.replace_playlist_items("playlist:123", uris)

        # Deve chamar playlist_replace_items com primeiros 100
        mock_sp.playlist_replace_items.assert_called_once_with("playlist:123", uris[:100])

        # Deve chamar playlist_add_items 2x (100 + 50)
        assert mock_sp.playlist_add_items.call_count == 2
        calls = mock_sp.playlist_add_items.call_args_list
        assert calls[0] == call("playlist:123", uris[100:200])
        assert calls[1] == call("playlist:123", uris[200:250])

    def test_replace_playlist_items_exact_200_uris(self, mock_spotify_client):
        """replace_playlist_items com 200 URIs (replace 100 + add 100)."""
        client, mock_sp = mock_spotify_client
        uris = [f"spotify:track:{i}" for i in range(200)]

        client.replace_playlist_items("playlist:123", uris)

        # playlist_replace_items com 100
        mock_sp.playlist_replace_items.assert_called_once_with("playlist:123", uris[:100])

        # playlist_add_items 1x com os 100 restantes
        mock_sp.playlist_add_items.assert_called_once_with("playlist:123", uris[100:200])

    def test_replace_playlist_items_exception_on_replace(self, mock_spotify_client):
        """replace_playlist_items levanta exceção se replace falhar."""
        client, mock_sp = mock_spotify_client
        uris = [f"spotify:track:{i}" for i in range(50)]

        # Setup: replace falha
        mock_sp.playlist_replace_items.side_effect = Exception("API error")

        with pytest.raises(Exception, match="API error"):
            client.replace_playlist_items("playlist:123", uris)

    def test_replace_playlist_items_exception_on_add(self, mock_spotify_client):
        """replace_playlist_items levanta exceção se add falhar após replace."""
        client, mock_sp = mock_spotify_client
        uris = [f"spotify:track:{i}" for i in range(150)]

        # Setup: add falha na 2ª chamada
        mock_sp.playlist_add_items.side_effect = Exception("API error on add")

        with pytest.raises(Exception, match="API error on add"):
            client.replace_playlist_items("playlist:123", uris)
