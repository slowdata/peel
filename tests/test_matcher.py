"""Testes para o matcher com fuzzy string comparison."""

from peel.matcher import best_match, is_match, normalize, score
from peel.models import Track


class TestNormalize:
    """Testa a normalização de strings."""

    def test_lowercase(self) -> None:
        """Converte para lowercase."""
        assert normalize("RADIOHEAD") == "radiohead"

    def test_remove_accents(self) -> None:
        """Remove acentos."""
        assert normalize("Björk") == "bjork"
        assert normalize("Beyoncé") == "beyonce"
        assert normalize("Asha Bhosle") == "asha bhosle"

    def test_remove_deluxe_suffix(self) -> None:
        """Remove (deluxe) e variações."""
        assert normalize("Blinding Lights (Deluxe)") == "blinding lights"
        assert normalize("Something (Deluxe Edition)") == "something"

    def test_remove_remastered_suffix(self) -> None:
        """Remove (remastered) e variações."""
        assert normalize("Karma Police (Remastered 2019)") == "karma police"
        assert normalize("Song - Remastered 2020") == "song"

    def test_remove_feat_suffix(self) -> None:
        """Remove feat. e ft. em qualquer posição."""
        assert normalize("Alright (feat. Pharrell Williams)") == "alright"
        assert normalize("Song feat. Artist") == "song"
        assert normalize("Track ft. Someone") == "track"

    def test_remove_radio_edit(self) -> None:
        """Remove (radio edit)."""
        assert normalize("Song (Radio Edit)") == "song"

    def test_collapse_whitespace(self) -> None:
        """Colapsa whitespace múltiplo."""
        assert normalize("Song    With   Spaces") == "song with spaces"

    def test_combined_cleanup(self) -> None:
        """Teste combinado."""
        result = normalize("Blinding Lights (Feat. The Weeknd - Deluxe)")
        assert result == "blinding lights"


class TestScore:
    """Testa o scoring com token_set_ratio."""

    def test_exact_match(self) -> None:
        """Strings idênticas têm score 100."""
        assert score("radiohead", "radiohead") == 100

    def test_similar_strings(self) -> None:
        """Strings similares têm score alto."""
        result = score("radiohead", "radioheadx")
        assert result > 80

    def test_token_set_ratio_robustness(self) -> None:
        """token_set_ratio é imune a palavras extra."""
        # "alright" vs "alright something" tem score alto
        result = score("alright", "alright something")
        assert result >= 80

    def test_different_strings(self) -> None:
        """Strings muito diferentes têm score baixo."""
        result = score("radiohead", "taylor swift")
        assert result < 50


class TestIsMatch:
    """Testa a validação de matches (artist AND title)."""

    def test_exact_match(self) -> None:
        """Caso exato deve passar."""
        track = Track(source_id="test", artist="Radiohead", title="Idioteque")
        assert is_match(track, "Radiohead", "Idioteque") is True

    def test_accents_and_case(self) -> None:
        """Acentos e maiúsculas não importam (Beyoncé vs Beyonce)."""
        track = Track(source_id="test", artist="Beyoncé", title="Halo")
        assert is_match(track, "Beyonce", "Halo") is True

    def test_remove_remastered_suffix(self) -> None:
        """Remove (Remastered) e ainda casa."""
        track = Track(source_id="test", artist="Radiohead", title="Karma Police")
        assert is_match(track, "Radiohead", "Karma Police - Remastered 2019") is True

    def test_remove_deluxe_suffix(self) -> None:
        """Remove (Deluxe) e ainda casa."""
        track = Track(source_id="test", artist="The Weeknd", title="Blinding Lights")
        assert is_match(track, "The Weeknd", "Blinding Lights (Deluxe)") is True

    def test_remove_feat_suffix(self) -> None:
        """Remove feat. e ainda casa."""
        track = Track(source_id="test", artist="Kendrick Lamar", title="Alright")
        assert is_match(track, "Kendrick Lamar", "Alright (feat. Pharrell Williams)") is True

    def test_accent_normalization(self) -> None:
        """Björk vs Bjork casam."""
        track = Track(source_id="test", artist="Björk", title="Hyperballad")
        assert is_match(track, "Bjork", "Hyperballad") is True

    def test_no_match_different_artist(self) -> None:
        """Artista diferente não casa."""
        track = Track(source_id="test", artist="Radiohead", title="Creep")
        assert is_match(track, "Radiohead", "Karma Police") is False

    def test_no_match_different_artists(self) -> None:
        """Artistas diferentes não casam."""
        track = Track(source_id="test", artist="Taylor Swift", title="Shake It Off")
        assert is_match(track, "Ed Sheeran", "Shake It Off") is False

    def test_threshold_customizable(self) -> None:
        """Threshold pode ser ajustado."""
        track = Track(source_id="test", artist="Song", title="Title")
        # Com threshold alto, coisas parecidas não casam
        assert is_match(track, "Songx", "Titlex", threshold=95) is False
        # Com threshold baixo, casam
        assert is_match(track, "Songx", "Titlex", threshold=70) is True


