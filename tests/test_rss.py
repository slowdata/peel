"""Testes para as fontes RSS."""

from pathlib import Path

import pytest

from peel.sources.rss import (
    AquariumDrunkard,
    GorillaVsBear,
    GuardianMusicAlbums,
    LineOfBestFitNews,
    NprNewMusicFridayStarting5,
    PitchforkBestAlbums,
    PitchforkBNT,
    PitchforkNews,
    StereogumNewMusic,
    TheQuietus,
    TheQuietusTracksOfMonth,
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

    def test_pitchfork_news_kind_is_track(self) -> None:
        source = PitchforkNews()
        assert source.kind == "track"

    def test_lineofbestfit_news_kind_is_track(self) -> None:
        source = LineOfBestFitNews()
        assert source.kind == "track"

    def test_stereogum_new_music_kind_is_track(self) -> None:
        """StereogumNewMusic.kind == 'track'."""
        source = StereogumNewMusic()
        assert source.kind == "track"

    def test_pitchfork_best_albums_kind_is_album(self) -> None:
        """PitchforkBestAlbums.kind == 'album'."""
        source = PitchforkBestAlbums()
        assert source.kind == "album"

    def test_guardian_music_albums_kind_is_album(self) -> None:
        """GuardianMusicAlbums.kind == 'album'."""
        source = GuardianMusicAlbums()
        assert source.kind == "album"

    def test_quietus_kind_is_album(self) -> None:
        """TheQuietus.kind == 'album'."""
        source = TheQuietus()
        assert source.kind == "album"

    def test_npr_starting5_kind_is_track(self) -> None:
        """NprNewMusicFridayStarting5.kind == 'track'."""
        source = NprNewMusicFridayStarting5()
        assert source.kind == "track"

    def test_quietus_tracks_of_month_kind_is_track(self) -> None:
        """TheQuietusTracksOfMonth.kind == 'track'."""
        source = TheQuietusTracksOfMonth()
        assert source.kind == "track"

    def test_aquarium_drunkard_kind_is_album(self) -> None:
        """AquariumDrunkard.kind == 'album'."""
        source = AquariumDrunkard()
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


class TestAquariumDrunkard:
    """Testa a source Aquarium Drunkard — On The Turntable."""

    @pytest.fixture
    def fixture_path(self) -> Path:
        """Retorna o path do fixture HTML."""
        return Path(__file__).parent / "fixtures" / "aquarium_drunkard_turntable.html"

    @pytest.fixture
    def fixture_html(self, fixture_path: Path) -> str:
        return fixture_path.read_text(encoding="utf-8")

    def test_parse_fixture_extracts_valid_albums(self, fixture_html: str) -> None:
        source = AquariumDrunkard()

        albums = source._parse_homepage_html(fixture_html)
        album_keys = {(album.artist, album.title) for album in albums}

        assert len(albums) == 7
        assert album_keys == {
            ("Wax Machine", "The Sky Unfurls, The Dance Goes On"),
            ("Masayoshi Takanaka", "All of Me"),
            ("Lifetones", "For A Reason"),
            ("This Heat", "Made Available: John Peel Sessions"),
            ("Boards of Canada", "Inferno"),
            ("Jeff Parker ETA IVtet", "Happy Today"),
            ("Setting", "S/T"),
        }

    def test_parse_fixture_decodes_ampersand_and_skips_empty_album(self, fixture_html: str) -> None:
        source = AquariumDrunkard()

        albums = source._parse_homepage_html(fixture_html)

        assert all(album.artist != "Cedric IM Brooks & The Light of Saba" for album in albums)

    def test_split_title_handles_ampersand_artist(self) -> None:
        source = AquariumDrunkard()

        assert source._split_turntable_title(
            "Cedric IM Brooks & The Light of Saba :: The Magical Light of Saba"
        ) == ("Cedric IM Brooks & The Light of Saba", "The Magical Light of Saba")

    def test_parse_fixture_preserves_source_metadata(self, fixture_html: str) -> None:
        source = AquariumDrunkard()

        albums = source._parse_homepage_html(fixture_html)
        wax_machine = next(album for album in albums if album.artist == "Wax Machine")

        assert wax_machine.source_id == "aquarium_drunkard"
        assert wax_machine.source_url == (
            "https://aquariumdrunkard.com/2026/05/29/wax-machine-the-sky-unfurls-the-dance-goes-on/"
        )
        assert wax_machine.raw_title == "Wax Machine :: The Sky Unfurls, The Dance Goes On"
        assert wax_machine.spotify_album_uri == "spotify:album:1WaxMachineAlbum"

    def test_missing_read_more_keeps_album_without_source_url(self, fixture_html: str) -> None:
        source = AquariumDrunkard()

        albums = source._parse_homepage_html(fixture_html)
        jeff_parker = next(album for album in albums if album.artist == "Jeff Parker ETA IVtet")

        assert jeff_parker.title == "Happy Today"
        assert jeff_parker.source_url is None

    def test_malformed_html_without_turntable_returns_empty_list(self) -> None:
        source = AquariumDrunkard()

        assert source._parse_homepage_html("<html><body>No turntable here</body></html>") == []

    def test_fetch_uses_http_homepage_response(
        self, fixture_html: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class Response:
            text = fixture_html

            def raise_for_status(self) -> None:
                return None

        monkeypatch.setattr("peel.sources.rss.httpx.get", lambda *args, **kwargs: Response())

        source = AquariumDrunkard()
        albums = source.fetch()

        assert len(albums) == 7


class TestGuardianMusicAlbums:
    """Testa a source de álbuns do Guardian Music."""

    @pytest.fixture
    def fixture_path(self) -> Path:
        return Path(__file__).parent / "fixtures" / "guardian_music.xml"

    def test_extract_album_review_title(self) -> None:
        source = GuardianMusicAlbums()
        entry = {
            "title": "Kneecap: Fenian review | Alexis Petridis's album of the week",
            "link": "https://www.theguardian.com/music/example",
        }
        assert source._extract_artist_title(entry) == ("Kneecap", "Fenian")

    def test_ignores_non_album_review_title(self) -> None:
        source = GuardianMusicAlbums()
        entry = {
            "title": "Add to playlist: the week’s best new tracks",
            "link": "https://www.theguardian.com/music/example",
        }
        assert source._extract_artist_title(entry) is None

    def test_fetch_from_fixture(self, fixture_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        fixture_url = fixture_path.as_uri()
        monkeypatch.setattr(GuardianMusicAlbums, "url", fixture_url)

        source = GuardianMusicAlbums()
        albums = source.fetch()

        assert len(albums) >= 5
        for album in albums:
            assert album.source_id == "guardian_music_albums"
            assert album.artist
            assert album.title
            assert album.source_url.startswith("https://www.theguardian.com/music/")

    def test_known_albums_in_fixture(
        self, fixture_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fixture_url = fixture_path.as_uri()
        monkeypatch.setattr(GuardianMusicAlbums, "url", fixture_url)

        source = GuardianMusicAlbums()
        albums = source.fetch()
        albums_dict = {(album.artist.lower(), album.title.lower()): album for album in albums}

        assert ("kneecap", "fenian") in albums_dict
        assert ("kacey musgraves", "middle of nowhere") in albums_dict


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

    def test_narrative_new_single_in_music_url(self) -> None:
        source = StereogumNewMusic()
        entry = {
            "title": "The Strokes Share New Single “Falling Out Of Love”: Listen",
            "link": "https://stereogum.com/2498847/the-strokes-falling-out-of-love/music/",
            "tags": [{"term": "New Music"}],
        }
        result = source._extract_artist_title(entry)
        assert result == ("The Strokes", "Falling Out Of Love")

    def test_narrative_new_album_share_track(self) -> None:
        source = StereogumNewMusic()
        entry = {
            "title": "Rico Nasty Announces New Album RX: Hear “Cupcake”",
            "link": "https://stereogum.com/example/music/",
            "tags": [{"term": "New Music"}],
        }
        result = source._extract_artist_title(entry)
        assert result == ("Rico Nasty", "Cupcake")

    def test_narrative_news_url_no_match(self) -> None:
        """Narrativas em /news/ continuam fora da track source."""
        source = StereogumNewMusic()
        entry = {
            "title": "The Strokes Share New Single “Falling Out Of Love”: Listen",
            "link": "https://stereogum.com/2498847/the-strokes-falling-out-of-love/news/",
            "tags": [{"term": "New Music"}],
        }
        result = source._extract_artist_title(entry)
        assert result is None

    def test_narrative_cover_no_match(self) -> None:
        """Covers não entram como release original."""
        source = StereogumNewMusic()
        entry = {
            "title": "S.G. Goodman Shares Studio Version Of Her Butthole Surfers Cover “Pepper”",
            "link": "https://stereogum.com/example/music/",
            "tags": [{"term": "New Music"}],
        }
        result = source._extract_artist_title(entry)
        assert result is None

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


class TestPitchforkNewsExtractArtistTitle:
    def test_listen_to_new_song(self) -> None:
        source = PitchforkNews()
        result = source._extract_artist_title(
            {"title": "Listen to The Strokes’ New Song “Falling Out of Love”"}
        )
        assert result == ("The Strokes", "Falling Out of Love")

    def test_announces_album_shares_track(self) -> None:
        source = PitchforkNews()
        result = source._extract_artist_title(
            {"title": "Rico Nasty Announces New Album RX: Hear “Cupcake”"}
        )
        assert result == ("Rico Nasty", "Cupcake")

    def test_introduces_album_with_track(self) -> None:
        source = PitchforkNews()
        result = source._extract_artist_title(
            {"title": "Rico Nasty Introduces New Album RX With “Cupcake”"}
        )
        assert result == ("Rico Nasty", "Cupcake")

    def test_surprise_releases_new_song(self) -> None:
        source = PitchforkNews()
        result = source._extract_artist_title(
            {"title": "Beyoncé Surprise-Releases New Song “Morning Dew (Donk)”"}
        )
        assert result == ("Beyoncé", "Morning Dew (Donk)")

    def test_listen_to_duet(self) -> None:
        source = PitchforkNews()
        result = source._extract_artist_title(
            {"title": "Listen to Steve Lacy and SZA’s Duet “Is It Cool?”"}
        )
        assert result == ("Steve Lacy and SZA", "Is It Cool?")

    def test_new_song_video(self) -> None:
        source = PitchforkNews()
        result = source._extract_artist_title(
            {"title": "Watch Charli XCX Let Loose in Video for New Song “Wink Wink”"}
        )
        assert result == ("Charli XCX", "Wink Wink")

    def test_listen_to_the_new_artist_song(self) -> None:
        source = PitchforkNews()
        result = source._extract_artist_title(
            {"title": "Listen to the New Aphex Twin Song “Two Remixes by AFX”"}
        )
        assert result == ("Aphex Twin", "Two Remixes by AFX")

    @pytest.mark.parametrize(
        "title",
        [
            "Questlove Announces New Book “Hip-Hop Is History”",
            "Mitski Announces New Album “The Land Is Inhospitable”",
            "Big Thief Shares Video for “Vampire Empire”",
            "Kraftwerk Release New Version Of “Tour de France”",
        ],
    )
    def test_release_news_false_positives_are_ignored(self, title: str) -> None:
        source = PitchforkNews()
        result = source._extract_artist_title({"title": title})
        assert result is None

    def test_quoted_live_in_title_is_not_excluded(self) -> None:
        source = PitchforkNews()
        result = source._extract_artist_title(
            {"title": "Oasis Release New Song “Live Forever Again”"}
        )
        assert result == ("Oasis", "Live Forever Again")

    def test_tour_news_is_ignored(self) -> None:
        source = PitchforkNews()
        result = source._extract_artist_title({"title": "The Strokes Announce Tour Dates"})
        assert result is None


class TestLineOfBestFitNewsExtractArtistTitle:
    def test_returns_with_new_single(self) -> None:
        source = LineOfBestFitNews()
        result = source._extract_artist_title(
            {"title": "Cigarettes After Sex return with new single, “Twizzler”"}
        )
        assert result == ("Cigarettes After Sex", "Twizzler")

    def test_announces_lp_shares_lead_single(self) -> None:
        source = LineOfBestFitNews()
        result = source._extract_artist_title(
            {
                "title": (
                    "Carmen Villain announces new LP Memoria, shares lead single “Entre Nosotros”"
                )
            }
        )
        assert result == ("Carmen Villain", "Entre Nosotros")

    def test_newcomers_are_back_with_single(self) -> None:
        source = LineOfBestFitNews()
        result = source._extract_artist_title(
            {
                "title": (
                    "'Slushy psychedelia' newcomers Captain Crocodile are back with "
                    'a new single, "Fragmented Tool"'
                )
            }
        )
        assert result == ("Captain Crocodile", "Fragmented Tool")

    def test_announces_album_title_is_ignored(self) -> None:
        source = LineOfBestFitNews()
        result = source._extract_artist_title(
            {"title": "Carmen Villain announces new LP, “Memoria”"}
        )
        assert result is None

    def test_album_review_is_ignored(self) -> None:
        source = LineOfBestFitNews()
        result = source._extract_artist_title(
            {"title": "Baby Rose wears her heart on her sleeve on YEARNALISM"}
        )
        assert result is None


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
            self._entry(
                "Abigail Snail – Rad Berms",
                "/quietus-reviews/abigail-snail-rad-berms-review/",
            )
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


class TestNprNewMusicFridayStarting5:
    """Parser da secção The Starting 5 da NPR."""

    @pytest.fixture
    def section_fixture_path(self) -> Path:
        return Path(__file__).parent / "fixtures" / "npr_new_music_friday_section.html"

    @pytest.fixture
    def article_fixture_path(self) -> Path:
        return Path(__file__).parent / "fixtures" / "npr_new_music_friday_starting5.html"

    def test_fixtures_exist(self, section_fixture_path: Path, article_fixture_path: Path) -> None:
        assert section_fixture_path.exists(), f"Fixture not found: {section_fixture_path}"
        assert article_fixture_path.exists(), f"Fixture not found: {article_fixture_path}"

    def test_latest_article_url_from_section(self, section_fixture_path: Path) -> None:
        source = NprNewMusicFridayStarting5()
        html = section_fixture_path.read_text(encoding="utf-8")

        url = source._latest_article_url(html)

        assert url == (
            "https://www.npr.org/2026/05/29/nx-s1-5830351/new-music-friday-best-albums-may-29-2026"
        )

    def test_parse_starting_5_only(self, article_fixture_path: Path) -> None:
        source = NprNewMusicFridayStarting5()
        html = article_fixture_path.read_text(encoding="utf-8")

        tracks = source._parse_article_html(
            html,
            "https://www.npr.org/2026/05/29/nx-s1-5830351/new-music-friday-best-albums-may-29-2026",
        )

        assert len(tracks) == 5
        tracks_dict = {(track.artist.lower(), track.title.lower()): track for track in tracks}
        assert ("boards of canada", "inferno") in tracks_dict
        assert ("kurt vile", "philadelphia's been good to me") in tracks_dict
        assert ("iceage", "for love of grace the hereafter") in tracks_dict
        assert ("feeble little horse", "bitknot") in tracks_dict
        assert ("greg mendez", "beauty land") in tracks_dict

        # Lightning Round / Long List não entram nesta source.
        assert ("rainao", "marcriá") not in tracks_dict
        assert ("brian jackson", "now more than ever") not in tracks_dict

        for track in tracks:
            assert track.source_id == "npr_new_music_friday_starting5"
            assert track.source_url.startswith("https://www.npr.org/2026/05/29/")
            assert track.published_at is not None
            assert track.artist
            assert track.title

    def test_parse_article_without_storytext_returns_empty(self) -> None:
        source = NprNewMusicFridayStarting5()

        tracks = source._parse_article_html("<html></html>", "https://example.com")

        assert tracks == []


class TestTheQuietusTracksOfMonth:
    """Parser dos melhores tracks mensais da Quietus."""

    @pytest.fixture
    def fixture_path(self) -> Path:
        return Path(__file__).parent / "fixtures" / "thequietus_tracks_of_month.html"

    def test_fixture_exists(self, fixture_path: Path) -> None:
        assert fixture_path.exists(), f"Fixture not found: {fixture_path}"

    def test_parse_tracks_section_only(self, fixture_path: Path) -> None:
        source = TheQuietusTracksOfMonth()
        html = fixture_path.read_text(encoding="utf-8")

        tracks = source._parse_chart_html(
            html,
            "https://thequietus.com/tq-charts/music-of-the-month/example/",
        )

        assert len(tracks) == 5
        tracks_dict = {(track.artist.lower(), track.title.lower()): track for track in tracks}
        assert ("james k", "peel (loidis remix)") in tracks_dict
        assert (
            "the standing stones, iona zajac, daragh lynch",
            "twa sisters",
        ) in tracks_dict
        assert ("kelela", "idea 1") in tracks_dict

        for track in tracks:
            assert track.source_id == "thequietus_tracks_of_month"
            assert (
                track.source_url == "https://thequietus.com/tq-charts/music-of-the-month/example/"
            )
            assert track.artist
            assert track.title

    def test_parse_missing_tracks_section_returns_empty(self) -> None:
        source = TheQuietusTracksOfMonth()

        tracks = source._parse_chart_html("<html><h2>ALBUMS</h2></html>", "https://example.com")

        assert tracks == []


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
        result = source._extract_artist_title(self._entry("Gorilla vs. Bear's Songs of 2025"))
        assert result is None

    def test_photos_post_rejected(self) -> None:
        source = GorillaVsBear()
        result = source._extract_artist_title(self._entry("photos: Oklou – live in Los Angeles"))
        assert result is None

    def test_live_review_rejected(self) -> None:
        source = GorillaVsBear()
        result = source._extract_artist_title(self._entry("shinetiac – live at café blue gelato"))
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
