"""Album novelty: only genuinely new releases enter the album queue."""

from __future__ import annotations

from datetime import date

from peel.novelty import NoveltyCheck, artist_ended_on, parse_release_date, spotify_album_id

WEEK = "2026-W45"  # Monday 2 Nov 2026


def _mb(*artists: dict) -> dict:
    return {"artists": list(artists)}


def _artist(name: str, end: str | None = None, score: int = 100) -> dict:
    span = {"ended": True, "end": end} if end else {"ended": None}
    return {"name": name, "score": score, "life-span": span}


def _check(spotify: dict | None = None, mb: dict | None = None) -> NoveltyCheck:
    if spotify is not None:
        spotify = {"total_tracks": 8, "album_type": "album", **spotify}
    return NoveltyCheck(
        spotify_album=(lambda _id: spotify) if spotify is not None else None,
        artist_search=(lambda _a: mb or _mb()),
        sleep=lambda _s: None,
    )


def test_archival_recording_by_an_artist_who_died_long_ago_is_not_new():
    check = _check(
        spotify={"name": "The Tiberi Tapes (Live)", "release_date": "2026-11-06"},
        mb=_mb(_artist("John Coltrane", "1967-07-17")),
    )
    reason = check("John Coltrane", "The Tiberi Tapes", "https://open.spotify.com/album/x", WEEK)
    assert reason and "1967" in reason


def test_live_album_by_an_active_artist_stays():
    check = _check(
        spotify={"name": "Live at Carnegie Hall", "release_date": "2026-10-09"},
        mb=_mb(_artist("Cameron Winter")),
    )
    assert (
        check("Cameron Winter", "Live at Carnegie Hall", "https://open.spotify.com/album/x", WEEK)
        is None
    )


def test_old_or_remastered_spotify_edition_is_not_new():
    old = _check(spotify={"name": "Fear of Music", "release_date": "1979-08-03"})
    assert "1979" in old("Talking Heads", "Fear of Music", "https://open.spotify.com/album/x", WEEK)
    remaster = _check(
        spotify={"name": "Collision Drive (Remastered)", "release_date": "2026-10-23"}
    )
    assert "Remastered" in remaster("Alan Vega", "Collision Drive", "spotify:album:x", WEEK)


def test_recent_posthumous_release_and_unknown_artist_stay():
    recent = _check(mb=_mb(_artist("Late Singer", "2025-03-01")))
    assert recent("Late Singer", "Last Songs", None, WEEK) is None
    assert _check()("Nobody Known", "Debut", None, WEEK) is None


def test_active_namesake_keeps_the_album():
    payload = _mb(_artist("Fine", "1990"), _artist("Fine"))
    assert artist_ended_on("Fine", payload) is None


def test_release_lookup_failure_holds_album_but_artist_failure_is_best_effort():
    def boom(_x):
        raise RuntimeError("down")

    check = NoveltyCheck(spotify_album=boom, artist_search=boom, sleep=lambda _s: None)
    assert "não confirmados" in check("Any", "Album", "https://open.spotify.com/album/x", WEEK)
    assert check("Any", "Album", None, WEEK) is None
    check.spotify_album = lambda _id: {
        "name": "Album",
        "release_date": "2026-11-06",
        "total_tracks": 8,
    }
    assert check("Any", "Album", "spotify:album:x", WEEK) is None


def test_helpers():
    assert parse_release_date("1963") == date(1963, 1, 1)
    assert parse_release_date("2026-10") == date(2026, 10, 1)
    assert spotify_album_id("https://open.spotify.com/album/abc?si=1") == "abc"
    assert spotify_album_id("https://label.bandcamp.com/album/x") is None


def test_album_date_and_format_admission():
    for data, expected in [
        ({"release_date": "2026-09-01"}, "35 dias"),
        ({"release_date": "2026-11-09"}, "futuro"),
        ({"release_date": None}, "não confirmada"),
        ({"release_date": "2026-11-06", "total_tracks": 3, "album_type": "single"}, "EP curto"),
        ({"release_date": "2026-11-06", "total_tracks": None}, "formato"),
        ({"release_date": "2026-11-06", "album_type": "compilation"}, "compilação"),
    ]:
        reason = _check(spotify=data)("Artist", "Album", "spotify:album:x", WEEK)
        assert reason and expected in reason


def test_explicit_long_form_album_is_not_rejected_only_for_few_tracks():
    for count in [1, 3]:
        check = _check(
            spotify={"release_date": "2026-11-06", "total_tracks": count, "album_type": "album"}
        )
        assert check("Artist", "Long Form", "spotify:album:x", WEEK) is None
    empty = _check(spotify={"release_date": "2026-11-06", "total_tracks": 0})
    assert "sem faixas" in empty("Artist", "Empty", "spotify:album:x", WEEK)


def test_actual_run_date_not_monday_or_future_sunday_controls_release_window():
    check = _check(spotify={"release_date": "2026-11-07"})
    check.reference_date = date(2026, 11, 6)
    assert "futuro" in check("Artist", "Album", "spotify:album:x", WEEK)
    check.reference_date = date(2026, 11, 7)
    assert check("Artist", "Album", "spotify:album:x", WEEK) is None


def test_bandcamp_links_are_checked_for_release_date_and_short_format():
    from datetime import UTC, datetime

    from peel.sources.bandcamp import ReleaseDetails

    good = ReleaseDetails(8, "Song", datetime(2026, 11, 6, tzinfo=UTC), False)
    check = NoveltyCheck(bandcamp_release=lambda _url: good, artist_search=None)
    url = "https://artist.bandcamp.com/album/release"
    assert check("Artist", "Album", url, WEEK) is None
    for bad in [
        ReleaseDetails(3, "Song", good.released_at, False),
        ReleaseDetails(8, "Song", None, False),
        ReleaseDetails(8, "Song", good.released_at, True),
        ReleaseDetails(8, "Song", good.released_at, False, "arquivo declarado"),
        ReleaseDetails(8, "Song", datetime(2000, 1, 1, tzinfo=UTC), False),
    ]:
        check.bandcamp_release = lambda _url, bad=bad: bad
        assert check("Artist", "Album", url, WEEK)
