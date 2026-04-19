"""Matching de faixas com fuzzy string comparison.

Decisão: usar rapidfuzz.fuzz.token_set_ratio em vez de ratio simples.
Razão: token_set_ratio é imune a palavras extra, ordem diferente, parêntesis.
"Alright (feat. Pharrell)" vs "Alright" casam bem. Ratio simples falha.

Fluxo:
1. normalize() limpa ambas as strings (lowercase, strip, remove sufixos)
2. score() compara as strings normalized com token_set_ratio
3. is_match() exige AMBOS (artist AND title) acima threshold
4. best_match() recebe candidatos de SpotifyClient.search_track(),
   tenta cada um, devolve URI do melhor match ou None
"""

from __future__ import annotations

import re
import unicodedata

import structlog
from rapidfuzz import fuzz

from peel.models import Track

log = structlog.get_logger()


def normalize(s: str) -> str:
    """Normaliza string para matching.

    - Lowercase
    - Remove acentos (é → e)
    - Remove sufixos comuns: (deluxe), (remastered), (feat. X), etc.
    - Strip de whitespace
    """
    s = s.lower()

    # Remove acentos via decomposição NFD
    s = unicodedata.normalize("NFD", s)
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")

    # Remove (deluxe), (deluxe edition), (remastered), etc.
    s = re.sub(r"\s*\(deluxe.*?\)\s*", " ", s, flags=re.IGNORECASE)
    s = re.sub(r"\s*\(remastered.*?\)\s*", " ", s, flags=re.IGNORECASE)
    s = re.sub(r"\s*\(radio edit\)\s*", " ", s, flags=re.IGNORECASE)
    s = re.sub(r"\s*\(feat\..*?\)\s*", " ", s, flags=re.IGNORECASE)

    # Remove "feat. X" ou "ft. X" em qualquer posição
    s = re.sub(r"\s+feat\..*", "", s, flags=re.IGNORECASE)
    s = re.sub(r"\s+ft\..*", "", s, flags=re.IGNORECASE)

    # Remove "- remastered", "- radio edit", etc.
    s = re.sub(r"\s*-\s*(remastered|radio edit|deluxe).*", "", s, flags=re.IGNORECASE)

    # Remove virgulas e "&" (separadores de artistas)
    s = re.sub(r"[,&]", " ", s)

    # Colapsa whitespace múltiplo
    s = re.sub(r"\s+", " ", s).strip()

    return s


def score(a: str, b: str) -> int:
    """Compara duas strings com token_set_ratio (0..100).

    token_set_ratio é robusto a:
    - Palavras extra: "Alright (feat. X)" vs "Alright" casam bem
    - Ordem: "feat. X Alright" vs "Alright feat. X" casam
    - Parêntesis: "Alright (Deluxe)" vs "Alright" casam

    Normalmente ambas já estão normalized antes de chamar isto.
    """
    return int(fuzz.token_set_ratio(a, b))


def is_match(
    source: Track, candidate_artist: str, candidate_title: str, threshold: int = 85
) -> bool:
    """Valida se um candidato do Spotify bate com a fonte.

    Exige AMBOS artist E title acima threshold (não média).
    Normaliza antes de comparar.

    Args:
        source: Track da fonte (artist, title validados pelo validator)
        candidate_artist: Nome do artista do Spotify (raw, pode ter sufixos)
        candidate_title: Título do Spotify (raw)
        threshold: Score mínimo por campo (default 85)

    Returns:
        True se ambos os campos batem, False caso contrário.
    """
    norm_src_artist = normalize(source.artist)
    norm_src_title = normalize(source.title)
    norm_cand_artist = normalize(candidate_artist)
    norm_cand_title = normalize(candidate_title)

    artist_score = score(norm_src_artist, norm_cand_artist)
    title_score = score(norm_src_title, norm_cand_title)

    match = artist_score >= threshold and title_score >= threshold

    if not match:
        log.debug(
            "matcher.no_match",
            source_artist=source.artist,
            source_title=source.title,
            candidate_artist=candidate_artist,
            candidate_title=candidate_title,
            artist_score=artist_score,
            title_score=title_score,
            threshold=threshold,
        )

    return match


def best_match(track: Track, candidates: list[dict], threshold: int = 85) -> str | None:
    """Encontra o melhor match de um candidato Spotify para uma faixa.

    Args:
        track: Track da fonte (de uma RSSSource)
        candidates: Lista de dicts de SpotifyClient.search_track()
                   Cada dict: {"uri": "spotify:track:...", "name": "...", "artists": [...]}
        threshold: Score mínimo (default 85)

    Returns:
        URI do Spotify (str) se encontrar match, None caso contrário.
    """
    if not candidates:
        return None

    best_uri = None
    best_artist_score = -1
    best_title_score = -1

    norm_src_artist = normalize(track.artist)
    norm_src_title = normalize(track.title)

    for candidate in candidates:
        cand_uri = candidate.get("uri")
        cand_name = candidate.get("name", "")
        cand_artists = candidate.get("artists", [])

        # Constrói string de artista (join dos artists)
        cand_artist_str = ", ".join(cand_artists) if cand_artists else ""

        # Normaliza candidato
        norm_cand_artist = normalize(cand_artist_str)
        norm_cand_title = normalize(cand_name)

        # Scores
        artist_score = score(norm_src_artist, norm_cand_artist)
        title_score = score(norm_src_title, norm_cand_title)

        # Verifica se ambos acima threshold
        if artist_score >= threshold and title_score >= threshold:
            # Usa a soma (artist_score + title_score) como tiebreaker
            combined = artist_score + title_score
            best_combined = best_artist_score + best_title_score

            if combined > best_combined:
                best_uri = cand_uri
                best_artist_score = artist_score
                best_title_score = title_score

                log.debug(
                    "matcher.candidate_match",
                    track_artist=track.artist,
                    track_title=track.title,
                    candidate_artist=cand_artist_str,
                    candidate_title=cand_name,
                    artist_score=artist_score,
                    title_score=title_score,
                    uri=cand_uri,
                )

    if best_uri:
        log.info(
            "matcher.best_match_found",
            track_artist=track.artist,
            track_title=track.title,
            uri=best_uri,
            artist_score=best_artist_score,
            title_score=best_title_score,
        )
    else:
        log.warning(
            "matcher.no_best_match",
            track_artist=track.artist,
            track_title=track.title,
            candidates_count=len(candidates),
            threshold=threshold,
        )

    return best_uri
