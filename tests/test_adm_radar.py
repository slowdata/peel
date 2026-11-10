from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from typer.testing import CliRunner

from peel import adm_radar as radar
from peel import cli

NOW = datetime(2026, 11, 10, 12, tzinfo=UTC)
FIXTURES = Path(__file__).parent / "fixtures"
DETAIL = (FIXTURES / "adm_album.html").read_text()
LISTING = (FIXTURES / "adm_listing.html").read_text()
PISS = radar.Reference(
    artist="PISS",
    album="You Made Me / Skin on Linen",
    url=radar.BASE_URL + "/review/15767/PISS.aspx",
    discovered_via=(radar.LISTINGS[0],),
)


def album_html(artist="Fontaines D.C.", album="New Album", released="30/10/2026"):
    return (
        DETAIL.replace("<h2>PISS</h2>", f"<h2>{artist}</h2>")
        .replace("<h3>You Made Me / Skin on Linen</h3>", f"<h3>{album}</h3>")
        .replace("30/10/2026", released)
    )


def listing(*items):
    return (
        '<div class="article"><ol class="main_chart">'
        + "".join(
            f'<li><div class="album_info"><h4>{artist}</h4>'
            f'<h5><a href="/review/{i}/Album.aspx">{album}</a></h5></div></li>'
            for i, artist, album in items
        )
        + "</ol></div>"
    )


def collect(pages=None, limit=30):
    pages = pages or {radar.LISTINGS[0]: LISTING, radar.LISTINGS[1]: listing()}
    calls = []

    def fetch(url):
        calls.append(url)
        if url in pages:
            value = pages[url]
            if isinstance(value, Exception):
                raise value
            return value
        if "/review/1/" in url:
            return album_html()
        return album_html("Unknown Artist", "Second Album")

    return radar.collect_radar(now=NOW, fetch=fetch, limit=limit), calls


def local_db(path, *, feedback=()):
    with sqlite3.connect(path) as db:
        db.executescript("""
            CREATE TABLE albums (artist TEXT, album TEXT, source_id TEXT);
            CREATE TABLE album_mentions (artist_key TEXT, album_key TEXT, source_id TEXT);
            CREATE TABLE album_feedback (
                artist_key TEXT, album_key TEXT, label TEXT, rated_at TEXT
            );
            INSERT INTO albums VALUES ('Fontaines DC', 'New Album', 'pitchfork-albums');
            INSERT INTO album_mentions VALUES ('fontaines d.c.', 'new album', 'treble-albums');
        """)
        db.executemany("INSERT INTO album_feedback VALUES (?, ?, ?, ?)", feedback)


def test_listing_ignores_sidebar_and_extracts_primary_cards():
    refs = radar.parse_listing(LISTING, radar.LISTINGS[0])
    assert [item.artist for item in refs] == ["Fontaines D.C.", "Unknown Artist"]
    assert refs[0].url == radar.BASE_URL + "/review/1/First.aspx"
    assert refs[0].discovered_via == (radar.LISTINGS[0],)


@pytest.mark.parametrize(
    "html",
    [
        "Runtime Error",
        '<div class="article"></div>',
        '<div class="article"><ol class="main_chart"><li>broken</li></ol></div>',
    ],
)
def test_broken_listing_is_not_a_successful_empty_feed(html):
    with pytest.raises(radar.RadarError):
        radar.parse_listing(html, radar.LISTINGS[0])


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost/review/1/Test.aspx",
        "//127.0.0.1/review/1/Test.aspx",
        "https://www.anydecentmusic.com.evil.test/review/1/Test.aspx",
        "http://user:password@www.anydecentmusic.com/review/1/Test.aspx",
        "/about.aspx",
        "javascript:alert(1)",
        "http://www.anydecentmusic.com:8080/review/1/Test.aspx",
    ],
)
def test_only_public_adm_routes_can_be_fetched(url):
    with pytest.raises(radar.RadarError):
        radar.adm_url(url)


def test_detail_keeps_real_metadata_without_interpreting_added_or_internal_age():
    item = radar.parse_album(DETAIL, PISS, NOW)
    assert item.artist == "PISS"
    assert item.release_uk == item.release_us == item.release_date == "2026-10-30"
    assert item.status == "recent"
    assert item.adm_rating_10 == 8.5
    assert len(item.reviews) == 2  # fewer than five reviews is fine
    assert item.reviews[0].publication == "Far Out"
    assert item.reviews[0].rating_10 == 10.0
    assert item.reviews[0].url.startswith("https://faroutmagazine.co.uk/")
    assert item.reviews[0].published_at is None  # data_age=147167 is NOT a date
    assert "?" not in item.listen_url
    assert item.known_to_peel is None


