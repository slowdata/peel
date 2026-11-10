"""Current label cards must not disappear behind the serialized catalogue."""

import json
from datetime import UTC, datetime
from html import escape
from pathlib import Path

import pytest

from peel.sources.bandcamp import BandcampLabel


def _release_html(titles, released, preorder=False, about=""):
    data = {
        "trackinfo": [{"title": t} for t in titles],
        "album_release_date": released,
        "album_is_preorder": preorder,
        "current": {"about": about},
    }
    return f'<div data-tralbum="{escape(json.dumps(data))}"></div>'


NOW = datetime(2026, 11, 10, 12, tzinfo=UTC)
FIXTURE = Path(__file__).parent / "fixtures/bandcamp_visible_grid.html"


def _source(**kwargs):
    return BandcampLabel("bandcamp_sub_pop", "Sub Pop", "subpop", **kwargs)


def _html(items):
    return f'<ol id="music-grid" data-client-items="{escape(json.dumps(items))}"></ol>'


def _item(title, artist="Artist", url=None):
    return {"artist": artist, "title": title, "type": "album", "page_url": url or f"/album/{title}"}


def _mock_pages(monkeypatch, html, pages):
    calls = []

    class Response:
        def __init__(self, text):
            self.text = text

        def raise_for_status(self):
            pass

    def get(url, **kwargs):
        calls.append(url)
        return Response(html if url.endswith("/music") else pages[url.split("?", 1)[0]])

    monkeypatch.setattr("peel.sources.bandcamp.httpx.get", get)
    return calls


def test_visible_grid_precedes_serialized_items_without_duplicate_release():
    tracks = _source(max_items=10)._parse_music_html(FIXTURE.read_text())
    assert [(x.artist, x.title) for x in tracks] == [
        ("Sebadoh", "Harmacy (30th Anniversary Expanded Edition)"),
        ("clipping.", "Splendor & Misery (Anniversary X)"),
        ("Twisted Teens", "The Holy Cross Tigers"),
        ("Hannah Jadagu", "Curse"),
        ("PISS", "you made me / skin on linen"),
        ("Old Catalog", "Old Record"),
    ]


def test_visible_grid_works_without_or_with_broken_serialized_data():
    html = FIXTURE.read_text()
    # Keep the real visible cards when the continuation is unavailable/broken.
    import re

    for replacement in ["", 'data-client-items="not json"']:
        changed = re.sub(r"data-client-items='[^']*'", replacement, html)
        assert len(_source(max_items=10)._parse_music_html(changed)) == 5


def test_fetch_fills_cap_with_current_releases_not_archive_cards(monkeypatch):
    pages = {
        "https://sebadoh.bandcamp.com/album/harmacy-expanded": _release_html(
            ["Old"] * 8, "06 Nov 2026 00:00:00 GMT"
        ),
        "https://clppng.bandcamp.com/album/splendor-misery-anniversary": _release_html(
            ["Old"] * 8, "06 Nov 2026 00:00:00 GMT"
        ),
        "https://twistedteens.bandcamp.com/album/the-holy-cross-tigers": _release_html(
            [f"Song {n}" for n in range(13)], "06 Nov 2026 00:00:00 GMT"
        ),
        "https://hannahjadagu.bandcamp.com/album/curse": _release_html(
            ["Curse"], "04 Nov 2026 00:00:00 GMT"
        ),
        "https://pisstheband.bandcamp.com/album/you-made-me-skin-on-linen": _release_html(
            [f"Song {n}" for n in range(15)], "30 Oct 2026 00:00:00 GMT"
        ),
    }
    calls = _mock_pages(monkeypatch, FIXTURE.read_text(), pages)
    items = _source(max_items=3).fetch(now=NOW)
    assert [(i.artist, i.kind) for i in items] == [
        ("Twisted Teens", "album"),
        ("Hannah Jadagu", "track"),
        ("PISS", "album"),
    ]
    assert len(calls) == 6  # metadata establishes format; archive albums do not fill the cap


