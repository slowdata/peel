"""The exploratory study cannot turn unknown outcomes into measured wins."""

from __future__ import annotations

import importlib.util
import io
import sqlite3
import sys
import tarfile
from dataclasses import replace
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "album_consensus_shadow",
    Path(__file__).resolve().parents[1] / "scripts/album_consensus_shadow.py",
)
assert SPEC is not None and SPEC.loader is not None
study = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = study
SPEC.loader.exec_module(study)


def candidate(artist: str, *, families: int = 1, affinity: float = 0.5, fresh: bool = True):
    return study.Candidate(
        key=(artist.lower(), "album"),
        artist=artist,
        album="Album",
        families=families,
        primary_family="publication",
        source_tier=0,
        quality=0.5,
        affinity=affinity,
        affinity_tier=1 if affinity >= 0.6 else -1 if affinity <= 0.45 else 0,
        fresh_at=100.0,
        fresh=fresh,
        first_week="2026-W45",
        known_artist=affinity != 0.5,
    )


def test_variants_are_fixed_not_a_retrospective_parameter_search() -> None:
    assert study.MODES == ("baseline", "cap_2", "bonus_020", "bonus_040")
    assert study.BONUSES == {"bonus_020": 0.2, "bonus_040": 0.4}
    assert tuple(study.SNAPSHOTS) == ("2026-W41", "2026-W42", "2026-W43", "2026-W45")
    assert all(
        len(ref) == 40 for ref in (study.BASELINE, study.OUTCOMES, *study.SNAPSHOTS.values())
    )


@pytest.mark.parametrize("families", [1, 2, 3, 100000])
def test_consensus_bonus_is_bounded_for_any_family_count(families: int) -> None:
    bonus = study.consensus_bonus(families, 0.2)
    assert 0 <= bonus < 0.2
    assert study.consensus_bonus(1, 0.2) == 0


def test_zero_families_is_not_a_valid_candidate() -> None:
    with pytest.raises(ValueError):
        study.consensus_bonus(0, 0.2)


def test_bounded_bonus_allows_real_taste_to_compete_with_consensus() -> None:
    known = candidate("Known", affinity=0.9)
    unknown = candidate("Unknown", families=3)
    assert study.choose([known, unknown], "baseline")[0] == unknown
    for mode in study.BONUSES:
        assert study.choose([known, unknown], mode)[0] == known


def test_cap_two_does_not_let_single_source_taste_compete() -> None:
    known = candidate("Known", affinity=0.9)
    unknown = candidate("Unknown", families=2)
    assert study.choose([known, unknown], "cap_2")[0] == unknown


@pytest.mark.parametrize("mode", study.MODES)
def test_unknown_artists_still_follow_consensus_without_a_personal_signal(mode: str) -> None:
    pool = [candidate("One"), candidate("Three", families=3), candidate("Two", families=2)]
    result = study.choose(pool, mode)
    # The cap can tie two and three; every other fixed mode preserves this
    # ordering. None discovers a missing stylistic preference by itself.
    assert result[-1].artist == "One"
    if mode != "cap_2":
        assert [c.artist for c in result] == ["Three", "Two", "One"]


@pytest.mark.parametrize("mode", study.MODES)
def test_freshness_precedes_ranking_and_links_are_not_selection_features(mode: str) -> None:
    fresh = candidate("Fresh")
    pending = candidate("Pending", families=4, affinity=0.99, fresh=False)
    assert study.choose([pending, fresh], mode)[0] == fresh
    annotated = replace(fresh, known_urls=("https://open.spotify.com/album/proof",))
    assert [c.key for c in study.choose([pending, fresh], mode)] == [
        c.key for c in study.choose([pending, annotated], mode)
    ]
    assert "outcome" not in study.Candidate.__dataclass_fields__


def test_candidate_order_is_deterministic_and_every_source_remains_eligible() -> None:
    pool = [candidate(f"Artist {i:02}") for i in range(20)]
    for mode in study.MODES:
        assert study.choose(pool, mode) == study.choose(list(reversed(pool)), mode)
        assert len(study.choose(pool, mode)) == 14


