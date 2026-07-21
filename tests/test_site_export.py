from __future__ import annotations

import json
from pathlib import Path

import pytest

from peel.db import DB
from peel.matcher import normalize
from peel.playlists import canonical_playlist_id
from peel.site_export import (
    build_site_week_payload,
    export_site,
    make_album_resolver,
    playlist_url_from_id,
    spotify_album_search_url,
    week_date_range,
    week_label,
    weeks_to_export,
)


class _FakeSpotify:
    """SpotifyClient mínimo para testar o resolver de álbuns sem rede."""

    def __init__(self, candidates: list[dict] | Exception) -> None:
        self._candidates = candidates

    def search_album(self, artist: str, album: str) -> list[dict]:
        if isinstance(self._candidates, Exception):
            raise self._candidates
        return self._candidates


def test_make_album_resolver_returns_url_on_confident_match() -> None:
    sp = _FakeSpotify(
        [
            {
                "url": "https://open.spotify.com/album/RIGHT",
                "name": "Cry Baby",
                "artists": ["Vince Staples"],
            }
        ]
    )
    resolve = make_album_resolver(sp)
    assert resolve("Vince Staples", "Cry Baby") == "https://open.spotify.com/album/RIGHT"


def test_make_album_resolver_rejects_mismatch() -> None:
    sp = _FakeSpotify(
        [
            {
                "url": "https://open.spotify.com/album/WRONG",
                "name": "Totally Other",
                "artists": ["Someone"],
            }
        ]
    )
    assert make_album_resolver(sp)("Vince Staples", "Cry Baby") is None


def test_make_album_resolver_is_fail_open_on_error() -> None:
    sp = _FakeSpotify(RuntimeError("boom"))
    assert make_album_resolver(sp)("X", "Y") is None


def test_album_resolver_fills_spotify_url_in_payload(tmp_path: Path) -> None:
    db = DB(str(tmp_path / "peel.db"))
    db.init_schema()
    _insert_album_mention(
        db,
        artist="Vince Staples",
        album="Cry Baby",
        source_id="thequietus",
        source_url="https://thequietus.com/x",
        spotify_album_uri=None,
        seen_at="2026-06-10T00:00:00+00:00",
        week="2026-W24",
    )
    resolver = make_album_resolver(
        _FakeSpotify(
            [
                {
                    "url": "https://open.spotify.com/album/ABC",
                    "name": "Cry Baby",
                    "artists": ["Vince Staples"],
                }
            ]
        )
    )
    payload = build_site_week_payload(db, "2026-W24", None, album_resolver=resolver)
    db.close()
    match = next(a for a in payload["albums"] if a["title"] == "Cry Baby")
    assert match["spotify_url"] == "https://open.spotify.com/album/ABC"
    assert match["spotify_match"] == "resolved"


def test_album_resolver_error_falls_back_to_spotify_search(tmp_path: Path) -> None:
    db = DB(str(tmp_path / "peel.db"))
    db.init_schema()
    _insert_album_mention(
        db,
        artist="Vince Staples",
        album="Cry Baby",
        source_id="thequietus",
        source_url="https://thequietus.com/x",
        spotify_album_uri=None,
        seen_at="2026-06-10T00:00:00+00:00",
        week="2026-W24",
    )

    def broken_resolver(_artist: str, _album: str) -> str | None:
        raise RuntimeError("spotify unavailable")

    payload = build_site_week_payload(db, "2026-W24", None, album_resolver=broken_resolver)
    db.close()

    match = next(a for a in payload["albums"] if a["title"] == "Cry Baby")
    assert match["spotify_url"] == "https://open.spotify.com/search/Vince%20Staples%20Cry%20Baby"
    assert match["spotify_match"] == "search"


def test_album_without_exact_match_falls_back_to_spotify_search(tmp_path: Path) -> None:
    db = DB(str(tmp_path / "peel.db"))
    db.init_schema()
    _insert_album_mention(
        db,
        artist="Khun Narin Electric Phin Band",
        album="III",
        source_id="thequietus",
        source_url="https://thequietus.com/x",
        spotify_album_uri=None,
        seen_at="2026-06-10T00:00:00+00:00",
        week="2026-W24",
    )

    payload = build_site_week_payload(db, "2026-W24", None, album_resolver=lambda _a, _b: None)
    db.close()

    match = next(a for a in payload["albums"] if a["title"] == "III")
    assert match["link"] == "https://thequietus.com/x"
    assert match["spotify_url"] == (
        "https://open.spotify.com/search/Khun%20Narin%20Electric%20Phin%20Band%20III"
    )
    assert match["spotify_match"] == "search"


