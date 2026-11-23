from __future__ import annotations

import copy
import json
from datetime import UTC, date, datetime
from pathlib import Path
from unittest.mock import MagicMock

import feedparser
import pytest

from peel.albums import AlbumRecommendation
from peel.discovery_trace import DiscoveryTrace, observe
from peel.main import _album_queue_snapshot_items
from peel.novelty import NoveltyCheck
from peel.site_export import make_album_resolver
from peel.sources.rss import (
    GorillaVsBear,
    StereogumNewMusic,
    TrebleAlbumReviews,
    TrebleFeedChallenge,
)

FIXTURES = Path(__file__).parent / "fixtures"
NOW = datetime(2026, 11, 14, 12, tzinfo=UTC)


@pytest.fixture
def u2_candidates():
    # Actual two leading results captured by W46. Search metadata alone does
    # not prove the standard edition's release eligibility.
    return json.loads((FIXTURES / "w46_u2_album_search.json").read_text())


def test_exact_album_title_beats_first_fuzzy_deluxe_without_another_search(u2_candidates, tmp_path):
    before = copy.deepcopy(u2_candidates)
    client = MagicMock()
    client.search_album.return_value = u2_candidates
    trace = DiscoveryTrace(tmp_path, dry_run=True)
    trace.record("run.started", started_at=NOW, week="2026-W46")
    with observe(trace):
        result = make_album_resolver(client)("U2", "Carnaval De Luz")
    assert result == u2_candidates[1]["url"]
    client.search_album.assert_called_once_with("U2", "Carnaval De Luz")
    client.sp.album.assert_not_called()
    assert u2_candidates == before
    assert (
        next(r for r in trace.records if r.get("event") == "albums.spotify_match")["choice_rule"]
        == "exact_album_title"
    )


def test_explicit_edition_request_is_not_silently_replaced_with_standard(u2_candidates):
    client = MagicMock()
    client.search_album.return_value = list(reversed(u2_candidates))
    assert (
        make_album_resolver(client)("U2", "Carnaval De Luz (Deluxe Edition)")
        == u2_candidates[0]["url"]
    )
    client.search_album.assert_called_once()


def test_no_exact_title_keeps_prior_threshold_fallback(u2_candidates):
    client = MagicMock()
    client.search_album.return_value = u2_candidates[:1]
    assert make_album_resolver(client)("U2", "Carnaval De Luz") == u2_candidates[0]["url"]


def test_exact_title_never_bypasses_artist_matching_or_missing_url(u2_candidates):
    wrong = {**u2_candidates[1], "artists": ["Unrelated Band"]}
    no_url = {**u2_candidates[1], "url": None}
    client = MagicMock()
    client.search_album.return_value = [wrong, no_url, *u2_candidates]
    assert make_album_resolver(client)("U2", "Carnaval De Luz") == u2_candidates[1]["url"]


def recommendation():
    return AlbumRecommendation(
        "U2",
        "Carnaval De Luz",
        3,
        ("source",),
        (("source", "https://review.example/u2"),),
        None,
        NOW.isoformat(),
        0.5,
        0.0,
        "u2",
        "carnaval de luz",
    )


def test_preferred_edition_still_passes_normal_date_format_and_archive_admission(u2_candidates):
    client = MagicMock()
    client.search_album.return_value = u2_candidates
    # Synthetic admission metadata, not an assertion about the historical LP.
    lookup = MagicMock(
        return_value={
            "name": "Carnaval De Luz",
            "total_tracks": 12,
            "album_type": "album",
            "release_date": "2026-11-13",
        }
    )
    novelty = NoveltyCheck(
        spotify_album=lookup, artist_search=None, reference_date=date(2026, 11, 14)
    )
    selected = _album_queue_snapshot_items(
        "2026-W46", [(recommendation(), True)], make_album_resolver(client), novelty=novelty
    )
    assert len(selected) == 1 and selected[0].listen_url == u2_candidates[1]["url"]
    lookup.assert_called_once_with("07Q9bUJarYrFjhxofoenM0")


@pytest.mark.parametrize(
    "changes",
    [
        {"release_date": "2020-01-01"},
        {"album_type": "compilation"},
        {"total_tracks": 1, "album_type": "single"},
        {"name": "Carnaval De Luz (Remaster)"},
    ],
)
def test_edition_preference_does_not_weaken_admission(u2_candidates, changes):
    client = MagicMock()
    client.search_album.return_value = u2_candidates
    lookup = MagicMock(
        return_value={
            "name": "Carnaval De Luz",
            "total_tracks": 12,
            "album_type": "album",
            "release_date": "2026-11-13",
            **changes,
        }
    )
    novelty = NoveltyCheck(
        spotify_album=lookup, artist_search=None, reference_date=date(2026, 11, 14)
    )
    assert (
        _album_queue_snapshot_items(
            "2026-W46", [(recommendation(), True)], make_album_resolver(client), novelty=novelty
        )
        == []
    )
    lookup.assert_called_once_with("07Q9bUJarYrFjhxofoenM0")


