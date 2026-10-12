"""Fast calendar validation retains canonical dates and Gregorian leap rules."""
from datetime import date, timedelta

import compile_memory as compiler
import pytest


def test_calendar_validation_covers_a_complete_gregorian_cycle():
    # Gregorian leap-year rules repeat every 400 years: 146097 days.
    beginning = date(1600, 1, 1)
    for offset in range(146097):
        value = (beginning + timedelta(days=offset)).isoformat()
        assert compiler._require_calendar_date(value) is None


@pytest.mark.parametrize('value', [
    '0001-01-01', '9999-12-31', '2000-02-29', '2024-02-29',
])
def test_calendar_boundary_and_leap_dates_remain_valid(value):
    assert compiler._require_calendar_date(value) is None


@pytest.mark.parametrize('value', [
    '0000-01-01', '10000-01-01', '1900-02-29', '2100-02-29', '2026-02-29',
    '2026-00-12', '2026-13-12', '2026-04-31', '2026-10-00', '2026-10-32',
    '20261012', '2026-W42-1', '2026-1-2', '2026-10-12T00:00:00', '2026-10-12\n',
])
def test_invalid_or_alternative_date_spelling_is_refused(value):
    with pytest.raises(ValueError, match='compile evidence date is invalid'):
        compiler._require_calendar_date(value)