@pytest.mark.parametrize(
    ("date", "status"),
    [
        ("06/10/2026", "recent"),
        ("05/10/2026", "old"),
        ("10/11/2026", "recent"),
        ("11/11/2026", "future"),
        ("TBC", "unconfirmed"),
        ("11/2026", "unconfirmed"),
        ("2026", "unconfirmed"),
        ("31/11/2026", "unconfirmed"),
    ],
)
def test_release_dates_and_35_day_boundary(date, status):
    item = radar.parse_album(DETAIL.replace("30/10/2026", date), PISS, NOW)
    assert item.status == status
    if status != "recent":
        assert item.reason


def test_earliest_regional_release_prevents_rejuvenation_by_later_edition():
    html = DETAIL.replace(
        "<dt>US Release date</dt><dd>30/10/2026", "<dt>US Release date</dt><dd>30/10/2006"
    )
    item = radar.parse_album(html, PISS, NOW)
    assert item.status == "old"
    assert item.release_date == "2006-10-30"
    assert item.release_uk == "2026-10-30"


def test_explicit_archive_title_is_not_a_recent_new_album():
    title = "You Made Me / Skin on Linen (2026 Remaster)"
    reference = PISS.model_copy(update={"album": title})
    item = radar.parse_album(DETAIL.replace(PISS.album, title), reference, NOW)
    assert item.status == "archival"


def test_missing_dates_do_not_fall_back_to_added():
    item = radar.parse_album(DETAIL.replace("30/10/2026", ""), PISS, NOW)
    assert item.release_date is None
    assert item.status == "unconfirmed"


@pytest.mark.parametrize("score", ["NaN", "Inf", "11", "-1", "tbc"])
def test_invalid_critic_ratings_are_unknown_not_zero(score):
    item = radar.parse_album(
        DETAIL.replace('class="score">8.5', f'class="score">{score}'), PISS, NOW
    )
    assert item.adm_rating_10 is None
    assert item.status == "recent"


def test_zero_reviews_is_not_a_consensus_threshold():
    html = (
        DETAIL[: DETAIL.index('<ol id="album_reviews_list"')]
        + '<ol id="album_reviews_list"></ol></div>'
    )
    assert radar.parse_album(html, PISS, NOW).reviews == ()
    assert radar.parse_album(html, PISS, NOW).status == "recent"
    assert (
        radar.parse_album(
            html.replace('id="album_reviews_list"', 'id="unknown"'), PISS, NOW
        ).reviews
        is None
    )


def test_mismatched_detail_identity_is_rejected():
    with pytest.raises(radar.RadarError):
        radar.parse_album(DETAIL.replace("<h2>PISS</h2>", "<h2>Another Artist</h2>"), PISS, NOW)


def test_shared_listing_url_is_requested_once_and_both_origins_survive():
    result, calls = collect(
        {radar.LISTINGS[0]: LISTING, radar.LISTINGS[1]: listing((1, "Fontaines DC", "New Album"))}
    )
    assert result.discovered_count == 2
    assert result.details_requested == 2
    assert len(calls) == len(set(calls)) == 4
    assert result.albums[0].discovered_via == radar.LISTINGS
    assert not result.partial


def test_bounded_requests_read_unlisted_too_not_just_the_main_chart():
    result, calls = collect(
        {
            radar.LISTINGS[0]: LISTING,
            radar.LISTINGS[1]: listing((3, "Unknown Artist", "Second Album")),
        },
        limit=2,
    )
    assert result.details_requested == 2
    assert result.discovered_count == 3
    assert result.truncated and result.partial
    assert len(calls) == 4
    assert "/review/3/" in calls[-1]
    assert all(url.startswith(radar.BASE_URL) for url in calls)


def test_artist_aliases_do_not_produce_two_album_identities():
    result, _ = collect(
        {
            radar.LISTINGS[0]: listing((1, "Fontaines D.C.", "New Album")),
            radar.LISTINGS[1]: listing((4, "Fontaines DC", "New Album")),
            radar.BASE_URL + "/review/4/Album.aspx": album_html("Fontaines DC"),
        }
    )
    assert len(result.albums) == 1
    assert len(result.albums[0].adm_urls) == 2
    assert len(result.albums[0].reviews) == 2  # not doubled as independent votes


def test_partial_listing_and_detail_failures_are_visible():
    result, _ = collect(
        {
            radar.LISTINGS[0]: httpx.ReadTimeout("listing timeout"),
            radar.LISTINGS[1]: listing(
                (1, "Fontaines DC", "New Album"), (2, "Unknown Artist", "Second Album")
            ),
            radar.BASE_URL + "/review/2/Album.aspx": "Runtime Error",
        }
    )
    assert result.partial
    assert len(result.errors) == 2
    assert [item.status for item in result.albums] == ["recent", "error"]