@pytest.mark.parametrize(
    "headline,expected",
    [
        (
            "Lamb Rebrands As Lambb Following Complaint From Trip-Hop Duo – “OBSESSION”",
            ("Lambb", "OBSESSION"),
        ),
        (
            "Lamb Rebrands As Lambb Following Complaint From Trip-Hop Duo, "
            "Shares New Single “OBSESSION”",
            ("Lambb", "OBSESSION"),
        ),
        ("Old Act Rebrands As The New Act After Dispute – “Song”", ("The New Act", "Song")),
        (
            "Courtney Werner (Magic Tuber Stringband, Dowsers, Weirs) – “sierra one whiskey eight”",
            ("Courtney Werner", "sierra one whiskey eight"),
        ),
        ("Artist (of Another Band) – “Song”", ("Artist", "Song")),
        ("Artist (from Another Band) – “Song”", ("Artist", "Song")),
        ("Artist (ex-Another Band) – “Song”", ("Artist", "Song")),
        ("Artist (Often Repeated) – “Song”", ("Artist (Often Repeated)", "Song")),
        ("Artist (Special Edition) – “Song”", ("Artist (Special Edition)", "Song")),
        ("Artist (With A, B, C) – “Song”", ("Artist (With A, B, C)", "Song")),
        ("Artist (Feat. A, B, C) – “Song”", ("Artist (Feat. A, B, C)", "Song")),
        ("Artist (Ft. A, B, C) – “Song”", ("Artist (Ft. A, B, C)", "Song")),
        ("Artist (North, East) – “Song”", ("Artist (North, East)", "Song")),
        ("Mr. Bungle – “Song”", ("Mr. Bungle", "Song")),
        ("AC/DC – “Song”", ("AC/DC", "Song")),
        ("Artist & Another Artist – “Song”", ("Artist & Another Artist", "Song")),
    ],
)
def test_stereogum_cleans_explicit_prose_without_erasing_real_names_or_guests(headline, expected):
    source = StereogumNewMusic()
    entry = {
        "title": headline,
        "link": "https://stereogum.com/example/music/",
        "tags": [{"term": "New Music"}],
    }
    assert source._extract_artist_title(entry) == expected


def test_parenthetical_name_corroborated_by_publisher_is_preserved():
    source = StereogumNewMusic()
    name = "Artist (North, East, West)"
    entry = {
        "title": name + " – “Song”",
        "tags": [{"term": name}],
        "link": "https://stereogum.com/music/",
    }
    assert source._extract_artist_title(entry) == (name, "Song")


@pytest.mark.parametrize("evidence", ["tag", "summary", "content"])
def test_location_caption_requires_corroboration_without_another_request(evidence):
    entry = {
        "title": "NYC’s bloodsports – “Ten Thousand Feet”",
        "link": "https://stereogum.com/music/",
    }
    anchor = '<a href="https://stereogum.com/tag/bloodsports/">bloodsports</a>'
    if evidence == "tag":
        entry["tags"] = [{"term": "bloodsports"}]
    elif evidence == "summary":
        entry["summary"] = anchor
    else:
        entry["content"] = [{"value": anchor}]
    assert StereogumNewMusic()._extract_artist_title(entry) == ("bloodsports", "Ten Thousand Feet")


@pytest.mark.parametrize("name", ["NYC's Finest", "NYC’s bloodsports"])
def test_location_alone_never_renames_an_artist(name):
    entry = {"title": name + " – “Song”", "link": "https://stereogum.com/music/"}
    assert StereogumNewMusic()._extract_artist_title(entry) == (name, "Song")


@pytest.mark.parametrize("prefix", ["video:", "VIDEO:", "video premiere:", "premiere:"])
def test_gorilla_media_marker_is_not_part_of_artist(prefix):
    assert GorillaVsBear()._extract_artist_title({"title": f"{prefix} Warpstr – GIRL"}) == (
        "Warpstr",
        "GIRL",
    )