def test_spotify_album_search_url_encodes_special_chars() -> None:
    assert spotify_album_search_url("Lee “Scratch” Perry", "Spatial, No Problem.") == (
        "https://open.spotify.com/search/Lee%20%E2%80%9CScratch%E2%80%9D%20Perry%20"
        "Spatial%2C%20No%20Problem."
    )


def test_spotify_album_search_url_ignores_empty_query() -> None:
    assert spotify_album_search_url(" ", "") is None


def test_export_site_skips_empty_current_week(tmp_path: Path) -> None:
    db = DB(str(tmp_path / "peel.db"))
    db.init_schema()
    _insert_track(
        db,
        uri="spotify:track:aaa",
        source_id="stereogum_new_music",
        artist="Snag",
        title="Unarrest Me",
        added_at="2026-06-10T00:00:00+00:00",
        week="2026-W24",
    )
    site_dir = tmp_path / "site"
    # Semana corrente W25 está vazia (sem faixas) → não deve ser escrita.
    exported = export_site(
        db, site_dir, weeks=2, playlist_id=None, current_week="2026-W25", album_resolver=None
    )
    db.close()
    written = {item.week for item in exported}
    assert "2026-W24" in written
    assert "2026-W25" not in written
    assert not (site_dir / "src" / "data" / "weeks" / "2026-W25.json").exists()


def test_week_helpers() -> None:
    assert week_label("2026-W24") == "Semana 24 · 2026"
    assert week_date_range("2026-W24") == "8 — 14 Jun 2026"
    assert weeks_to_export("2026-W24", 2) == ["2026-W23", "2026-W24"]


def test_canonical_playlist_id_accepts_id_uri_and_url() -> None:
    assert canonical_playlist_id("playlist-id") == "playlist-id"
    assert canonical_playlist_id("spotify:playlist:playlist-id") == "playlist-id"
    assert (
        canonical_playlist_id("https://open.spotify.com/playlist/playlist-id?si=share")
        == "playlist-id"
    )


def test_playlist_url_from_id_accepts_id_uri_and_url() -> None:
    assert playlist_url_from_id(None) is None
    assert playlist_url_from_id("playlist-id") == "https://open.spotify.com/playlist/playlist-id"
    assert playlist_url_from_id("spotify:playlist:abc") == "https://open.spotify.com/playlist/abc"
    assert playlist_url_from_id("https://open.spotify.com/playlist/abc?si=x") == (
        "https://open.spotify.com/playlist/abc?si=x"
    )


def test_build_site_week_payload_ranks_tracks_and_counts_sources(tmp_path: Path) -> None:
    db = DB(str(tmp_path / "peel.db"))
    db.init_schema()
    week = "2026-W24"
    _insert_track(
        db,
        uri="spotify:track:shared",
        source_id="npr_new_music_friday_starting5",
        artist="Artist Shared",
        title="Shared Song",
        added_at="2026-06-10T10:00:00+00:00",
        week=week,
    )
    _insert_track(
        db,
        uri="spotify:track:shared",
        source_id="stereogum_new_music",
        artist="Artist Shared",
        title="Shared Song",
        added_at="2026-06-10T11:00:00+00:00",
        week=week,
    )
    _insert_track(
        db,
        uri="spotify:track:solo",
        source_id="pitchfork_bnt",
        artist="Artist Solo",
        title="Solo Song",
        added_at="2026-06-11T10:00:00+00:00",
        week=week,
    )

    payload = build_site_week_payload(
        db,
        week,
        "https://open.spotify.com/playlist/test",
        source_quality={
            "pitchfork_bnt": (2.0, 100.0),
            "npr_new_music_friday_starting5": (1.0, 10.0),
            "stereogum_new_music": (0.5, 5.0),
        },
    )

    assert payload["week"] == week
    assert payload["label"] == "Semana 24 · 2026"
    assert payload["playlist_url"] == "https://open.spotify.com/playlist/test"
    assert payload["tracks"][0] == {
        "rank": 1,
        "artist": "Artist Shared",
        "title": "Shared Song",
        "source": "NPR",
        "source_count": 2,
        "spotify_url": "https://open.spotify.com/track/shared",
    }
    assert payload["tracks"][1]["source"] == "Pitchfork"
    assert payload["sources"] == [
        {"name": "NPR", "url": "https://www.npr.org/music"},
        {"name": "Pitchfork", "url": "https://pitchfork.com"},
    ]


