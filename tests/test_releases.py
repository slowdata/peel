"""Release evidence must not be replaced with collection/publication dates."""

from datetime import date

import pytest

from peel.releases import release_date_bounds, release_date_reason

NOW = date(2026, 11, 10)


@pytest.mark.parametrize(
    "value",
    [None, "", "unknown", 2026, "2026-13", "2026-02-30", "2026-10-01-extra", "2026-1-2", "0000"],
)
def test_invalid_dates_are_unconfirmed(value):
    assert release_date_bounds(value) is None
    assert release_date_reason(value, NOW)


def test_partial_dates_are_intervals_not_invented_first_days():
    assert release_date_bounds("2024-02") == (date(2024, 2, 1), date(2024, 2, 29))
    assert release_date_bounds("2026") == (date(2026, 1, 1), date(2026, 12, 31))
    assert release_date_reason("2026-10", date(2026, 11, 4)) is None
    assert "imprecisa" in release_date_reason("2026-10", NOW)
    assert "imprecisa" in release_date_reason("2026-11", NOW)
    assert "imprecisa" in release_date_reason("2026", NOW)


def test_exact_cutoff_is_inclusive_and_future_is_not_released():
    assert release_date_reason("2026-10-06", NOW) is None
    assert release_date_reason("2026-11-10", NOW) is None
    assert "35 dias" in release_date_reason("2026-10-05", NOW)
    assert "futuro" in release_date_reason("2026-11-11", NOW)
    assert "lançado" in release_date_reason("1963", NOW)
