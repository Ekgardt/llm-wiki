"""Doctor knows the abort states and keeps the retention of dated drop logs (audit 2026-09-26 C-12).

docs/research/2026-09-26-doctor-knows-the-abort-states.md
"""
from __future__ import annotations

import doctor
import maintenance_helpers


def test_an_aborting_transaction_is_unsettled_and_an_aborted_one_is_known() -> None:
    states = {state: 0 for state in doctor.TRANSACTION_STATES}
    states["aborting"] = 1

    assert (doctor._unsettled_count(states), "aborted" in doctor.TRANSACTION_STATES) == (1, True)


def test_the_compile_drop_logs_have_a_retention() -> None:
    assert "compile-drops-*.jsonl" in maintenance_helpers.MAINTENANCE_REPORT_PATTERNS
