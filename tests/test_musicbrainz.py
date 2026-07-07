from __future__ import annotations

from peel.musicbrainz import parse_musicbrainz_artist_genres


def test_parse_musicbrainz_prefers_official_genres() -> None:
    payload = {
        "artists": [
            {
                "id": "mbid-1",
                "name": "IDLES",
                "genres": [{"name": "post-punk", "count": 2}],
                "tags": [{"name": "rock", "count": 99}],
            }
        ]
    }

    result = parse_musicbrainz_artist_genres("idles", payload)

    assert result is not None
    assert result.name == "IDLES"
    assert result.mbid == "mbid-1"
    assert result.genres == ("post-punk",)


def test_parse_musicbrainz_uses_tags_with_threshold() -> None:
    payload = {
        "artists": [
            {
                "id": "mbid-1",
                "name": "IDLES",
                "tags": [
                    {"name": "seen live", "count": 100},
                    {"name": "female", "count": 20},
                    {"name": "2010s", "count": 20},
                    {"name": "uk", "count": 20},
                    {"name": "rock", "count": 1},
                    {"name": "post-punk", "count": 4},
                    {"name": "art punk", "count": 3},
                ],
            }
        ]
    }

    result = parse_musicbrainz_artist_genres("IDLES", payload, min_tag_count=2)

    assert result is not None
    assert result.genres == ("post-punk", "art punk")


def test_parse_musicbrainz_rejects_non_exact_match() -> None:
    payload = {"artists": [{"id": "mbid-1", "name": "Drake", "tags": [{"name": "rap"}]}]}

    assert parse_musicbrainz_artist_genres("Drake Sexyy Red", payload) is None


def test_parse_musicbrainz_rejects_empty_genres() -> None:
    payload = {"artists": [{"id": "mbid-1", "name": "Unknown", "tags": []}]}

    assert parse_musicbrainz_artist_genres("Unknown", payload) is None