def test_finalized_snapshot_overrides_editorial_ranking_and_survives_reexport(
    tmp_path: Path,
) -> None:
    db = DB(str(tmp_path / "peel.db"))
    db.init_schema()
    week = "2026-W24"
    _insert_track(
        db,
        uri="spotify:track:editorial",
        source_id="high",
        artist="Editorial Artist",
        title="Editorial Track",
        added_at="2026-06-10T10:00:00+00:00",
        week=week,
    )
    _insert_track(
        db,
        uri="spotify:track:confirmed",
        source_id="low",
        artist="Confirmed Artist",
        title="Confirmed Track",
        added_at="2026-06-09T10:00:00+00:00",
        week=week,
    )
    db.replace_finalized_week_tracks(
        week,
        "weekly",
        ["spotify:track:confirmed"],
    )
    db.replace_album_queue(week, [])
    # Uma menção posterior não pode alterar metadata da semana já finalizada.
    _insert_track(
        db,
        uri="spotify:track:confirmed",
        source_id="future",
        artist="Changed Artist",
        title="Changed Track",
        added_at="2026-06-16T10:00:00+00:00",
        week="2026-W25",
    )

    initial = build_site_week_payload(
        db,
        week,
        None,
        source_quality={"high": (2.0, 100.0), "low": (0.0, 0.0)},
        finalized_playlist_id="https://open.spotify.com/playlist/weekly?si=share",
    )
    reexport = build_site_week_payload(
        db,
        week,
        None,
        source_quality={"high": (-2.0, -100.0), "low": (2.0, 100.0)},
        finalized_playlist_id="spotify:playlist:weekly",
    )

    assert [track["spotify_url"] for track in initial["tracks"]] == [
        "https://open.spotify.com/track/confirmed"
    ]
    assert [track["spotify_url"] for track in reexport["tracks"]] == [
        "https://open.spotify.com/track/confirmed"
    ]
    assert initial["tracks"][0]["artist"] == "Confirmed Artist"
    assert initial["tracks"][0]["title"] == "Confirmed Track"
    assert initial["tracks"][0]["source_count"] == 1

    site_dir = tmp_path / "site"
    export_site(
        db,
        site_dir,
        weeks=1,
        playlist_id="https://open.spotify.com/playlist/weekly?si=share",
        current_week=week,
    )
    payload = json.loads((site_dir / "src" / "data" / "weeks" / f"{week}.json").read_text())
    assert [track["spotify_url"] for track in payload["tracks"]] == [
        "https://open.spotify.com/track/confirmed"
    ]
    db.close()


def test_week_without_finalized_snapshot_keeps_editorial_fallback(tmp_path: Path) -> None:
    db = DB(str(tmp_path / "peel.db"))
    db.init_schema()
    week = "2026-W24"
    _insert_track(
        db,
        uri="spotify:track:editorial",
        source_id="high",
        artist="Editorial Artist",
        title="Editorial Track",
        added_at="2026-06-10T10:00:00+00:00",
        week=week,
    )
    _insert_track(
        db,
        uri="spotify:track:other",
        source_id="low",
        artist="Other Artist",
        title="Other Track",
        added_at="2026-06-09T10:00:00+00:00",
        week=week,
    )

    payload = build_site_week_payload(
        db,
        week,
        None,
        source_quality={"high": (2.0, 100.0), "low": (0.0, 0.0)},
        finalized_playlist_id="spotify:playlist:weekly",
    )

    assert payload["tracks"][0]["spotify_url"] == "https://open.spotify.com/track/editorial"
    db.close()


def test_finalized_snapshot_missing_track_metadata_fails_clearly(tmp_path: Path) -> None:
    db = DB(str(tmp_path / "peel.db"))
    db.init_schema()
    db.replace_finalized_week_tracks(
        "2026-W24",
        "spotify:playlist:weekly",
        ["spotify:track:missing"],
    )

    with pytest.raises(ValueError, match="without track metadata"):
        build_site_week_payload(
            db,
            "2026-W24",
            None,
            finalized_playlist_id="spotify:playlist:weekly",
        )
    db.close()


