from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from peel.album_discovery import AlbumDiscoveryError, discover_album_mentions
from peel.db import DB
from peel.models import Track
from peel.sources.base import Source


class FixtureSource(Source):
    id = "fixture_album"
    name = "Fixture Album"
    kind = "album"

    def __init__(self, items: list[Track] | None = None, error: Exception | None = None) -> None:
        self.items = items or []
        self.error = error

    def fetch(self) -> list[Track]:
        if self.error:
            raise self.error
        return self.items


class TrackSource(FixtureSource):
    id = "fixture_track"
    kind = "track"


def test_album_discovery_filters_stale_and_never_fetches_track_sources(tmp_path: Path) -> None:
    db = DB(str(tmp_path / "peel.db"))
    db.init_schema()
    now = datetime(2026, 8, 8, 12, tzinfo=UTC)
    album_source = FixtureSource(
        [
            Track(
                source_id="fixture_album",
                artist="Fresh",
                title="Fresh Album",
                source_url="https://fresh.bandcamp.com/album/fresh",
                published_at=now - timedelta(days=1),
            ),
            Track(
                source_id="fixture_album",
                artist="Stale",
                title="Stale Album",
                source_url="https://stale.bandcamp.com/album/stale",
                published_at=now - timedelta(days=60),
            ),
        ]
    )
    track_source = TrackSource(error=AssertionError("track source fetched"))

    result = discover_album_mentions(
        db,
        now=now,
        sources=[album_source, track_source],
    )

    assert result.sources == 1
    assert result.fetched == 2
    assert result.fresh == 1
    assert result.new_albums == 1
    rows = db.conn.execute("SELECT artist, album FROM albums").fetchall()
    assert rows == [("Fresh", "Fresh Album")]
    db.close()


def test_source_failure_happens_before_any_album_write(tmp_path: Path) -> None:
    db = DB(str(tmp_path / "peel.db"))
    db.init_schema()
    good = FixtureSource(
        [
            Track(
                source_id="fixture_album",
                artist="Would Be Partial",
                title="Album",
                source_url="https://x.bandcamp.com/album/a",
            )
        ]
    )
    broken = FixtureSource(error=RuntimeError("offline"))
    broken.id = "broken_album"

    with pytest.raises(AlbumDiscoveryError, match="broken_album: offline"):
        discover_album_mentions(db, sources=[good, broken])

    assert db.conn.execute("SELECT COUNT(*) FROM albums").fetchone()[0] == 0
    db.close()
