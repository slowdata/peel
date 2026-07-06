"""Testes para a camada de database."""

from datetime import UTC, datetime
from pathlib import Path

import pytest

from peel.db import DB, iso_week, rank_window_uris
from peel.matcher import normalize


class TestInitSchema:
    """Testa a inicialização idempotente do schema."""

    def test_init_schema_creates_tables(self, tmp_path: Path) -> None:
        """init_schema() cria as tabelas."""
        db_path = tmp_path / "test.db"
        db = DB(str(db_path))
        db.init_schema()

        # Verifica que as tabelas esperadas existem
        cursor = db.conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
        tables = [row[0] for row in cursor.fetchall()]

        assert "album_mentions" in tables
        assert "albums" in tables
        assert "feedback" in tables
        assert "source_runs" in tables
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

        # Ainda temos as tabelas esperadas
        cursor = db.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = {row[0] for row in cursor.fetchall()}
        assert {
            "album_mentions",
            "albums",
            "feedback",
            "source_runs",
            "sources_state",
            "tracks",
            "unmatched",
        }.issubset(tables)

    def test_db_directory_created(self, tmp_path: Path) -> None:
        """DB cria o diretório pai se não existir."""
        nested_path = tmp_path / "subdir" / "deep" / "test.db"
        db = DB(str(nested_path))
        db.init_schema()

        assert nested_path.exists()


class TestSourceRunsSchema:
    def test_init_schema_creates_source_runs_index(self, tmp_path: Path) -> None:
        db = DB(str(tmp_path / "test.db"))
        db.init_schema()

        rows = db.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='source_runs'"
        ).fetchall()
        indexes = {row[0] for row in rows}

        assert "idx_source_runs_source_run_at" in indexes

    def test_record_source_run_truncates_error(self, tmp_path: Path) -> None:
        db = DB(str(tmp_path / "test.db"))
        db.init_schema()

        db.record_source_run(
            source_id="source-a",
            run_at="2026-05-03T10:00:00+00:00",
            fetched_count=10,
            fresh_count=8,
            processed_count=5,
            matched_count=4,
            new_unique_count=3,
            unmatched_count=1,
            album_count=0,
            skipped_stale_count=2,
            skipped_cap_count=3,
            status="error",
            error="x" * 600,
        )

        row = db.conn.execute(
            """
            SELECT
                source_id,
                run_at,
                fetched_count,
                fresh_count,
                processed_count,
                matched_count,
                new_unique_count,
                unmatched_count,
                album_count,
                skipped_stale_count,
                skipped_cap_count,
                status,
                length(error)
            FROM source_runs
            """
        ).fetchone()

        assert row == (
            "source-a",
            "2026-05-03T10:00:00+00:00",
            10,
            8,
            5,
            4,
            3,
            1,
            0,
            2,
            3,
            "error",
            500,
        )


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

    def test_record_track_returns_inserted_flag(self, tmp_path: Path) -> None:
        """record_track() devolve True quando insere e False em duplicate key."""
        db_path = tmp_path / "test.db"
        db = DB(str(db_path))
        db.init_schema()

        uri = "spotify:track:123"
        assert db.record_track(uri, "pitchfork_bnt", "Artist", "Title", None) is True
        assert db.record_track(uri, "pitchfork_bnt", "Artist", "Title", None) is False

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

    def test_track_sources_returns_ordered_sources(self, tmp_path: Path) -> None:
        """track_sources() devolve as fontes de uma URI, por ordem de inserção."""
        db_path = tmp_path / "test.db"
        db = DB(str(db_path))
        db.init_schema()

        uri = "spotify:track:123"
        db.record_track(uri, "source-a", "Artist", "Title", "https://a")
        db.record_track(uri, "source-b", "Artist", "Title", "https://b")

        assert db.track_sources(uri) == [("source-a", "https://a"), ("source-b", "https://b")]

    def test_recent_tracks_with_sources_aggregates_source_count(self, tmp_path: Path) -> None:
        """recent_tracks_with_sources() agrega várias fontes na mesma URI."""
        db_path = tmp_path / "test.db"
        db = DB(str(db_path))
        db.init_schema()

        uri = "spotify:track:123"
        db.record_track(uri, "source-a", "Artist", "Title", "https://a")
        db.record_track(uri, "source-b", "Artist", "Title", "https://b")

        rows = db.recent_tracks_with_sources(limit=10)
        assert len(rows) == 1
        assert rows[0][0] == uri
        assert rows[0][4] == 2


