"""Testes para a camada de database."""

from datetime import UTC, datetime
from pathlib import Path

from peel.db import DB, iso_week


class TestInitSchema:
    """Testa a inicialização idempotente do schema."""

    def test_init_schema_creates_tables(self, tmp_path: Path) -> None:
        """init_schema() cria as 4 tabelas."""
        db_path = tmp_path / "test.db"
        db = DB(str(db_path))
        db.init_schema()

        # Verifica que as 4 tabelas existem
        cursor = db.conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
        tables = [row[0] for row in cursor.fetchall()]

        assert "albums" in tables
        assert "sources_state" in tables
        assert "tracks" in tables
        assert "unmatched" in tables

    def test_init_schema_idempotent(self, tmp_path: Path) -> None:
        """init_schema() pode ser corrido 2x sem erro."""
        db_path = tmp_path / "test.db"
        db = DB(str(db_path))

        # Correr 2x
        db.init_schema()
        db.init_schema()  # Não deve falhar

        # Ainda temos as 4 tabelas
        cursor = db.conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table'")
        table_count = cursor.fetchone()[0]
        assert table_count == 4

    def test_db_directory_created(self, tmp_path: Path) -> None:
        """DB cria o diretório pai se não existir."""
        nested_path = tmp_path / "subdir" / "deep" / "test.db"
        db = DB(str(nested_path))
        db.init_schema()

        assert nested_path.exists()


class TestAlreadyAdded:
    """Testa a verificação de faixa já adicionada."""

    def test_already_added_false_when_empty(self, tmp_path: Path) -> None:
        """already_added() retorna False para URI inexistente."""
        db_path = tmp_path / "test.db"
        db = DB(str(db_path))
        db.init_schema()

        result = db.already_added("spotify:track:nothere")
        assert result is False

    def test_already_added_true_after_record(self, tmp_path: Path) -> None:
        """already_added() retorna True após record_track()."""
        db_path = tmp_path / "test.db"
        db = DB(str(db_path))
        db.init_schema()

        uri = "spotify:track:123"
        db.record_track(uri, "test_source", "Artist", "Title", None)

        result = db.already_added(uri)
        assert result is True

    def test_already_added_true_regardless_of_source(self, tmp_path: Path) -> None:
        """already_added() retorna True mesmo com source_id diferente.

        Dedup é por spotify_uri global, não por (uri, source_id).
        """
        db_path = tmp_path / "test.db"
        db = DB(str(db_path))
        db.init_schema()

        uri = "spotify:track:123"
        db.record_track(uri, "source1", "Artist", "Title", None)

        # Mesma URI, source diferente
        result = db.already_added(uri)
        assert result is True


class TestRecordTrack:
    """Testa o registro de faixas."""

    def test_record_track_basic(self, tmp_path: Path) -> None:
        """record_track() adiciona uma faixa à tabela."""
        db_path = tmp_path / "test.db"
        db = DB(str(db_path))
        db.init_schema()

        uri = "spotify:track:123"
        db.record_track(uri, "pitchfork_bnt", "Radiohead", "Idioteque", None)

        cursor = db.conn.execute("SELECT artist, title FROM tracks WHERE spotify_uri = ?", (uri,))
        row = cursor.fetchone()
        assert row is not None
        assert row[0] == "Radiohead"
        assert row[1] == "Idioteque"

    def test_record_track_idempotent(self, tmp_path: Path) -> None:
        """record_track() com mesma (uri, source_id) não duplica.

        PRIMARY KEY (spotify_uri, source_id) + INSERT OR IGNORE.
        """
        db_path = tmp_path / "test.db"
        db = DB(str(db_path))
        db.init_schema()

        uri = "spotify:track:123"
        source_id = "pitchfork_bnt"

        # Adicionar 2x
        db.record_track(uri, source_id, "Artist", "Title", None)
        db.record_track(uri, source_id, "Artist", "Title", None)

        # Contar: deve ser 1
        cursor = db.conn.execute(
            "SELECT COUNT(*) FROM tracks WHERE spotify_uri = ? AND source_id = ?",
            (uri, source_id),
        )
        count = cursor.fetchone()[0]
        assert count == 1

    def test_record_track_same_uri_different_source(self, tmp_path: Path) -> None:
        """Mesma spotify_uri com source_id diferente adiciona nova linha.

        Mesma faixa de duas fontes é legítimo.
        """
        db_path = tmp_path / "test.db"
        db = DB(str(db_path))
        db.init_schema()

        uri = "spotify:track:123"

        # Mesma faixa, duas fontes
        db.record_track(uri, "source1", "Artist", "Title", None)
        db.record_track(uri, "source2", "Artist", "Title", None)

        # Contar: deve ser 2
        cursor = db.conn.execute("SELECT COUNT(*) FROM tracks WHERE spotify_uri = ?", (uri,))
        count = cursor.fetchone()[0]
        assert count == 2

    def test_record_track_with_url(self, tmp_path: Path) -> None:
        """record_track() salva source_url se fornecido."""
        db_path = tmp_path / "test.db"
        db = DB(str(db_path))
        db.init_schema()

        uri = "spotify:track:123"
        url = "https://example.com/review"
        db.record_track(uri, "source", "Artist", "Title", url)

        cursor = db.conn.execute("SELECT source_url FROM tracks WHERE spotify_uri = ?", (uri,))
        row = cursor.fetchone()
        assert row is not None
        assert row[0] == url


