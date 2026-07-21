from __future__ import annotations

from pathlib import Path

from peel.sources.bandcamp import BandcampLabel

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

    assert [(track.artist, track.title) for track in tracks] == [
        ("LCD Soundsystem", "American Dream"),
        ("Automatic", "Is It Now?"),
        ("Factory Floor", "Two Different Ways"),
    ]
    assert all(track.source_id == "bandcamp_dfa" for track in tracks)


def test_parse_fixture_resolves_relative_and_absolute_urls() -> None:
    source = _source(max_items=3)

    tracks = source._parse_music_html(FIXTURE.read_text(encoding="utf-8"))

    assert tracks[0].source_url == "https://dfarecords.bandcamp.com/album/american-dream"
    assert tracks[1].source_url == "https://automaticband.bandcamp.com/album/is-it-now"
    assert tracks[2].source_url == "https://dfarecords.bandcamp.com/album/two-different-ways"


def test_parse_fixture_skips_malformed_and_unsupported_items() -> None:
    source = _source(max_items=10)

    tracks = source._parse_music_html(FIXTURE.read_text(encoding="utf-8"))

    assert [(track.artist, track.title) for track in tracks] == [
        ("LCD Soundsystem", "American Dream"),
        ("Automatic", "Is It Now?"),
        ("Factory Floor", "Two Different Ways"),
    ]


def test_parse_html_without_client_items_returns_empty_list() -> None:
    source = _source()

    assert source._parse_music_html("<html><body>No releases</body></html>") == []


def test_fetch_uses_bandcamp_music_page(monkeypatch) -> None:
    source = _source(max_items=1)

    class Response:
        text = FIXTURE.read_text(encoding="utf-8")

        def raise_for_status(self) -> None:
            return None

    calls = []

    def fake_get(*args, **kwargs):
        calls.append((args, kwargs))
        return Response()

    monkeypatch.setattr("peel.sources.bandcamp.httpx.get", fake_get)

    tracks = source.fetch()

    assert len(tracks) == 1
    assert calls[0][0][0] == "https://dfarecords.bandcamp.com/music"
    assert calls[0][1]["follow_redirects"] is True
