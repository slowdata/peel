"""Afinidade local de gosto para ranking secundário.

v1 usa só dados locais:
- feedback explícito em `feedback` + `tracks`;
- cache `artist_genres`, quando existir;
- prior estático derivado do perfil proxy calculado a partir das playlists do Dias.

Não faz chamadas de rede. O backfill de géneros vive no CLI e só corre quando
invocado explicitamente.
"""

from __future__ import annotations

import json
import math
import sqlite3
from dataclasses import dataclass, field
from typing import Any

from peel.matcher import normalize

DEFAULT_AFFINITY = 0.5

# Spec do Dias/Claude: meh conta como sinal negativo suave, mesmo que o valor
# histórico em `FEEDBACK_RATINGS` seja 0 para retrocompatibilidade.
FEEDBACK_AFFINITY_WEIGHTS = {
    "love": 2.0,
    "like": 1.0,
    "meh": -1.0,
    "skip": -1.0,
    "ban": -2.0,
}

# Prior estático vindo do proxy de playlists longas (ver conversa 2026-07-06).
# Usado apenas quando o artista ainda não tem ratings no Peel.
SEED_ARTIST_WEIGHTS: tuple[tuple[str, float], ...] = (
    ("Yussef Dayes", 0.0317),
    ("IDLES", 0.0284),
    ("Iosonouncane", 0.0284),
    ("Kanye West", 0.0267),
    ("Bon Iver", 0.0217),
    ("Clipse", 0.0217),
    ("Ethel Cain", 0.0217),
    ("James Blake", 0.0217),
    ("Joe Armon-Jones", 0.0200),
    ("Deftones", 0.0184),
    ("McKinley Dixon", 0.0184),
    ("Geese", 0.0184),
    ("Fontaines D.C.", 0.0184),
    ("Dry Cleaning", 0.0184),
    ("Nilüfer Yanya", 0.0184),
    ("shame", 0.0167),
    ("Metronomy", 0.0117),
    ("Arctic Monkeys", 0.0100),
    ("Interpol", 0.0100),
    ("Radiohead", 0.0083),
)

# Heurística de afinidade por macro-género. Não é Spotify-derived; serve como
# fallback quando houver cache de géneros local, ou quando a source fornecer
# géneros no futuro.
SEED_GENRE_WEIGHTS: dict[str, float] = {
    "post-punk uk indie": 0.22,
    "post-punk": 0.22,
    "uk indie": 0.22,
    "indie rock alternative": 0.20,
    "indie rock": 0.20,
    "alternative": 0.20,
    "indie electronic melodic": 0.17,
    "indie electronic": 0.17,
    "electronic melodic": 0.17,
    "experimental ambient electronic": 0.11,
    "experimental": 0.11,
    "ambient": 0.11,
    "electronic": 0.11,
    "alternative rap hip hop": 0.10,
    "alternative rap": 0.10,
    "hip hop": 0.10,
    "jazz groove soul": 0.08,
    "jazz": 0.08,
    "groove": 0.08,
    "soul": 0.08,
    "psychedelic krautrock": 0.07,
    "psychedelic": 0.07,
    "krautrock": 0.07,
    "art pop sophisti pop": 0.05,
    "art pop": 0.05,
    "sophisti pop": 0.05,
}


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, value))


def _score_from_feedback_weight(raw_weight: float) -> float:
    """Converte soma de feedbacks em 0..1 com saturação suave.

    `tanh` dá peso a escutas/ratings repetidos sem deixar que artistas com muitos
    ratings dominem infinitamente.
    """
    return _clamp01((math.tanh(raw_weight / 4.0) + 1.0) / 2.0)


def _seed_artist_scores() -> dict[str, float]:
    max_weight = max(weight for _, weight in SEED_ARTIST_WEIGHTS)
    min_weight = min(weight for _, weight in SEED_ARTIST_WEIGHTS)
    spread = max(max_weight - min_weight, 0.0001)
    scores: dict[str, float] = {}
    for artist, weight in SEED_ARTIST_WEIGHTS:
        # Prior conservador: forte o suficiente para desempatar, fraco o
        # suficiente para ratings reais o ultrapassarem. Tecto em 0.70 mantém
        # o prior abaixo de um love real (~0.731) e do badge threshold (0.75):
        # o 🎯 só aparece com feedback explícito.
        scores[normalize(artist)] = 0.55 + 0.15 * ((weight - min_weight) / spread)
    return scores


