"""Testes para o cliente Spotify."""

from unittest.mock import MagicMock, call, patch

import pytest

from peel.spotify_client import SpotifyClient, _and_the_variant, _clean_for_query


class TestCleanForQuery:
    """Normalização de strings antes da search Spotify."""

    def test_strip_feat_parens(self) -> None:
        assert _clean_for_query("Miss You (feat. Nourished By Time)") == "Miss You"

    def test_strip_ft_brackets(self) -> None:
        assert _clean_for_query("Landgrab [ft. Earl Sweatshirt]") == "Landgrab"

    def test_strip_curly_quotes(self) -> None:
        assert _clean_for_query("\u201cLandgrab\u201d") == "Landgrab"

    def test_collab_x_to_ampersand(self) -> None:
        assert _clean_for_query("Shlohmo x SALEM") == "Shlohmo & SALEM"

    def test_strip_residual_brackets(self) -> None:
        assert _clean_for_query("Title [Album Version]") == "Title"

    def test_collapse_whitespace(self) -> None:
        assert _clean_for_query("  Hello   World  ") == "Hello World"

    def test_preserves_normal_text(self) -> None:
        assert _clean_for_query("Abigail Snail") == "Abigail Snail"


class TestAndTheVariant:
    def test_and_the_detected(self) -> None:
        assert _and_the_variant("Ryan Davis And The Roadhouse Band") == (
            "Ryan Davis & The Roadhouse Band"
        )

    def test_case_insensitive(self) -> None:
        assert _and_the_variant("Artist and the Band") == "Artist & The Band"

    def test_no_variant_when_absent(self) -> None:
        assert _and_the_variant("Massive Attack") is None
        assert _and_the_variant("Ryan Davis Roadhouse") is None


class TestSearchTrackFallback:
    """Verifica o fluxo de fallback And The no search_track."""

    def test_uses_fallback_variant_when_primary_empty(self, mock_spotify_client):
        client, mock_sp = mock_spotify_client

        # Primeira chamada vazia; segunda (com variante) devolve um hit
        hit = {
            "tracks": {
                "items": [
                    {
                        "uri": "spotify:track:xxx",
                        "name": "New Threats From the Soul",
                        "artists": [{"name": "Ryan Davis & The Roadhouse Band"}],
                    }
                ]
            }
        }
        mock_sp.search.side_effect = [{"tracks": {"items": []}}, hit]

        candidates = client.search_track(
            "Ryan Davis And The Roadhouse Band",
            "New Threats From the Soul",
        )
        assert len(candidates) == 1
        assert candidates[0]["uri"] == "spotify:track:xxx"
        # Primeira query com "And The", segunda com "& The"
        first_q = mock_sp.search.call_args_list[0].kwargs["q"]
        second_q = mock_sp.search.call_args_list[1].kwargs["q"]
        assert "And The" in first_q
        assert "& The" in second_q

    def test_feat_stripped_from_query(self, mock_spotify_client):
        client, mock_sp = mock_spotify_client
        mock_sp.search.return_value = {"tracks": {"items": []}}

        client.search_track("Ms Ray", "Miss You (feat. Nourished By Time)")
        query = mock_sp.search.call_args_list[0].kwargs["q"]
        assert "feat" not in query.lower()
        assert "Miss You" in query


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
