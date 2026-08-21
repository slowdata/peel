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
    ClashAlbumReviews,
    ClashFirstTake,
    ConsequenceMusic,
    DIYAlbumReviews,
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
    "pitchfork_album_reviews": "Pitchfork",
    "pitchfork_news": "Pitchfork News",
    "lineofbestfit_news": "The Line of Best Fit",
    "consequence_music": "Consequence",
    "diy_album_reviews": "DIY",
    "clash_album_reviews": "Clash",
    "clash_first_take": "Clash",
    "kexp_in_our_headphones": "KEXP",
    "stereogum_new_music": "Stereogum",
    "thequietus": "The Quietus",
    "thequietus_feedbacker": "The Quietus",
    "thequietus_tracks_of_month": "The Quietus",
    "gorillavsbear": "Gorilla vs Bear",
    "guardian_music_albums": "The Guardian",
    "npr_new_music_friday_starting5": "NPR",
    "aquarium_drunkard": "Aquarium Drunkard",
}


ACTIVE_SOURCES: list[SourceSpec] = [
    SourceSpec(PitchforkBNT),
    SourceSpec(StereogumNewMusic),
    SourceSpec(PitchforkNews),
    SourceSpec(LineOfBestFitNews),
    SourceSpec(ConsequenceMusic),
    SourceSpec(TheQuietus),
    SourceSpec(TheQuietusFeedbacker),
    SourceSpec(TheQuietusTracksOfMonth),
    SourceSpec(GorillaVsBear),
    SourceSpec(KexpInOurHeadphones),
    SourceSpec(GuardianMusicAlbums),
    SourceSpec(DIYAlbumReviews),
    SourceSpec(ClashAlbumReviews),
    SourceSpec(ClashFirstTake),
    SourceSpec(NprNewMusicFridayStarting5),
    SourceSpec(PitchforkBestAlbums),
    SourceSpec(PitchforkAlbumReviews),
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


# Homepage de cada curador, por LABEL (a array `sources` do site usa labels).
# Credita os curadores e dá ao visitante um caminho para a fonte.
SOURCE_HOMEPAGE: dict[str, str] = {
    "Pitchfork": "https://pitchfork.com",
    "Pitchfork News": "https://pitchfork.com/news",
    "The Line of Best Fit": "https://www.thelineofbestfit.com/news",
    "Consequence": "https://consequence.net/category/music/",
    "DIY": "https://diymag.com/review/album",
    "Clash": "https://www.clashmusic.com/reviews/",
    "Stereogum": "https://www.stereogum.com",
    "The Quietus": "https://thequietus.com",
    "Gorilla vs Bear": "https://www.gorillavsbear.net",
    "KEXP": "https://www.kexp.org/podcasts/in-our-headphones/",
    "The Guardian": "https://www.theguardian.com/music",
    "NPR": "https://www.npr.org/music",
    "Aquarium Drunkard": "https://aquariumdrunkard.com",
    "DFA Records (Bandcamp)": "https://dfarecords.bandcamp.com",
    "Sacred Bones (Bandcamp)": "https://sacredbonesrecords.bandcamp.com",
    "Sub Pop (Bandcamp)": "https://subpop.bandcamp.com",
    "Stones Throw (Bandcamp)": "https://stonesthrow.bandcamp.com",
    "Ghostly International (Bandcamp)": "https://ghostly.bandcamp.com",
}


def source_homepage(label: str) -> str | None:
    """Homepage do curador para um label amigável, ou None se desconhecida."""
    return SOURCE_HOMEPAGE.get(label)
