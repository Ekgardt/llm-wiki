"""The concurrent journal caller passes its already-declared wait to publication."""
import project_journal

from tests.slow_machine import LONG_TIMEOUT
from tests.test_project_journal import (
    state_root as state_root,
)
from tests.test_project_journal import (
    test_simultaneous_projectors_append_once_per_event as run_projectors,
)
from tests.test_project_journal import (
    vault as vault,
)


def test_projector_test_passes_its_existing_caller_wait(vault, state_root, monkeypatch):
    waits = []
    original = project_journal._apply_transaction

    def observed(coordinator, transaction_id, writer_wait_seconds):
        waits.append(writer_wait_seconds)
        return original(coordinator, transaction_id, writer_wait_seconds)

    monkeypatch.setattr(project_journal, '_apply_transaction', observed)
    run_projectors(vault, state_root)
    assert waits
    assert all(value is not None and 0 < value <= LONG_TIMEOUT for value in waits), waits