def test_export_site_writes_empty_finalized_snapshot(tmp_path: Path) -> None:
    db = DB(str(tmp_path / "peel.db"))
    db.init_schema()
    db.replace_finalized_week_tracks("2026-W24", "weekly", [])

    exported = export_site(
        db,
        tmp_path / "site",
        weeks=1,
        playlist_id="spotify:playlist:weekly",
        current_week="2026-W24",
    )

    assert [item.week for item in exported] == ["2026-W24"]
    payload = json.loads(
        (tmp_path / "site" / "src" / "data" / "weeks" / "2026-W24.json").read_text()
    )
    assert payload["tracks"] == []
    db.close()


def test_build_site_week_payload_limits_tracks_to_seven(tmp_path: Path) -> None:
    db = DB(str(tmp_path / "peel.db"))
    db.init_schema()
    week = "2026-W24"
    for index in range(8):
        _insert_track(
            db,
            uri=f"spotify:track:{index}",
            source_id="stereogum_new_music",
            artist=f"Artist {index}",
            title=f"Track {index}",
            added_at=f"2026-06-10T1{index}:00:00+00:00",
            week=week,
        )

    payload = build_site_week_payload(db, week, None, source_quality={})

    assert len(payload["tracks"]) == 7
    assert payload["tracks"][0]["title"] == "Track 7"
    assert {track["title"] for track in payload["tracks"]} == {
        f"Track {index}" for index in range(1, 8)
    }


def test_build_site_week_payload_filters_banned_tracks(tmp_path: Path) -> None:
    db = DB(str(tmp_path / "peel.db"))
    db.init_schema()
    week = "2026-W24"
    _insert_track(
        db,
        uri="spotify:track:banned",
        source_id="npr_new_music_friday_starting5",
        artist="Artist",
        title="Banned Song",
        added_at="2026-06-10T10:00:00+00:00",
        week=week,
    )
    db.upsert_feedback("spotify:track:banned", "ban", None)

    payload = build_site_week_payload(db, week, None, source_quality={})

    assert payload["tracks"] == []


def test_build_site_week_payload_exports_album_recommendations(tmp_path: Path) -> None:
    db = DB(str(tmp_path / "peel.db"))
    db.init_schema()
    week = "2026-W24"
    _insert_album_mention(
        db,
        artist="Album Artist",
        album="Consensus Album",
        source_id="aquarium_drunkard",
        source_url="https://aquarium.example/review",
        spotify_album_uri="spotify:album:abc123",
        seen_at="2026-06-10T10:00:00+00:00",
        week=week,
    )
    _insert_album_mention(
        db,
        artist="Album Artist",
        album="Consensus Album",
        source_id="pitchfork_best_albums",
        source_url="https://pitchfork.example/review",
        spotify_album_uri=None,
        seen_at="2026-06-10T11:00:00+00:00",
        week=week,
    )

    payload = build_site_week_payload(
        db,
        week,
        None,
        source_quality={"pitchfork_best_albums": (1.0, 20.0), "aquarium_drunkard": (0.0, 0.0)},
    )

    assert payload["albums"][0] == {
        "rank": 1,
        "artist": "Album Artist",
        "title": "Consensus Album",
        "source": "Pitchfork, Aquarium Drunkard",
        "source_count": 2,
        "link": "https://pitchfork.example/review",
        "spotify_url": "https://open.spotify.com/album/abc123",
        "spotify_match": "direct",
    }
    assert payload["sources"] == [
        {"name": "Pitchfork", "url": "https://pitchfork.com"},
        {"name": "Aquarium Drunkard", "url": "https://aquariumdrunkard.com"},
    ]


def test_build_site_week_payload_empty_week_is_valid(tmp_path: Path) -> None:
    db = DB(str(tmp_path / "peel.db"))
    db.init_schema()

    payload = build_site_week_payload(db, "2026-W30", None, source_quality={})

    assert payload == {
        "week": "2026-W30",
        "start_date": "2026-07-20",
        "end_date": "2026-07-26",
        "label": "Semana 30 · 2026",
        "date_range": "20 — 26 Jul 2026",
        "playlist_url": None,
        "tracks": [],
        "albums": [],
        "sources": [],
    }