def test_unobserved_alternative_is_unknown_not_a_zero_or_a_success_rate() -> None:
    a, b = candidate("A"), candidate("B")
    result = study.describe([a, b], {a.key: "love"}, "2026-W45")
    assert result["musical_outcomes_known"] == 1
    assert result["positive_outcomes_known"] == 1
    assert result["unknown_or_control_outcomes"] == 1
    assert result["items"][1]["outcome"] is None
    assert not any("rate" in key or "accuracy" in key for key in result)


@pytest.mark.parametrize("label", [None, "ban", "unavailable"])
def test_ordering_metrics_refuse_incomplete_or_control_outcomes(label: str | None) -> None:
    a = candidate("A")
    with pytest.raises(ValueError):
        study.ordering_metrics([a], {a.key: label})


def test_pairs_are_only_positive_versus_nonpositive_and_cannot_discriminate_zero_positives() -> (
    None
):
    a, b, c = candidate("A"), candidate("B"), candidate("C")
    result = study.ordering_metrics([a, b, c], {a.key: "love", b.key: "meh", c.key: "like"})
    assert result["comparable_pairs"] == 2
    assert result["correct_pairs"] == 1
    assert result["pair_accuracy"] == 0.5
    negative = study.ordering_metrics([a, b], {a.key: "dislike", b.key: "meh"})
    assert negative["comparable_pairs"] == 0
    assert negative["pair_accuracy"] is None


def test_snapshot_connection_is_readonly_without_constructor_or_migration(tmp_path: Path) -> None:
    path = tmp_path / "snapshot.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE example (value TEXT)")
    original = path.read_bytes()

    class NeverInitialize:
        def __init__(self, *_):
            raise AssertionError("No constructor/schema migration during the study")

    db = study.open_readonly(path, NeverInitialize, str.lower)
    assert (
        db.conn.execute("SELECT artist_identity_key('Fontaines DC')").fetchone()[0]
        == "fontaines dc"
    )
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        db.conn.execute("INSERT INTO example VALUES ('changed')")
    db.conn.close()
    assert path.read_bytes() == original


def temporal_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        "CREATE TABLE feedback (rated_at TEXT);"
        "CREATE TABLE album_feedback (rated_at TEXT);"
        "CREATE TABLE tracks (added_at TEXT);"
        "CREATE TABLE unmatched (seen_at TEXT);"
        "CREATE TABLE album_mentions (seen_at TEXT, first_seen_at TEXT, last_seen_at TEXT);"
        "CREATE TABLE artist_genres (fetched_at TEXT);"
    )
    return conn


@pytest.mark.parametrize(
    ("table", "column"),
    [
        ("feedback", "rated_at"),
        ("album_feedback", "rated_at"),
        ("tracks", "added_at"),
        ("unmatched", "seen_at"),
        ("album_mentions", "seen_at"),
        ("album_mentions", "first_seen_at"),
        ("album_mentions", "last_seen_at"),
        ("artist_genres", "fetched_at"),
    ],
)
def test_guard_rejects_future_feedback_mentions_tracks_and_genres(table: str, column: str) -> None:
    conn = temporal_db()
    conn.execute(f'INSERT INTO "{table}" ("{column}") VALUES (?)', ("2026-11-08T00:00:00Z",))
    holder = type("ReadConnection", (), {"conn": conn})()
    with pytest.raises(ValueError, match="Future input"):
        study.validate_inputs(holder, "2026-11-07T20:00:00+00:00")
    conn.close()


def test_temporal_guard_accepts_equal_or_earlier_instants_across_timezones() -> None:
    conn = temporal_db()
    conn.execute("INSERT INTO feedback VALUES ('2026-11-07T21:00:00+01:00')")
    holder = type("ReadConnection", (), {"conn": conn})()
    study.validate_inputs(holder, "2026-11-07T20:00:00Z")
    conn.close()


@pytest.mark.parametrize("name", ["/src/peel/evil.py", "src/peel/../../evil.py", "other/evil.py"])
def test_pinned_package_extraction_rejects_paths_outside_peel(tmp_path: Path, name: str) -> None:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w") as tar:
        info = tarfile.TarInfo(name)
        info.size = 1
        tar.addfile(info, io.BytesIO(b"x"))
    with pytest.raises(ValueError, match="Unexpected source path"):
        study.extract_code(output.getvalue(), tmp_path)
    assert not list(tmp_path.rglob("*.py"))
