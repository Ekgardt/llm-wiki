"""Sixty-four historical CAS refusals must not strand a still-owed append.

The count reproduces the installed vault's 2026-10-03 incident. Each refusal
is made through real prepare/apply around an external editor's file change.
No transaction state, clock, database outcome or publication check is replaced.
"""
from __future__ import annotations

import time
from pathlib import Path

import markdown_transaction
import pytest
from markdown_transaction import MarkdownChange, MarkdownCoordinator, TransactionFailure

from tests.slow_machine import LONG_TIMEOUT
from tests.test_append_race_lineage import _unresolved

_DAILY = "knowledge/daily/2026-10-02.md"
_OPERATION = "breadcrumb:retained-intent"
_BLOCK = b"\nretained breadcrumb\n"


def _refuse(coordinator: MarkdownCoordinator, ordinal: int) -> None:
    target = coordinator.vault / _DAILY
    before = target.read_bytes()
    transaction = coordinator.prepare(
        [MarkdownChange.replace(_DAILY, before + _BLOCK)],
        operation_id=markdown_transaction._append_candidate_id(_OPERATION, ordinal),
        preconditions=markdown_transaction._append_preconditions(_DAILY, before, None),
    )
    target.write_bytes(before + f"external edit {ordinal}\n".encode())
    with pytest.raises(TransactionFailure, match="precondition"):
        coordinator.apply(transaction.id)
    assert coordinator._record(transaction.id).state == "quarantined"


def test_refused_history_does_not_consume_the_next_calls_budget(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    (root / "knowledge/daily").mkdir(parents=True)
    target = root / _DAILY
    target.write_bytes(b"# Daily\n")
    coordinator = MarkdownCoordinator(root, tmp_path / "state")
    for ordinal in range(64):
        _refuse(coordinator, ordinal)
    before = target.read_bytes()
    assert _unresolved(coordinator) == 64

    record = markdown_transaction._append_until_committed(
        coordinator, _OPERATION, _DAILY, _BLOCK,
        deadline=time.monotonic() + LONG_TIMEOUT, cancelled=None,
    )

    assert record.state == "committed"
    assert target.read_bytes() == before + _BLOCK
    assert _unresolved(coordinator) == 0
    repeated = markdown_transaction._append_until_committed(
        coordinator, _OPERATION, _DAILY, _BLOCK,
        deadline=time.monotonic() + LONG_TIMEOUT, cancelled=None,
    )
    assert repeated.id == record.id
    assert target.read_bytes() == before + _BLOCK
