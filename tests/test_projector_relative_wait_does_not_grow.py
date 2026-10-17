"""The real concurrent publisher cannot receive more than its caller's budget."""
import math
from types import SimpleNamespace

import project_journal

from tests import test_project_journal as projectors
from tests.test_project_journal import state_root as state_root
from tests.test_project_journal import vault as vault


def test_float_rounding_cannot_extend_the_existing_projector_wait(vault, state_root, monkeypatch):
    # Addition crosses a binary exponent boundary; subtraction grows the budget.
    budget = projectors.LONG_TIMEOUT
    boundary = math.ldexp(1.0, math.frexp(budget)[1] + 1)
    clock = boundary - budget / 2
    clock += 3 * math.ulp(clock)
    assert (clock + budget) - clock > budget
    monkeypatch.setattr(projectors, "time", SimpleNamespace(monotonic=lambda: clock))
    original = project_journal._apply_transaction
    waits = []

    def observed(coordinator, transaction_id, wait):
        waits.append(wait)
        return original(coordinator, transaction_id, wait)

    monkeypatch.setattr(project_journal, "_apply_transaction", observed)
    projectors.test_simultaneous_projectors_append_once_per_event(vault, state_root)
    assert waits
    assert all(wait is not None and 0 < wait <= budget for wait in waits), waits