class TestRecordUnmatched:
    """Testa o registro de faixas não-emparelhadas."""

    def test_record_unmatched_basic(self, tmp_path: Path) -> None:
        """record_unmatched() adiciona à tabela unmatched."""
        db_path = tmp_path / "test.db"
        db = DB(str(db_path))
        db.init_schema()

        db.record_unmatched(
            "test_source",
            "Unknown Artist",
            "Unknown Title",
            "https://source/item",
        )

        cursor = db.conn.execute(
            "SELECT artist, title, source_url FROM unmatched WHERE source_id = ?",
            ("test_source",),
        )
        row = cursor.fetchone()
        assert row is not None
        assert row[0] == "Unknown Artist"
        assert row[1] == "Unknown Title"
        assert row[2] == "https://source/item"

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

    def test_record_album_writes_album_mention_with_spotify_uri(self, tmp_path: Path) -> None:
        db = DB(str(tmp_path / "test.db"))
        db.init_schema()

        db.record_album(
            "Wax Machine",
            "The Sky Unfurls",
            "aquarium_drunkard",
            "https://example.com/review",
            spotify_album_uri="spotify:album:abc123",
        )

        row = db.conn.execute(
            """
            SELECT artist, album, artist_key, album_key, source_id, source_url,
                   spotify_album_uri
            FROM album_mentions
            WHERE source_id = ?
            """,
            ("aquarium_drunkard",),
        ).fetchone()

        assert row == (
            "Wax Machine",
            "The Sky Unfurls",
            "wax machine",
            "the sky unfurls",
            "aquarium_drunkard",
            "https://example.com/review",
            "spotify:album:abc123",
        )

    def test_record_album_duplicate_album_still_records_new_source_mention(
        self, tmp_path: Path
    ) -> None:
        db = DB(str(tmp_path / "test.db"))
        db.init_schema()

        assert db.record_album("Artist", "Album", "source-a", "https://a") is True
        assert db.record_album("Artist", "Album", "source-b", "https://b") is False

        count = db.conn.execute(
            """
            SELECT COUNT(DISTINCT source_id)
            FROM album_mentions
            WHERE artist_key = ? AND album_key = ?
            """,
            ("artist", "album"),
        ).fetchone()[0]

        assert count == 2

    def test_record_album_duplicate_normalized_case_returns_false(
        self, tmp_path: Path
    ) -> None:
        db = DB(str(tmp_path / "test.db"))
        db.init_schema()

        assert db.record_album("Sml", "Spontaneous Music Live", "source-a", "https://a") is True
        assert db.record_album("SML", "Spontaneous Music Live", "source-b", "https://b") is False

        albums_count = db.conn.execute("SELECT COUNT(*) FROM albums").fetchone()[0]
        mentions_count = db.conn.execute(
            """
            SELECT COUNT(*)
            FROM album_mentions
            WHERE artist_key = ? AND album_key = ?
            """,
            ("sml", "spontaneous music live"),
        ).fetchone()[0]

        assert albums_count == 1
        assert mentions_count == 2


