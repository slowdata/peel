from __future__ import annotations

import json
from datetime import UTC, datetime
from html import escape
from pathlib import Path

import httpx
import pytest

from peel.sources.bandcamp import BandcampLabel, parse_release_details

FIXTURE = Path(__file__).parent / "fixtures" / "bandcamp_label.html"


def _source(max_items: int = 5) -> BandcampLabel:
    return BandcampLabel(
        "bandcamp_dfa",
        "DFA Records (Bandcamp)",
        "dfarecords",
        max_items=max_items,
    )


def test_bandcamp_label_kind_is_album() -> None:
    assert _source().kind == "album"


def test_parse_fixture_extracts_releases_and_respects_max_items() -> None:
    source = _source(max_items=3)

    tracks = source._parse_music_html(FIXTURE.read_text(encoding="utf-8"))

    assert [(track.artist, track.title, track.kind) for track in tracks] == [
        ("LCD Soundsystem", "American Dream", "album"),
        ("Automatic", "Is It Now?", "album"),
        ("Yaeji", "Raingurl", "track"),
    ]
    assert all(track.source_id == "bandcamp_dfa" for track in tracks)


def test_parse_fixture_resolves_relative_and_absolute_urls() -> None:
    source = _source(max_items=3)

    tracks = source._parse_music_html(FIXTURE.read_text(encoding="utf-8"))

    assert tracks[0].source_url == "https://dfarecords.bandcamp.com/album/american-dream"
    assert tracks[1].source_url == "https://automaticband.bandcamp.com/album/is-it-now"
    assert tracks[2].source_url == "https://dfarecords.bandcamp.com/track/raingurl"


def test_parse_fixture_skips_malformed_and_unsupported_items() -> None:
    source = _source(max_items=10)

    tracks = source._parse_music_html(FIXTURE.read_text(encoding="utf-8"))

    assert [(track.artist, track.title) for track in tracks] == [
        ("LCD Soundsystem", "American Dream"),
        ("Automatic", "Is It Now?"),
        ("Yaeji", "Raingurl"),
        ("Factory Floor", "Two Different Ways"),
    ]


def test_parse_html_without_client_items_returns_empty_list() -> None:
    source = _source()

    assert source._parse_music_html("<html><body>No releases</body></html>") == []


def _release_html(titles: list[str], released: str | None, preorder: bool = False) -> str:
    data = {
        "trackinfo": [{"title": title} for title in titles],
        "album_release_date": released,
        "album_is_preorder": preorder,
        "current": {},
    }
    return f'<div data-tralbum="{escape(json.dumps(data))}"></div>'


RELEASES = {
    "https://dfarecords.bandcamp.com/album/american-dream": _release_html(
        ["Oh Baby", "Other Voices", "I Used To", "Change Yr Mind", "How Do You Sleep?"],
        "01 Oct 2026 00:00:00 GMT",
    ),
    "https://automaticband.bandcamp.com/album/is-it-now": _release_html(
        ["Is It Now?", "B-side"], "28 Sep 2026 00:00:00 GMT"
    ),
    "https://dfarecords.bandcamp.com/track/raingurl": _release_html(
        ["Raingurl"], "07 Jul 2026 00:00:00 GMT"
    ),
    "https://dfarecords.bandcamp.com/album/two-different-ways": _release_html(
        ["A", "B", "C", "D"], None, preorder=True
    ),
}


def _fake_bandcamp(monkeypatch, releases=RELEASES, failing=()):
    calls = []

    class Response:
        def __init__(self, text: str) -> None:
            self.text = text

        def raise_for_status(self) -> None:
            return None

    def fake_get(url, *args, **kwargs):
        calls.append((url, kwargs))
        if url in failing:
            raise httpx.ConnectError("down")
        if url.endswith("/music"):
            return Response(FIXTURE.read_text(encoding="utf-8"))
        return Response(releases[url])

    monkeypatch.setattr("peel.sources.bandcamp.httpx.get", fake_get)
    return calls


def test_fetch_reads_each_release_and_routes_singles_to_tracks(monkeypatch) -> None:
    calls = _fake_bandcamp(monkeypatch)
    source = _source(max_items=10)

    releases = source.fetch()

    assert calls[0][0] == "https://dfarecords.bandcamp.com/music"
    assert calls[0][1]["follow_redirects"] is True
    assert source.last_raw_entries == 6
    assert [(r.artist, r.title, r.kind) for r in releases] == [
        ("LCD Soundsystem", "American Dream", "album"),
        # Two tracks: a single, sent to triage by its lead track.
        ("Automatic", "Is It Now?", "track"),
        ("Yaeji", "Raingurl", "track"),
        # Factory Floor is a pre-order: skipped until it is out.
    ]
    assert releases[0].published_at == datetime(2026, 10, 1, tzinfo=UTC)
    assert releases[2].published_at == datetime(2026, 7, 7, tzinfo=UTC)


def test_release_page_failure_skips_only_that_release(monkeypatch) -> None:
    _fake_bandcamp(monkeypatch, failing={"https://dfarecords.bandcamp.com/album/american-dream"})
    releases = _source(max_items=10).fetch()
    assert [r.artist for r in releases] == ["Automatic", "Yaeji"]


def test_parse_release_details_reads_real_shape() -> None:
    details = parse_release_details(
        _release_html(["Tougher Than the Rest", "The Conversation"], "07 Jul 2026 00:00:00 GMT")
    )
    assert details.track_count == 2
    assert details.lead_track == "Tougher Than the Rest"
    assert details.released_at == datetime(2026, 7, 7, tzinfo=UTC)
    assert not details.preorder
    with pytest.raises(ValueError):
        parse_release_details("<html></html>")