@pytest.mark.parametrize(
    "title,expected",
    [
        ("Video – Song", ("Video", "Song")),
        ("Premiere – Song", ("Premiere", "Song")),
        ("photos: Artist – live in London", None),
        ("video: Artist – live at Venue", None),
        ("video: Gorilla vs. Bear's Songs of 2026 – Playlist", None),
    ],
)
def test_gorilla_keeps_actual_names_and_existing_live_photo_exclusions(title, expected):
    assert GorillaVsBear()._extract_artist_title({"title": title}) == expected


def parsed_treble():
    return feedparser.parse((FIXTURES / "treble_reviews_reduced.xml").read_bytes())


def test_treble_current_feed_format_still_produces_album_reviews(monkeypatch):
    feed = parsed_treble()
    empty = feedparser.parse(b'<rss version="2.0"><channel><title>Treble</title></channel></rss>')
    parser = MagicMock(side_effect=[feed, empty])
    monkeypatch.setattr("peel.sources.rss.feedparser.parse", parser)
    source = TrebleAlbumReviews()
    monkeypatch.setattr(source, "_now", lambda: NOW)
    found = source.fetch()
    assert [x.artist for x in found] == [
        "Olivia Rodrigo",
        "By Storm",
        "Anna Butterss, John Raymond & Ben Lumsdaine",
    ]
    assert source.last_raw_entries == 4 and parser.call_count == 2


def test_treble_malformed_empty_response_is_failure_not_healthy_empty(monkeypatch):
    broken = feedparser.parse(b"<html><body>temporary invalid & response</body></html>")
    assert broken.bozo and not broken.entries
    parser = MagicMock(return_value=broken)
    monkeypatch.setattr("peel.sources.rss.feedparser.parse", parser)
    with pytest.raises(ValueError, match="could not be parsed"):
        TrebleAlbumReviews().fetch()
    parser.assert_called_once()


@pytest.mark.parametrize("status", [403, 429, 500, 503])
def test_treble_http_error_cannot_be_reported_as_empty_success(monkeypatch, status):
    feed = parsed_treble()
    feed["status"] = status
    monkeypatch.setattr("peel.sources.rss.feedparser.parse", lambda *a, **kw: feed)
    with pytest.raises(ValueError, match=f"HTTP {status}"):
        TrebleAlbumReviews().fetch()


@pytest.mark.parametrize("status", [403, 200])
def test_treble_challenge_is_reported_without_retries(monkeypatch, status):
    response = feedparser.parse(b"<html><title>Just a moment...</title></html>")
    response.update(status=status, headers={"cf-mitigated": "challenge"})
    parser = MagicMock(return_value=response)
    monkeypatch.setattr("peel.sources.rss.feedparser.parse", parser)
    with pytest.raises(TrebleFeedChallenge, match=f"Cloudflare challenge.*HTTP {status}"):
        TrebleAlbumReviews().fetch()
    parser.assert_called_once()


def test_treble_well_formed_html_is_not_a_valid_empty_feed(monkeypatch):
    response = feedparser.parse(b"<html><title>Not a feed</title></html>")
    monkeypatch.setattr("peel.sources.rss.feedparser.parse", lambda *a, **kw: response)
    with pytest.raises(ValueError, match="could not be parsed"):
        TrebleAlbumReviews().fetch()


def test_treble_valid_empty_and_usable_bozo_feeds_remain_tolerated(monkeypatch):
    empty = feedparser.parse(b'<rss version="2.0"><channel><title>Treble</title></channel></rss>')
    feed = parsed_treble()
    monkeypatch.setattr("peel.sources.rss.feedparser.parse", lambda *a, **kw: empty)
    assert TrebleAlbumReviews().fetch() == []
    feed["bozo"] = True
    feed["bozo_exception"] = ValueError("namespace warning")
    monkeypatch.setattr("peel.sources.rss.feedparser.parse", lambda *a, **kw: feed)
    assert TrebleAlbumReviews()._parse_feed("https://www.treblezine.com/feed/") is feed


def test_secondary_bad_page_preserves_first_page_and_marks_trace_partial(monkeypatch, tmp_path):
    first = parsed_treble()
    bad = feedparser.parse(b"<html>not an RSS & bad</html>")
    parser = MagicMock(side_effect=[first, bad])
    monkeypatch.setattr("peel.sources.rss.feedparser.parse", parser)
    source = TrebleAlbumReviews()
    monkeypatch.setattr(source, "_now", lambda: NOW)
    trace = DiscoveryTrace(tmp_path, dry_run=True)
    trace.record("run.started", started_at=NOW, week="2026-W46")
    with observe(trace):
        result = source.fetch()
    trace.finish("returned")
    assert len(result) == 3 and parser.call_count == 2
    assert any(r.get("event") == "source.pagination_failed" for r in trace.records)
    assert trace.records[-1]["trace_complete"] is False