class TestFeedback:
    """Testa feedback explícito do utilizador."""

    def test_upsert_feedback_inserts_and_reads(self, tmp_path: Path) -> None:
        db = DB(str(tmp_path / "test.db"))
        db.init_schema()

        db.upsert_feedback("spotify:track:123", "love", "muito bom")

        assert db.feedback_for_track("spotify:track:123") == (2, "love", "muito bom")

    def test_upsert_feedback_updates_existing(self, tmp_path: Path) -> None:
        db = DB(str(tmp_path / "test.db"))
        db.init_schema()

        db.upsert_feedback("spotify:track:123", "meh", None)
        db.upsert_feedback("spotify:track:123", "like", "melhorou")

        assert db.feedback_for_track("spotify:track:123") == (1, "like", "melhorou")

    def test_upsert_feedback_rejects_invalid_label(self, tmp_path: Path) -> None:
        db = DB(str(tmp_path / "test.db"))
        db.init_schema()

        with pytest.raises(ValueError):
            db.upsert_feedback("spotify:track:123", "great")

    def test_unrated_tracks_filters_feedback(self, tmp_path: Path) -> None:
        db = DB(str(tmp_path / "test.db"))
        db.init_schema()

        db.record_track("spotify:track:1", "source-a", "Artist A", "Track A", None)
        db.record_track("spotify:track:2", "source-b", "Artist B", "Track B", None)
        db.upsert_feedback("spotify:track:1", "like", None)

        rows = db.unrated_tracks(limit=10)
        assert {row[0] for row in rows} == {"spotify:track:2"}

    def test_unrated_tracks_filters_same_artist_title_with_different_uri(
        self,
        tmp_path: Path,
    ) -> None:
        db = DB(str(tmp_path / "test.db"))
        db.init_schema()

        db.record_track("spotify:track:old", "source-a", "Artist A", "Track A", None)
        db.record_track("spotify:track:new", "source-b", "Artist A", "Track A", None)
        db.record_track("spotify:track:other", "source-c", "Artist B", "Track B", None)
        db.upsert_feedback("spotify:track:old", "ban", None)

        rows = db.unrated_tracks(limit=10)

        assert {row[0] for row in rows} == {"spotify:track:other"}

    def test_is_banned_uri(self, tmp_path: Path) -> None:
        db = DB(str(tmp_path / "test.db"))
        db.init_schema()

        db.upsert_feedback("spotify:track:banned", "ban", None)
        db.upsert_feedback("spotify:track:loved", "love", None)

        assert db.is_banned_uri("spotify:track:banned") is True
        assert db.is_banned_uri("spotify:track:loved") is False
        assert db.is_banned_uri("spotify:track:unknown") is False

    def test_banned_track_keys_uses_normalized_artist_title(self, tmp_path: Path) -> None:
        db = DB(str(tmp_path / "test.db"))
        db.init_schema()

        db.record_track("spotify:track:ban", "source-a", "Beyoncé", "Halo (feat. Jay-Z)", None)
        db.record_track("spotify:track:ok", "source-b", "Beyoncé", "Formation", None)
        db.upsert_feedback("spotify:track:ban", "ban", None)
        db.upsert_feedback("spotify:track:ok", "love", None)

        assert db.banned_track_keys() == {(normalize("Beyoncé"), normalize("Halo"))}


