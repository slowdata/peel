"""Testes para as fontes RSS."""

from pathlib import Path

import pytest

from peel.sources.rss import (
    GorillaVsBear,
    PitchforkBestAlbums,
    PitchforkBNT,
    StereogumNewMusic,
    TheQuietus,
    _slugify_pitchfork,
    _split_artist_title_dash,
    _strip_html_tags,
)


class TestSourceKind:
    """Testa o atributo 'kind' em sources."""

    def test_pitchfork_bnt_kind_is_track(self) -> None:
        """PitchforkBNT.kind == 'track'."""
        source = PitchforkBNT()
        assert source.kind == "track"

    def test_stereogum_new_music_kind_is_track(self) -> None:
        """StereogumNewMusic.kind == 'track'."""
        source = StereogumNewMusic()
        assert source.kind == "track"

    def test_pitchfork_best_albums_kind_is_album(self) -> None:
        """PitchforkBestAlbums.kind == 'album'."""
        source = PitchforkBestAlbums()
        assert source.kind == "album"


class TestPitchforkSlugify:
    """Testa a função de slugify do Pitchfork."""

    def test_simple_lowercase(self) -> None:
        """Caso simples: lowercase."""
        assert _slugify_pitchfork("High Rollers") == "high-rollers"

    def test_curly_quotes(self) -> None:
        """Remove aspas curly."""
        # Aspas curly: unicode U+201C e U+201D
        result = _slugify_pitchfork("\u201cTape 05\u201d")
        assert result == "tape-05"

    def test_straight_quotes(self) -> None:
        """Remove aspas retas."""
        assert _slugify_pitchfork('"Dum Maro Dum"') == "dum-maro-dum"

    def test_apostrophes(self) -> None:
        """Remove apóstrofos."""
        assert _slugify_pitchfork("It's Working") == "its-working"

    def test_special_chars(self) -> None:
        """Substitui pontuação/special chars por hyphen."""
        assert _slugify_pitchfork("Hello, World!") == "hello-world"

    def test_collapse_hyphens(self) -> None:
        """Colapsa hyphens repetidos."""
        assert _slugify_pitchfork("Something---Else") == "something-else"


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

        # Caso 1: Tiga - High Rollers
        assert ("tiga", "high rollers") in tracks_dict
        t1 = tracks_dict[("tiga", "high rollers")]
        assert "tiga-high-rollers" in t1.source_url

        # Caso 2: Aldous Harding - One Stop
        assert ("aldous harding", "one stop") in tracks_dict
        t2 = tracks_dict[("aldous harding", "one stop")]
        assert "aldous-harding-one-stop" in t2.source_url

        # Caso 3: Alex G - Afterlife
        assert ("alex g", "afterlife") in tracks_dict
        t3 = tracks_dict[("alex g", "afterlife")]
        assert "alex-g-afterlife" in t3.source_url


class TestPitchforkBestAlbumsExtractArtistTitle:
    """Testa a extraction de artista e album title em PitchforkBestAlbums."""

    def test_simple_case_underscores(self) -> None:
        """Underscores - U."""
        source = PitchforkBestAlbums()
        artist = source._extract_artist_from_link(
            "https://pitchfork.com/reviews/albums/underscores-u/",
            "U",
        )
        assert artist == "Underscores"

    def test_multi_word_artist_and_album(self) -> None:
        """Neurosis - An Undying Love for a Burning World."""
        source = PitchforkBestAlbums()
        artist = source._extract_artist_from_link(
            "https://pitchfork.com/reviews/albums/neurosis-an-undying-love-for-a-burning-world/",
            "An Undying Love for a Burning World",
        )
        assert artist == "Neurosis"

    def test_multi_word_artist_with_apostrophe(self) -> None:
        """Ratboys - Singin' to an Empty Chair."""
        source = PitchforkBestAlbums()
        artist = source._extract_artist_from_link(
            "https://pitchfork.com/reviews/albums/ratboys-singin-to-an-empty-chair/",
            "Singin' to an Empty Chair",
        )
        assert artist == "Ratboys"


