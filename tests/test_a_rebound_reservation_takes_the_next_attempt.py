"""A reservation whose files moved must take the next attempt, not refuse forever.

A reservation carries the operation id of attempt 1, and that id is bound to
the request it was first prepared with — the journal and the state file as they
were then. Replay it after either has moved and `prepare` refuses: same id,
different request. The row can never settle, and every event behind it queues
forever.

Measured on this vault on 2026-09-07: `no-hands` sequence 839 refused this way,
1 127 events queued behind it over five hours, `run/state.json` grew to 2.8 MB
— eleven times the bound doctor may read — and two of its checks went blind
while the state lock began timing out under the size.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import project_journal  # noqa: E402
from markdown_transaction import TransactionFailure  # noqa: E402


class _Reservation:
    project = "no-hands"
    sequence = 839
    operation_id = "project:no-hands:839:attempt:1:epoch:11708:abc"


class _Store:
    """Only the two methods the replay path reaches, and a record of the calls."""

    def __init__(self, first, fresh=None):
        self.first = first
        self.fresh = fresh
        self.reserved_with = []
        self.quarantined = []
        self.coordinator = self

    def retry_unsettled_sequence(self, project, sequence, precondition):
        self.retried = (project, sequence)
        return self.fresh

    def _project_reserved(self, row, lease, *, writer_wait_seconds=None):
        self.reserved_with.append(row)
        outcome = self.first if len(self.reserved_with) == 1 else "receipt from attempt 2"
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def _set_checkpoint_state(self, project, sequence, state):
        self.quarantined.append((project, sequence, state))

    # The three methods under test, borrowed from the real class so the fake
    # cannot drift away from what it is standing in for.
    _replayed_under_lease = project_journal.ProjectStore._replayed_under_lease
    _replayed_after_rebinding = project_journal.ProjectStore._replayed_after_rebinding
    _replayed_as_a_new_attempt = project_journal.ProjectStore._replayed_as_a_new_attempt
    _quarantined_or_raised = project_journal.ProjectStore._quarantined_or_raised


def _replay(store, lease=None):
    return store._replayed_under_lease(_Reservation(), lease, None)


def test_a_rebound_reservation_is_replayed_under_the_next_attempt(monkeypatch):
    monkeypatch.setattr(
        project_journal, "_project_lease_precondition", lambda slug, lease: {}
    )
    fresh = _Reservation()
    store = _Store(ValueError(project_journal.REBOUND_REQUEST), fresh=fresh)

    assert _replay(store) == "receipt from attempt 2"
    assert store.retried == ("no-hands", 839)
    assert store.reserved_with[1] is fresh


def test_a_sequence_that_settled_meanwhile_needs_no_attempt(monkeypatch):
    monkeypatch.setattr(
        project_journal, "_project_lease_precondition", lambda slug, lease: {}
    )
    store = _Store(ValueError(project_journal.REBOUND_REQUEST), fresh=None)

    assert _replay(store) is None


def test_any_other_value_error_still_raises():
    store = _Store(ValueError("something else entirely"))

    with pytest.raises(ValueError, match="something else"):
        _replay(store)


def test_a_failed_precondition_still_quarantines():
    store = _Store(TransactionFailure("no", "precondition_failed", "quarantined"))

    assert _replay(store) is None
    assert store.quarantined == [("no-hands", 839, "quarantined")]


def test_another_transaction_failure_still_raises():
    store = _Store(TransactionFailure("no", "owner_identity_conflict", "conflicted"))

    with pytest.raises(TransactionFailure):
        _replay(store)


def test_a_pending_prior_still_waits():
    store = _Store(project_journal.ProjectPendingPriorError("no-hands", 839, 838))

    assert _replay(store) is None