class TestRecordUnmatched:
    """Testa o registro de faixas não-emparelhadas."""

    def test_record_unmatched_basic(self, tmp_path: Path) -> None:
        """record_unmatched() adiciona à tabela unmatched."""
        db_path = tmp_path / "test.db"
        db = DB(str(db_path))
        db.init_schema()

        db.record_unmatched("test_source", "Unknown Artist", "Unknown Title")

        cursor = db.conn.execute(
            "SELECT artist, title FROM unmatched WHERE source_id = ?", ("test_source",)
        )
        row = cursor.fetchone()
        assert row is not None
        assert row[0] == "Unknown Artist"
        assert row[1] == "Unknown Title"

    def test_record_unmatched_multiple(self, tmp_path: Path) -> None:
        """Múltiplas unmatched não são dedupadas (cada seen_at é único)."""
        db_path = tmp_path / "test.db"
        db = DB(str(db_path))
        db.init_schema()

        source_id = "test_source"
        artist = "Artist"
        title = "Title"

        # Adicionar 2x (mesma faixa, diferentes seen_at)
        db.record_unmatched(source_id, artist, title)
        db.record_unmatched(source_id, artist, title)

        # Contar: deve ser 2
        cursor = db.conn.execute(
            "SELECT COUNT(*) FROM unmatched WHERE source_id = ? AND artist = ? AND title = ?",
            (source_id, artist, title),
        )
        count = cursor.fetchone()[0]
        assert count == 2


class TestUpdateSourceState:
    """Testa a atualização de estado das sources."""

    def test_update_source_state_ok(self, tmp_path: Path) -> None:
        """update_source_state() com status='ok' (sem erro)."""
        db_path = tmp_path / "test.db"
        db = DB(str(db_path))
        db.init_schema()

        source_id = "test_source"
        db.update_source_state(source_id, "ok", error=None)

        cursor = db.conn.execute(
            "SELECT last_status, last_error FROM sources_state WHERE source_id = ?",
            (source_id,),
        )
        row = cursor.fetchone()
        assert row is not None
        assert row[0] == "ok"
        assert row[1] is None

    def test_update_source_state_error(self, tmp_path: Path) -> None:
        """update_source_state() com status='error' e mensagem."""
        db_path = tmp_path / "test.db"
        db = DB(str(db_path))
        db.init_schema()

        source_id = "test_source"
        error_msg = "Connection timeout"
        db.update_source_state(source_id, "error", error=error_msg)

        cursor = db.conn.execute(
            "SELECT last_status, last_error FROM sources_state WHERE source_id = ?",
            (source_id,),
        )
        row = cursor.fetchone()
        assert row is not None
        assert row[0] == "error"
        assert row[1] == error_msg

    def test_update_source_state_replace(self, tmp_path: Path) -> None:
        """update_source_state() com mesma source_id substitui (INSERT OR REPLACE)."""
        db_path = tmp_path / "test.db"
        db = DB(str(db_path))
        db.init_schema()

        source_id = "test_source"

        # Primeira atualização
        db.update_source_state(source_id, "ok")

        # Segunda atualização
        db.update_source_state(source_id, "error", "New error")

        cursor = db.conn.execute(
            "SELECT COUNT(*) FROM sources_state WHERE source_id = ?", (source_id,)
        )
        count = cursor.fetchone()[0]
        assert count == 1  # Só um registro, não dois

        cursor = db.conn.execute(
            "SELECT last_status, last_error FROM sources_state WHERE source_id = ?",
            (source_id,),
        )
        row = cursor.fetchone()
        assert row[0] == "error"
        assert row[1] == "New error"


