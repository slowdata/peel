"""Testes para as fontes RSS."""

from pathlib import Path

import pytest

from peel.sources.rss import PitchforkBNT


class TestRSSSplitArtistTitle:
    """Testa o parsing de artist/title de strings de título."""

    def test_colon_with_quotes(self) -> None:
        """Padrão: "Artist: 'Track Title'"""
        source = PitchforkBNT()
        artist, title = source._split_artist_title("The Smile: 'A Light for Attracting Attention'")
        assert artist == "The Smile"
        assert title == "A Light for Attracting Attention"

    def test_dash_with_accents(self) -> None:
        """Padrão: "Björk – 'Track'" com acentos."""
        source = PitchforkBNT()
        artist, title = source._split_artist_title("Björk – 'Arisen My Senses'")
        assert artist == "Björk"
        assert title == "Arisen My Senses"

    def test_hyphen_no_quotes(self) -> None:
        """Padrão: "Artist - Track Title" sem aspas."""
        source = PitchforkBNT()
        artist, title = source._split_artist_title("Radiohead - Creep")
        assert artist == "Radiohead"
        assert title == "Creep"

    def test_empty_string(self) -> None:
        """String vazia retorna ("", "")."""
        source = PitchforkBNT()
        artist, title = source._split_artist_title("")
        assert artist == ""
        assert title == ""

    def test_no_separator(self) -> None:
        """Sem separador retorna ("", "")."""
        source = PitchforkBNT()
        artist, title = source._split_artist_title("Just A Title")
        assert artist == ""
        assert title == ""


class TestPitchforkBNTFixture:
    """Testa o parsing do fixture XML do Pitchfork."""

    @pytest.fixture
    def fixture_path(self) -> Path:
        """Retorna o path do fixture XML."""
        return Path(__file__).parent / "fixtures" / "pitchfork_bnt.xml"

    def test_fixture_exists(self, fixture_path: Path) -> None:
        """Valida que o fixture existe."""
        assert fixture_path.exists(), f"Fixture not found: {fixture_path}"

    def test_fetch_from_fixture(self, fixture_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Testa o parse do fixture XML."""
        # Monkey-patch a URL para apontar para o fixture local
        fixture_url = fixture_path.as_uri()
        monkeypatch.setattr(PitchforkBNT, "url", fixture_url)

        source = PitchforkBNT()
        tracks = source.fetch()

        # Valida que temos tracks
        assert len(tracks) >= 3, f"Expected >=3 tracks, got {len(tracks)}"

        # Valida primeira track (The Smile)
        t1 = tracks[0]
        assert t1.artist == "The Smile"
        assert t1.title == "A Light for Attracting Attention"
        assert t1.source_id == "pitchfork_bnt"
        assert t1.source_url is not None

        # Valida segunda track (Björk) — verifica acentos
        t2 = tracks[1]
        assert t2.artist == "Björk"
        assert t2.title == "Arisen My Senses"

        # Valida terceira track (Radiohead)
        t3 = tracks[2]
        assert t3.artist == "Radiohead"
        assert t3.title == "Creep Remastered"

    def test_no_empty_artist_or_title(
        self, fixture_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Valida que todas as tracks têm artist e title não-vazios."""
        fixture_url = fixture_path.as_uri()
        monkeypatch.setattr(PitchforkBNT, "url", fixture_url)

        source = PitchforkBNT()
        tracks = source.fetch()

        for track in tracks:
            assert track.artist, f"Track {track.raw_title} has empty artist"
            assert track.title, f"Track {track.raw_title} has empty title"
            # Track validator também testa isto, mas verificamos aqui por clareza
            assert len(track.artist.strip()) > 0
            assert len(track.title.strip()) > 0