class TestBestMatch:
    """Testa a selection do melhor candidato."""

    def test_exact_match_returned(self) -> None:
        """Match exato é devolvido."""
        track = Track(source_id="test", artist="Radiohead", title="Idioteque")
        candidates = [
            {
                "uri": "spotify:track:123",
                "name": "Idioteque",
                "artists": ["Radiohead"],
            }
        ]
        result = best_match(track, candidates)
        assert result == "spotify:track:123"

    def test_best_of_multiple(self) -> None:
        """Melhor match de vários é selecionado."""
        track = Track(source_id="test", artist="Radiohead", title="Idioteque")
        candidates = [
            {
                "uri": "spotify:track:bad",
                "name": "Something Else",
                "artists": ["Different Artist"],
            },
            {
                "uri": "spotify:track:good",
                "name": "Idioteque",
                "artists": ["Radiohead"],
            },
        ]
        result = best_match(track, candidates)
        assert result == "spotify:track:good"

    def test_no_match_under_threshold(self) -> None:
        """Nenhum match abaixo threshold devolve None."""
        track = Track(source_id="test", artist="Radiohead", title="Idioteque")
        candidates = [
            {
                "uri": "spotify:track:123",
                "name": "Taylor Swift Song",
                "artists": ["Taylor Swift"],
            }
        ]
        result = best_match(track, candidates)
        assert result is None

    def test_empty_candidates(self) -> None:
        """Lista vazia de candidatos devolve None."""
        track = Track(source_id="test", artist="Radiohead", title="Idioteque")
        result = best_match(track, [])
        assert result is None

    def test_multiple_artists_combined(self) -> None:
        """Vários artistas são combinados em string."""
        track = Track(source_id="test", artist="Massive Attack", title="Boots on the Ground")
        candidates = [
            {
                "uri": "spotify:track:123",
                "name": "Boots on the Ground",
                "artists": ["Massive Attack", "Tom Waits"],
            }
        ]
        result = best_match(track, candidates)
        # Mesmo com "Tom Waits" na lista, "Massive Attack" é encontrado por token_set_ratio
        assert result == "spotify:track:123"

    def test_with_custom_threshold(self) -> None:
        """Threshold customizável funciona."""
        track = Track(source_id="test", artist="Song", title="Title")
        candidates = [
            {
                "uri": "spotify:track:123",
                "name": "Titlex",  # Levemente diferente
                "artists": ["Songx"],
            }
        ]
        # Threshold alto: não casa
        assert best_match(track, candidates, threshold=95) is None
        # Threshold baixo: casa
        assert best_match(track, candidates, threshold=70) == "spotify:track:123"

    def test_with_deluxe_and_feat(self) -> None:
        """Testa múltiplos sufixos ao mesmo tempo."""
        track = Track(source_id="test", artist="The Weeknd", title="Blinding Lights")
        candidates = [
            {
                "uri": "spotify:track:123",
                "name": "Blinding Lights (Deluxe) (feat. SomeArtist)",
                "artists": ["The Weeknd"],
            }
        ]
        result = best_match(track, candidates)
        assert result == "spotify:track:123"
