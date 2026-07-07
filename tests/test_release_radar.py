from datetime import UTC, datetime

from peel.release_radar import (
    ReleaseRadarTrack,
    parse_release_radar_html,
    release_radar_snapshot_payload,
    tracks_from_snapshot,
)

SAMPLE_HTML = """
<html><body>
  <div data-testid="track-row" aria-label="Wink Wink"
       aria-labelledby="listrow-title-track-spotify:track:6GjXR9FaxCuDNbqBrP9aYO-0">
    <span data-testid="internal-artist-link"><a href="/artist/25ui">Charli xcx</a></span>
  </div>
  <div data-testid="track-row" aria-label="Elitest G.O.A.T. - Remix"
       aria-labelledby="listrow-title-track-spotify:track:4flGYl0U5izyutdDcHP6fN-1">
    <span data-testid="internal-artist-link"><a href="/artist/a">Sleaford Mods</a></span>
    <span data-testid="internal-artist-link"><a href="/artist/b">Aldous Harding</a></span>
  </div>
</body></html>
"""


def test_parse_release_radar_html_extracts_tracks() -> None:
    tracks = parse_release_radar_html(SAMPLE_HTML)

    assert tracks == [
        ReleaseRadarTrack(
            position=1,
            spotify_uri="spotify:track:6GjXR9FaxCuDNbqBrP9aYO",
            title="Wink Wink",
            artists=["Charli xcx"],
        ),
        ReleaseRadarTrack(
            position=2,
            spotify_uri="spotify:track:4flGYl0U5izyutdDcHP6fN",
            title="Elitest G.O.A.T. - Remix",
            artists=["Sleaford Mods", "Aldous Harding"],
        ),
    ]


def test_release_radar_track_has_artist_and_url() -> None:
    track = ReleaseRadarTrack(
        position=1,
        spotify_uri="spotify:track:abc123",
        title="Track",
        artists=["Artist A", "Artist B"],
    )

    assert track.artist == "Artist A, Artist B"
    assert track.spotify_url == "https://open.spotify.com/track/abc123"
    assert track.to_dict()["artist"] == "Artist A, Artist B"


def test_snapshot_roundtrip() -> None:
    tracks = parse_release_radar_html(SAMPLE_HTML)
    payload = release_radar_snapshot_payload(
        tracks,
        week="2026-W28",
        url="https://example.test/playlist",
        fetched_at=datetime(2026, 7, 7, 12, 0, tzinfo=UTC),
    )

    assert payload["week"] == "2026-W28"
    assert payload["track_count"] == 2
    assert payload["fetched_at"] == "2026-07-07T12:00:00+00:00"
    assert tracks_from_snapshot(payload) == tracks
