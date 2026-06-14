from __future__ import annotations

from peel.sources.base import Source
from peel.sources.registry import ACTIVE_SOURCES, SourceSpec, active_source_specs, active_sources
from peel.sources.rss import (
    GorillaVsBear,
    GuardianMusicAlbums,
    NprNewMusicFridayStarting5,
    PitchforkBestAlbums,
    PitchforkBNT,
    StereogumNewMusic,
    TheQuietus,
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
    assert [spec.source_cls for spec in ACTIVE_SOURCES] == [
        PitchforkBNT,
        StereogumNewMusic,
        TheQuietus,
        TheQuietusTracksOfMonth,
        GorillaVsBear,
        GuardianMusicAlbums,
        NprNewMusicFridayStarting5,
        PitchforkBestAlbums,
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
