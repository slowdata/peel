"""Explicit offline reconciliation of discovery history on a disposable DB.

Not an automatic sync policy: queue selection and Spotify read-back remain the
caller's responsibility. Never run this against the live DB. Conflicting source
run IDs are branch-local counters, not evidence identities.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

from peel.db import DB


def _rows(conn: sqlite3.Connection, table: str) -> list[dict]:
    cursor = conn.execute(f'SELECT * FROM "{table}"')
    columns = [column[0] for column in cursor.description]
    return [dict(zip(columns, row, strict=True)) for row in cursor]


def _time(row: dict, column: str) -> datetime:
    value = datetime.fromisoformat(row[column])
    if value.tzinfo is None:
        raise ValueError(f"Ambiguous timestamp: {column}")
    return value


def _resolve(table: str, local: dict, remote: dict) -> dict:
    if table == "album_mentions":
        immutable = local.keys() - {"last_seen_at", "last_seen_week"}
        if _time(local, "first_seen_at") == _time(remote, "first_seen_at") and any(
            local[key] != remote[key] for key in immutable
        ):
            raise ValueError("Conflicting first album observation")
        first = min((remote, local), key=lambda row: _time(row, "first_seen_at"))
        last = max((remote, local), key=lambda row: _time(row, "last_seen_at"))
        merged = dict(first)
        merged.update(last_seen_at=last["last_seen_at"], last_seen_week=last["last_seen_week"])
        # Do not upgrade uncertain early evidence using a later observation.
        return merged
    if table in {"tracks", "albums"}:
        column = "added_at" if table == "tracks" else "seen_at"
        if _time(local, column) == _time(remote, column):
            raise ValueError(f"Conflicting first discovery: {table}")
        return min((remote, local), key=lambda row: _time(row, column))
    column = {
        "feedback": "rated_at",
        "album_feedback": "rated_at",
        "artist_genres": "fetched_at",
        "sources_state": "last_run_at",
    }.get(table)
    if column:
        if _time(local, column) == _time(remote, column):
            raise ValueError(f"Conflicting {table} rows with the same timestamp")
        return max((remote, local), key=lambda row: _time(row, column))
    raise ValueError(f"Conflicting immutable rows: {table}")


def merge_discovery_history(candidate: DB, local_path: Path) -> dict:
    """Union evidence, retain earliest discovery and latest observations/feedback.

    Returns an audit of conflicts, including both original rows. Does not touch
    any queue, reset, recovery note or finalized edition. The caller must retain
    this audit and both input backups. All changes roll back on ambiguity.
    """
    if Path(candidate.path).resolve() == local_path.resolve():
        raise ValueError("Reconcile into a separate disposable DB")
    keys = {
        "tracks": ("spotify_uri", "source_id"),
        "albums": ("artist", "album"),
        "album_mentions": ("artist_key", "album_key", "source_id"),
        "feedback": ("spotify_uri",),
        "album_feedback": ("artist_key", "album_key"),
        "artist_genres": ("artist",),
        "sources_state": ("source_id",),
        "source_runs": ("source_id", "run_at"),
        "unmatched": (),
    }
    audit: dict = {"counts": {}, "conflicts": []}
    source = sqlite3.connect(f"{local_path.resolve().as_uri()}?mode=ro", uri=True)
    try:
        with candidate.conn:
            for table, key_columns in keys.items():
                remote_rows = _rows(candidate.conn, table)
                local_rows = _rows(source, table)
                # IDs may overlap between independent runs; remap only the counter.
                if table == "source_runs":
                    remote_rows = [
                        {k: v for k, v in row.items() if k != "id"} for row in remote_rows
                    ]
                    local_rows = [{k: v for k, v in row.items() if k != "id"} for row in local_rows]
                columns = tuple(local_rows[0]) if local_rows else ()
                table_keys = key_columns or columns
                index = {tuple(row[key] for key in table_keys): row for row in remote_rows}
                inserted = updated = 0
                for local in local_rows:
                    key = tuple(local[column] for column in table_keys)
                    remote = index.get(key)
                    if remote == local:
                        continue
                    if remote is not None:
                        if local.keys() != remote.keys():
                            raise ValueError(f"Schema mismatch: {table}")
                        chosen = _resolve(table, local, remote)
                        audit["conflicts"].append(
                            {
                                "table": table,
                                "key": key,
                                "local": local,
                                "remote": remote,
                                "merged": chosen,
                            }
                        )
                        if chosen == remote:
                            continue
                        assignments = ", ".join(f'"{column}" = ?' for column in columns)
                        where = " AND ".join(f'"{column}" = ?' for column in table_keys)
                        candidate.conn.execute(
                            f'UPDATE "{table}" SET {assignments} WHERE {where}',
                            (*[chosen[column] for column in columns], *key),
                        )
                        updated += 1
                    else:
                        chosen = local
                        names = ", ".join(f'"{column}"' for column in columns)
                        placeholders = ", ".join("?" for _ in columns)
                        candidate.conn.execute(
                            f'INSERT INTO "{table}" ({names}) VALUES ({placeholders})',
                            [chosen[column] for column in columns],
                        )
                        inserted += 1
                    index[key] = chosen
                audit["counts"][table] = {"inserted": inserted, "updated": updated}
    finally:
        source.close()
    return audit
