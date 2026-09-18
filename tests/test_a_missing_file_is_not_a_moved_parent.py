"""A containment failure is named by its type, never by words in a message.

`_is_target_boundary_error` used to search the text of an exception for eight
phrases — "parent identity", "outside the vault", "stable directory" — and to
answer True for every `FileNotFoundError` besides. Two ordinary things then read
as a moved parent and were quarantined, which is the one disposition an operator
cannot undo automatically: a page whose own file name contains one of the
phrases, and any missing file on the apply path.

See `docs/research/2026-09-18-a-boundary-failure-is-a-type-not-a-phrase.md`.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import markdown_transaction  # noqa: E402
from markdown_transaction import (  # noqa: E402
    MarkdownChange,
    MarkdownCoordinator,
    TransactionFailure,
)

# A perfectly ordinary page whose name carries two of the phrases the old
# predicate searched for.
_PAGE = "knowledge/notes/parent identity outside the vault.md"


def _coordinator(tmp_path: Path) -> MarkdownCoordinator:
    root = tmp_path / "vault"
    (root / "knowledge/notes").mkdir(parents=True)
    (root / _PAGE).write_bytes(b"one\n")
    return MarkdownCoordinator(root, tmp_path / "state")


def test_a_conflict_on_a_page_named_after_the_phrases_is_still_a_conflict(
    tmp_path: Path,
) -> None:
    coordinator = _coordinator(tmp_path)
    record = coordinator.prepare(
        [MarkdownChange.replace(_PAGE, b"two\n")],
        operation_id="phrases-in-the-name",
    )
    (coordinator.vault / _PAGE).write_bytes(b"someone else\n")

    with pytest.raises(TransactionFailure) as raised:
        coordinator.apply(record.id)

    settled = coordinator._record(record.id)
    assert (raised.value.code, settled.state) == (
        "before_hash_mismatch",
        "conflicted",
    )


def test_a_parent_that_is_gone_is_still_a_boundary_failure(tmp_path: Path) -> None:
    """The genuine case keeps its quarantine, now by type rather than by words."""
    coordinator = _coordinator(tmp_path)
    record = coordinator.prepare(
        [MarkdownChange.replace(_PAGE, b"two\n")],
        operation_id="parent-removed",
    )
    (coordinator.vault / _PAGE).unlink()
    (coordinator.vault / "knowledge/notes").rmdir()

    with pytest.raises(TransactionFailure) as raised:
        coordinator.apply(record.id)

    settled = coordinator._record(record.id)
    assert (raised.value.code, settled.state) == (
        "parent_identity_changed",
        "quarantined",
    )


def test_only_the_two_boundary_types_answer_the_boundary_question() -> None:
    """A bare ENOENT, and a mismatch whose text reads like one, are not boundaries."""
    errors = (
        markdown_transaction.TargetBoundaryFailure("parent identity mismatch for a"),
        markdown_transaction.TargetPathBoundaryError("target escapes the vault"),
        FileNotFoundError(2, "no such file"),
        markdown_transaction.TargetStateMismatch("before", "a parent identity.md"),
    )
    answers = tuple(
        markdown_transaction._is_target_boundary_error(error) for error in errors
    )

    assert answers == (True, True, False, False)