class TestRecordAlbum:
    """Testa o registro de álbuns."""

    def test_record_album_new_returns_true(self, tmp_path: Path) -> None:
        """record_album() com álbum novo retorna True."""
        db_path = tmp_path / "test.db"
        db = DB(str(db_path))
        db.init_schema()

        artist = "Radiohead"
        album = "A Moon Shaped Pool"
        result = db.record_album(artist, album, "pitchfork_best_albums", "https://example.com")

        assert result is True

    def test_record_album_duplicate_returns_false(self, tmp_path: Path) -> None:
        """record_album() com álbum duplicado retorna False."""
        db_path = tmp_path / "test.db"
        db = DB(str(db_path))
        db.init_schema()

        artist = "Radiohead"
        album = "A Moon Shaped Pool"

        # Primeira inserção
        result1 = db.record_album(artist, album, "pitchfork_best_albums", "https://example.com")
        assert result1 is True

        # Segunda inserção (duplicado)
        result2 = db.record_album(artist, album, "pitchfork_best_albums", "https://example.com")
        assert result2 is False

    def test_record_album_stored_correctly(self, tmp_path: Path) -> None:
        """record_album() armazena os dados corretamente."""
        db_path = tmp_path / "test.db"
        db = DB(str(db_path))
        db.init_schema()

        artist = "Radiohead"
        album = "A Moon Shaped Pool"
        source_id = "pitchfork_best_albums"
        source_url = "https://example.com/review"

        db.record_album(artist, album, source_id, source_url)

        cursor = db.conn.execute(
            "SELECT artist, album, source_id, source_url"
            " FROM albums WHERE artist = ? AND album = ?",
            (artist, album),
        )
        row = cursor.fetchone()
        assert row is not None
        assert row[0] == artist
        assert row[1] == album
        assert row[2] == source_id
        assert row[3] == source_url

    def test_record_album_with_null_url(self, tmp_path: Path) -> None:
        """record_album() com source_url=None."""
        db_path = tmp_path / "test.db"
        db = DB(str(db_path))
        db.init_schema()

        result = db.record_album("Artist", "Album", "source", None)
        assert result is True

        cursor = db.conn.execute(
            "SELECT source_url FROM albums WHERE artist = ? AND album = ?",
            ("Artist", "Album"),
        )
        row = cursor.fetchone()
        assert row[0] is None


class TestDatetimeISO8601:
    """Testa que as datas são armazenadas em ISO 8601 UTC."""

    def test_added_at_is_iso8601(self, tmp_path: Path) -> None:
        """added_at em tracks é ISO 8601 format."""
        db_path = tmp_path / "test.db"
        db = DB(str(db_path))
        db.init_schema()

        uri = "spotify:track:123"
        db.record_track(uri, "source", "Artist", "Title", None)

        cursor = db.conn.execute("SELECT added_at FROM tracks WHERE spotify_uri = ?", (uri,))
        added_at = cursor.fetchone()[0]

        # Valida que é ISO 8601 (contém "T" e ":")
        assert "T" in added_at
        assert added_at.endswith("+00:00")  # UTC offset

    def test_seen_at_is_iso8601(self, tmp_path: Path) -> None:
        """seen_at em unmatched é ISO 8601 format."""
        db_path = tmp_path / "test.db"
        db = DB(str(db_path))
        db.init_schema()

        db.record_unmatched("source", "Artist", "Title")

        cursor = db.conn.execute("SELECT seen_at FROM unmatched LIMIT 1")
        seen_at = cursor.fetchone()[0]

        assert "T" in seen_at
        assert seen_at.endswith("+00:00")  # UTC offset

    def test_album_seen_at_is_iso8601(self, tmp_path: Path) -> None:
        """seen_at em albums é ISO 8601 format."""
        db_path = tmp_path / "test.db"
        db = DB(str(db_path))
        db.init_schema()

        db.record_album("Artist", "Album", "source", None)

        cursor = db.conn.execute("SELECT seen_at FROM albums LIMIT 1")
        seen_at = cursor.fetchone()[0]

        assert "T" in seen_at
        assert seen_at.endswith("+00:00")  # UTC offset


