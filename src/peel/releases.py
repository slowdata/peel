"""Shared release-date admission, separate from editorial publication dates."""

from __future__ import annotations

import calendar
import re
from datetime import date, timedelta

MAX_RELEASE_AGE_DAYS = 35
MIN_ALBUM_TRACKS = 4
_DATE = re.compile(r"\d{4}(?:-\d{2}){0,2}\Z")


def release_date_bounds(value: object) -> tuple[date, date] | None:
    """Bound partial provider dates without inventing a January/first-day release."""
    if not isinstance(value, str) or not _DATE.fullmatch(value):
        return None
    parts = [int(part) for part in value.split("-")]
    try:
        year = parts[0]
        month = parts[1] if len(parts) >= 2 else 1
        start = date(year, month, parts[2] if len(parts) == 3 else 1)
        if len(parts) == 3:
            return start, start
        end_month = month if len(parts) == 2 else 12
        end = date(year, end_month, calendar.monthrange(year, end_month)[1])
        return start, end
    except (ValueError, OverflowError):
        return None


def release_date_reason(
    value: object, reference: date, *, max_age_days: int = MAX_RELEASE_AGE_DAYS
) -> str | None:
    """Require a release interval wholly inside the recent, already-out window.

    Unknown/ambiguous dates hold admission; they are not musical feedback.
    Edition dates still cannot establish an unmarked recording's first release.
    """
    bounds = release_date_bounds(value)
    if bounds is None:
        return "data de lançamento não confirmada"
    earliest, latest = bounds
    cutoff = reference - timedelta(days=max_age_days)
    if latest < cutoff:
        return f"lançado em {value} (mais de {max_age_days} dias)"
    if earliest > reference:
        return f"lançamento futuro ({value})"
    if earliest < cutoff or latest > reference:
        return f"data de lançamento imprecisa ({value})"
    return None
