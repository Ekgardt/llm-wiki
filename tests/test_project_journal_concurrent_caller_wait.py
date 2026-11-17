"""The concurrent journal caller passes its already-declared wait to publication."""
import project_journal

from tests.project_journal_helpers import state_root as state_root
from tests.project_journal_helpers import vault as vault
from tests.simultaneous_projectors_helpers import run_simultaneous_projectors as run_projectors
from tests.slow_machine import LONG_TIMEOUT


def test_projector_test_passes_its_existing_caller_wait(vault, state_root, monkeypatch):
    waits = []
    original = project_journal.apply_transaction

    def observed(coordinator, transaction_id, writer_wait_seconds):
        waits.append(writer_wait_seconds)
        return original(coordinator, transaction_id, writer_wait_seconds)

    monkeypatch.setattr(project_journal, 'apply_transaction', observed)
    run_projectors(vault, state_root)
    assert waits
    assert all(value is not None and 0 < value <= LONG_TIMEOUT for value in waits), waits
