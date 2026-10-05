"""Publicação de origem de cada source.

Duas sources da mesma publicação não fazem consenso: um single que o Stereogum
noticia e depois põe nas «5 Best Songs» é um endosso forte, mas continua a ser
uma só voz editorial. Usado nas faixas e nos álbuns.
"""

from __future__ import annotations

SOURCE_FAMILIES: dict[str, str] = {
    "pitchfork_bnt": "pitchfork",
    "pitchfork_news": "pitchfork",
    "pitchfork_selects": "pitchfork",
    "pitchfork_best_albums": "pitchfork",
    "pitchfork_album_reviews": "pitchfork",
    "stereogum_new_music": "stereogum",
    "stereogum_best_songs": "stereogum",
    "thequietus": "thequietus",
    "thequietus_feedbacker": "thequietus",
    "thequietus_tracks_of_month": "thequietus",
    "diy_album_reviews": "diy",
    "clash_album_reviews": "clash",
    "clash_first_take": "clash",
}


def source_family(source_id: str) -> str:
    """Publicação de uma source; sources sem família são independentes."""
    return SOURCE_FAMILIES.get(source_id, source_id)
