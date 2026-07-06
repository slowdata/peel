"""Testes de integração para main.py."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from peel.albums import AlbumRecommendation
from peel.main import (
    _album_digest_items,
    _filter_fresh_source_items,
    _retry_unmatched,
    run,
    slots_for_source,
)
from peel.models import Track
from peel.scoring import SourceScore
from peel.sources.bandcamp import BandcampLabel
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
)


@pytest.fixture(autouse=True)
def _disable_network_album_sources(monkeypatch: pytest.MonkeyPatch) -> None:
    """Evita rede nos testes de main; o registry valida que as sources estão activas."""
    monkeypatch.setattr(PitchforkBestAlbums, "fetch", lambda self: [])
    monkeypatch.setattr(PitchforkNews, "fetch", lambda self: [])
    monkeypatch.setattr(LineOfBestFitNews, "fetch", lambda self: [])
    monkeypatch.setattr(AquariumDrunkard, "fetch", lambda self: [])
    monkeypatch.setattr(BandcampLabel, "fetch", lambda self: [])


def test_album_digest_items_use_listen_url_and_source_url() -> None:
    """Telegram: título abre onde se ouve; source fica como link secundário."""
    recommendations = [
        AlbumRecommendation(
            artist="Direct Artist",
            album="Direct Album",
            source_count=1,
            sources=("guardian_music_albums",),
            source_urls=(("guardian_music_albums", "https://guardian/review"),),
            spotify_album_uri="spotify:album:abc123",
            latest_seen_at="2026-06-10T00:00:00+00:00",
            best_avg_rating=0.0,
            best_score=0.0,
        ),
        AlbumRecommendation(
            artist="Bandcamp Artist",
            album="Bandcamp Album",
            source_count=1,
            sources=("bandcamp_ghostly",),
            source_urls=(("bandcamp_ghostly", "https://artist.bandcamp.com/album/x"),),
            spotify_album_uri=None,
            latest_seen_at="2026-06-10T00:00:00+00:00",
            best_avg_rating=0.0,
            best_score=0.0,
        ),
    ]

    items = _album_digest_items(recommendations, album_resolver=lambda _artist, _album: None)

    assert items[0][4] == "https://open.spotify.com/album/abc123"
    assert items[0][5] == "https://guardian/review"
    assert items[1][4] == "https://artist.bandcamp.com/album/x"
    assert items[1][5] == "https://artist.bandcamp.com/album/x"


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
            patch.object(GuardianMusicAlbums, "fetch", return_value=[]),
            patch.object(NprNewMusicFridayStarting5, "fetch", return_value=[]),
            patch.object(TheQuietusTracksOfMonth, "fetch", return_value=[]),
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
            patch.object(GuardianMusicAlbums, "fetch", return_value=[]),
            patch.object(NprNewMusicFridayStarting5, "fetch", return_value=[]),
            patch.object(TheQuietusTracksOfMonth, "fetch", return_value=[]),
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
            patch.object(StereogumNewMusic, "fetch", return_value=[]),
            patch.object(TheQuietus, "fetch", return_value=[]),
            patch.object(TheQuietusTracksOfMonth, "fetch", return_value=[]),
            patch.object(GorillaVsBear, "fetch", return_value=[]),
            patch.object(GuardianMusicAlbums, "fetch", return_value=[]),
            patch.object(NprNewMusicFridayStarting5, "fetch", return_value=[]),
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

        run_row = db.conn.execute(
            "SELECT status, error FROM source_runs WHERE source_id='pitchfork_bnt'"
        ).fetchone()
        assert run_row is not None
        assert run_row[0] == "error"
        assert "simulated source crash" in run_row[1]

        db.close()

    def test_run_iterates_active_source_registry(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        db_path = tmp_path / "test.db"
        monkeypatch.setenv("PEEL_PLAYLIST_ID", "spotify:playlist:test")
        monkeypatch.setenv("SPOTIFY_CLIENT_ID", "test_id")
        monkeypatch.setenv("SPOTIFY_CLIENT_SECRET", "test_secret")
        monkeypatch.setenv("SPOTIFY_REFRESH_TOKEN", "test_token")

        from peel import config as config_module

        monkeypatch.setattr(config_module.settings, "db_path", str(db_path))

        source = MagicMock()
        source.id = "registry_source"
        source.kind = "track"
        source.fetch.return_value = [
            Track(source_id="registry_source", artist="Registry Artist", title="Registry Track")
        ]

        mock_sp = MagicMock()
        mock_sp.search_track.return_value = [
            {
                "uri": "spotify:track:registry",
                "name": "Registry Track",
                "artists": ["Registry Artist"],
            }
        ]
        mock_sp.replace_playlist_items = MagicMock()

        with (
            patch("peel.main.active_sources", return_value=[source]),
            patch("peel.main.SpotifyClient", return_value=mock_sp),
            patch("peel.main.send_digest"),
        ):
            run()

        source.fetch.assert_called_once()
        assert mock_sp.replace_playlist_items.call_args.args[1] == ["spotify:track:registry"]

    def test_run_records_guardian_album_source(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        db_path = tmp_path / "test.db"
        monkeypatch.setenv("PEEL_PLAYLIST_ID", "spotify:playlist:test")
        monkeypatch.setenv("SPOTIFY_CLIENT_ID", "test_id")
        monkeypatch.setenv("SPOTIFY_CLIENT_SECRET", "test_secret")
        monkeypatch.setenv("SPOTIFY_REFRESH_TOKEN", "test_token")

        from peel import config as config_module

        monkeypatch.setattr(config_module.settings, "db_path", str(db_path))

        album = Track(
            source_id="guardian_music_albums",
            artist="Kneecap",
            title="Fenian",
            source_url="https://www.theguardian.com/music/example",
        )
        mock_sp = MagicMock()
        mock_sp.search_track = MagicMock(return_value=[])
        mock_sp.replace_playlist_items = MagicMock()

        with (
            patch.object(PitchforkBNT, "fetch", return_value=[]),
            patch.object(StereogumNewMusic, "fetch", return_value=[]),
            patch.object(TheQuietus, "fetch", return_value=[]),
            patch.object(TheQuietusTracksOfMonth, "fetch", return_value=[]),
            patch.object(GorillaVsBear, "fetch", return_value=[]),
            patch.object(GuardianMusicAlbums, "fetch", return_value=[album]),
            patch.object(NprNewMusicFridayStarting5, "fetch", return_value=[]),
            patch("peel.main.SpotifyClient", return_value=mock_sp),
            patch("peel.main.send_digest"),
        ):
            run()

        from peel.db import DB

        db = DB(str(db_path))
        db.init_schema()
        row = db.conn.execute(
            "SELECT artist, album, source_id FROM albums WHERE artist = ? AND album = ?",
            ("Kneecap", "Fenian"),
        ).fetchone()
        assert row == ("Kneecap", "Fenian", "guardian_music_albums")
        run_row = db.conn.execute(
            """
            SELECT
                fetched_count,
                fresh_count,
                processed_count,
                album_count,
                status,
                error
            FROM source_runs
            WHERE source_id = 'guardian_music_albums'
            """
        ).fetchone()
        assert run_row == (1, 1, 1, 1, "ok", None)
        mock_sp.search_track.assert_not_called()
        db.close()


class TestPlaylistSafetyCaps:
    def test_run_limits_new_tracks_per_source(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        db_path = tmp_path / "test.db"
        monkeypatch.setenv("PEEL_PLAYLIST_ID", "spotify:playlist:test")
        monkeypatch.setenv("SPOTIFY_CLIENT_ID", "test_id")
        monkeypatch.setenv("SPOTIFY_CLIENT_SECRET", "test_secret")
        monkeypatch.setenv("SPOTIFY_REFRESH_TOKEN", "test_token")

        from peel import config as config_module

        monkeypatch.setattr(config_module.settings, "db_path", str(db_path))
        monkeypatch.setattr(config_module.settings, "peel_max_tracks_per_source", 2)
        monkeypatch.setattr(config_module.settings, "peel_max_tracks_per_run", 10)

        tracks = [
            Track(source_id="pitchfork_bnt", artist="Artist", title=f"Track {idx}")
            for idx in range(1, 4)
        ]

        mock_sp = MagicMock()
        mock_sp.search_track.side_effect = [
            [{"uri": f"spotify:track:{idx}", "name": f"Track {idx}", "artists": ["Artist"]}]
            for idx in range(1, 4)
        ]
        mock_sp.replace_playlist_items = MagicMock()

        with (
            patch.object(PitchforkBNT, "fetch", return_value=tracks),
            patch.object(StereogumNewMusic, "fetch", return_value=[]),
            patch.object(TheQuietus, "fetch", return_value=[]),
            patch.object(TheQuietusTracksOfMonth, "fetch", return_value=[]),
            patch.object(GorillaVsBear, "fetch", return_value=[]),
            patch.object(GuardianMusicAlbums, "fetch", return_value=[]),
            patch.object(NprNewMusicFridayStarting5, "fetch", return_value=[]),
            patch("peel.main.SpotifyClient", return_value=mock_sp),
            patch("peel.main.send_digest"),
        ):
            run()

        from peel.db import DB

        db = DB(str(db_path))
        db.init_schema()
        count = db.conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0]
        assert count == 2
        called_uris = mock_sp.replace_playlist_items.call_args.args[1]
        assert set(called_uris) == {"spotify:track:1", "spotify:track:2"}
        db.close()

    def test_run_limits_new_tracks_globally(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        db_path = tmp_path / "test.db"
        monkeypatch.setenv("PEEL_PLAYLIST_ID", "spotify:playlist:test")
        monkeypatch.setenv("SPOTIFY_CLIENT_ID", "test_id")
        monkeypatch.setenv("SPOTIFY_CLIENT_SECRET", "test_secret")
        monkeypatch.setenv("SPOTIFY_REFRESH_TOKEN", "test_token")

        from peel import config as config_module

        monkeypatch.setattr(config_module.settings, "db_path", str(db_path))
        monkeypatch.setattr(config_module.settings, "peel_max_tracks_per_source", 10)
        monkeypatch.setattr(config_module.settings, "peel_max_tracks_per_run", 2)

        def track(source_id: str, idx: int) -> Track:
            return Track(source_id=source_id, artist=f"Artist {idx}", title=f"Track {idx}")

        mock_sp = MagicMock()
        mock_sp.search_track.side_effect = [
            [
                {
                    "uri": f"spotify:track:{idx}",
                    "name": f"Track {idx}",
                    "artists": [f"Artist {idx}"],
                }
            ]
            for idx in range(1, 5)
        ]
        mock_sp.replace_playlist_items = MagicMock()

        with (
            patch.object(PitchforkBNT, "fetch", return_value=[track("pitchfork_bnt", 1)]),
            patch.object(
                StereogumNewMusic,
                "fetch",
                return_value=[track("stereogum_new_music", 2)],
            ),
            patch.object(TheQuietus, "fetch", return_value=[track("thequietus", 3)]),
            patch.object(GorillaVsBear, "fetch", return_value=[track("gorillavsbear", 4)]),
            patch.object(GuardianMusicAlbums, "fetch", return_value=[]),
            patch.object(NprNewMusicFridayStarting5, "fetch", return_value=[]),
            patch.object(TheQuietusTracksOfMonth, "fetch", return_value=[]),
            patch("peel.main.SpotifyClient", return_value=mock_sp),
            patch("peel.main.send_digest"),
        ):
            run()

        from peel.db import DB

        db = DB(str(db_path))
        db.init_schema()
        count = db.conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0]
        assert count == 2
        called_uris = mock_sp.replace_playlist_items.call_args.args[1]
        assert set(called_uris) == {"spotify:track:1", "spotify:track:2"}
        db.close()

    def test_run_cap_does_not_burn_on_unmatched_late_sources_contribute(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Região de regressão: faixas unmatched não devem "queimar" o cap global.

        Antes do fix, o cap contava tentativas (inclusive unmatched), o que
        starvationava fontes tardias quando uma fonte cedo tinha muitos itens sem
        match no Spotify. Agora o cap conta só NOVIDADES registadas — logo uma
        fonte com unmatched não bloqueia fontes tardias (ex.: NPR) de contribuir.
        """
        db_path = tmp_path / "test.db"
        monkeypatch.setenv("PEEL_PLAYLIST_ID", "spotify:playlist:test")
        monkeypatch.setenv("SPOTIFY_CLIENT_ID", "test_id")
        monkeypatch.setenv("SPOTIFY_CLIENT_SECRET", "test_secret")
        monkeypatch.setenv("SPOTIFY_REFRESH_TOKEN", "test_token")

        from peel import config as config_module

        monkeypatch.setattr(config_module.settings, "db_path", str(db_path))
        monkeypatch.setattr(config_module.settings, "peel_max_tracks_per_source", 10)
        monkeypatch.setattr(config_module.settings, "peel_max_tracks_per_run", 2)

        def track(source_id: str, idx: int) -> Track:
            return Track(source_id=source_id, artist=f"Artist {idx}", title=f"Track {idx}")

        mock_sp = MagicMock()
        mock_sp.replace_playlist_items = MagicMock()
        # Ordem de searches: pitchfork(t1 unmatched, t2 unmatched, t3 match),
        # stereogum(t4 match), gorillavsbear(t5 match mas cap → skip).
        mock_sp.search_track.side_effect = [
            [],  # t1 → no match
            [],  # t2 → no match
            [{"uri": "spotify:track:3", "name": "Track 3", "artists": ["Artist 3"]}],
            [{"uri": "spotify:track:4", "name": "Track 4", "artists": ["Artist 4"]}],
            [{"uri": "spotify:track:5", "name": "Track 5", "artists": ["Artist 5"]}],
        ]

        with (
            patch.object(
                PitchforkBNT,
                "fetch",
                return_value=[
                    track("pitchfork_bnt", 1),
                    track("pitchfork_bnt", 2),
                    track("pitchfork_bnt", 3),
                ],
            ),
            patch.object(
                StereogumNewMusic, "fetch", return_value=[track("stereogum_new_music", 4)]
            ),
            patch.object(GorillaVsBear, "fetch", return_value=[track("gorillavsbear", 5)]),
            patch.object(TheQuietus, "fetch", return_value=[]),
            patch.object(TheQuietusTracksOfMonth, "fetch", return_value=[]),
            patch.object(GuardianMusicAlbums, "fetch", return_value=[]),
            patch.object(NprNewMusicFridayStarting5, "fetch", return_value=[]),
            patch("peel.main.SpotifyClient", return_value=mock_sp),
            patch("peel.main.send_digest"),
        ):
            run()

        from peel.db import DB

        db = DB(str(db_path))
        db.init_schema()
        # Só as 2 novidades registadas (t3 pitchfork + t4 stereogum);
        # unmatched não foram registadas em `tracks`; t5 capped não registada.
        count = db.conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0]
        assert count == 2
        recorded_uris = {
            row[0] for row in db.conn.execute("SELECT DISTINCT spotify_uri FROM tracks").fetchall()
        }
        assert recorded_uris == {"spotify:track:3", "spotify:track:4"}
        # Unmatched ficaram na tabela `unmatched` para retry futuro.
        unmatched_count = db.conn.execute("SELECT COUNT(*) FROM unmatched").fetchone()[0]
        assert unmatched_count == 2
        called_uris = mock_sp.replace_playlist_items.call_args.args[1]
        assert set(called_uris) == {"spotify:track:3", "spotify:track:4"}
        db.close()

    def test_run_skips_non_track_non_album_sources(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        db_path = tmp_path / "test.db"
        monkeypatch.setenv("PEEL_PLAYLIST_ID", "spotify:playlist:test")
        monkeypatch.setenv("SPOTIFY_CLIENT_ID", "test_id")
        monkeypatch.setenv("SPOTIFY_CLIENT_SECRET", "test_secret")
        monkeypatch.setenv("SPOTIFY_REFRESH_TOKEN", "test_token")

        from peel import config as config_module

        monkeypatch.setattr(config_module.settings, "db_path", str(db_path))

        context_track = Track(
            source_id="pitchfork_bnt",
            artist="Context Artist",
            title="Context Item",
        )
        mock_sp = MagicMock()
        mock_sp.search_track = MagicMock()
        mock_sp.replace_playlist_items = MagicMock()

        with (
            patch.object(PitchforkBNT, "kind", "context", create=True),
            patch.object(PitchforkBNT, "fetch", return_value=[context_track]),
            patch.object(StereogumNewMusic, "fetch", return_value=[]),
            patch.object(TheQuietus, "fetch", return_value=[]),
            patch.object(TheQuietusTracksOfMonth, "fetch", return_value=[]),
            patch.object(GorillaVsBear, "fetch", return_value=[]),
            patch.object(GuardianMusicAlbums, "fetch", return_value=[]),
            patch.object(NprNewMusicFridayStarting5, "fetch", return_value=[]),
            patch("peel.main.SpotifyClient", return_value=mock_sp),
            patch("peel.main.send_digest"),
        ):
            run()

        mock_sp.search_track.assert_not_called()
        called_uris = mock_sp.replace_playlist_items.call_args.args[1]
        assert called_uris == []

    def test_run_skips_banned_uri(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """URI com feedback ban não é registada nem entra na rotação."""
        db_path = tmp_path / "test.db"
        monkeypatch.setenv("PEEL_PLAYLIST_ID", "spotify:playlist:test")
        monkeypatch.setenv("SPOTIFY_CLIENT_ID", "test_id")
        monkeypatch.setenv("SPOTIFY_CLIENT_SECRET", "test_secret")
        monkeypatch.setenv("SPOTIFY_REFRESH_TOKEN", "test_token")

        from peel import config as config_module
        from peel.db import DB

        monkeypatch.setattr(config_module.settings, "db_path", str(db_path))

        db = DB(str(db_path))
        db.init_schema()
        db.upsert_feedback("spotify:track:banned", "ban", "não quero isto")
        db.close()

        track = Track(source_id="pitchfork_bnt", artist="Banned Artist", title="Banned Song")
        mock_sp = MagicMock()
        mock_sp.search_track = MagicMock(
            return_value=[
                {
                    "uri": "spotify:track:banned",
                    "name": "Banned Song",
                    "artists": ["Banned Artist"],
                }
            ]
        )
        mock_sp.replace_playlist_items = MagicMock()

        with (
            patch.object(PitchforkBNT, "fetch", return_value=[track]),
            patch.object(StereogumNewMusic, "fetch", return_value=[]),
            patch.object(TheQuietus, "fetch", return_value=[]),
            patch.object(TheQuietusTracksOfMonth, "fetch", return_value=[]),
            patch.object(GorillaVsBear, "fetch", return_value=[]),
            patch.object(GuardianMusicAlbums, "fetch", return_value=[]),
            patch.object(NprNewMusicFridayStarting5, "fetch", return_value=[]),
            patch("peel.main.SpotifyClient", return_value=mock_sp),
            patch("peel.main.send_digest"),
        ):
            run()

        db = DB(str(db_path))
        db.init_schema()
        assert db.conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0] == 0
        assert mock_sp.replace_playlist_items.call_args.args[1] == []
        db.close()

    def test_run_skips_banned_artist_title_without_banning_artist(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Ban bloqueia a mesma faixa, mas não bloqueia automaticamente o artista."""
        db_path = tmp_path / "test.db"
        monkeypatch.setenv("PEEL_PLAYLIST_ID", "spotify:playlist:test")
        monkeypatch.setenv("SPOTIFY_CLIENT_ID", "test_id")
        monkeypatch.setenv("SPOTIFY_CLIENT_SECRET", "test_secret")
        monkeypatch.setenv("SPOTIFY_REFRESH_TOKEN", "test_token")

        from peel import config as config_module
        from peel.db import DB

        monkeypatch.setattr(config_module.settings, "db_path", str(db_path))
        monkeypatch.setattr(config_module.settings, "peel_max_tracks_per_source", 10)

        db = DB(str(db_path))
        db.init_schema()
        db.record_track("spotify:track:old", "source-a", "Artist", "Bad Song", None)
        db.upsert_feedback("spotify:track:old", "ban", None)
        db.close()

        tracks = [
            Track(source_id="pitchfork_bnt", artist="Artist", title="Bad Song - Radio Edit"),
            Track(source_id="pitchfork_bnt", artist="Artist", title="Good Song"),
        ]
        mock_sp = MagicMock()
        mock_sp.search_track = MagicMock(
            return_value=[
                {
                    "uri": "spotify:track:good",
                    "name": "Good Song",
                    "artists": ["Artist"],
                }
            ]
        )
        mock_sp.replace_playlist_items = MagicMock()

        with (
            patch.object(PitchforkBNT, "fetch", return_value=tracks),
            patch.object(StereogumNewMusic, "fetch", return_value=[]),
            patch.object(TheQuietus, "fetch", return_value=[]),
            patch.object(TheQuietusTracksOfMonth, "fetch", return_value=[]),
            patch.object(GorillaVsBear, "fetch", return_value=[]),
            patch.object(GuardianMusicAlbums, "fetch", return_value=[]),
            patch.object(NprNewMusicFridayStarting5, "fetch", return_value=[]),
            patch("peel.main.SpotifyClient", return_value=mock_sp),
            patch("peel.main.send_digest"),
        ):
            run()

        # Só a música nova do mesmo artista foi pesquisada/adicionada.
        mock_sp.search_track.assert_called_once_with("Artist", "Good Song", limit=5)
        called_uris = mock_sp.replace_playlist_items.call_args.args[1]
        assert called_uris == ["spotify:track:good"]

    def test_run_orders_playlist_by_consensus_before_recency(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Faixas multi-source sobem acima de singles mais recentes."""
        db_path = tmp_path / "test.db"
        monkeypatch.setenv("PEEL_PLAYLIST_ID", "spotify:playlist:test")
        monkeypatch.setenv("SPOTIFY_CLIENT_ID", "test_id")
        monkeypatch.setenv("SPOTIFY_CLIENT_SECRET", "test_secret")
        monkeypatch.setenv("SPOTIFY_REFRESH_TOKEN", "test_token")

        from peel import config as config_module

        monkeypatch.setattr(config_module.settings, "db_path", str(db_path))
        monkeypatch.setattr(config_module.settings, "peel_max_tracks_per_source", 10)
        monkeypatch.setattr(config_module.settings, "peel_max_tracks_per_run", 10)

        shared_a = Track(source_id="pitchfork_bnt", artist="Shared", title="Consensus")
        shared_b = Track(source_id="stereogum_new_music", artist="Shared", title="Consensus")
        recent_single = Track(source_id="gorillavsbear", artist="Recent", title="Single")

        def search_track(artist, title, limit=5):
            uri = "spotify:track:shared" if title == "Consensus" else "spotify:track:single"
            return [{"uri": uri, "name": title, "artists": [artist]}]

        mock_sp = MagicMock()
        mock_sp.search_track = MagicMock(side_effect=search_track)
        mock_sp.replace_playlist_items = MagicMock()

        with (
            patch.object(PitchforkBNT, "fetch", return_value=[shared_a]),
            patch.object(StereogumNewMusic, "fetch", return_value=[shared_b]),
            patch.object(TheQuietus, "fetch", return_value=[]),
            patch.object(TheQuietusTracksOfMonth, "fetch", return_value=[]),
            patch.object(GorillaVsBear, "fetch", return_value=[recent_single]),
            patch.object(GuardianMusicAlbums, "fetch", return_value=[]),
            patch.object(NprNewMusicFridayStarting5, "fetch", return_value=[]),
            patch("peel.main.SpotifyClient", return_value=mock_sp),
            patch("peel.main.send_digest"),
        ):
            run()

        called_uris = mock_sp.replace_playlist_items.call_args.args[1]
        assert called_uris == ["spotify:track:shared", "spotify:track:single"]

    def test_run_falls_back_to_recency_if_playlist_ranking_fails(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        db_path = tmp_path / "test.db"
        monkeypatch.setenv("PEEL_PLAYLIST_ID", "spotify:playlist:test")
        monkeypatch.setenv("SPOTIFY_CLIENT_ID", "test_id")
        monkeypatch.setenv("SPOTIFY_REFRESH_TOKEN", "test_token")
        monkeypatch.setenv("SPOTIFY_CLIENT_SECRET", "test_secret")

        from peel import config as config_module
        from peel.db import DB

        monkeypatch.setattr(config_module.settings, "db_path", str(db_path))

        mock_sp = MagicMock()
        mock_sp.search_track = MagicMock(
            return_value=[{"uri": "spotify:track:1", "name": "Track", "artists": ["Artist"]}]
        )
        mock_sp.replace_playlist_items = MagicMock()

        with (
            patch.object(
                PitchforkBNT,
                "fetch",
                return_value=[Track(source_id="pitchfork_bnt", artist="Artist", title="Track")],
            ),
            patch.object(StereogumNewMusic, "fetch", return_value=[]),
            patch.object(TheQuietus, "fetch", return_value=[]),
            patch.object(TheQuietusTracksOfMonth, "fetch", return_value=[]),
            patch.object(GorillaVsBear, "fetch", return_value=[]),
            patch.object(GuardianMusicAlbums, "fetch", return_value=[]),
            patch.object(NprNewMusicFridayStarting5, "fetch", return_value=[]),
            patch.object(DB, "ranked_tracks_in_window", side_effect=RuntimeError("boom")),
            patch("peel.main.SpotifyClient", return_value=mock_sp),
            patch("peel.main.send_digest"),
        ):
            run()

        assert mock_sp.replace_playlist_items.call_args.args[1] == ["spotify:track:1"]


class TestSourceSlots:
    def test_slots_for_source_uses_default_without_score(self) -> None:
        assert slots_for_source(None, default=8) == 8

    def test_slots_for_source_uses_default_with_insufficient_feedback(self) -> None:
        score = SourceScore(source_id="source", rating_total=8, rating_count=4)

        assert slots_for_source(score, default=8) == 8

    def test_slots_for_source_rewards_high_average(self) -> None:
        score = SourceScore(source_id="source", rating_total=6, rating_count=5)

        assert slots_for_source(score, default=8) == 12

    def test_slots_for_source_penalizes_negative_average(self) -> None:
        score = SourceScore(source_id="source", rating_total=-3, rating_count=5)

        assert slots_for_source(score, default=4) == 2

    def test_slots_for_source_keeps_neutral_average(self) -> None:
        score = SourceScore(source_id="source", rating_total=2, rating_count=5)

        assert slots_for_source(score, default=8) == 8


class TestFreshnessFilter:
    def test_filter_fresh_source_items_skips_old_published_tracks(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from peel import config as config_module

        monkeypatch.setattr(config_module.settings, "peel_max_source_item_age_days", 30)
        now = datetime(2026, 5, 1, tzinfo=UTC)
        fresh = Track(
            source_id="source-a",
            artist="Fresh Artist",
            title="Fresh Track",
            published_at=now - timedelta(days=5),
        )
        old = Track(
            source_id="source-a",
            artist="Old Artist",
            title="Old Track",
            published_at=now - timedelta(days=60),
        )
        no_date = Track(source_id="source-a", artist="No Date", title="No Date Track")

        result = _filter_fresh_source_items("source-a", [fresh, old, no_date], now)

        assert result == [fresh, no_date]


class TestRetryUnmatched:
    """Testa o fluxo de retry de unmatched (fix #2)."""

    def test_retry_promotes_matched_and_cleans_unmatched(self, tmp_path: Path) -> None:
        """Track que antes falhou é promovida quando Spotify agora devolve hit."""
        from peel.db import DB

        db = DB(str(tmp_path / "test.db"))
        db.init_schema()
        db.record_unmatched(
            "pitchfork_bnt",
            "Claire Rousay",
            "Hey Eleanor",
            "https://source/hey-eleanor",
        )

        mock_sp = MagicMock()
        mock_sp.search_track = MagicMock(
            return_value=[
                {
                    "uri": "spotify:track:abc",
                    "name": "Hey Eleanor",
                    "artists": ["Claire Rousay"],
                }
            ]
        )

        digest_entries: list = []
        total, matched = _retry_unmatched(db, mock_sp, digest_entries)

        assert total == 1
        assert matched == 1
        # Unmatched foi limpo
        assert db.list_unmatched(30) == []
        # Track foi registada e atribuída à source original
        row = db.conn.execute(
            "SELECT spotify_uri FROM tracks WHERE spotify_uri='spotify:track:abc'"
        ).fetchone()
        assert row is not None
        assert db.track_sources("spotify:track:abc") == [
            ("pitchfork_bnt", "https://source/hey-eleanor")
        ]
        # Entrou no digest do Telegram com link da source preservado
        assert digest_entries == [
            ("pitchfork_bnt", "Claire Rousay", "Hey Eleanor", "https://source/hey-eleanor")
        ]
        db.close()

    def test_retry_keeps_source_attribution_for_existing_uri(self, tmp_path: Path) -> None:
        """Se a URI já existir, retry regista a nova source mas não duplica digest."""
        from peel.db import DB

        db = DB(str(tmp_path / "test.db"))
        db.init_schema()

        uri = "spotify:track:shared"
        db.record_track(uri, "source-a", "Artist", "Title", None)
        db.record_unmatched("source-b", "Artist", "Title")

        mock_sp = MagicMock()
        mock_sp.search_track = MagicMock(
            return_value=[
                {
                    "uri": uri,
                    "name": "Title",
                    "artists": ["Artist"],
                }
            ]
        )

        digest_entries: list = []
        total, matched = _retry_unmatched(db, mock_sp, digest_entries)

        assert total == 1
        assert matched == 1
        assert db.list_unmatched(30) == []
        assert db.track_sources(uri) == [("source-a", None), ("source-b", None)]
        assert digest_entries == []
        db.close()

    def test_retry_respects_new_track_cap(self, tmp_path: Path) -> None:
        """Retry não deve promover backlog inteiro quando o cap foi atingido."""
        from peel.db import DB

        db = DB(str(tmp_path / "test.db"))
        db.init_schema()
        db.record_unmatched("source-a", "Artist A", "Track A")
        db.record_unmatched("source-b", "Artist B", "Track B")

        mock_sp = MagicMock()
        mock_sp.search_track.side_effect = [
            [{"uri": "spotify:track:a", "name": "Track A", "artists": ["Artist A"]}],
            [{"uri": "spotify:track:b", "name": "Track B", "artists": ["Artist B"]}],
        ]

        digest_entries: list = []
        total, matched = _retry_unmatched(db, mock_sp, digest_entries, max_new_tracks=1)

        assert total == 2
        assert matched == 1
        assert digest_entries == [("source-a", "Artist A", "Track A", None)]
        assert db.already_added("spotify:track:a") is True
        assert db.already_added("spotify:track:b") is False
        assert db.list_unmatched(30) == [("source-b", "Artist B", "Track B")]
        db.close()

    def test_retry_skips_banned_artist_title(self, tmp_path: Path) -> None:
        """Retry não reintroduz uma faixa já banida por artist+title."""
        from peel.db import DB

        db = DB(str(tmp_path / "test.db"))
        db.init_schema()
        db.record_track("spotify:track:banned", "source-a", "Artist", "Bad Song", None)
        db.upsert_feedback("spotify:track:banned", "ban", None)
        db.record_unmatched("source-b", "Artist", "Bad Song")

        mock_sp = MagicMock()
        digest_entries: list = []
        total, matched = _retry_unmatched(db, mock_sp, digest_entries)

        assert (total, matched) == (1, 0)
        assert digest_entries == []
        assert db.list_unmatched(30) == []
        mock_sp.search_track.assert_not_called()
        db.close()

    def test_retry_skips_banned_uri(self, tmp_path: Path) -> None:
        """Retry não promove um match cuja URI já tem feedback ban."""
        from peel.db import DB

        db = DB(str(tmp_path / "test.db"))
        db.init_schema()
        db.upsert_feedback("spotify:track:banned", "ban", None)
        db.record_unmatched("source-a", "Artist", "Title")

        mock_sp = MagicMock()
        mock_sp.search_track = MagicMock(
            return_value=[
                {
                    "uri": "spotify:track:banned",
                    "name": "Title",
                    "artists": ["Artist"],
                }
            ]
        )

        digest_entries: list = []
        total, matched = _retry_unmatched(db, mock_sp, digest_entries)

        assert (total, matched) == (1, 0)
        assert digest_entries == []
        assert db.list_unmatched(30) == []
        assert db.conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0] == 0
        db.close()

    def test_retry_handles_empty_table(self, tmp_path: Path) -> None:
        """Sem rows unmatched, retorna (0, 0) sem chamar Spotify."""
        from peel.db import DB

        db = DB(str(tmp_path / "test.db"))
        db.init_schema()

        mock_sp = MagicMock()
        total, matched = _retry_unmatched(db, mock_sp, [])
        assert (total, matched) == (0, 0)
        mock_sp.search_track.assert_not_called()
        db.close()


class TestConsensusAttribution:
    """Tracks iguais vindas de múltiplas sources devem ser atribuídas, não ignoradas."""

    def test_run_records_same_uri_from_multiple_sources_once_in_playlist(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        db_path = tmp_path / "test.db"
        monkeypatch.setenv("PEEL_PLAYLIST_ID", "spotify:playlist:test")
        monkeypatch.setenv("SPOTIFY_CLIENT_ID", "test_id")
        monkeypatch.setenv("SPOTIFY_CLIENT_SECRET", "test_secret")
        monkeypatch.setenv("SPOTIFY_REFRESH_TOKEN", "test_token")

        from peel import config as config_module

        monkeypatch.setattr(config_module.settings, "db_path", str(db_path))

        mock_sp = MagicMock()
        shared_uri = "spotify:track:shared"
        mock_sp.search_track = MagicMock(
            return_value=[
                {
                    "uri": shared_uri,
                    "name": "Shared Track",
                    "artists": ["Shared Artist"],
                }
            ]
        )
        mock_sp.replace_playlist_items = MagicMock()

        shared_track = Track(
            source_id="pitchfork_bnt",
            artist="Shared Artist",
            title="Shared Track",
            source_url="https://example.com/pitchfork",
        )
        shared_track_2 = Track(
            source_id="stereogum_new_music",
            artist="Shared Artist",
            title="Shared Track",
            source_url="https://example.com/stereogum",
        )
        shared_track_3 = Track(
            source_id="thequietus",
            artist="Shared Artist",
            title="Shared Track",
            source_url="https://example.com/quietus",
        )
        shared_track_4 = Track(
            source_id="gorillavsbear",
            artist="Shared Artist",
            title="Shared Track",
            source_url="https://example.com/gvb",
        )

        with (
            patch.object(PitchforkBNT, "fetch", return_value=[shared_track]),
            patch.object(StereogumNewMusic, "fetch", return_value=[shared_track_2]),
            patch.object(TheQuietus, "kind", "track", create=True),
            patch.object(TheQuietus, "fetch", return_value=[shared_track_3]),
            patch.object(TheQuietusTracksOfMonth, "fetch", return_value=[]),
            patch.object(GorillaVsBear, "fetch", return_value=[shared_track_4]),
            patch.object(GuardianMusicAlbums, "fetch", return_value=[]),
            patch.object(NprNewMusicFridayStarting5, "fetch", return_value=[]),
            patch("peel.main.SpotifyClient", return_value=mock_sp),
            patch("peel.main.send_digest"),
        ):
            run()

        from peel.db import DB

        db = DB(str(db_path))
        db.init_schema()

        cursor = db.conn.execute(
            "SELECT COUNT(*) FROM tracks WHERE spotify_uri = ?",
            (shared_uri,),
        )
        count = cursor.fetchone()[0]
        assert count == 4
        assert db.track_sources(shared_uri) == [
            ("pitchfork_bnt", "https://example.com/pitchfork"),
            ("stereogum_new_music", "https://example.com/stereogum"),
            ("thequietus", "https://example.com/quietus"),
            ("gorillavsbear", "https://example.com/gvb"),
        ]

        called_uris = mock_sp.replace_playlist_items.call_args.args[1]
        assert called_uris == [shared_uri]

        db.close()
