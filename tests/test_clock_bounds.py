from __future__ import annotations

import pytest

from tests import clock_bounds
from tests.clock_bounds import ROUNDING_CLOCK_READING as _START
from tests.clock_bounds import within_budget

_ROUNDED_BUDGET = (_START + 2.0) - _START


@pytest.fixture(autouse=True)
def _clock_at_start(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(clock_bounds.time, "monotonic", lambda: _START)


def test_a_budget_read_back_from_its_deadline_can_round_above_itself() -> None:
    assert _ROUNDED_BUDGET > 2.0


def test_the_rounding_of_the_deadline_is_within_budget() -> None:
    assert within_budget(_ROUNDED_BUDGET, 2.0)


def test_a_real_overrun_is_not_within_budget() -> None:
    assert not within_budget(2.0 + 1e-9, 2.0)