class TestRankWindowUris:
    def test_rank_window_uris_prioritizes_consensus_then_quality_then_recency(self) -> None:
        rows = [
            ("spotify:track:single-recent", "A", "Recent", "low", "2026-06-14T10:00:00+00:00"),
            ("spotify:track:consensus-low", "B", "Low", "low", "2026-06-10T10:00:00+00:00"),
            ("spotify:track:consensus-low", "B", "Low", "neutral", "2026-06-10T11:00:00+00:00"),
            ("spotify:track:consensus-high", "C", "High", "high", "2026-06-09T10:00:00+00:00"),
            ("spotify:track:consensus-high", "C", "High", "neutral", "2026-06-09T11:00:00+00:00"),
            ("spotify:track:neutral-new", "D", "New", "unknown", "2026-06-08T10:00:00+00:00"),
            ("spotify:track:neutral-old", "E", "Old", "unknown", "2026-06-07T10:00:00+00:00"),
        ]
        source_quality = {
            "high": (1.5, 30.0),
            "low": (-0.5, -5.0),
            "neutral": (0.0, 0.0),
        }

        ranked = rank_window_uris(rows, source_quality)

        assert ranked == [
            "spotify:track:consensus-high",
            "spotify:track:consensus-low",
            "spotify:track:neutral-new",
            "spotify:track:neutral-old",
            "spotify:track:single-recent",
        ]

    def test_rank_window_uris_uses_score_then_uri_as_deterministic_tiebreaker(self) -> None:
        rows = [
            ("spotify:track:b", "B", "Track", "source-b", "2026-06-10T10:00:00+00:00"),
            ("spotify:track:a", "A", "Track", "source-a", "2026-06-10T10:00:00+00:00"),
        ]
        source_quality = {
            "source-a": (1.0, 10.0),
            "source-b": (1.0, 5.0),
        }

        ranked = rank_window_uris(rows, source_quality)

        assert ranked == ["spotify:track:a", "spotify:track:b"]

    def test_rank_window_uris_dedupes_same_artist_title_with_different_uris(self) -> None:
        """Diferentes URIs do mesmo (artista, título) devem colapsar para uma faixa.

        Spotify devolve às vezes URIs distintos para a mesma música (variantes de
        edição/região/clean/explicit). Sem dedupe por (artista, título) a mesma
        música apareceria duas vezes na playlist. Aqui:
          - "Dopamine" (Robyn) entra com URI-A (1 source) e URI-B (2 sources).
          - Deve sobrar só 1 URI na saída — o com mais sources (URI-B).
          - Consenso agregado = 3 sources (cruza URIs), à frente da faixa com 1 source.
        """
        rows = [
            (
                "spotify:track:dopamine-a",
                "Robyn",
                "Dopamine",
                "pitchfork_bnt",
                "2026-04-19T16:57:00+00:00",
            ),
            (
                "spotify:track:dopamine-b",
                "Robyn",
                "Dopamine",
                "pitchfork_bnt",
                "2026-04-19T23:01:00+00:00",
            ),
            (
                "spotify:track:dopamine-b",
                "Robyn",
                "Dopamine",
                "stereogum_new_music",
                "2026-04-20T10:00:00+00:00",
            ),
            (
                "spotify:track:other",
                "Other",
                "Other Track",
                "gorillavsbear",
                "2026-04-18T10:00:00+00:00",
            ),
        ]

        ranked = rank_window_uris(rows)

        assert len(ranked) == 2
        # Dopamine collapsed → sobra o URI com mais sources (b), não o URI-a.
        assert ranked[0] == "spotify:track:dopamine-b"  # consenso 3 > 1
        assert ranked[1] == "spotify:track:other"


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

    def test_migration_backfill_album_mentions_is_idempotent(self, tmp_path: Path) -> None:
        """Migração cria album_mentions e faz backfill sem duplicar."""
        db_path = tmp_path / "test.db"
        db = DB(str(db_path))

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
        iso_timestamp = datetime(2026, 4, 19, 10, 30, 0, tzinfo=UTC).isoformat()
        cursor.execute(
            """
            INSERT INTO albums
            (artist, album, source_id, source_url, seen_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            ("Artist1", "Album1", "source1", "https://album", iso_timestamp),
        )
        db.conn.commit()

        db.init_schema()
        db.init_schema()

        rows = db.conn.execute(
            """
            SELECT artist, album, artist_key, album_key, source_id, source_url,
                   spotify_album_uri, added_at_week
            FROM album_mentions
            """
        ).fetchall()

        assert rows == [
            (
                "Artist1",
                "Album1",
                "artist1",
                "album1",
                "source1",
                "https://album",
                None,
                "2026-W16",
            )
        ]

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

    def test_tracks_in_window_does_not_include_future_weeks(self, tmp_path: Path) -> None:
        """Consultar uma semana histórica não pode apanhar semanas posteriores."""
        db_path = tmp_path / "test.db"
        db = DB(str(db_path))
        db.init_schema()

        for uri, week, added_at in [
            ("spotify:track:w24", "2026-W24", "2026-06-13T10:00:00+00:00"),
            ("spotify:track:w25", "2026-W25", "2026-06-20T10:00:00+00:00"),
        ]:
            db.conn.execute(
                """
                INSERT INTO tracks
                (spotify_uri, source_id, artist, title, source_url, added_at, added_at_week)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (uri, "source1", "Artist", uri.rsplit(":", 1)[-1], None, added_at, week),
            )
        db.conn.commit()

        assert db.tracks_in_window("2026-W24", window=1) == ["spotify:track:w24"]
        assert db.ranked_tracks_in_window("2026-W24", window=1) == ["spotify:track:w24"]

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

    def test_tracks_in_window_filters_banned_tracks(self, tmp_path: Path) -> None:
        """Bans explícitos não entram na rotação, mesmo com outra URI."""
        db_path = tmp_path / "test.db"
        db = DB(str(db_path))
        db.init_schema()

        for uri, artist, title in [
            ("spotify:track:banned", "Artist", "Bad Song"),
            ("spotify:track:alt", "Artist", "Bad Song - Radio Edit"),
            ("spotify:track:ok", "Artist", "Good Song"),
        ]:
            db.conn.execute(
                """
                INSERT INTO tracks
                (spotify_uri, source_id, artist, title, source_url, added_at, added_at_week)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    uri,
                    "source1",
                    artist,
                    title,
                    None,
                    "2026-04-19T10:00:00+00:00",
                    "2026-W16",
                ),
            )
        db.conn.commit()
        db.upsert_feedback("spotify:track:banned", "ban", None)

        result = db.tracks_in_window("2026-W16", window=1)

        assert result == ["spotify:track:ok"]

    def test_ranked_tracks_in_window_keeps_bans_filtered(self, tmp_path: Path) -> None:
        db_path = tmp_path / "test.db"
        db = DB(str(db_path))
        db.init_schema()
        db.record_track("spotify:track:banned", "source-a", "Artist", "Bad Song", None)
        db.record_track("spotify:track:ok", "source-a", "Artist", "Good Song", None)
        db.upsert_feedback("spotify:track:banned", "ban", None)

        result = db.ranked_tracks_in_window(
            iso_week(datetime.now(UTC)),
            window=1,
            source_quality={"source-a": (2.0, 20.0)},
        )

        assert result == ["spotify:track:ok"]


class TestUnmatchedRetryHelpers:
    """list_unmatched, delete_unmatched, prune_unmatched."""

    def test_list_unmatched_dedupes_and_filters_age(self, tmp_path: Path) -> None:
        db = DB(str(tmp_path / "test.db"))
        db.init_schema()

        # 3 rows iguais (simula múltiplas runs) + 1 antiga
        for _ in range(3):
            db.record_unmatched("pitchfork_bnt", "Ms Ray", "Miss You")

        # Row antiga: injectada directamente com seen_at no passado
        old = (datetime.now(UTC)).replace(year=2020).isoformat()
        db.conn.execute(
            "INSERT INTO unmatched (source_id, artist, title, seen_at) VALUES (?,?,?,?)",
            ("pitchfork_bnt", "Old", "Forgotten", old),
        )
        db.conn.commit()

        rows = db.list_unmatched(max_age_days=30)
        # Row antiga filtrada, duplicados colapsados
        assert rows == [("pitchfork_bnt", "Ms Ray", "Miss You")]

    def test_list_unmatched_with_urls_preserves_source_url(self, tmp_path: Path) -> None:
        db = DB(str(tmp_path / "test.db"))
        db.init_schema()

        db.record_unmatched(
            "stereogum_new_music",
            "Helado Negro",
            "Dance To The Music",
            "https://stereogum.com/example",
        )

        rows = db.list_unmatched_with_urls(max_age_days=30)

        assert rows == [
            (
                "stereogum_new_music",
                "Helado Negro",
                "Dance To The Music",
                "https://stereogum.com/example",
            )
        ]

    def test_delete_unmatched_removes_all_matching(self, tmp_path: Path) -> None:
        db = DB(str(tmp_path / "test.db"))
        db.init_schema()

        for _ in range(3):
            db.record_unmatched("pitchfork_bnt", "Ms Ray", "Miss You")
        db.record_unmatched("pitchfork_bnt", "Other", "Song")

        deleted = db.delete_unmatched("pitchfork_bnt", "Ms Ray", "Miss You")
        assert deleted == 3

        remaining = db.list_unmatched(max_age_days=30)
        assert remaining == [("pitchfork_bnt", "Other", "Song")]

    def test_prune_unmatched_removes_old(self, tmp_path: Path) -> None:
        db = DB(str(tmp_path / "test.db"))
        db.init_schema()

        db.record_unmatched("pitchfork_bnt", "Fresh", "Track")
        old = datetime.now(UTC).replace(year=2020).isoformat()
        db.conn.execute(
            "INSERT INTO unmatched (source_id, artist, title, seen_at) VALUES (?,?,?,?)",
            ("pitchfork_bnt", "Stale", "Song", old),
        )
        db.conn.commit()

        pruned = db.prune_unmatched(max_age_days=30)
        assert pruned == 1

        remaining = db.list_unmatched(max_age_days=365 * 10)
        assert remaining == [("pitchfork_bnt", "Fresh", "Track")]


def test_week_keeper_uris_excludes_meh_skip_ban_keeps_unrated(tmp_path: Path) -> None:
    db = DB(str(tmp_path / "keepers.db"))
    db.init_schema()
    week = iso_week(datetime.now(UTC))
    specs = [
        ("spotify:track:lov", "love"),
        ("spotify:track:lik", "like"),
        ("spotify:track:meh", "meh"),
        ("spotify:track:skp", "skip"),
        ("spotify:track:ban", "ban"),
        ("spotify:track:non", None),
    ]
    for uri, label in specs:
        db.record_track(uri, "stereogum_new_music", "Artist", uri[-3:], None)
        if label is not None:
            db.upsert_feedback(uri, label)

    keepers = db.week_keeper_uris(week)
    db.close()
    # Mantém love/like e o não-avaliado; exclui meh/skip/ban.
    assert set(keepers) == {"spotify:track:lov", "spotify:track:lik", "spotify:track:non"}