class TestIsoWeek:
    """Testa a função iso_week."""

    def test_iso_week_format(self) -> None:
        """iso_week() retorna formato 'YYYY-Www'."""
        dt = datetime(2026, 4, 19, tzinfo=UTC)
        result = iso_week(dt)
        assert result == "2026-W16"

    def test_iso_week_week_01(self) -> None:
        """iso_week() para primeira semana do ano."""
        dt = datetime(2026, 1, 1, tzinfo=UTC)  # 2026-01-01 é W01
        result = iso_week(dt)
        assert result == "2026-W01"

    def test_iso_week_week_52(self) -> None:
        """iso_week() para última semana do ano (wrap)."""
        dt = datetime(2025, 12, 30, tzinfo=UTC)  # 2025-12-30 é W01 de 2026 (ISO wrap)
        result = iso_week(dt)
        assert "W01" in result and "2026" in result


class TestMigrationIdempotent:
    """Testa que a migração de colunas é idempotente."""

    def test_migration_idempotent_call_twice(self, tmp_path: Path) -> None:
        """init_schema() pode ser corrido 2x sem erro."""
        db_path = tmp_path / "test.db"
        db = DB(str(db_path))

        # Correr 2x
        db.init_schema()
        db.init_schema()  # Não deve falhar

        # Verifica que coluna existe
        cols = [row[1] for row in db.conn.execute("PRAGMA table_info(tracks)").fetchall()]
        assert "added_at_week" in cols


class TestMigrationBackfill:
    """Testa a migração e backfill de added_at_week."""

    def test_migration_backfill_tracks(self, tmp_path: Path) -> None:
        """Migração backfill added_at_week em tracks."""
        db_path = tmp_path / "test.db"
        db = DB(str(db_path))

        # Cria schema inicial (sem coluna added_at_week)
        cursor = db.conn.cursor()
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS tracks (
                spotify_uri TEXT NOT NULL,
                source_id   TEXT NOT NULL,
                artist      TEXT NOT NULL,
                title       TEXT NOT NULL,
                source_url  TEXT,
                added_at    TEXT NOT NULL,
                PRIMARY KEY (spotify_uri, source_id)
            )
            """
        )
        db.conn.commit()

        # Insere linhas manualmente com added_at
        iso_timestamp = datetime(2026, 4, 19, 10, 30, 0, tzinfo=UTC).isoformat()
        cursor.execute(
            """
            INSERT INTO tracks
            (spotify_uri, source_id, artist, title, source_url, added_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            ("spotify:track:1", "source1", "Artist1", "Title1", None, iso_timestamp),
        )
        db.conn.commit()

        # Agora roda init_schema (trigger migration)
        db.init_schema()

        # Verifica que coluna foi adicionada e preenchida
        cursor = db.conn.execute(
            "SELECT added_at_week FROM tracks WHERE spotify_uri = ?",
            ("spotify:track:1",),
        )
        row = cursor.fetchone()
        assert row is not None
        assert row[0] == "2026-W16"  # 2026-04-19 é semana 16

    def test_migration_backfill_albums(self, tmp_path: Path) -> None:
        """Migração backfill added_at_week em albums."""
        db_path = tmp_path / "test.db"
        db = DB(str(db_path))

        # Cria schema inicial (sem coluna added_at_week)
        cursor = db.conn.cursor()
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS albums (
                artist     TEXT NOT NULL,
                album      TEXT NOT NULL,
                source_id  TEXT NOT NULL,
                source_url TEXT,
                seen_at    TEXT NOT NULL,
                PRIMARY KEY (artist, album)
            )
            """
        )
        db.conn.commit()

        # Insere linha com seen_at
        iso_timestamp = datetime(2026, 4, 19, 10, 30, 0, tzinfo=UTC).isoformat()
        cursor.execute(
            """
            INSERT INTO albums
            (artist, album, source_id, source_url, seen_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            ("Artist1", "Album1", "source1", None, iso_timestamp),
        )
        db.conn.commit()

        # Roda init_schema
        db.init_schema()

        # Verifica backfill
        cursor = db.conn.execute(
            "SELECT added_at_week FROM albums WHERE artist = ?",
            ("Artist1",),
        )
        row = cursor.fetchone()
        assert row is not None
        assert row[0] == "2026-W16"


