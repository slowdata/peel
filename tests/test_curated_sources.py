"""Weekly curated lists and publication-level track consensus."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

from peel.db import DB, rank_window_uris
from peel.sources.families import source_family
from peel.sources.registry import source_label
from peel.sources.rss import PitchforkSelects, StereogumBestSongs

STEREOGUM_HTML = """
<article>
  <h2>The 5 Best Songs Of The Week</h2>
  <h2>5. mercury - "Roar Of The Heliac Goat"</h2>
  <h3>4. Father John Misty \u2013 \u201cThe X-panded Universe\u201d</h3>
  <h2>Phoebe Bridgers - \u201cOn Video\u201d</h2>
  <h2>Subscribe to our newsletter</h2>
</article>
"""

SELECTS_HTML = """
<p>Every week Pitchfork editors pick the tracks they love.</p>
<p>Lily Konigsberg: \u201cFree Byrd\u201d \u2014<em>Marissa Lorusso</em><br>
Krill: \u201cDominate\u201d \u2014<em>ML</em><br>
300SkullsAndCounting, Jenny Sparks: \u201c100% Legal Intro\u201d \u2014<em>Mano</em><br>
Kllhhr: "<a href="https://soundcloud.com/x">topman</a>" [ft. Izaya Tiji] \u2014<em>OL</em></p>
"""


def test_stereogum_best_songs_reads_ranked_headings():
    pairs = StereogumBestSongs()._parse_list(STEREOGUM_HTML)
    assert pairs == [
        ("mercury", "Roar Of The Heliac Goat"),
        ("Father John Misty", "The X-panded Universe"),
        ("Phoebe Bridgers", "On Video"),
    ]


def test_pitchfork_selects_reads_one_track_per_line():
    assert PitchforkSelects()._parse_list(SELECTS_HTML) == [
        ("Lily Konigsberg", "Free Byrd"),
        ("Krill", "Dominate"),
        ("300SkullsAndCounting, Jenny Sparks", "100% Legal Intro"),
        ("Kllhhr", "topman"),
    ]


def _feed(*titles):
    entries = [
        SimpleNamespace(
            get=lambda key, default="", t=title: (
                {"title": t, "link": f"https://x/{t}"}.get(key, default)
                if key != "published_parsed"
                else (2026, 10, 2, 18, 36, 31, 0, 0, 0)
            ),
            published_parsed=(2026, 10, 2, 18, 36, 31, 0, 0, 0),
        )
        for title in titles
    ]
    return SimpleNamespace(entries=entries)


def test_weekly_list_without_this_weeks_article_is_quiet_not_broken(monkeypatch):
    source = StereogumBestSongs()
    monkeypatch.setattr(
        "peel.sources.rss.feedparser.parse", lambda *a, **k: _feed("Some news", "Other news")
    )
    get = MagicMock()
    monkeypatch.setattr("peel.sources.rss.httpx.get", get)
    assert source.fetch() == []
    assert source.last_raw_entries == 2  # feed alive: no empty-feed warning
    get.assert_not_called()


def test_weekly_list_fetches_the_article_and_dates_tracks(monkeypatch):
    source = StereogumBestSongs()
    monkeypatch.setattr(
        "peel.sources.rss.feedparser.parse",
        lambda *a, **k: _feed("News", "The 5 Best Songs Of The Week"),
    )
    response = MagicMock(text=STEREOGUM_HTML)
    monkeypatch.setattr("peel.sources.rss.httpx.get", MagicMock(return_value=response))
    tracks = source.fetch()
    assert len(tracks) == 3
    assert tracks[-1].artist == "Phoebe Bridgers" and tracks[-1].title == "On Video"
    assert tracks[0].published_at == datetime(2026, 10, 2, 18, 36, 31, tzinfo=UTC)
    assert tracks[0].source_url == "https://x/The 5 Best Songs Of The Week"


def test_same_publication_is_not_consensus():
    rows = [
        ("spotify:track:same", "A", "Song", "pitchfork_bnt", "2026-10-01T10:00:00+00:00"),
        ("spotify:track:same", "A", "Song", "pitchfork_selects", "2026-10-01T10:00:00+00:00"),
        ("spotify:track:two", "B", "Tune", "pitchfork_bnt", "2026-09-30T10:00:00+00:00"),
        ("spotify:track:two", "B", "Tune", "gorillavsbear", "2026-09-30T10:00:00+00:00"),
    ]
    # Independent publications (Pitchfork + Gorilla vs Bear) beat a newer
    # track endorsed twice by Pitchfork alone.
    assert rank_window_uris(rows) == ["spotify:track:two", "spotify:track:same"]


def test_track_source_count_counts_publications(tmp_path):
    db = DB(str(tmp_path / "peel.db"))
    db.init_schema()
    for source in ("stereogum_new_music", "stereogum_best_songs"):
        db.record_track("spotify:track:x", source, "Tinashe", "Brainrot", None)
    assert db.source_count_for_track_identity("Tinashe", "Brainrot") == 1
    db.record_track("spotify:track:x", "gorillavsbear", "Tinashe", "Brainrot", None)
    assert db.source_count_for_track_identity("Tinashe", "Brainrot") == 2
    db.close()


def test_curated_sources_belong_to_their_publications():
    assert source_family("stereogum_best_songs") == source_family("stereogum_new_music")
    assert source_family("pitchfork_selects") == source_family("pitchfork_bnt")
    assert source_label("stereogum_best_songs") == "Stereogum"
    assert source_label("pitchfork_selects") == "Pitchfork"
    assert source_family("bandcamp_sub_pop") != source_family("bandcamp_dfa")
