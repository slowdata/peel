"""Offline, fixed-variant album consensus study; never edits live Peel state.

Run with the project's locked Python environment:
    uv run --frozen python scripts/album_consensus_shadow.py > /tmp/consensus.json

The code package, historical DBs and evaluation outcomes all come from pinned
Git commits. Only disposable temporary files are written by this script.
See docs/analysis/2026-11-09-album-consensus-plan.md for the exploratory design.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

BASELINE = "dba7228630db8c883669d87495afcd95635eab48"
OUTCOMES = "68e49f236c4cae4f353a79141b007ef06a581b52"
SNAPSHOTS = {
    "2026-W41": "d89c442416986134a108ac01d9b322b1b5eaa702",
    "2026-W42": "b5d12d6f37e414b0c5437d6b0fb80a5e690a3173",
    "2026-W43": "85f53c8f3d189b3dfbf1d1b920280f7529695cbd",
    "2026-W45": "5f9ed2ef694d1b5e8f7b631ed8bbd13b7a67caaf",
}
MODES = ("baseline", "cap_2", "bonus_020", "bonus_040")
BONUSES = {"bonus_020": 0.20, "bonus_040": 0.40}
# The W45 refresh overwrote its initial queue; preserve the earlier persisted
# listening-link proof (not a fifth outcome week or an input to ranking).
EXTRA_LINK_PROOFS = {"2026-W45": ("041e31233611b9288956f4148c1ddc46b311df91",)}
POSITIVE = {"love", "like"}
MUSICAL = {"love", "like", "meh", "dislike", "hate", "skip"}


@dataclass(frozen=True)
class Candidate:
    # Outcome labels and current feedback deliberately do not enter this type.
    key: tuple[str, str]
    artist: str
    album: str
    families: int
    primary_family: str
    source_tier: int
    quality: float
    affinity: float
    affinity_tier: int
    fresh_at: float
    fresh: bool
    first_week: str
    known_artist: bool
    previous_queue: bool = False
    known_urls: tuple[str, ...] = ()


def consensus_bonus(families: int, maximum: float) -> float:
    if families < 1:
        raise ValueError("A candidate must have at least one publication family")
    return maximum * (1 - 1 / families)


def choose(candidates: list[Candidate], mode: str, limit: int = 14) -> list[Candidate]:
    """Same freshness/diversity/ties for every mode; no outcome access."""
    if mode not in MODES:
        raise ValueError(mode)
    selected: list[Candidate] = []
    counts: Counter[str] = Counter()
    for fresh in (True, False):
        remaining = [c for c in candidates if c.fresh == fresh]
        while remaining and len(selected) < limit:

            def key(c: Candidate) -> tuple:
                repeats = counts[c.primary_family]
                adjusted = c.quality - 0.05 * repeats / (repeats + 1)
                ties = (-adjusted, c.source_tier, -c.fresh_at, c.artist.lower(), c.album.lower())
                if mode in BONUSES:
                    personal = c.affinity + consensus_bonus(c.families, BONUSES[mode])
                    return (-personal, *ties)
                consensus = min(c.families, 2) if mode == "cap_2" else c.families
                return (-consensus, -c.affinity_tier, *ties)

            chosen = min(remaining, key=key)
            remaining.remove(chosen)
            selected.append(chosen)
            counts[chosen.primary_family] += 1
    return selected


def ordering_metrics(items: list[Candidate], outcomes: dict[tuple[str, str], str]) -> dict:
    """Metrics for a fixed, fully observed set only, never for unknown picks."""
    labels = [outcomes.get(c.key) for c in items]
    if any(label not in MUSICAL for label in labels):
        raise ValueError("Ordering metrics require musical outcomes for the entire fixed subset")
    positive = [label in POSITIVE for label in labels]
    pairs = sum(a != b for i, a in enumerate(positive) for b in positive[i + 1 :])
    correct = sum(a and not b for i, a in enumerate(positive) for b in positive[i + 1 :])
    return {
        "n": len(items),
        "positive": sum(positive),
        "top3_positive": sum(positive[:3]),
        "top3_known_artists": sum(c.known_artist for c in items[:3]),
        "comparable_pairs": pairs,
        "correct_pairs": correct,
        "pair_accuracy": correct / pairs if pairs else None,
    }


def describe(items: list[Candidate], outcomes: dict, week: str) -> dict:
    """Keep unknown/control outcomes visible; do not estimate selection accuracy."""
    musical = [outcomes[c.key] for c in items if outcomes.get(c.key) in MUSICAL]
    urls = [url for c in items for url in c.known_urls]
    return {
        "candidate_count": len(items),
        "known_artist_count": sum(c.known_artist for c in items),
        "first_discovered_this_week": sum(c.first_week == week for c in items),
        "previous_queue_count": sum(c.previous_queue for c in items),
        "known_playable_count": sum(bool(c.known_urls) for c in items),
        "missing_playability_proof": sum(not c.known_urls for c in items),
        "known_link_collisions": len(urls) - len(set(urls)),
        "musical_outcomes_known": len(musical),
        "positive_outcomes_known": sum(label in POSITIVE for label in musical),
        "unknown_or_control_outcomes": len(items) - len(musical),
        "items": [
            {
                "artist": c.artist,
                "album": c.album,
                "families": c.families,
                "affinity": c.affinity,
                "known_artist": c.known_artist,
                "fresh_mention": c.fresh,
                "first_discovered_week": c.first_week,
                "previous_queue": c.previous_queue,
                "known_urls_at_delivery": c.known_urls,
                "outcome": outcomes.get(c.key),
            }
            for c in items
        ],
    }


def git(repo: Path, *args: str) -> bytes:
    return subprocess.check_output(["git", "-C", str(repo), *args])


def extract_code(archive: bytes, root: Path) -> None:
    """Extract regular Python files only, never symlinks or archive paths."""
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        for member in tar.getmembers():
            path = Path(member.name)
            if not member.isfile() or path.suffix != ".py":
                continue
            if path.is_absolute() or ".." in path.parts or path.parts[:2] != ("src", "peel"):
                raise ValueError(f"Unexpected source path: {member.name}")
            source = tar.extractfile(member)
            if source is None:
                raise ValueError(member.name)
            target = root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(source.read())


def open_readonly(path: Path, database_type: Any, identity: Any) -> Any:
    # DB treats a URI as a literal filename. Bypass its constructor and schema
    # migration; register only the pure identity function needed by read queries.
    db = object.__new__(database_type)
    db.path = str(path)
    db.conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    db.conn.execute("PRAGMA query_only=ON")
    db.conn.create_function("artist_identity_key", 1, identity, deterministic=True)
    return db


def parse_date(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def validate_inputs(db: Any, created: str) -> None:
    reference = parse_date(created)
    for table, column in (
        ("feedback", "rated_at"),
        ("album_feedback", "rated_at"),
        ("tracks", "added_at"),
        ("unmatched", "seen_at"),
        ("album_mentions", "seen_at"),
        ("album_mentions", "first_seen_at"),
        ("album_mentions", "last_seen_at"),
        ("artist_genres", "fetched_at"),
    ):
        for (value,) in db.conn.execute(
            f'SELECT DISTINCT "{column}" FROM "{table}" WHERE "{column}" IS NOT NULL'
        ):
            if parse_date(value) > reference:
                raise ValueError(f"Future input: {table}.{column}={value} after {created}")


def build_candidates(
    db: Any,
    week: str,
    created: str,
    modules: Any,
    extra_known_urls: dict[tuple[str, str], set[str]] | None = None,
) -> list[Candidate]:
    albums, affinity, scoring, identity, family = modules
    profile = affinity.build_affinity_profile(db)
    base = {
        s.source_id: (s.avg_rating or 0.0, s.score)
        for s in scoring.build_source_scores(db, weeks=4, reference_dt=parse_date(created))
    }
    quality = albums._album_feedback_quality(db, base)
    mentions = albums._load_all_album_mentions(db, through_week=week)
    ranked = albums.rank_album_recommendations(mentions, quality, limit=len(mentions))
    first_weeks = dict(
        ((artist, album), first)
        for artist, album, first in db.conn.execute(
            "SELECT artist_identity_key(artist_key), album_key, "
            "MIN(COALESCE(first_seen_week, added_at_week)) "
            "FROM album_mentions GROUP BY artist_identity_key(artist_key), album_key"
        )
    )
    known_urls = {key: set(urls) for key, urls in (extra_known_urls or {}).items()}
    previous: set[tuple[str, str]] = set()
    for artist, album, url, queue_week, queue_created in db.conn.execute(
        "SELECT i.artist_key, i.album_key, i.listen_url, i.week, w.created_at "
        "FROM album_queue_items i JOIN album_queue_weeks w USING (week)"
    ):
        if parse_date(queue_created) > parse_date(created):
            continue
        key = (identity(artist), album)
        if url:
            known_urls.setdefault(key, set()).add(url)
        if queue_week < week:
            previous.add(key)
    candidates = []
    cutoff = albums._cutoff_week(week, 6)
    for item in ranked:
        if db.album_feedback_for_identity(item.artist, item.album) is not None:
            continue
        fresh = item.newest_source_week == week
        if not fresh and not cutoff <= item.newest_source_week < week:
            continue
        source = item.sources[0]
        candidates.append(
            Candidate(
                key=(item.artist_key, item.album_key),
                artist=item.artist,
                album=item.album,
                families=item.source_count,
                primary_family=family(source),
                source_tier=albums._source_tier(source),
                quality=item.best_score + item.best_avg_rating,
                affinity=profile.score(item.artist),
                affinity_tier=albums._affinity_tier(profile.score, item.artist),
                fresh_at=albums._timestamp(item.newest_source_at or item.latest_seen_at),
                fresh=fresh,
                first_week=first_weeks[(item.artist_key, item.album_key)],
                known_artist=identity(item.artist) in profile.rated_artist_keys,
                previous_queue=(item.artist_key, item.album_key) in previous,
                known_urls=tuple(sorted(known_urls.get((item.artist_key, item.album_key), ()))),
            )
        )
    expected = albums.select_album_queue(
        db, week, limit=14, source_quality=base, affinity=profile.score
    )
    assert [c.key for c in choose(candidates, "baseline")] == [
        (item.artist_key, item.album_key) for item, _ in expected
    ], "Simulator baseline diverges from pinned production code"
    assert len({c.key for c in candidates}) == len(candidates)
    return candidates


def run(repo: Path, root: Path) -> dict:
    if "peel" in sys.modules:
        raise RuntimeError("Run this study in a fresh process to pin the imported Peel package")
    extract_code(git(repo, "archive", BASELINE, "src/peel"), root)
    sys.path.insert(0, str(root / "src"))
    from peel import affinity, albums, db, scoring
    from peel.matcher import artist_identity_key
    from peel.sources.families import source_family

    assert Path(albums.__file__).is_relative_to(root / "src")
    modules = (albums, affinity, scoring, artist_identity_key, source_family)
    outcome_path = root / "outcomes.db"
    outcome_bytes = git(repo, "show", f"{OUTCOMES}:data/peel.db")
    outcome_path.write_bytes(outcome_bytes)
    evaluated = open_readonly(outcome_path, db.DB, artist_identity_key)
    outcomes = {}
    for artist, album, label, _ in evaluated.conn.execute(
        "SELECT artist_key, album_key, label, rated_at FROM album_feedback "
        "ORDER BY julianday(rated_at), rated_at, (label='ban'), artist_key DESC"
    ):
        outcomes[artist_identity_key(artist), album] = label
    evaluated.conn.close()
    weeks = {}
    observed: set[tuple[str, str]] = set()
    for week, ref in SNAPSHOTS.items():
        content = git(repo, "show", f"{ref}:data/peel.db")
        path = root / f"{week}.db"
        path.write_bytes(content)
        history = open_readonly(path, db.DB, artist_identity_key)
        created = history.conn.execute(
            "SELECT created_at FROM album_queue_weeks WHERE week=?", (week,)
        ).fetchone()[0]
        validate_inputs(history, created)
        extra_links: dict[tuple[str, str], set[str]] = {}
        for proof_ref in EXTRA_LINK_PROOFS.get(week, ()):
            proof_path = root / f"links-{proof_ref}.db"
            proof_path.write_bytes(git(repo, "show", f"{proof_ref}:data/peel.db"))
            proof = open_readonly(proof_path, db.DB, artist_identity_key)
            proof_created = proof.conn.execute(
                "SELECT created_at FROM album_queue_weeks WHERE week=?", (week,)
            ).fetchone()[0]
            if parse_date(proof_created) > parse_date(created):
                raise ValueError("Listening-link proof is newer than the historical queue")
            for item in proof.album_queue(week) or []:
                if item.listen_url:
                    key = (artist_identity_key(item.artist_key), item.album_key)
                    extra_links.setdefault(key, set()).add(item.listen_url)
            proof.conn.close()
        pool = build_candidates(history, week, created, modules, extra_links)
        index = {c.key: c for c in pool}
        delivered_keys = [
            (artist_identity_key(i.artist_key), i.album_key) for i in history.album_queue(week)
        ]
        assert len(delivered_keys) == len(set(delivered_keys)) == 7
        fixed_subset = [index[key] for key in delivered_keys]
        observed.update(delivered_keys)
        baseline_keys = {c.key for c in choose(pool, "baseline", limit=7)}
        variants = {}
        for mode in MODES:
            picks = choose(pool, mode, limit=7)
            reordered = choose(fixed_subset, mode, limit=7)
            variants[mode] = {
                "fixed_observed_subset": {
                    **ordering_metrics(reordered, outcomes),
                    "order": [c.artist for c in reordered],
                },
                "unconfirmed_pool_top7": {
                    **describe(picks, outcomes, week),
                    "new_vs_baseline": sum(c.key not in baseline_keys for c in picks),
                },
                "unconfirmed_reserve_top14": describe(choose(pool, mode), outcomes, week),
            }
        weeks[week] = {
            "snapshot_ref": git(repo, "rev-parse", ref).decode().strip(),
            "snapshot_sha256": hashlib.sha256(content).hexdigest(),
            "created_at": created,
            "extra_listening_link_proofs": list(EXTRA_LINK_PROOFS.get(week, ())),
            "temporal_guards_passed": True,
            "eligible_candidates": len(pool),
            "variants": variants,
        }
        history.conn.close()
    return {
        "baseline_code": BASELINE,
        "outcomes_ref": OUTCOMES,
        "outcomes_sha256": hashlib.sha256(outcome_bytes).hexdigest(),
        "study_type": "exploratory_not_independent_validation",
        "observed_slots": 28,
        "observed_unique_albums": len(observed),
        "observed_positive_unique_albums": sum(outcomes[k] in POSITIVE for k in observed),
        "weeks": weeks,
        "limitations": [
            "Ordering metrics are conditional on the albums actually delivered and later rated.",
            "Unconfirmed pools are not playable queues; selection success is unmeasured.",
            "Known outcomes for alternatives are selective, not a representative test set.",
            "Current outcome labels lack complete revision history and have calibration drift.",
            "Correlated weeks/pairs do not justify independent-sample significance tests.",
            "Known links are diagnostic only; novelty and live resolution are not replayed.",
            "Bonus modes use continuous rather than tiered affinity; effects are coupled.",
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="peel-consensus-shadow-") as tmp:
        print(json.dumps(run(args.repo.resolve(), Path(tmp)), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
