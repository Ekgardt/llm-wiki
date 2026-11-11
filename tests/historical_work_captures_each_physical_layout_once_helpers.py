"""Helpers shared by test_historical_work_captures_each_physical_layout_once.py and the test files importing them."""

from tests.doctor_current_v4_exact_historical_work_helpers import (
    _publish_multi_current,
    _two_current_records,
    _two_source_history,
)
from tests.doctor_current_v4_exact_historical_work_helpers import exact_work as exact_work


def _published_units(exact_work):
    history, parts = _two_source_history(exact_work)
    _publish_multi_current(history, _two_current_records(history[4], parts))
    return history, parts