def _genre_score(genres: list[str] | tuple[str, ...] | None) -> float | None:
    if not genres:
        return None
    matched: list[float] = []
    seed = {normalize(key): value for key, value in SEED_GENRE_WEIGHTS.items()}
    max_weight = max(seed.values())
    for genre in genres:
        genre_key = normalize(genre)
        if not genre_key:
            continue
        for seed_key, weight in seed.items():
            if genre_key == seed_key or seed_key in genre_key or genre_key in seed_key:
                matched.append(weight)
                break
    if not matched:
        return None
    avg_weight = sum(matched) / len(matched)
    return _clamp01(0.50 + 0.40 * (avg_weight / max_weight))


@dataclass(slots=True)
class AffinityProfile:
    """Perfil de afinidade local e determinístico."""

    artist_scores: dict[str, float] = field(default_factory=dict)
    genre_scores: dict[str, float] = field(default_factory=lambda: dict(SEED_GENRE_WEIGHTS))
    artist_genres: dict[str, tuple[str, ...]] = field(default_factory=dict)
    rated_artist_keys: set[str] = field(default_factory=set)

    def score(self, artist: str, genres: list[str] | tuple[str, ...] | None = None) -> float:
        """Score 0..1 para um artista, sem rede nem estado global mutável."""
        artist_key = normalize(artist)
        cached_genres = genres or self.artist_genres.get(artist_key)
        genre_affinity = _genre_score(list(cached_genres) if cached_genres else None)
        artist_affinity = self.artist_scores.get(artist_key)

        if artist_affinity is None:
            return genre_affinity if genre_affinity is not None else DEFAULT_AFFINITY

        if artist_key in self.rated_artist_keys:
            # Ratings reais são o sinal principal. Géneros só desempurram muito
            # ligeiramente se estiverem disponíveis.
            if genre_affinity is None:
                return _clamp01(artist_affinity)
            return _clamp01(0.90 * artist_affinity + 0.10 * genre_affinity)

        if genre_affinity is None:
            return _clamp01(artist_affinity)
        return _clamp01(0.75 * artist_affinity + 0.25 * genre_affinity)

    def normalized_artist_scores(self) -> dict[str, float]:
        """Mapa simples para APIs antigas/testes."""
        return dict(self.artist_scores)


def build_affinity_profile(db: Any | None = None) -> AffinityProfile:
    """Constrói perfil local a partir da DB, com prior estático como fallback."""
    artist_scores = _seed_artist_scores()
    rated_artist_keys: set[str] = set()
    artist_genres: dict[str, tuple[str, ...]] = {}

    if db is not None:
        rating_totals = _artist_feedback_weights(db)
        for artist_key, raw_weight in rating_totals.items():
            artist_scores[artist_key] = _score_from_feedback_weight(raw_weight)
            rated_artist_keys.add(artist_key)
        artist_genres = _cached_artist_genres(db)

    return AffinityProfile(
        artist_scores=artist_scores,
        genre_scores=dict(SEED_GENRE_WEIGHTS),
        artist_genres=artist_genres,
        rated_artist_keys=rated_artist_keys,
    )


def affinity_score(artist: str, genres: list[str] | tuple[str, ...] | None = None) -> float:
    """Score público estático, sem DB. Útil como fallback e em testes."""
    return build_affinity_profile().score(artist, genres)


def _artist_feedback_weights(db: Any) -> dict[str, float]:
    rows = db.conn.execute(
        """
        SELECT t.spotify_uri, MIN(t.artist) AS artist, f.label, f.rating
        FROM tracks t
        JOIN feedback f ON f.spotify_uri = t.spotify_uri
        GROUP BY t.spotify_uri, f.label, f.rating
        """
    ).fetchall()
    totals: dict[str, float] = {}
    for _, artist, label, rating in rows:
        artist_key = normalize(str(artist))
        if not artist_key:
            continue
        weight = FEEDBACK_AFFINITY_WEIGHTS.get(str(label).lower())
        if weight is None:
            weight = _weight_from_numeric_rating(int(rating))
        totals[artist_key] = totals.get(artist_key, 0.0) + weight
    return totals


def _weight_from_numeric_rating(rating: int) -> float:
    if rating >= 2:
        return 2.0
    if rating == 1:
        return 1.0
    if rating == 0:
        return -1.0
    if rating <= -2:
        return -2.0
    return -1.0


def _cached_artist_genres(db: Any) -> dict[str, tuple[str, ...]]:
    try:
        rows = db.conn.execute("SELECT artist, genres FROM artist_genres").fetchall()
    except sqlite3.Error:
        return {}

    result: dict[str, tuple[str, ...]] = {}
    for artist, raw_genres in rows:
        try:
            parsed = json.loads(str(raw_genres))
        except json.JSONDecodeError:
            continue
        if not isinstance(parsed, list):
            continue
        genres = tuple(str(item) for item in parsed if str(item).strip())
        if genres:
            result[normalize(str(artist))] = genres
    return result
