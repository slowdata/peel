"""Diversity breaks close album-quality ties, not meaningful differences."""

from __future__ import annotations

from peel.albums import AlbumRecommendation, _softly_diverse


def _album(artist: str, source: str, average: float, *, consensus: int = 1) -> AlbumRecommendation:
    return AlbumRecommendation(
        artist=artist,
        album="Album",
        artist_key=artist.lower(),
        album_key="album",
        source_count=consensus,
        sources=(source,),
        source_urls=((source, None),),
        spotify_album_uri=None,
        latest_seen_at="2026-11-06T10:00:00+00:00",
        best_avg_rating=average,
        best_score=0.0,
    )


def test_diversity_does_not_overturn_realistic_album_quality_gap() -> None:
    candidates = [
        _album("A1", "clash_album_reviews", 0.74),
        _album("A2", "clash_album_reviews", 0.74),
        _album("B", "guardian_music_albums", 0.61),
    ]
    assert [a.artist for a in _softly_diverse(candidates, {}, 2)] == ["A1", "A2"]


def test_diversity_still_breaks_close_quality_ties() -> None:
    candidates = [
        _album("A1", "clash_album_reviews", 0.74),
        _album("A2", "clash_album_reviews", 0.74),
        _album("B", "guardian_music_albums", 0.73),
    ]
    assert [a.artist for a in _softly_diverse(candidates, {}, 3)] == ["A1", "B", "A2"]


def test_diversity_adjustment_remains_bounded_with_many_previous_picks() -> None:
    already = [_album(f"Past {i}", "clash_album_reviews", 0.74) for i in range(100)]
    candidates = [
        _album("Better", "clash_album_reviews", 0.74),
        _album("Weaker", "guardian_music_albums", 0.68),
    ]
    assert _softly_diverse(candidates, {}, 1, already)[0].artist == "Better"


def test_diversity_does_not_create_a_per_source_cap() -> None:
    candidates = [_album(f"Artist {i}", "clash_album_reviews", 0.74) for i in range(15)]
    assert len(_softly_diverse(candidates, {}, 14)) == 14


def test_diversity_keeps_consensus_and_affinity_hierarchy() -> None:
    candidates = [
        _album("Consensus", "clash_album_reviews", 0.0, consensus=2),
        _album("Loved", "clash_album_reviews", 0.0),
        _album("Unknown", "guardian_music_albums", 2.0),
    ]
    selected = _softly_diverse(
        candidates, {}, 3, affinity=lambda artist: 0.75 if artist == "Loved" else 0.5
    )
    assert [a.artist for a in selected] == ["Consensus", "Loved", "Unknown"]


def test_w45_final_place_does_not_rotate_away_from_better_quality() -> None:
    # After six 3-family picks, Clash has been represented twice and the
    # Guardian once. The old penalty 2.0 reversed this 0.128 quality gap.
    already = [
        _album("Clash 1", "clash_album_reviews", 0.737, consensus=3),
        _album("Clash 2", "clash_album_reviews", 0.737, consensus=3),
        _album("Guardian 1", "guardian_music_albums", 0.609, consensus=3),
        _album("BPM 1", "beats_per_minute_album_reviews", 0.647, consensus=3),
        _album("BPM 2", "beats_per_minute_album_reviews", 0.647, consensus=3),
        _album("Pitchfork 1", "pitchfork_album_reviews", 0.486, consensus=3),
    ]
    candidates = [
        _album("Gyrofield", "clash_album_reviews", 0.737, consensus=2),
        _album("Shovel Dance Collective", "guardian_music_albums", 0.609, consensus=2),
        _album("Alchemical Sisters", "pitchfork_best_albums", 0.5, consensus=2),
    ]
    assert _softly_diverse(candidates, {}, 1, already)[0].artist == "Gyrofield"