class TestTracksInWindow:
    """Testa a query de tracks em janela de semanas."""

    def test_tracks_in_window_basic(self, tmp_path: Path) -> None:
        """tracks_in_window() retorna URIs da janela recente."""
        db_path = tmp_path / "test.db"
        db = DB(str(db_path))
        db.init_schema()

        # Insere tracks em diferentes semanas
        # W14: 2 semanas atrás
        dt_w14 = datetime(2026, 4, 6, tzinfo=UTC)  # W14
        db.conn.execute(
            """
            INSERT INTO tracks
            (spotify_uri, source_id, artist, title, source_url, added_at, added_at_week)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "spotify:track:w14",
                "source1",
                "Artist",
                "Title",
                None,
                dt_w14.isoformat(),
                "2026-W14",
            ),
        )

        # W15: 1 semana atrás
        dt_w15 = datetime(2026, 4, 13, tzinfo=UTC)  # W15
        db.conn.execute(
            """
            INSERT INTO tracks
            (spotify_uri, source_id, artist, title, source_url, added_at, added_at_week)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "spotify:track:w15",
                "source1",
                "Artist",
                "Title",
                None,
                dt_w15.isoformat(),
                "2026-W15",
            ),
        )

        # W16: semana atual
        dt_w16 = datetime(2026, 4, 19, tzinfo=UTC)  # W16
        db.conn.execute(
            """
            INSERT INTO tracks
            (spotify_uri, source_id, artist, title, source_url, added_at, added_at_week)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "spotify:track:w16",
                "source1",
                "Artist",
                "Title",
                None,
                dt_w16.isoformat(),
                "2026-W16",
            ),
        )
        db.conn.commit()

        # Window=1: só W16
        result = db.tracks_in_window("2026-W16", window=1)
        assert result == ["spotify:track:w16"]

        # Window=2: W15 + W16
        result = db.tracks_in_window("2026-W16", window=2)
        assert set(result) == {"spotify:track:w15", "spotify:track:w16"}

        # Window=3: W14 + W15 + W16
        result = db.tracks_in_window("2026-W16", window=3)
        assert set(result) == {"spotify:track:w14", "spotify:track:w15", "spotify:track:w16"}

    def test_tracks_in_window_year_wrap(self, tmp_path: Path) -> None:
        """tracks_in_window() funciona com wrap de ano (2025-W52 → 2026-W01)."""
        db_path = tmp_path / "test.db"
        db = DB(str(db_path))
        db.init_schema()

        # Insere track em 2025-W52 (última semana de 2025)
        db.conn.execute(
            """
            INSERT INTO tracks
            (spotify_uri, source_id, artist, title, source_url, added_at, added_at_week)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "spotify:track:2025w52",
                "source1",
                "Artist",
                "Title",
                None,
                "2025-12-29T10:00:00+00:00",
                "2025-W52",
            ),
        )

        # Insere track em 2026-W01
        db.conn.execute(
            """
            INSERT INTO tracks
            (spotify_uri, source_id, artist, title, source_url, added_at, added_at_week)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "spotify:track:2026w01",
                "source1",
                "Artist",
                "Title",
                None,
                "2026-01-05T10:00:00+00:00",
                "2026-W01",
            ),
        )
        db.conn.commit()

        # Query com current=2026-W01, window=2: deve incluir ambas
        result = db.tracks_in_window("2026-W01", window=2)
        assert set(result) == {"spotify:track:2025w52", "spotify:track:2026w01"}

        # Query com current=2026-W01, window=1: só 2026-W01
        result = db.tracks_in_window("2026-W01", window=1)
        assert result == ["spotify:track:2026w01"]

    def test_tracks_in_window_empty(self, tmp_path: Path) -> None:
        """tracks_in_window() retorna [] se nenhuma track na janela."""
        db_path = tmp_path / "test.db"
        db = DB(str(db_path))
        db.init_schema()

        result = db.tracks_in_window("2026-W16", window=2)
        assert result == []

    def test_tracks_in_window_dedup_distinct(self, tmp_path: Path) -> None:
        """tracks_in_window() retorna URIs distintos (DISTINCT)."""
        db_path = tmp_path / "test.db"
        db = DB(str(db_path))
        db.init_schema()

        # Insere mesma track 2x (de fontes diferentes)
        db.conn.execute(
            """
            INSERT INTO tracks
            (spotify_uri, source_id, artist, title, source_url, added_at, added_at_week)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "spotify:track:123",
                "source1",
                "Artist",
                "Title",
                None,
                "2026-04-19T10:00:00+00:00",
                "2026-W16",
            ),
        )
        db.conn.execute(
            """
            INSERT INTO tracks
            (spotify_uri, source_id, artist, title, source_url, added_at, added_at_week)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "spotify:track:123",
                "source2",
                "Artist",
                "Title",
                None,
                "2026-04-19T10:00:00+00:00",
                "2026-W16",
            ),
        )
        db.conn.commit()

        result = db.tracks_in_window("2026-W16", window=1)
        assert result == ["spotify:track:123"]
        assert len(result) == 1
