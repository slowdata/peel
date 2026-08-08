from __future__ import annotations

from peel.sources.bandcamp import BandcampLabel
from peel.sources.base import Source
from peel.sources.registry import (
    ACTIVE_SOURCES,
    SourceSpec,
    active_source_specs,
    active_sources,
    source_homepage,
    source_label,
)
from peel.sources.rss import (
    AquariumDrunkard,
    ConsequenceMusic,
    GorillaVsBear,
    GuardianMusicAlbums,
    KexpInOurHeadphones,
    LineOfBestFitNews,
    NprNewMusicFridayStarting5,
    PitchforkAlbumReviews,
    PitchforkBestAlbums,
    PitchforkBNT,
    PitchforkNews,
    StereogumNewMusic,
    TheQuietus,
    TheQuietusFeedbacker,
    TheQuietusTracksOfMonth,
)


class DummySource(Source):
    id = "dummy"
    name = "Dummy"

    def fetch(self):
        return []


class DisabledSource(Source):
    id = "disabled"
    name = "Disabled"

    def fetch(self):
        return []


def test_active_sources_registry_contains_expected_order() -> None:
    assert [spec.source_id for spec in ACTIVE_SOURCES] == [
        PitchforkBNT.id,
        StereogumNewMusic.id,
        PitchforkNews.id,
        LineOfBestFitNews.id,
        ConsequenceMusic.id,
        TheQuietus.id,
        TheQuietusFeedbacker.id,
        TheQuietusTracksOfMonth.id,
        GorillaVsBear.id,
        KexpInOurHeadphones.id,
        GuardianMusicAlbums.id,
        NprNewMusicFridayStarting5.id,
        PitchforkBestAlbums.id,
        PitchforkAlbumReviews.id,
        AquariumDrunkard.id,
        "bandcamp_dfa",
        "bandcamp_sacred_bones",
        "bandcamp_sub_pop",
        "bandcamp_stones_throw",
        "bandcamp_ghostly",
    ]


def test_active_sources_instantiates_enabled_sources(monkeypatch) -> None:
    monkeypatch.setattr(
        "peel.sources.registry.ACTIVE_SOURCES",
        [SourceSpec(DummySource), SourceSpec(DisabledSource, enabled=False)],
    )

    assert [spec.source_id for spec in active_source_specs()] == ["dummy"]
    sources = active_sources()

    assert len(sources) == 1
    assert isinstance(sources[0], DummySource)


def test_consequence_is_registered_once_with_label_and_homepage() -> None:
    assert [spec.source_id for spec in ACTIVE_SOURCES].count(ConsequenceMusic.id) == 1
    assert source_label(ConsequenceMusic.id) == "Consequence"
    assert source_homepage("Consequence") == "https://consequence.net/category/music/"


def test_source_label_returns_short_labels_and_fallback() -> None:
    assert source_label("npr_new_music_friday_starting5") == "NPR"
    assert source_label("pitchfork_best_albums") == "Pitchfork"
    assert source_label("pitchfork_album_reviews") == "Pitchfork"
    assert source_label("thequietus_feedbacker") == "The Quietus"
    assert source_label("custom_source") == "Custom Source"


def test_active_sources_instantiates_configured_bandcamp_labels() -> None:
    sources = active_sources()
    bandcamp_sources = [source for source in sources if isinstance(source, BandcampLabel)]

    assert [source.id for source in bandcamp_sources] == [
        "bandcamp_dfa",
        "bandcamp_sacred_bones",
        "bandcamp_sub_pop",
        "bandcamp_stones_throw",
        "bandcamp_ghostly",
    ]
    assert all(source.kind == "album" for source in bandcamp_sources)
    assert all(source.max_items == 5 for source in bandcamp_sources)
