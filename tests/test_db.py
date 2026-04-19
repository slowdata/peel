"""Testes para a camada de database."""

from pathlib import Path

from peel.db import DB


class TestInitSchema:
    """Testa a inicialização idempotente do schema."""

    def test_init_schema_creates_tables(self, tmp_path: Path) -> None:
        """init_schema() cria as 3 tabelas."""
        db_path = tmp_path / "test.db"
        db = DB(str(db_path))
        db.init_schema()

        # Verifica que as 3 tabelas existem
        cursor = db.conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
        tables = [row[0] for row in cursor.fetchall()]

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

        # Ainda temos as tabelas
        cursor = db.conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table'")
        table_count = cursor.fetchone()[0]
        assert table_count == 3

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
