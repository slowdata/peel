from __future__ import annotations

from peel.albums import AlbumMention, rank_album_recommendations, spotify_album_url


def _mention(
    artist: str,
    album: str,
    source_id: str,
    seen_at: str,
    *,
    spotify_album_uri: str | None = None,
    source_url: str | None = None,
) -> AlbumMention:
    return AlbumMention(
        artist=artist,
        album=album,
        artist_key=artist.lower(),
        album_key=album.lower(),
        source_id=source_id,
        source_url=source_url,
        spotify_album_uri=spotify_album_uri,
        seen_at=seen_at,
    )


def test_album_ranking_consensus_beats_single_source_quality() -> None:
    rows = [
        _mention("Artist A", "Album A", "weak-a", "2026-06-10T10:00:00+00:00"),
        _mention("Artist A", "Album A", "weak-b", "2026-06-10T11:00:00+00:00"),
        _mention("Artist B", "Album B", "strong", "2026-06-10T12:00:00+00:00"),
    ]

    ranked = rank_album_recommendations(
        rows,
        source_quality={"strong": (2.0, 100.0), "weak-a": (0.0, 0.0), "weak-b": (0.0, 0.0)},
    )

    assert [(item.artist, item.album) for item in ranked] == [
        ("Artist A", "Album A"),
        ("Artist B", "Album B"),
    ]
    assert ranked[0].source_count == 2


def test_album_ranking_uses_quality_then_recency_as_tie_breakers() -> None:
    rows = [
        _mention("Artist Low", "Album Low", "low", "2026-06-12T12:00:00+00:00"),
        _mention("Artist High", "Album High", "high", "2026-06-11T12:00:00+00:00"),
        _mention("Artist Recent", "Album Recent", "neutral", "2026-06-13T12:00:00+00:00"),
    ]

    ranked = rank_album_recommendations(
        rows,
        source_quality={"high": (1.0, 5.0), "low": (0.0, 0.0), "neutral": (0.0, 0.0)},
    )

    assert [(item.artist, item.album) for item in ranked] == [
        ("Artist High", "Album High"),
        ("Artist Recent", "Album Recent"),
        ("Artist Low", "Album Low"),
    ]


def test_album_ranking_is_deterministic_and_respects_limit() -> None:
    rows = [
        _mention(f"Artist {letter}", f"Album {letter}", "source", "2026-06-10T10:00:00+00:00")
        for letter in "IHGFEDCBA"
    ]

    ranked = rank_album_recommendations(rows, limit=7)

    assert len(ranked) == 7
    assert [(item.artist, item.album) for item in ranked] == [
        ("Artist A", "Album A"),
        ("Artist B", "Album B"),
        ("Artist C", "Album C"),
        ("Artist D", "Album D"),
        ("Artist E", "Album E"),
        ("Artist F", "Album F"),
        ("Artist G", "Album G"),
    ]


def test_album_ranking_propagates_spotify_uri_and_source_urls() -> None:
    rows = [
        _mention(
            "Artist",
            "Album",
            "source-a",
            "2026-06-10T10:00:00+00:00",
            source_url="https://example.com/a",
        ),
        _mention(
            "Artist",
            "Album",
            "source-b",
            "2026-06-10T11:00:00+00:00",
            spotify_album_uri="spotify:album:abc123",
            source_url="https://example.com/b",
        ),
    ]

    item = rank_album_recommendations(rows)[0]

    assert item.spotify_album_uri == "spotify:album:abc123"
    assert item.link_url == "https://open.spotify.com/album/abc123"
    assert item.source_urls == (
        ("source-a", "https://example.com/a"),
        ("source-b", "https://example.com/b"),
    )


def test_spotify_album_url_passes_through_non_uri_values() -> None:
    assert spotify_album_url("https://example.com/album") == "https://example.com/album"