def test_throttle_stops_further_detail_requests():
    url = radar.BASE_URL + "/review/1/First.aspx"
    request = httpx.Request("GET", url)
    response = httpx.Response(429, request=request)
    failure = httpx.HTTPStatusError("rate limited", request=request, response=response)
    result, calls = collect(
        {radar.LISTINGS[0]: LISTING, radar.LISTINGS[1]: listing(), url: failure}
    )
    assert len(calls) == 3
    assert result.details_requested == 1
    assert result.partial and result.truncated


def test_explicit_archive_edition_does_not_hide_the_original_album():
    title = "New Album (2026 Remaster)"
    result, _ = collect(
        {
            radar.LISTINGS[0]: listing((1, "Fontaines D.C.", "New Album")),
            radar.LISTINGS[1]: listing((3, "Fontaines DC", title)),
            radar.BASE_URL + "/review/3/Album.aspx": album_html("Fontaines DC", title),
        }
    )
    assert [item.status for item in result.albums] == ["recent", "archival"]


def test_duplicate_original_review_is_not_an_extra_vote():
    duplicate = (
        '<li class="review_item">'
        + DETAIL.split('<li class="review_item">')[1].split("</li>")[0]
        + "</li>"
    )
    item = radar.parse_album(DETAIL.replace("</ol>", duplicate + "</ol>"), PISS, NOW)
    assert len(item.reviews) == 2


def test_both_unreadable_listings_fail_instead_of_empty_success():
    with pytest.raises(radar.RadarError):
        collect({url: "Runtime Error" for url in radar.LISTINGS})


def test_circuit_breaker_bounds_repeated_failures():
    pages = {
        radar.LISTINGS[0]: listing(*[(i, f"Artist {i}", f"Album {i}") for i in range(1, 8)]),
        radar.LISTINGS[1]: listing(),
    }
    pages.update(
        {
            radar.BASE_URL + f"/review/{i}/Album.aspx": httpx.ReadTimeout("offline")
            for i in range(1, 8)
        }
    )
    result, calls = collect(pages)
    assert result.details_requested == 3
    assert len(calls) == 5
    assert result.truncated and result.partial
    assert "interrompida" in result.errors[-1]


def test_redirects_are_validated_before_requesting_the_target():
    calls = []

    def handler(request):
        calls.append(str(request.url))
        assert request.method == "GET"
        return httpx.Response(302, headers={"location": "http://127.0.0.1/private"})

    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as client,
        pytest.raises(radar.RadarError),
    ):
        radar._get_page(client, PISS.url)
    assert calls == [PISS.url]


def test_redirect_budget_is_bounded():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(302, headers={"location": PISS.url})

    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as client,
        pytest.raises(radar.RadarError, match="budget"),
    ):
        radar._get_page(client, PISS.url)
    assert len(calls) == 3


@pytest.mark.parametrize(
    "label", ["love", "like", "meh", "dislike", "hate", "ban", "unavailable", "skip"]
)
def test_local_aliases_share_latest_feedback_without_writing_or_changing_meaning(tmp_path, label):
    path = tmp_path / "state.db"
    local_db(
        path,
        feedback=[
            ("fontaines d.c.", "new album", "ban", "2026-11-09T12:00:00+00:00"),
            ("fontaines dc", "new album", label, "2026-11-09T12:00:00.100000+00:00"),
        ],
    )
    before = path.read_bytes()
    result, _ = collect()
    result = radar.annotate_local(result, path, now=NOW)
    assert path.read_bytes() == before
    assert result.local_requested and result.local_available
    assert result.albums[0].known_to_peel is True
    assert result.albums[0].feedback_label == label
    assert result.albums[0].local_sources == ("pitchfork-albums", "treble-albums")
    assert result.albums[1].known_to_peel is False
    assert result.albums[1].feedback_label is None


def test_missing_local_db_is_unknown_and_is_never_created(tmp_path):
    path = tmp_path / "missing" / "state.db"
    result, _ = collect()
    result = radar.annotate_local(result, path)
    assert not path.parent.exists()
    assert not result.local_available and result.local_requested and result.partial
    assert all(item.known_to_peel is None for item in result.albums)
    assert result.local_error


def test_failed_local_refresh_clears_stale_annotations(tmp_path):
    path = tmp_path / "state.db"
    local_db(path)
    result = radar.annotate_local(collect()[0], path, now=NOW)
    assert result.albums[0].known_to_peel is True
    result = radar.annotate_local(result, tmp_path / "missing.db", now=NOW)
    assert not result.local_available
    assert result.local_observed_at is None
    assert all(item.known_to_peel is None and not item.local_sources for item in result.albums)


