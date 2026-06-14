"""Registo único de sources activas no Peel.

DECISÃO: manter o registo declarativo e simples. Para desligar uma source sem
apagar a classe, marcar ``enabled=False`` ou remover a linha de ``ACTIVE_SOURCES``.
Adicionar uma source nova deve ser uma linha neste ficheiro, não uma alteração ao
orquestrador em ``main.py``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from peel.sources.bandcamp import BandcampLabel
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

SourceFactory = type[Source] | Callable[[], Source]


@dataclass(frozen=True, slots=True)
class SourceSpec:
    """Config declarativa de uma source.

    Aceita classes sem argumentos ou factories para sources configuradas (ex. a
    mesma classe BandcampLabel com várias labels). Para desligar uma source,
    usar ``enabled=False`` ou remover a spec do registo.
    """

    factory: SourceFactory
    enabled: bool = True
    id: str | None = None

    @property
    def source_id(self) -> str:
        if self.id is not None:
            return self.id
        class_id = getattr(self.factory, "id", None)
        if class_id is not None:
            return str(class_id)
        return self.create().id

    @property
    def source_name(self) -> str:
        class_name = getattr(self.factory, "name", None)
        if class_name is not None:
            return str(class_name)
        return self.create().name

    def create(self) -> Source:
        return self.factory()


def _bandcamp_label(
    source_id: str,
    name: str,
    subdomain: str,
    max_items: int = 5,
) -> SourceSpec:
    return SourceSpec(
        lambda: BandcampLabel(source_id, name, subdomain, max_items=max_items),
        id=source_id,
    )


SOURCE_LABEL_OVERRIDES: dict[str, str] = {
    "pitchfork_bnt": "Pitchfork",
    "pitchfork_best_albums": "Pitchfork",
    "stereogum_new_music": "Stereogum",
    "thequietus": "The Quietus",
    "thequietus_tracks_of_month": "The Quietus",
    "gorillavsbear": "Gorilla vs Bear",
    "guardian_music_albums": "The Guardian",
    "npr_new_music_friday_starting5": "NPR",
    "aquarium_drunkard": "Aquarium Drunkard",
}


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
    _bandcamp_label("bandcamp_dfa", "DFA Records (Bandcamp)", "dfarecords"),
    _bandcamp_label(
        "bandcamp_sacred_bones",
        "Sacred Bones (Bandcamp)",
        "sacredbonesrecords",
    ),
    _bandcamp_label("bandcamp_sub_pop", "Sub Pop (Bandcamp)", "subpop"),
    _bandcamp_label("bandcamp_stones_throw", "Stones Throw (Bandcamp)", "stonesthrow"),
    _bandcamp_label("bandcamp_ghostly", "Ghostly International (Bandcamp)", "ghostly"),
]


def active_source_specs() -> list[SourceSpec]:
    """Specs activas, preservando a ordem da run."""
    return [spec for spec in ACTIVE_SOURCES if spec.enabled]


def active_sources() -> list[Source]:
    """Instancia sources activas para uma run."""
    return [spec.create() for spec in active_source_specs()]


def source_label_map() -> dict[str, str]:
    """Mapa reutilizável source_id -> nome humano curto/legível."""
    return {
        spec.source_id: SOURCE_LABEL_OVERRIDES.get(spec.source_id, spec.source_name)
        for spec in ACTIVE_SOURCES
    }


def source_label(source_id: str) -> str:
    """Nome humano para uma source, com fallback determinístico."""
    labels = source_label_map()
    if source_id in labels:
        return labels[source_id]
    return source_id.replace("_", " ").replace("-", " ").title()
