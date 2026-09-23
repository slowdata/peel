from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from peel.config import settings
from peel.db import DB
from peel.main import SourceRunStats
from peel.sources.fetch import fetch_source
from peel.sources.rss import (
    ClashAlbumReviews,
    ClashFirstTake,
    ConsequenceMusic,
    DIYAlbumReviews,
    PitchforkAlbumReviews,
    RSSSource,
)


class ProbeRSS(RSSSource):
    id = "probe"
    name = "probe"
    url = "https://example.invalid/feed"
    lookback_days = 14

    def _extract_artist_title(self, entry):
        return entry["title"].split(" — ", 1)


@pytest.fixture
def db(tmp_path):
    db = DB(str(tmp_path / "test.db"))
    db.init_schema()
    yield db
    db.close()


def record_run(db, status):
    SourceRunStats("probe", "2026-08-21T18:00:00+00:00").record(db, status)


@pytest.mark.parametrize(
    "cls",
    [
        DIYAlbumReviews,
        ClashAlbumReviews,
        ClashFirstTake,
        PitchforkAlbumReviews,
        ConsequenceMusic,
    ],
)
def test_ordinary_window_is_fourteen_days(cls):
    assert cls.lookback_days == 14


@pytest.mark.parametrize("prior_status, expected", [(None, 30), ("error", 30), ("ok", 14)])
def test_bootstrap_window_is_bounded_and_restored(db, prior_status, expected):
    if prior_status:
        record_run(db, prior_status)
    source = ProbeRSS()
    observed = []
    source.fetch = lambda: observed.append(source.lookback_days) or []
    before = db.conn.execute("SELECT COUNT(*) FROM source_runs").fetchone()
    assert fetch_source(db, source) == []
    assert observed == [expected]
    assert source.lookback_days == 14
    assert db.conn.execute("SELECT COUNT(*) FROM source_runs").fetchone() == before


def test_failed_bootstrap_restores_config_and_can_be_attempted_later(db):
    source = ProbeRSS()
    source.fetch = MagicMock(side_effect=RuntimeError("feed unavailable"))
    with pytest.raises(RuntimeError, match="unavailable"):
        fetch_source(db, source)
    assert source.lookback_days == 14
    assert db.conn.execute("SELECT COUNT(*) FROM source_runs").fetchone()[0] == 0


def test_error_after_previous_success_does_not_rebootstrap(db):
    record_run(db, "ok")
    record_run(db, "error")
    source = ProbeRSS()
    observed = []
    source.fetch = lambda: observed.append(source.lookback_days) or []
    fetch_source(db, source)
    assert observed == [14]


def test_bootstrap_recovers_twenty_day_review_from_existing_page(db, monkeypatch):
    source = ProbeRSS()
    monkeypatch.setattr(source, "_now", lambda: datetime(2026, 8, 21, 18, tzinfo=UTC))
    entry = {
        "title": "Artist — Album",
        "link": "https://example.invalid/review/album",
        "published_parsed": (2026, 8, 1, 8, 0, 0, 0, 0, 0),
    }
    from types import SimpleNamespace

    parse = MagicMock(return_value=SimpleNamespace(entries=[entry, entry], bozo=False))
    monkeypatch.setattr(source, "_parse_feed", parse)
    assert len(fetch_source(db, source)) == 1
    assert parse.call_count == 1  # no added pagination/scraping
    record_run(db, "ok")
    assert fetch_source(db, source) == []
    assert source.lookback_days == 14


