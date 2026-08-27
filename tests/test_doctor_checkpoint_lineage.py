"""A committed later attempt of a project checkpoint resolves the refused one.

Project-checkpoint attempts carry their retry ordinal in the middle of the
identity — `project:<slug>:<sequence>:attempt:<n>:epoch:<m>:<digest>` — so the
suffix-stripping lineage rule never matched them, and the vault's own recovered
checkpoint 716 stayed a permanently red finding. The proof this shape demands
is strict: same slug, same sequence, and the same payload digest. A different
payload at the same sequence, or the same payload under another sequence, is
not proof and must stay a finding.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import doctor  # noqa: E402

TRANSACTION_COLUMNS = {"id", "operation_id", "state", "parent_transaction_id"}

# The live vault's recovered checkpoint of 2026-08-26, in shape: attempt 1
# refused with `precondition_failed` and a NULL parent, attempt 2 committed
# five minutes later under the next fencing epoch with the same digest.
DIGEST = "399559fff7d1f24b8a7eb42319b9f52370048f12df81813c14caf9f57c548e41"
REFUSED = f"project:llm-wiki:716:attempt:1:epoch:41:{DIGEST}"
COMMITTED = f"project:llm-wiki:716:attempt:2:epoch:42:{DIGEST}"
JOURNAL = "knowledge/projects/llm-wiki/journal.md"
STATE = "knowledge/projects/llm-wiki/state.md"
CHECKPOINT_WRITES = (("replace", JOURNAL), ("replace", STATE))


def _database() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.execute(
        'CREATE TABLE "transaction" ('
        "id TEXT PRIMARY KEY, operation_id TEXT NOT NULL UNIQUE, "
        "state TEXT NOT NULL, parent_transaction_id TEXT)"
    )
    connection.execute(
        'CREATE TABLE "operation" ('
        "transaction_id TEXT NOT NULL, position INTEGER NOT NULL, "
        "kind TEXT NOT NULL, path TEXT NOT NULL)"
    )
    return connection


def _add(
    database: sqlite3.Connection,
    identifier: str,
    operation_id: str,
    state: str,
    operations: tuple[tuple[str, str], ...] = (),
    parent: str | None = None,
) -> None:
    database.execute(
        'INSERT INTO "transaction" (id, operation_id, state, parent_transaction_id) '
        "VALUES (?, ?, ?, ?)",
        (identifier, operation_id, state, parent),
    )
    for position, (kind, path) in enumerate(operations):
        database.execute(
            'INSERT INTO "operation" (transaction_id, position, kind, path) '
            "VALUES (?, ?, ?, ?)",
            (identifier, position, kind, path),
        )


def _unresolved(database: sqlite3.Connection) -> int:
    return doctor._unresolved_quarantine(database, TRANSACTION_COLUMNS)


def test_a_committed_later_attempt_resolves_the_checkpoint_it_retried() -> None:
    """The live shape: NULL parent, replace writes, next attempt and epoch."""
    database = _database()
    _add(database, "refused", REFUSED, "quarantined", CHECKPOINT_WRITES)
    _add(database, "winner", COMMITTED, "committed", CHECKPOINT_WRITES)

    assert _unresolved(database) == 0


def test_a_different_payload_at_the_same_sequence_is_not_proof() -> None:
    """Same slug and sequence, another digest: this attempt's content is gone."""
    database = _database()
    other = f"project:llm-wiki:716:attempt:2:epoch:42:{'0' * 64}"
    _add(database, "refused", REFUSED, "quarantined", CHECKPOINT_WRITES)
    _add(database, "stranger", other, "committed", CHECKPOINT_WRITES)

    assert _unresolved(database) == 1


def test_the_same_payload_under_another_sequence_is_not_proof() -> None:
    """The sequence is part of the request; a lookalike elsewhere proves nothing."""
    database = _database()
    other = f"project:llm-wiki:717:attempt:1:epoch:43:{DIGEST}"
    _add(database, "refused", REFUSED, "quarantined", CHECKPOINT_WRITES)
    _add(database, "stranger", other, "committed", CHECKPOINT_WRITES)

    assert _unresolved(database) == 1


def test_an_unrelated_commit_to_the_same_files_is_not_proof() -> None:
    """Every checkpoint replaces the journal; the paths alone must not resolve."""
    database = _database()
    _add(database, "refused", REFUSED, "quarantined", CHECKPOINT_WRITES)
    _add(database, "stranger", "compile:unrelated", "committed", CHECKPOINT_WRITES)

    assert _unresolved(database) == 1


def test_a_checkpoint_retry_that_also_lost_a_cas_race_resolves() -> None:
    """A committed `:cas:<n>` on top of the attempt ordinal still canonicalises."""
    database = _database()
    _add(database, "refused", REFUSED, "quarantined", CHECKPOINT_WRITES)
    _add(database, "winner", f"{COMMITTED}:cas:1", "committed", CHECKPOINT_WRITES)

    assert _unresolved(database) == 0


@pytest.mark.parametrize(
    ("operation_id", "expected"),
    (
        (REFUSED, f"project:llm-wiki:716:{DIGEST}"),
        (COMMITTED, f"project:llm-wiki:716:{DIGEST}"),
        (f"{COMMITTED}:cas:2", f"project:llm-wiki:716:{DIGEST}"),
        # Malformed shapes stay as they are — fail closed, never resolve.
        (f"project:llm-wiki:716:attempt:1:{DIGEST}", None),
        (f"project:llm-wiki:notanumber:attempt:1:epoch:2:{DIGEST}", None),
        ("compile:abc#2", "compile:abc"),
        ("post-tool:abc:cas:1", "post-tool:abc"),
    ),
)
def test_the_base_identity_removes_only_the_attempt_epoch_pair(
    operation_id: str, expected: str | None
) -> None:
    resolved = doctor._base_operation_identity(operation_id)
    assert resolved == (operation_id if expected is None else expected)
