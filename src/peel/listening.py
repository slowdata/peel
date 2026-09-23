"""Retire listening backlog artifacts without rewriting historical reports."""

from __future__ import annotations

import os
from pathlib import Path

from peel.db import DB


def archive_report_files(db: DB, reports_dir: Path) -> list[Path]:
    """Move archived Markdown/previews byte-for-byte; refuse destination conflicts.

    DB decisions remain in listening_resets. A filesystem interruption can be
    resumed without losing or regenerating the original historical artifacts.
    """
    moves: list[tuple[Path, Path]] = []
    for parent in (reports_dir, reports_dir / ".html"):
        for source in sorted(parent.glob("????-W*")):
            if source.suffix not in {".md", ".html"} or not source.is_file():
                continue
            if not db.is_archived_week(source.stem):
                continue
            destination = reports_dir / "archive" / source.relative_to(reports_dir)
            if destination.exists() and destination.read_bytes() != source.read_bytes():
                raise ValueError(f"Archive conflict: {destination}; no reports moved")
            moves.append((source, destination))
    for source, destination in moves:
        destination.parent.mkdir(parents=True, exist_ok=True)
        os.replace(source, destination)
    return [destination for _, destination in moves]
