from __future__ import annotations

from pathlib import Path

from peel.db import DB
from peel.report import build_weekly_report, generate_weekly_report


class TestWeeklyReport:
    def test_build_weekly_report_renders_sections(self, tmp_path: Path) -> None:
        db = DB(str(tmp_path / "peel.db"))
        db.init_schema()

        week = "2026-W18"

        db.conn.execute(
            """
            INSERT INTO tracks
            (spotify_uri, source_id, artist, title, source_url, added_at, added_at_week)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "spotify:track:shared",
                "source-a",
                "Artist A",
                "Track A",
                "https://a",
                "2026-05-01T10:00:00+00:00",
                week,
            ),
        )
        db.conn.execute(
            """
            INSERT INTO tracks
            (spotify_uri, source_id, artist, title, source_url, added_at, added_at_week)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "spotify:track:shared",
                "source-b",
                "Artist A",
                "Track A",
                "https://b",
                "2026-05-01T11:00:00+00:00",
                week,
            ),
        )
        db.conn.execute(
            """
            INSERT INTO tracks
            (spotify_uri, source_id, artist, title, source_url, added_at, added_at_week)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "spotify:track:unique",
                "source-c",
                "Artist B",
                "Track B",
                None,
                "2026-05-01T12:00:00+00:00",
                week,
            ),
        )
        db.conn.execute(
            """
            INSERT INTO albums
            (artist, album, source_id, source_url, seen_at, added_at_week)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                "Album Artist",
                "Album Name",
                "album-source",
                "https://album",
                "2026-05-01T13:00:00+00:00",
                week,
            ),
        )
        db.conn.execute(
            """
            INSERT INTO unmatched
            (source_id, artist, title, source_url, seen_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                "source-a",
                "Ghost Artist",
                "Ghost Song",
                "https://ghost",
                "2026-05-01T15:00:00+00:00",
            ),
        )
        db.conn.commit()

        db.upsert_feedback("spotify:track:shared", "like", "bom groove")

        report = build_weekly_report(db, week)

        assert f"# Peel {week}" in report
        assert "Artist A — Track A — 2 fontes — rating: like" in report
        assert "Spotify: `spotify:track:shared`" in report
        assert "source-a — https://a" in report
        assert "source-b — https://b" in report
        assert "Album Artist — Album Name" in report
        assert "source-a — Ghost Artist — Ghost Song — https://ghost" in report
        assert "| source-a | 1 | 1 | 1 | 1 | 1.00 |" in report
        assert "| source-b | 1 | 0 | 1 | 0 | 1.00 |" in report
        assert "| source-c | 1 | 1 | 0 | 0 | — |" in report
        db.close()

    def test_build_weekly_report_dedupes_album_context_by_normalized_identity(
        self, tmp_path: Path
    ) -> None:
        db = DB(str(tmp_path / "peel.db"))
        db.init_schema()
        week = "2026-W27"
        db.conn.executemany(
            """
            INSERT INTO albums
            (artist, album, source_id, source_url, seen_at, added_at_week)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    "Sml",
                    "Spontaneous Music Live",
                    "pitchfork_best_albums",
                    "https://pitchfork.example/sml",
                    "2026-07-04T10:00:00+00:00",
                    week,
                ),
                (
                    "SML",
                    "Spontaneous Music Live",
                    "aquarium_drunkard",
                    "https://aquarium.example/sml",
                    "2026-07-04T11:00:00+00:00",
                    week,
                ),
            ],
        )
        db.conn.commit()

        report = build_weekly_report(db, week)

        assert report.count("Spontaneous Music Live") == 1
        assert "Sml — Spontaneous Music Live" in report
        db.close()

    def test_build_weekly_report_renders_album_recommendations(self, tmp_path: Path) -> None:
        db = DB(str(tmp_path / "peel.db"))
        db.init_schema()
        week = "2026-W18"

        db.conn.executemany(
            """
            INSERT INTO album_mentions
            (artist, album, artist_key, album_key, source_id, source_url,
             spotify_album_uri, seen_at, added_at_week)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    "Wax Machine",
                    "The Sky Unfurls",
                    "wax machine",
                    "the sky unfurls",
                    "aquarium_drunkard",
                    "https://ad/wax",
                    "spotify:album:abc123",
                    "2026-05-01T10:00:00+00:00",
                    week,
                ),
                (
                    "Wax Machine",
                    "The Sky Unfurls",
                    "wax machine",
                    "the sky unfurls",
                    "pitchfork_best_albums",
                    "https://pitchfork/wax",
                    None,
                    "2026-05-01T11:00:00+00:00",
                    week,
                ),
                (
                    "No Spotify",
                    "Fallback Album",
                    "no spotify",
                    "fallback album",
                    "guardian_music_albums",
                    "https://guardian/fallback",
                    None,
                    "2026-05-01T12:00:00+00:00",
                    week,
                ),
            ],
        )
        db.conn.commit()

        report = build_weekly_report(db, week)

        assert "## 🎧 7 Álbuns a Ouvir" in report
        assert (
            "Wax Machine — The Sky Unfurls — 2 fontes — https://open.spotify.com/album/abc123"
        ) in report
        assert "Sources: aquarium_drunkard, pitchfork_best_albums" in report
        assert "No Spotify — Fallback Album — 1 fontes — https://guardian/fallback" in report
        db.close()

    def test_generate_weekly_report_writes_file(self, tmp_path: Path) -> None:
        db = DB(str(tmp_path / "peel.db"))
        db.init_schema()

        week = "2026-W18"
        db.conn.execute(
            """
            INSERT INTO tracks
            (spotify_uri, source_id, artist, title, source_url, added_at, added_at_week)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "spotify:track:1",
                "source-a",
                "Artist A",
                "Track A",
                None,
                "2026-05-01T10:00:00+00:00",
                week,
            ),
        )
        db.conn.commit()

        output_dir = tmp_path / "reports"
        path = generate_weekly_report(db, week=week, output_dir=output_dir)

        assert path == output_dir / "2026-W18.md"
        assert path.exists()
        content = path.read_text(encoding="utf-8")
        assert "# Peel 2026-W18" in content
        db.close()

    def test_generate_weekly_report_accepts_lowercase_week(self, tmp_path: Path) -> None:
        db = DB(str(tmp_path / "peel.db"))
        db.init_schema()

        week = "2026-W19"
        db.conn.execute(
            """
            INSERT INTO tracks
            (spotify_uri, source_id, artist, title, source_url, added_at, added_at_week)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "spotify:track:1",
                "source-a",
                "Artist A",
                "Track A",
                None,
                "2026-05-09T10:00:00+00:00",
                week,
            ),
        )
        db.conn.commit()

        output_dir = tmp_path / "reports"
        path = generate_weekly_report(db, week="2026-w19", output_dir=output_dir)

        assert path == output_dir / "2026-W19.md"
        assert path.exists()
        content = path.read_text(encoding="utf-8")
        assert "# Peel 2026-W19" in content
        assert "Artist A — Track A" in content
        db.close()
