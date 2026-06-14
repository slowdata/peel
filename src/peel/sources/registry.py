"""Registo único de sources activas no Peel.

DECISÃO: manter o registo declarativo e simples. Para desligar uma source sem
apagar a classe, marcar ``enabled=False`` ou remover a linha de ``ACTIVE_SOURCES``.
Adicionar uma source nova deve ser uma linha neste ficheiro, não uma alteração ao
orquestrador em ``main.py``.
"""

from __future__ import annotations

from dataclasses import dataclass

from peel.sources.base import Source
from peel.sources.rss import (
    AquariumDrunkard,
    GorillaVsBear,
    GuardianMusicAlbums,
    NprNewMusicFridayStarting5,
    PitchforkBestAlbums,
    PitchforkBNT,
    StereogumNewMusic,
    TheQuietus,
    TheQuietusTracksOfMonth,
)


@dataclass(frozen=True, slots=True)
class SourceSpec:
    """Config declarativa de uma source."""

    source_cls: type[Source]
    enabled: bool = True

    @property
    def source_id(self) -> str:
        return self.source_cls.id


ACTIVE_SOURCES: list[SourceSpec] = [
    SourceSpec(PitchforkBNT),
    SourceSpec(StereogumNewMusic),
    SourceSpec(TheQuietus),
    SourceSpec(TheQuietusTracksOfMonth),
    SourceSpec(GorillaVsBear),
    SourceSpec(GuardianMusicAlbums),
    SourceSpec(NprNewMusicFridayStarting5),
    SourceSpec(PitchforkBestAlbums),
    SourceSpec(AquariumDrunkard),
]


def active_source_specs() -> list[SourceSpec]:
    """Specs activas, preservando a ordem da run."""
    return [spec for spec in ACTIVE_SOURCES if spec.enabled]


def active_sources() -> list[Source]:
    """Instancia sources activas para uma run."""
    return [spec.source_cls() for spec in active_source_specs()]
