"""A CAS append that loses a race must leave a trail that can resolve itself.

`append_knowledge` retries a lost compare-and-swap: the refused attempt is
quarantined and the next candidate id repeats the work, so no bytes are lost.
But until the retry names the attempt it follows, doctor sees a quarantined row
it can never clear — an append to an existing daily log is a `replace`, so the
outcome proof in `doctor._unresolved_quarantine` (which reads `create`
operations) cannot speak for it, and the lineage proof has no parent to read.
The live vault reached `transactions (error)` this way with three refusals
whose retries had all committed.

The race is produced for real: several threads appending to one daily log
through the real coordinator on a temporary vault. Nothing in the transaction
layer is replaced or patched — a refusal cannot be staged, because `prepare`
rejects a stale precondition up front and recovery rolls a prepared attempt
forward, so only genuine interleaving can refuse one. Four workers times four
rounds refused 5, 6 and 7 attempts on three measured runs; the assertions state
the invariant rather than the count, so a quiet run still checks something true.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

import doctor
import markdown_transaction
import pytest
from markdown_transaction import MarkdownCoordinator

# Bounds a hang on the slowest supported machine, not the expected duration.
_APPEND_BUDGET_SECONDS = 60.0
_DAILY = "knowledge/daily/2026-08-25.md"
_WORKERS = 4
_ROUNDS = 4


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    root = tmp_path / "vault"
    for relative in ("knowledge/daily", "knowledge/notes", "knowledge/projects"):
        (root / relative).mkdir(parents=True)
    (root / _DAILY).write_bytes(b"# 2026-08-25\n")
    return root


@pytest.fixture
def state_root(tmp_path: Path) -> Path:
    return tmp_path / "state"


def _append(coordinator: MarkdownCoordinator, operation_id: str, block: bytes):
    return markdown_transaction._append_until_committed(
        coordinator,
        operation_id,
        _DAILY,
        block,
        deadline=time.monotonic() + _APPEND_BUDGET_SECONDS,
        cancelled=None,
    )


def _transactions(coordinator: MarkdownCoordinator) -> list[tuple]:
    with sqlite3.connect(coordinator.database_path) as database:
        return database.execute(
            "SELECT id, operation_id, state, error_code, parent_transaction_id "
            'FROM "transaction" ORDER BY created_at'
        ).fetchall()


def _unresolved(coordinator: MarkdownCoordinator) -> int:
    with sqlite3.connect(coordinator.database_path) as database:
        columns = {
            row[1] for row in database.execute('PRAGMA table_info("transaction")')
        }
        return doctor._unresolved_quarantine(database, columns)


def _refusals(rows: list[tuple]) -> list[tuple]:
    return [row for row in rows if row[2] == "quarantined"]


def _mark_ancestors(start: str | None, parents: dict[str, str], seen: set[str]) -> None:
    while start and start not in seen:
        seen.add(start)
        start = parents.get(start)


def _followed_refusals(rows: list[tuple]) -> set[str]:
    """Attempts whose chain of retries reached a commit, however deep."""
    parents = {row[0]: row[4] for row in rows if row[4]}
    seen: set[str] = set()
    for row in rows:
        _mark_ancestors(parents.get(row[0]) if row[2] == "committed" else None, parents, seen)
    return seen


def _unfollowed_refusals(rows: list[tuple]) -> list[str]:
    """Quarantined attempts no committed retry in their chain ever replaced."""
    followed = _followed_refusals(rows)
    return [row[1] for row in _refusals(rows) if row[0] not in followed]


def _append_worker(
    coordinator: MarkdownCoordinator,
    index: int,
    start: threading.Barrier,
    failures: list[BaseException],
) -> None:
    start.wait(timeout=_APPEND_BUDGET_SECONDS)
    try:
        for round_number in range(_ROUNDS):
            _append(
                coordinator,
                f"post-tool:{index}-{round_number}",
                f"line-{index}-{round_number}\n".encode("ascii"),
            )
    except BaseException as exc:  # reported by the test, never swallowed
        failures.append(exc)


def _run_concurrent_appends(coordinator: MarkdownCoordinator) -> list[BaseException]:
    start = threading.Barrier(_WORKERS)
    failures: list[BaseException] = []
    threads = [
        threading.Thread(
            target=_append_worker, args=(coordinator, index, start, failures)
        )
        for index in range(_WORKERS)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=_APPEND_BUDGET_SECONDS)
    return failures


def _expected_lines() -> set[str]:
    return {
        f"line-{index}-{round_number}"
        for index in range(_WORKERS)
        for round_number in range(_ROUNDS)
    }


def _written_lines(vault: Path) -> set[str]:
    text = (vault / _DAILY).read_text(encoding="utf-8")
    return {line for line in text.splitlines() if line.startswith("line-")}


def test_concurrent_appends_lose_no_bytes(vault: Path, state_root: Path) -> None:
    coordinator = MarkdownCoordinator(vault, state_root)

    failures = _run_concurrent_appends(coordinator)

    assert not failures, failures
    assert _written_lines(vault) == _expected_lines()


def test_every_refused_append_is_named_by_the_retry_that_replaced_it(
    vault: Path, state_root: Path
) -> None:
    coordinator = MarkdownCoordinator(vault, state_root)

    failures = _run_concurrent_appends(coordinator)

    assert not failures, failures
    assert _unfollowed_refusals(_transactions(coordinator)) == []


def test_doctor_reports_no_open_quarantine_after_concurrent_appends(
    vault: Path, state_root: Path
) -> None:
    coordinator = MarkdownCoordinator(vault, state_root)

    failures = _run_concurrent_appends(coordinator)

    assert not failures, failures
    assert _unresolved(coordinator) == 0
