"""An aborted transaction is over: recovery neither re-judges it nor lets it go first.

Recovery selected `aborted` rows on every pass, ahead of crashed work, and its
check asked whether the targets still held their before-state — true when the
abort finished, false after the next legitimate write. An intact receipt was
then flagged invalid, and a bounded recovery spent its budget on old aborts.
Research: `docs/research/2026-09-17-a-transaction-that-is-over-stays-over.md`.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

TESTS = Path(__file__).resolve().parent
SCRIPTS_DIR = TESTS.parent / "scripts"
for entry in (str(SCRIPTS_DIR), str(TESTS)):
    if entry not in sys.path:
        sys.path.insert(0, entry)

from markdown_transaction import MarkdownChange  # noqa: E402
from test_transaction_abort import _abort_fixture  # noqa: E402


def _aborted(tmp_path: Path):
    coordinator, registry, owner, fence, binding, record = _abort_fixture(tmp_path)
    coordinator.abort_for_discard(
        record.id,
        intent_fence=fence,
        active_link_digest=binding.active_digest,
        actor_identity="posix-uid:1000",
    )
    coordinator.release_intent_fence(fence)
    registry.release(owner)
    return coordinator, record


def _verdict(coordinator, transaction_id: str) -> tuple[str, str | None]:
    with sqlite3.connect(coordinator.database_path) as database:
        return database.execute(
            'SELECT state, error_code FROM "transaction" WHERE id=?', (transaction_id,)
        ).fetchone()


def test_a_later_write_to_the_same_path_does_not_condemn_the_receipt(
    tmp_path: Path,
) -> None:
    coordinator, record = _aborted(tmp_path)
    coordinator.apply(
        coordinator.prepare(
            [MarkdownChange.replace("knowledge/notes/replace.md", b"written later")],
            operation_id="a-later-write",
        ).id
    )

    coordinator.recover()

    assert _verdict(coordinator, record.id) == ("aborted", None)


def test_a_bounded_recovery_reaches_crashed_work_before_an_abort(
    tmp_path: Path,
) -> None:
    coordinator, record = _aborted(tmp_path)
    crashed = coordinator.prepare(
        [MarkdownChange.create("knowledge/notes/crashed.md", b"owed")],
        operation_id="crashed-after-prepare",
    )

    recovered = coordinator.recover(max_transactions=1)

    assert [(item.id, item.state) for item in recovered] == [(crashed.id, "committed")]
    assert (tmp_path / "knowledge/notes/crashed.md").read_bytes() == b"owed"