class TestPitchforkBestAlbumsFetchFixture:
    """Testa o fetch do feed de Best Albums."""

    @pytest.fixture
    def fixture_path(self) -> Path:
        """Retorna o path do fixture XML."""
        return Path(__file__).parent / "fixtures" / "pitchfork_best_albums.xml"

    def test_fixture_exists(self, fixture_path: Path) -> None:
        """Valida que o fixture existe."""
        assert fixture_path.exists(), f"Fixture not found: {fixture_path}"

    def test_fetch_from_fixture(self, fixture_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Testa o parse do fixture XML."""
        fixture_url = fixture_path.as_uri()
        monkeypatch.setattr(PitchforkBestAlbums, "url", fixture_url)

        source = PitchforkBestAlbums()
        tracks = source.fetch()

        # Valida que temos pelo menos 15 albums (feed real tem 30)
        # Alguma tolerância porque alguns slugs podem não bater
        assert len(tracks) >= 15, f"Expected >=15 albums, got {len(tracks)}"

        # Valida propriedades comuns
        for track in tracks:
            assert track.source_id == "pitchfork_best_albums"
            assert track.artist, f"Album {track.raw_title} has empty artist"
            assert track.title, f"Album {track.raw_title} has empty title"
            assert track.source_url.startswith("https://pitchfork.com/reviews/albums/"), (
                f"Invalid URL: {track.source_url}"
            )

    def test_known_albums_in_fixture(
        self, fixture_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Valida que albums conhecidos estao no fixture."""
        fixture_url = fixture_path.as_uri()
        monkeypatch.setattr(PitchforkBestAlbums, "url", fixture_url)

        source = PitchforkBestAlbums()
        tracks = source.fetch()

        # Cria dict para lookup rapido (note: aqui title=album name)
        tracks_dict = {(t.artist.lower(), t.title.lower()): t for t in tracks}

        # Caso 1: Underscores - U
        assert ("underscores", "u") in tracks_dict
        t1 = tracks_dict[("underscores", "u")]
        assert "underscores-u" in t1.source_url

        # Caso 2: Ratboys - Singin' to an Empty Chair (com apóstrofo curly U+2019)
        assert ("ratboys", "singin\u2019 to an empty chair") in tracks_dict
        t2 = tracks_dict[("ratboys", "singin\u2019 to an empty chair")]
        assert "ratboys-singin-to-an-empty-chair" in t2.source_url


class TestStereogumExtractArtistTitle:
    """Testa a extraction de artista e título no Stereogum."""

    def test_simple_track(self) -> None:
        """Caso simples: Artist – "Title"."""
        source = StereogumNewMusic()
        entry = {
            "title": 'Glazyhaze – "Do You?"',
            "link": "https://stereogum.com/example",
            "tags": [{"term": "New Music"}],
        }
        result = source._extract_artist_title(entry)
        assert result == ("Glazyhaze", "Do You?")

    def test_multiple_tracks_takes_first(self) -> None:
        """Com múltiplas tracks ("A" & "B"), pega só na primeira."""
        source = StereogumNewMusic()
        entry = {
            "title": 'Pope – "John Thomas" & "Sick Minute" (Feat. Ratboys\' Julia Steiner)',
            "link": "https://stereogum.com/example",
            "tags": [{"term": "New Music"}],
        }
        result = source._extract_artist_title(entry)
        assert result == ("Pope", "John Thomas")

    def test_with_features(self) -> None:
        """Track com features no final."""
        source = StereogumNewMusic()
        entry = {
            "title": 'Tyla – "She Did It Again" (Feat. Zara Larsson)',
            "link": "https://stereogum.com/example",
            "tags": [{"term": "New Music"}],
        }
        result = source._extract_artist_title(entry)
        assert result == ("Tyla", "She Did It Again")

    def test_curly_quotes(self) -> None:
        """Suporta curly quotes (U+201C/U+201D)."""
        source = StereogumNewMusic()
        entry = {
            "title": "Madonna – \u201cI Feel So Free\u201d",
            "link": "https://stereogum.com/example",
            "tags": [{"term": "New Music"}],
        }
        result = source._extract_artist_title(entry)
        assert result == ("Madonna", "I Feel So Free")

    def test_em_dash_u2013(self) -> None:
        """Suporta em-dash U+2013."""
        source = StereogumNewMusic()
        # U+2013 é –
        entry = {
            "title": 'Artist – "Title"',
            "link": "https://stereogum.com/example",
            "tags": [{"term": "New Music"}],
        }
        result = source._extract_artist_title(entry)
        assert result == ("Artist", "Title")

    def test_em_dash_u2014(self) -> None:
        """Suporta em-dash U+2014."""
        source = StereogumNewMusic()
        # U+2014 é —
        entry = {
            "title": 'Artist — "Title"',
            "link": "https://stereogum.com/example",
            "tags": [{"term": "New Music"}],
        }
        result = source._extract_artist_title(entry)
        assert result == ("Artist", "Title")

    def test_ascii_hyphen(self) -> None:
        """Suporta ASCII hyphen."""
        source = StereogumNewMusic()
        entry = {
            "title": 'Artist - "Title"',
            "link": "https://stereogum.com/example",
            "tags": [{"term": "New Music"}],
        }
        result = source._extract_artist_title(entry)
        assert result == ("Artist", "Title")

    def test_narrative_no_match(self) -> None:
        """Narrativas sem padrão retornam None."""
        source = StereogumNewMusic()
        entry = {
            "title": "Boards Of Canada Share First New Music In 13 Years",
            "link": "https://stereogum.com/example",
            "tags": [{"term": "New Music"}],
        }
        result = source._extract_artist_title(entry)
        assert result is None


class TestStereogumParseEntryFilter:
    """Testa o filtro de categoria "New Music" no _parse_entry."""

    def test_parse_entry_with_new_music_tag(self) -> None:
        """Entry com tag New Music passa pelo filtro."""
        source = StereogumNewMusic()
        entry = {
            "title": 'Artist – "Title"',
            "link": "https://stereogum.com/example",
            "tags": [{"term": "New Music"}],
            "published": "2026-04-19T10:00:00Z",
            "published_parsed": (2026, 4, 19, 10, 0, 0, 0, 0, 0),
        }
        result = source._parse_entry(entry)
        assert result is not None
        assert result.artist == "Artist"
        assert result.title == "Title"
        assert result.source_id == "stereogum_new_music"

    def test_parse_entry_without_new_music_tag(self) -> None:
        """Entry SEM tag New Music é filtrada."""
        source = StereogumNewMusic()
        entry = {
            "title": 'Artist – "Title"',
            "link": "https://stereogum.com/example",
            "tags": [{"term": "News"}],
        }
        result = source._parse_entry(entry)
        assert result is None

    def test_parse_entry_no_tags(self) -> None:
        """Entry sem tags é filtrada."""
        source = StereogumNewMusic()
        entry = {
            "title": 'Artist – "Title"',
            "link": "https://stereogum.com/example",
            "tags": [],
        }
        result = source._parse_entry(entry)
        assert result is None


class TestStereogumFetchFixture:
    """Testa o fetch do feed real do Stereogum."""

    @pytest.fixture
    def fixture_path(self) -> Path:
        """Retorna o path do fixture XML."""
        return Path(__file__).parent / "fixtures" / "stereogum_new_music.xml"

    def test_fixture_exists(self, fixture_path: Path) -> None:
        """Valida que o fixture existe."""
        assert fixture_path.exists(), f"Fixture not found: {fixture_path}"

    def test_fetch_from_fixture(self, fixture_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Testa o parse do fixture XML."""
        # Monkey-patch a URL para apontar para o fixture local
        fixture_url = fixture_path.as_uri()
        monkeypatch.setattr(StereogumNewMusic, "url", fixture_url)

        source = StereogumNewMusic()
        tracks = source.fetch()

        # Valida que temos pelo menos 10 tracks (feed real tem 21 "New Music")
        assert len(tracks) >= 10, f"Expected >=10 tracks, got {len(tracks)}"

        # Valida propriedades comuns
        for track in tracks:
            assert track.source_id == "stereogum_new_music"
            assert track.artist, f"Track {track.raw_title} has empty artist"
            assert track.title, f"Track {track.raw_title} has empty title"
            assert track.source_url, f"Track {track.raw_title} has no source_url"

    def test_known_tracks_in_fixture(
        self, fixture_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Valida que tracks conhecidas estao no fixture."""
        fixture_url = fixture_path.as_uri()
        monkeypatch.setattr(StereogumNewMusic, "url", fixture_url)

        source = StereogumNewMusic()
        tracks = source.fetch()

        # Cria dict para lookup rapido
        tracks_dict = {(t.artist.lower(), t.title.lower()): t for t in tracks}

        # Caso 1: Pope – "John Thomas" & "Sick Minute"
        assert ("pope", "john thomas") in tracks_dict

        # Caso 2: Glazyhaze – "Do You?"
        assert ("glazyhaze", "do you?") in tracks_dict

        # Caso 3: Madonna – "I Feel So Free"
        assert ("madonna", "i feel so free") in tracks_dict

    def test_filters_out_narratives(
        self, fixture_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Valida que narrativas (sem padrão) sao filtradas."""
        fixture_url = fixture_path.as_uri()
        monkeypatch.setattr(StereogumNewMusic, "url", fixture_url)

        source = StereogumNewMusic()
        tracks = source.fetch()

        # Narrativas conhecidas que nao devem estar nos tracks
        narrative_titles = [
            "S.G. Goodman Shares Studio Version",
            "Former Yamantaka // Sonic Titan",
            "Nine Inch Noize Is Here",
            "Boards Of Canada Share First New Music",
        ]

        for track in tracks:
            for narrative in narrative_titles:
                assert narrative.lower() not in track.raw_title.lower(), (
                    f"Narrative leaked into tracks: {track.raw_title}"
                )


class TestHelpers:
    """Testes dos helpers partilhados de RSS."""

    def test_strip_html_tags_italic(self) -> None:
        assert _strip_html_tags("Smerz drop <i>Big city life EDITS</i>") == (
            "Smerz drop Big city life EDITS"
        )

    def test_strip_html_tags_entity_passthrough(self) -> None:
        # Não toca em entities HTML (deixa para o feedparser decidificar)
        assert _strip_html_tags("A &amp; B") == "A &amp; B"

    def test_split_artist_title_en_dash(self) -> None:
        assert _split_artist_title_dash("Abigail Snail – Rad Berms") == (
            "Abigail Snail",
            "Rad Berms",
        )

    def test_split_artist_title_em_dash(self) -> None:
        assert _split_artist_title_dash("Artist — Song") == ("Artist", "Song")

    def test_split_artist_title_rejects_ascii_hyphen(self) -> None:
        # Evita falsos positivos com hyphens em nomes ("Lo-Fi", "X-Files")
        assert _split_artist_title_dash("Lo-Fi Band - Track") is None

    def test_split_artist_title_no_dash(self) -> None:
        assert _split_artist_title_dash("Just a narrative title") is None


class TestTheQuietusExtractArtistTitle:
    """Testes do parser do The Quietus."""

    def _entry(self, title: str, path: str) -> dict:
        return {"title": title, "link": f"https://thequietus.com{path}"}

    def test_direct_review_extracts(self) -> None:
        source = TheQuietus()
        result = source._extract_artist_title(
            self._entry("Abigail Snail – Rad Berms", "/quietus-reviews/abigail-snail-rad-berms-review/")
        )
        assert result == ("Abigail Snail", "Rad Berms")

    def test_review_with_ampersand_artist(self) -> None:
        source = TheQuietus()
        result = source._extract_artist_title(
            self._entry(
                "Radwan Ghazi Moumneh & Frédéric D. Oberland – Eternal Life No End",
                "/quietus-reviews/radwan-ghazi-moumneh-frederic-d-oberland-review/",
            )
        )
        assert result == (
            "Radwan Ghazi Moumneh & Frédéric D. Oberland",
            "Eternal Life No End",
        )

    def test_nested_review_path_rejected(self) -> None:
        # /quietus-reviews/reissue-of-the-week/... → listicle/reissue, skip
        source = TheQuietus()
        result = source._extract_artist_title(
            self._entry(
                "Reissue of the Week: The Beastie Boys",
                "/quietus-reviews/reissue-of-the-week/beastie-boys-to-the-5-boroughs-review/",
            )
        )
        assert result is None

    def test_news_path_rejected(self) -> None:
        source = TheQuietus()
        result = source._extract_artist_title(
            self._entry(
                "Boards Of Canada Share New Track, 'Tape 05'",
                "/news/boards-of-canada-share-new-track-tape-05/",
            )
        )
        assert result is None

    def test_interviews_path_rejected(self) -> None:
        source = TheQuietus()
        result = source._extract_artist_title(
            self._entry(
                "The Strange World Of… Spacemen 3",
                "/interviews/strange-world-of/spacemen-3-best-music/",
            )
        )
        assert result is None

    def test_title_without_dash_rejected(self) -> None:
        # URL é review directa mas título não tem o formato Artist – Title
        source = TheQuietus()
        result = source._extract_artist_title(
            self._entry("Some Weird Title", "/quietus-reviews/some-slug-review/")
        )
        assert result is None


class TestTheQuietusFetchFixture:
    """Fetch do feed real do Quietus (fixture)."""

    @pytest.fixture
    def fixture_path(self) -> Path:
        return Path(__file__).parent / "fixtures" / "thequietus.xml"

    def test_fixture_exists(self, fixture_path: Path) -> None:
        assert fixture_path.exists(), f"Fixture not found: {fixture_path}"

    def test_fetch_extracts_reviews(
        self, fixture_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fixture_url = fixture_path.as_uri()
        monkeypatch.setattr(TheQuietus, "url", fixture_url)

        source = TheQuietus()
        tracks = source.fetch()

        assert len(tracks) >= 3, f"Expected >=3 reviews, got {len(tracks)}"

        tracks_dict = {(t.artist.lower(), t.title.lower()): t for t in tracks}

        # Reviews directas conhecidas no fixture
        assert ("abigail snail", "rad berms") in tracks_dict
        assert ("adult.", "kissing luck goodbye") in tracks_dict
        assert ("drass", "on the hill") in tracks_dict

        # Todos os tracks devem vir de /quietus-reviews/ não aninhado
        for t in tracks:
            assert "/quietus-reviews/" in t.source_url
            assert t.source_id == "thequietus"

    def test_fetch_filters_out_news_and_interviews(
        self, fixture_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fixture_url = fixture_path.as_uri()
        monkeypatch.setattr(TheQuietus, "url", fixture_url)

        source = TheQuietus()
        tracks = source.fetch()

        leaked = [
            "kraftwerk lose",  # news
            "spacemen 3",  # interview / strange world of
            "björk reveals",  # news
            "rough trade",  # news
            "portraits of the artist",  # culture/books
        ]
        for t in tracks:
            for bad in leaked:
                assert bad not in t.raw_title.lower(), f"Ruído passou: {t.raw_title}"


class TestGorillaVsBearExtractArtistTitle:
    """Testes do parser do Gorilla vs. Bear."""

    def _entry(self, title: str) -> dict:
        return {"title": title, "link": "https://www.gorillavsbear.net/example/"}

    def test_simple_track(self) -> None:
        source = GorillaVsBear()
        result = source._extract_artist_title(self._entry("Carla Dal Forno – Going Out"))
        assert result == ("Carla Dal Forno", "Going Out")

    def test_track_with_features_kept_in_title(self) -> None:
        source = GorillaVsBear()
        result = source._extract_artist_title(
            self._entry("Ms Ray – Miss You (feat. Nourished By Time)")
        )
        assert result == ("Ms Ray", "Miss You (feat. Nourished By Time)")

    def test_album_italic_tags_stripped(self) -> None:
        source = GorillaVsBear()
        result = source._extract_artist_title(
            self._entry("Nashpaints – <i>Everyone Good is Called Molly</i>")
        )
        assert result == ("Nashpaints", "Everyone Good is Called Molly")

    def test_editorial_list_rejected(self) -> None:
        source = GorillaVsBear()
        result = source._extract_artist_title(
            self._entry("Gorilla vs. Bear's Songs of 2025")
        )
        assert result is None

    def test_photos_post_rejected(self) -> None:
        source = GorillaVsBear()
        result = source._extract_artist_title(
            self._entry("photos: Oklou – live in Los Angeles")
        )
        assert result is None

    def test_live_review_rejected(self) -> None:
        source = GorillaVsBear()
        result = source._extract_artist_title(
            self._entry("shinetiac – live at café blue gelato")
        )
        assert result is None

    def test_no_dash_rejected(self) -> None:
        source = GorillaVsBear()
        result = source._extract_artist_title(self._entry("Just a random post title"))
        assert result is None


class TestGorillaVsBearFetchFixture:
    """Fetch do feed real do GvB (fixture)."""

    @pytest.fixture
    def fixture_path(self) -> Path:
        return Path(__file__).parent / "fixtures" / "gorillavsbear.xml"

    def test_fixture_exists(self, fixture_path: Path) -> None:
        assert fixture_path.exists(), f"Fixture not found: {fixture_path}"

    def test_fetch_extracts_tracks(
        self, fixture_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fixture_url = fixture_path.as_uri()
        monkeypatch.setattr(GorillaVsBear, "url", fixture_url)

        source = GorillaVsBear()
        tracks = source.fetch()

        assert len(tracks) >= 5, f"Expected >=5 tracks, got {len(tracks)}"

        tracks_dict = {(t.artist.lower(), t.title.lower()): t for t in tracks}

        # Tracks conhecidos no fixture
        assert ("carla dal forno", "going out") in tracks_dict
        assert ("molina", "golden brown sugar") in tracks_dict

        for t in tracks:
            assert t.source_id == "gorillavsbear"

    def test_fetch_filters_out_noise(
        self, fixture_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fixture_url = fixture_path.as_uri()
        monkeypatch.setattr(GorillaVsBear, "url", fixture_url)

        source = GorillaVsBear()
        tracks = source.fetch()

        for t in tracks:
            raw_lower = t.raw_title.lower()
            assert not raw_lower.startswith("photos"), f"Photos post leaked: {t.raw_title}"
            assert not raw_lower.startswith("gorilla vs. bear"), (
                f"Editorial list leaked: {t.raw_title}"
            )