def test_active_week_can_be_recovered_before_latest_discovery(db, monkeypatch):
    from peel.models import ReviewQueueItem
    from peel.report import build_weekly_html_report, build_weekly_report

    monkeypatch.setattr(settings, "peel_review_playlist_id", "review")

    def queue(week):
        return [
            ReviewQueueItem(
                spotify_uri=f"spotify:track:{week[-2:]}",
                artist="Artist",
                title=f"Title {week}",
                source_id="diy_album_reviews",
                source_url=None,
                source_count=1,
                affinity=0.5,
                is_new=True,
                added_at_week=week,
                current_week=week,
            )
        ]

    db.replace_album_queue("2026-W36", [])
    db.replace_album_queue("2026-W37", [])
    db.replace_review_queue("review", queue("2026-W37"))
    db.replace_review_queue("review", queue("2026-W36"))
    db.conn.execute(
        "INSERT INTO queue_recoveries VALUES (?, ?, ?, ?)",
        ("2026-W36", "2026-09-07T18:00:00Z", "Recuperada hoje <não é o original>", "{}"),
    )
    db.conn.commit()
    assert db.active_review_week("review") == "2026-W36"
    assert db.latest_album_queue("review") == []
    assert "Recuperada hoje" in build_weekly_report(db, "2026-W36")
    assert "&lt;não é o original&gt;" in build_weekly_html_report(db, "2026-W36")
    assert len(db.review_queue_snapshot("review", "2026-W37")) == 1
    db.conn.execute("DELETE FROM album_queue_weeks WHERE week='2026-W36'")
    db.conn.commit()
    assert db.latest_album_queue("review") is None
    db.replace_review_queue("review", queue("2026-W37"))
    assert db.active_review_week("review") == "2026-W37"


def test_default_human_commands_follow_recovered_week(db, monkeypatch, tmp_path):
    from typer.testing import CliRunner

    from peel import cli
    from peel.models import AlbumQueueItem, ReviewQueueItem

    monkeypatch.setattr(settings, "db_path", db.path)
    monkeypatch.setattr(settings, "peel_review_playlist_id", "review")
    monkeypatch.setattr(settings, "peel_playlist_id", "weekly")
    monkeypatch.setattr(cli, "_auto_sync_state", lambda _: None)
    for week in ("2026-W37", "2026-W36"):
        db.replace_album_queue(
            week,
            [
                AlbumQueueItem(
                    week=week,
                    position=1,
                    artist=f"Artist {week}",
                    album=f"Album {week}",
                    artist_key=week,
                    album_key=week,
                    source_ids=("diy_album_reviews",),
                    source_count=1,
                    listen_url="https://open.spotify.com/album/abc",
                    listen_kind="spotify",
                    editorial_url=None,
                    is_new=True,
                )
            ],
        )
        db.replace_review_queue(
            "review",
            [
                ReviewQueueItem(
                    spotify_uri=f"spotify:track:{week[-2:]}",
                    artist="Artist",
                    title=week,
                    source_id="source-a",
                    source_url=None,
                    source_count=1,
                    affinity=0.5,
                    is_new=True,
                    added_at_week=week,
                    current_week=week,
                )
            ],
        )
    runner = CliRunner()
    result = runner.invoke(cli.app, ["albums"])
    assert result.exit_code == 0, result.output
    assert "Album 2026-W36" in result.output
    assert "Album 2026-W37" not in result.output
    result = runner.invoke(cli.app, ["albums", "feedback"], input="q\n")
    assert result.exit_code == 0, result.output
    assert "Artist 2026-W36" in result.output
    result = runner.invoke(cli.app, ["report", "--html", "--output-dir", str(tmp_path / "reports")])
    assert result.exit_code == 0, result.output
    assert (tmp_path / "reports/2026-W36.md").exists()
    assert not (tmp_path / "reports/2026-W37.md").exists()
    (tmp_path / "reports/2026-W37.md").write_text("Frozen W37\n")
    result = runner.invoke(
        cli.app, ["report", "--week", "2026-W37", "--output-dir", str(tmp_path / "reports")]
    )
    assert result.exit_code == 0, result.output
    assert (tmp_path / "reports/2026-W37.md").read_text() == "Frozen W37\n"
    (tmp_path / "reports/2026-W36.md").write_text("Stale active report\n")
    result = runner.invoke(cli.app, ["report", "--output-dir", str(tmp_path / "reports")])
    assert result.exit_code == 0, result.output
    assert "# Peel 2026-W36" in (tmp_path / "reports/2026-W36.md").read_text()
    monkeypatch.setattr(cli, "_project_path", lambda path: tmp_path / path)
    assert [p.name for p in cli._regenerate_state_reports(Path(db.path))] == ["2026-W36.md"]
    spotify = MagicMock()
    monkeypatch.setattr(cli, "SpotifyClient", lambda: spotify)
    result = runner.invoke(cli.app, ["finalize", "--no-export"])
    assert result.exit_code == 0, result.output
    assert db.finalized_week_uris("2026-W36", "weekly") == []
    assert db.finalized_week_uris("2026-W37", "weekly") is None
