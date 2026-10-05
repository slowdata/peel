"""Broken sources must be visible, never silently recorded as a healthy run."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from urllib.parse import urlparse

import pytest

from peel.db import iso_week
from peel.main import run, source_health_notice
from peel.models import Track
from peel.sources.base import Source
from peel.sources.rss import PitchforkNews, _extract_release_news_artist_title


class FakeSource(Source):
    kind = "track"

    def __init__(self, source_id: str, name: str, *, raw: int | None, error: bool = False):
        self.id, self.name = source_id, name
        self._raw, self._error = raw, error

    def fetch(self) -> list[Track]:
        if self._error:
            raise RuntimeError("feed down")
        self.last_raw_entries = self._raw
        return []


def test_notice_names_empty_and_failed_sources():
    assert source_health_notice([], []) is None
    notice = source_health_notice(["Pitchfork News"], ["Clash"])
    assert notice is not None
    assert "Pitchfork News (feed vazio)" in notice
    assert "Clash (erro)" in notice


def test_rss_records_raw_entry_count_even_when_empty(monkeypatch):
    source = PitchforkNews()
    monkeypatch.setattr(
        source, "_parse_feed", MagicMock(return_value=SimpleNamespace(entries=[], bozo=False))
    )
    assert source.fetch() == []
    assert source.last_raw_entries == 0


def test_pitchfork_news_uses_the_responding_host():
    assert urlparse(PitchforkNews.url).hostname == "pitchfork.com"


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Mogwai Recruit Iggy Pop for New Song \u201cUnderground\u201d", ("Mogwai", "Underground")),
        (
            "Ang\u00e8le Recruits Caroline Polachek and SebastiAn "
            "for New Song \u201cLove Triangle\u201d",
            ("Ang\u00e8le", "Love Triangle"),
        ),
        ("Kali Malone Unveils U.S. Tour Dates and New Song", None),
        ("Rick Ross Booked on Domestic Violence Charges", None),
    ],
)
def test_guest_headlines_credit_the_lead_artist(title, expected):
    assert _extract_release_news_artist_title(title) == expected


def test_run_announces_broken_sources_but_keeps_quiet_filters_silent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from peel import config as config_module

    monkeypatch.setattr(config_module.settings, "db_path", str(tmp_path / "health.db"))
    monkeypatch.setattr(config_module.settings, "peel_playlist_id", "spotify:playlist:test")
    sources = [
        FakeSource("dead", "Dead Feed", raw=0),
        FakeSource("quiet", "Quiet Filter", raw=12),
        FakeSource("unknown", "Scraper", raw=None),
        FakeSource("broken", "Broken Feed", raw=None, error=True),
    ]
    with (
        patch("peel.main.active_sources", return_value=sources),
        patch("peel.main.SpotifyClient", return_value=MagicMock()),
        patch("peel.main.select_album_queue", return_value=[]),
        patch("peel.main._album_queue_snapshot_items", return_value=[]),
        patch("peel.main.send_digest") as digest,
    ):
        run()
    notice = digest.call_args.kwargs["notice"]
    assert "Dead Feed (feed vazio)" in notice
    assert "Broken Feed (erro)" in notice
    assert "Quiet Filter" not in notice and "Scraper" not in notice
    assert iso_week(datetime.now(UTC))  # run completed for the current week


def test_album_source_routes_single_items_to_track_triage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """A label single goes through Spotify matching; its albums stay albums."""
    from peel import config as config_module
    from peel.db import DB

    monkeypatch.setattr(config_module.settings, "db_path", str(tmp_path / "route.db"))
    monkeypatch.setattr(config_module.settings, "peel_playlist_id", "spotify:playlist:test")
    now = datetime.now(UTC)

    class Label(Source):
        id, name, kind = "bandcamp_sub_pop", "Sub Pop (Bandcamp)", "album"

        def fetch(self) -> list[Track]:
            self.last_raw_entries = 3
            base = {"source_id": self.id, "source_url": "https://x.bandcamp.com/album/y"}
            return [
                Track(artist="Band", title="Real Album", kind="album", published_at=now, **base),
                Track(
                    artist="Nation of Language",
                    title="Tougher Than the Rest",
                    kind="track",
                    published_at=now,
                    **base,
                ),
                Track(
                    artist="Old",
                    title="July Single",
                    kind="track",
                    published_at=datetime(2026, 7, 7, tzinfo=UTC),
                    **base,
                ),
            ]

    sp = MagicMock()
    sp.search_track.return_value = [
        {
            "uri": "spotify:track:nol",
            "name": "Tougher Than the Rest",
            "artists": ["Nation of Language"],
        }
    ]
    with (
        patch("peel.main.active_sources", return_value=[Label()]),
        patch("peel.main.SpotifyClient", return_value=sp),
        patch("peel.main.select_album_queue", return_value=[]),
        patch("peel.main._album_queue_snapshot_items", return_value=[]),
        patch("peel.main.send_digest"),
    ):
        run()
    searched = [call.args[:2] for call in sp.search_track.call_args_list]
    assert searched == [("Nation of Language", "Tougher Than the Rest")]
    db = DB(str(tmp_path / "route.db"))
    assert db.conn.execute("SELECT source_id FROM tracks").fetchall() == [("bandcamp_sub_pop",)]
    albums = db.conn.execute("SELECT album FROM album_mentions").fetchall()
    assert albums == [("Real Album",)]
    db.close()