def test_export_preserves_published_albums_without_snapshot_and_snapshot_supersedes(
    tmp_path: Path,
) -> None:
    db = DB(str(tmp_path / "peel.db"))
    db.init_schema()
    week = "2026-W24"
    _insert_track(
        db,
        uri="spotify:track:x",
        source_id="stereogum_new_music",
        artist="Artist",
        title="Track",
        added_at="2026-06-10T00:00:00+00:00",
        week=week,
    )
    path = tmp_path / "site" / "src" / "data" / "weeks" / f"{week}.json"
    path.parent.mkdir(parents=True)
    preserved = [{"rank": 9, "artist": "Frozen", "title": "Album", "source": "Pitchfork"}]
    path.write_text(json.dumps({"albums": preserved}), encoding="utf-8")
    resolver_calls: list[tuple[str, str]] = []

    def resolver(artist: str, album: str) -> str | None:
        resolver_calls.append((artist, album))
        return None

    export_site(
        db, tmp_path / "site", weeks=1, playlist_id=None, current_week=week, album_resolver=resolver
    )
    payload = json.loads(path.read_text())
    assert payload["albums"] == preserved
    assert payload["sources"] == [
        {"name": "Stereogum", "url": "https://www.stereogum.com"},
        {"name": "Pitchfork", "url": "https://pitchfork.com"},
    ]
    assert resolver_calls == []
    db.replace_album_queue(week, [])
    export_site(db, tmp_path / "site", weeks=1, playlist_id=None, current_week=week)
    assert json.loads(path.read_text())["albums"] == []
    db.close()


def test_current_week_tracks_without_album_snapshot_or_artifact_fails_closed(
    tmp_path: Path,
) -> None:
    db = DB(str(tmp_path / "peel.db"))
    db.init_schema()
    _insert_track(
        db,
        uri="spotify:track:x",
        source_id="stereogum_new_music",
        artist="Artist",
        title="Track",
        added_at="2026-06-10T00:00:00+00:00",
        week="2026-W24",
    )
    with pytest.raises(ValueError, match="confirmed album snapshot"):
        export_site(db, tmp_path / "site", weeks=1, playlist_id=None, current_week="2026-W24")
    db.close()


def test_export_site_writes_idempotent_json_files(tmp_path: Path) -> None:
    db = DB(str(tmp_path / "peel.db"))
    db.init_schema()
    for week in ("2026-W23", "2026-W24"):
        _insert_track(
            db,
            uri=f"spotify:track:{week}",
            source_id="stereogum_new_music",
            artist="Snag",
            title="Unarrest Me",
            added_at=f"{week[:4]}-06-10T00:00:00+00:00",
            week=week,
        )
    site_dir = tmp_path / "site"
    db.replace_album_queue("2026-W24", [])

    exported = export_site(
        db,
        site_dir,
        weeks=2,
        playlist_id="playlist-id",
        current_week="2026-W24",
    )
    first_content = [item.path.read_text(encoding="utf-8") for item in exported]
    exported_again = export_site(
        db,
        site_dir,
        weeks=2,
        playlist_id="playlist-id",
        current_week="2026-W24",
    )
    second_content = [item.path.read_text(encoding="utf-8") for item in exported_again]

    assert [item.week for item in exported] == ["2026-W23", "2026-W24"]
    assert first_content == second_content
    data = json.loads((site_dir / "src" / "data" / "weeks" / "2026-W24.json").read_text())
    assert data["playlist_url"] == "https://open.spotify.com/playlist/playlist-id"


def _insert_track(
    db: DB,
    *,
    uri: str,
    source_id: str,
    artist: str,
    title: str,
    added_at: str,
    week: str,
) -> None:
    db.conn.execute(
        """
        INSERT INTO tracks
        (spotify_uri, source_id, artist, title, source_url, added_at, added_at_week)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (uri, source_id, artist, title, None, added_at, week),
    )
    db.conn.commit()


def _insert_album_mention(
    db: DB,
    *,
    artist: str,
    album: str,
    source_id: str,
    source_url: str | None,
    spotify_album_uri: str | None,
    seen_at: str,
    week: str,
) -> None:
    db.conn.execute(
        """
        INSERT INTO album_mentions
        (artist, album, artist_key, album_key, source_id, source_url,
         spotify_album_uri, seen_at, added_at_week)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            artist,
            album,
            normalize(artist),
            normalize(album),
            source_id,
            source_url,
            spotify_album_uri,
            seen_at,
            week,
        ),
    )
    db.conn.commit()