def test_skipped_dates_formats_preorders_and_failures_do_not_fill_cap(monkeypatch):
    titles = ["old", "future", "preorder", "undated", "empty", "fresh"]
    pages = {
        "https://subpop.bandcamp.com/album/old": _release_html(
            ["S"] * 5, "01 Jan 2026 00:00:00 GMT"
        ),
        "https://subpop.bandcamp.com/album/future": _release_html(
            ["S"] * 5, "20 Nov 2026 00:00:00 GMT"
        ),
        "https://subpop.bandcamp.com/album/preorder": _release_html(
            ["S"] * 5, "09 Nov 2026 00:00:00 GMT", True
        ),
        "https://subpop.bandcamp.com/album/undated": _release_html(["S"] * 5, None),
        "https://subpop.bandcamp.com/album/empty": _release_html([], "09 Nov 2026 00:00:00 GMT"),
        "https://subpop.bandcamp.com/album/fresh": _release_html(
            ["New"], "09 Nov 2026 00:00:00 GMT"
        ),
    }
    calls = _mock_pages(monkeypatch, _html([_item(t) for t in titles]), pages)
    items = _source(max_items=1).fetch(now=NOW)
    assert [(x.title, x.kind) for x in items] == [("New", "track")]
    assert len(calls) == 7


def test_recent_dated_vault_release_does_not_fill_the_admission_cap(monkeypatch):
    pages = {
        "https://subpop.bandcamp.com/album/vault": _release_html(
            ["Old"] * 8,
            "09 Nov 2026 00:00:00 GMT",
            about="A collection of unreleased material selected from their personal vault.",
        ),
        "https://subpop.bandcamp.com/album/fresh": _release_html(
            ["New"] * 8,
            "09 Nov 2026 00:00:00 GMT",
        ),
    }
    _mock_pages(monkeypatch, _html([_item("vault"), _item("fresh")]), pages)
    assert [x.title for x in _source(max_items=1).fetch(now=NOW)] == ["fresh"]


def test_detail_request_budget_is_bounded_even_when_nothing_is_eligible(monkeypatch):
    items = [_item(f"old-{n}") for n in range(30)]
    pages = {
        f"https://subpop.bandcamp.com/album/old-{n}": _release_html(
            ["S"] * 5, "01 Jan 2026 00:00:00 GMT"
        )
        for n in range(30)
    }
    calls = _mock_pages(monkeypatch, _html(items), pages)
    assert _source(max_items=2, max_candidates=4).fetch(now=NOW) == []
    assert len(calls) == 5


@pytest.mark.parametrize("field,value", [("artist", None), ("title", 123), ("page_url", None)])
def test_invalid_serialized_fields_are_not_invented(field, value):
    item = {**_item("Title"), field: value}
    assert _source()._parse_music_html(_html([item])) == []


@pytest.mark.parametrize("kind", ["track", "album"])
def test_new_track_title_does_not_use_album_archive_title_heuristics(monkeypatch, kind):
    raw = {"artist": "Artist", "title": "Archive", "type": kind, "page_url": f"/{kind}/archive"}
    pages = {
        f"https://subpop.bandcamp.com/{kind}/archive": _release_html(
            ["Archive"], "09 Nov 2026 00:00:00 GMT"
        )
    }
    _mock_pages(monkeypatch, _html([raw]), pages)
    assert [(x.title, x.kind) for x in _source().fetch(now=NOW)] == [("Archive", "track")]


def test_invalid_limits_are_rejected():
    with pytest.raises(ValueError):
        _source(max_items=0)
    with pytest.raises(ValueError):
        _source(max_items=5, max_candidates=4)


def test_external_links_merch_and_missing_artists_are_not_catalogue_releases():
    html = (
        '<ol id="music-grid">'
        + "".join(
            f'<li class="music-grid-item"><a href="{url}"><p class="title">{title}</p></a></li>'
            for url, title in [
                (
                    "https://example.com/album/untrusted",
                    'Title<span class="artist-override">Artist</span>',
                ),
                ("/merch/poster", 'Poster<span class="artist-override">Artist</span>'),
                ("/album/missing-artist", "Missing"),
            ]
        )
        + "</ol>"
    )
    assert _source()._parse_music_html(html) == []