def test_ban_only_breaks_an_exact_timestamp_tie(tmp_path):
    path = tmp_path / "state.db"
    local_db(
        path,
        feedback=[
            ("fontaines d.c.", "new album", "ban", NOW.isoformat()),
            ("fontaines dc", "new album", "hate", NOW.isoformat()),
        ],
    )
    result = radar.annotate_local(collect()[0], path, now=NOW)
    assert result.albums[0].feedback_label == "ban"


def test_legacy_schema_is_not_migrated(tmp_path):
    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE albums (artist TEXT)")
    before = path.read_bytes()
    result = radar.annotate_local(collect()[0], path)
    assert path.read_bytes() == before
    assert not result.local_available
    assert result.local_error


def test_snapshot_is_explicit_dated_json_and_cannot_overwrite(tmp_path):
    result, _ = collect()
    target = tmp_path / "adm-20261110.json"
    radar.save_snapshot(result, target)
    payload = json.loads(target.read_text())
    assert payload["observed_at"] == NOW.isoformat()
    assert payload["schema_version"] == 1
    assert payload["partial"] is False
    before = target.read_bytes()
    with pytest.raises(FileExistsError):
        radar.save_snapshot(result, target)
    assert target.read_bytes() == before


def test_snapshot_blocks_canonical_state_and_symlink_parents(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(state, target_is_directory=True)
    for target in (state / "new.json", alias / "new.json"):
        with pytest.raises(ValueError, match="canónico"):
            radar.save_snapshot(collect()[0], target, protected_roots=(state,))
    assert not list(state.iterdir())
    with pytest.raises(ValueError, match="canónico"):
        radar.snapshot_destination(cli.PROJECT_ROOT / "data" / "unsafe-new.json")


def test_cli_has_no_sync_schema_or_service_side_effects_and_json_is_clean(tmp_path, monkeypatch):
    path = tmp_path / "state.db"
    local_db(path)
    before = path.read_bytes()
    result, _ = collect()
    calls = []

    def fake_collect(**kwargs):
        calls.append(kwargs)
        return result

    def forbidden(*args, **kwargs):
        pytest.fail("read-only radar invoked a state/service write path")

    monkeypatch.setattr(cli, "settings", SimpleNamespace(db_path=str(path)))
    monkeypatch.setattr(radar, "collect_radar", fake_collect)
    for name in ("_auto_sync_state", "DB", "SpotifyClient", "run_pipeline", "export_site"):
        monkeypatch.setattr(cli, name, forbidden)
    snapshot = tmp_path / "observation.json"
    response = CliRunner().invoke(cli.app, ["radar", "--json", "--snapshot", str(snapshot)])
    assert response.exit_code == 0, response.output
    payload = json.loads(response.output)
    assert json.loads(snapshot.read_text()) == payload
    assert payload["local_available"]
    assert path.read_bytes() == before
    assert calls == [{"limit": 30}]
    assert set(p.name for p in tmp_path.iterdir()) == {"state.db", "observation.json"}


def test_cli_refuses_bad_snapshot_before_network_access(tmp_path, monkeypatch):
    project = tmp_path / "project"
    (project / "data").mkdir(parents=True)
    monkeypatch.setattr(cli, "PROJECT_ROOT", project)
    monkeypatch.setattr(
        cli, "settings", SimpleNamespace(db_path=str(project / "data" / "state.db"))
    )
    monkeypatch.setattr(
        radar, "collect_radar", lambda **kwargs: pytest.fail("network before guard")
    )
    response = CliRunner().invoke(
        cli.app, ["radar", "--snapshot", str(project / "data" / "new.json")]
    )
    assert response.exit_code == 2
    assert not (project / "data" / "new.json").exists()


def test_cli_no_local_does_not_touch_database_and_default_hides_old_rows(tmp_path, monkeypatch):
    result, _ = collect()
    old = result.albums[1].model_copy(update={"status": "old", "reason": "too old"})
    result = result.model_copy(update={"albums": (result.albums[0], old)})
    monkeypatch.setattr(radar, "collect_radar", lambda **kwargs: result)
    monkeypatch.setattr(radar, "annotate_local", lambda *args: pytest.fail("local read disabled"))
    monkeypatch.setattr(cli, "settings", SimpleNamespace(db_path=str(tmp_path / "absent.db")))
    runner = CliRunner()
    response = runner.invoke(cli.app, ["radar", "--no-local"])
    assert response.exit_code == 0
    assert "Fontaines" in response.output
    assert "Unknown Artist" not in response.output
    assert "Unknown Artist" in runner.invoke(cli.app, ["radar", "--no-local", "--all"]).output
    assert not list(tmp_path.iterdir())
