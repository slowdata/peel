"""Testes para as fontes RSS."""

from pathlib import Path

import pytest

from peel.sources.rss import PitchforkBNT


class TestPitchforkSlugify:
    """Testa a função de slugify do Pitchfork."""

    def test_simple_lowercase(self) -> None:
        """Caso simples: lowercase."""
        source = PitchforkBNT()
        assert source._slugify("High Rollers") == "high-rollers"

    def test_curly_quotes(self) -> None:
        """Remove aspas curly."""
        source = PitchforkBNT()
        # Aspas curly: unicode U+201C e U+201D
        result = source._slugify("\u201cTape 05\u201d")
        assert result == "tape-05"

    def test_straight_quotes(self) -> None:
        """Remove aspas retas."""
        source = PitchforkBNT()
        assert source._slugify('"Dum Maro Dum"') == "dum-maro-dum"

    def test_apostrophes(self) -> None:
        """Remove apóstrofos."""
        source = PitchforkBNT()
        assert source._slugify("It's Working") == "its-working"

    def test_special_chars(self) -> None:
        """Substitui pontuação/special chars por hyphen."""
        source = PitchforkBNT()
        assert source._slugify("Hello, World!") == "hello-world"

    def test_collapse_hyphens(self) -> None:
        """Colapsa hyphens repetidos."""
        source = PitchforkBNT()
        assert source._slugify("Something---Else") == "something-else"


class TestPitchforkExtractArtistFromSlug:
    """Testa a extraction de artista a partir do slug."""

    def test_simple_case(self) -> None:
        """Caso simples: boards-of-canada-tape-05 -> Boards Of Canada."""
        source = PitchforkBNT()
        artist = source._extract_artist_from_link(
            "https://pitchfork.com/reviews/tracks/boards-of-canada-tape-05/",
            "Tape 05",
        )
        assert artist == "Boards Of Canada"

    def test_single_word_artist(self) -> None:
        """Artist com uma palavra: tiga-high-rollers -> Tiga."""
        source = PitchforkBNT()
        artist = source._extract_artist_from_link(
            "https://pitchfork.com/reviews/tracks/tiga-high-rollers/",
            "High Rollers",
        )
        assert artist == "Tiga"

    def test_multi_word_artist_and_title(self) -> None:
        """Artist e titulo com varias palavras: asha-bhosle-dum-maro-dum."""
        source = PitchforkBNT()
        artist = source._extract_artist_from_link(
            "https://pitchfork.com/reviews/tracks/asha-bhosle-dum-maro-dum/",
            "Dum Maro Dum",
        )
        assert artist == "Asha Bhosle"

    def test_slug_mismatch_returns_none(self) -> None:
        """Se a slugification diverges, retorna None."""
        source = PitchforkBNT()
        # Slug completo nao termina com o title-slug esperado
        artist = source._extract_artist_from_link(
            "https://pitchfork.com/reviews/tracks/some-artist-wrong-title/",
            "Correct Title",
        )
        assert artist is None


class TestPitchforkFetchFixture:
    """Testa o fetch do feed real do Pitchfork."""

    @pytest.fixture
    def fixture_path(self) -> Path:
        """Retorna o path do fixture XML."""
        return Path(__file__).parent / "fixtures" / "pitchfork_feed.xml"

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

        # Valida que temos pelo menos 3 tracks (feed real tem 7)
        assert len(tracks) >= 3, f"Expected >=3 tracks, got {len(tracks)}"

        # Valida propriedades comuns
        for track in tracks:
            assert track.source_id == "pitchfork_bnt"
            assert track.artist, f"Track {track.raw_title} has empty artist"
            assert track.title, f"Track {track.raw_title} has empty title"
            assert track.source_url.startswith("https://pitchfork.com/reviews/tracks/"), (
                f"Invalid URL: {track.source_url}"
            )

    def test_known_tracks_in_fixture(
        self, fixture_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Valida que tracks conhecidas estao no fixture."""
        fixture_url = fixture_path.as_uri()
        monkeypatch.setattr(PitchforkBNT, "url", fixture_url)

        source = PitchforkBNT()
        tracks = source.fetch()

        # Cria dict para lookup rapido
        tracks_dict = {(t.artist.lower(), t.title.lower()): t for t in tracks}

        # Caso 1: Boards Of Canada - Tape 05
        assert ("boards of canada", "tape 05") in tracks_dict
        t1 = tracks_dict[("boards of canada", "tape 05")]
        assert "boards-of-canada-tape-05" in t1.source_url

        # Caso 2: Tiga - High Rollers
        assert ("tiga", "high rollers") in tracks_dict
        t2 = tracks_dict[("tiga", "high rollers")]
        assert "tiga-high-rollers" in t2.source_url

        # Caso 3: Asha Bhosle - Dum Maro Dum
        assert ("asha bhosle", "dum maro dum") in tracks_dict
        t3 = tracks_dict[("asha bhosle", "dum maro dum")]
        assert "asha-bhosle-dum-maro-dum" in t3.source_url

    def test_only_reviews_tracks_category(
        self, fixture_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Valida que apenas entries com category 'Reviews / Tracks' sao incluidas."""
        fixture_url = fixture_path.as_uri()
        monkeypatch.setattr(PitchforkBNT, "url", fixture_url)

        source = PitchforkBNT()
        tracks = source.fetch()

        # Nenhuma track deve ter titles de News ou Albums
        # (estas aparecem no fixture mas sao filtradas)
        news_album_titles = [
            "listen to madonna's new song",
            "life for rent",
            "nine inch noize",
        ]

        for track in tracks:
            for bad_title in news_album_titles:
                assert bad_title.lower() not in track.raw_title.lower(), (
                    f"Non-track category item leaked: {track.raw_title}"
                )
