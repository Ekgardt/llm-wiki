"""The health check must clear itself when a retry of the attempt committed.

A quarantined attempt is retained evidence, not an open problem, once the work
it wanted was done. The lineage that proves it is written in two places: the
`parent_transaction_id` column, and the retry ordinal carried in the operation
identity. Only the second survives for the append races refused before the
parent column was being populated, and this vault holds three of them.

What must not happen is the opposite mistake — treating "somebody else wrote
that file" as proof. These tests pin both directions.
"""

from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import doctor  # noqa: E402

TRANSACTION_COLUMNS = {"id", "operation_id", "state", "parent_transaction_id"}


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


# The identity of the three appends this vault lost on 2026-08-25, in shape.
APPEND = "post-tool:ded036738e573eb5e2eb606a7fdf571765250a6dd2a3005315ed39ba93b5e92f"
DAILY = "knowledge/daily/2026-08-25.md"


def test_a_committed_cas_retry_resolves_the_append_it_replaced() -> None:
    """The lost-append race the parent column was introduced too late to record."""
    database = _database()
    _add(database, "refused", APPEND, "quarantined", (("replace", DAILY),))
    _add(
        database,
        "winner",
        f"{APPEND}:cas:1",
        "committed",
        (("replace", DAILY),),
        parent=None,
    )

    assert _unresolved(database) == 0


def test_an_unrelated_commit_to_the_same_file_is_not_proof() -> None:
    """The weakening this must never become.

    Every append to a daily log replaces the same path, so "the file was
    written by a commit" would resolve a genuinely lost append. Only a commit
    carrying this attempt's own request identity counts.
    """
    database = _database()
    _add(database, "refused", APPEND, "quarantined", (("replace", DAILY),))
    _add(
        database,
        "stranger",
        "post-tool:0000000000000000000000000000000000000000000000000000000000000000",
        "committed",
        (("replace", DAILY),),
    )

    assert _unresolved(database) == 1


def test_a_hash_ordinal_retry_resolves_without_the_parent_column() -> None:
    """`#2` is the other retry form, and it must not depend on the parent either."""
    database = _database()
    _add(database, "refused", "compile:abc", "quarantined", (("replace", DAILY),))
    _add(database, "winner", "compile:abc#2", "committed", (("replace", DAILY),))

    assert _unresolved(database) == 0


def test_a_retry_of_a_retry_resolves_the_whole_chain() -> None:
    """Ordinals stack, so stripping them must reach the request they share."""
    database = _database()
    _add(database, "first", APPEND, "quarantined", (("replace", DAILY),))
    _add(database, "second", f"{APPEND}:cas:1", "quarantined", (("replace", DAILY),))
    _add(database, "winner", f"{APPEND}:cas:2", "committed", (("replace", DAILY),))

    assert _unresolved(database) == 0


def test_a_partly_written_compile_stays_a_finding() -> None:
    """Seven of eight receipts written is a loss, and the check must say so.

    This is the live vault's remaining record: `cb387b96…`, refused on
    2026-08-25 with `dlp_content_blocked`, whose eighth receipt no committed
    transaction ever created.
    """
    database = _database()
    _add(
        database,
        "refused",
        "compile:75bcc",
        "quarantined",
        (("create", "knowledge/daily/receipts/one.md"),
         ("create", "knowledge/daily/receipts/two.md")),
    )
    _add(
        database,
        "later",
        "compile:different",
        "committed",
        (("create", "knowledge/daily/receipts/one.md"),),
    )

    assert _unresolved(database) == 1


def test_a_fully_written_outcome_still_resolves() -> None:
    """The pre-existing outcome proof is untouched by the ordinal rule."""
    database = _database()
    _add(
        database,
        "refused",
        "compile:75bcc",
        "quarantined",
        (("create", "knowledge/daily/receipts/one.md"),),
    )
    _add(
        database,
        "later",
        "compile:different",
        "committed",
        (("create", "knowledge/daily/receipts/one.md"),),
    )

    assert _unresolved(database) == 0


def test_the_parent_chain_still_resolves_on_its_own() -> None:
    """Lineage recorded the original way keeps working."""
    database = _database()
    _add(database, "refused", "compile:abc", "quarantined", (("replace", DAILY),))
    _add(
        database,
        "winner",
        "compile:zzz",
        "committed",
        (("replace", DAILY),),
        parent="refused",
    )

    assert _unresolved(database) == 0


@pytest.mark.parametrize(
    ("operation_id", "expected"),
    (
        ("compile:abc", "compile:abc"),
        ("compile:abc#2", "compile:abc"),
        ("post-tool:abc:cas:1", "post-tool:abc"),
        ("post-tool:abc#3:cas:2", "post-tool:abc"),
        ("compile:abc:cas:notanumber", "compile:abc:cas:notanumber"),
    ),
)
def test_the_base_identity_strips_only_retry_ordinals(
    operation_id: str, expected: str
) -> None:
    assert doctor._base_operation_identity(operation_id) == expected


def test_the_maintenance_owner_opens_the_adoption_aware_coordinator(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Adoption turned the pre-adoption database into a JSON tombstone.

    A writer that constructs `MarkdownCoordinator` directly reopens that
    tombstone and dies with `file is not a database` on every call. One rule
    decides which coordinator a writer gets, and this call site has to ask it.
    """
    import markdown_transaction

    asked: list[tuple[Path, Path]] = []

    class Routed(Exception):
        pass

    def record(vault: Path, state_root: Path) -> object:
        asked.append((vault, state_root))
        raise Routed

    monkeypatch.setattr(
        markdown_transaction, "active_or_legacy_coordinator", record
    )

    with pytest.raises(Routed):
        doctor._acquire_maintenance_owner(
            tmp_path, tmp_path, datetime.now(timezone.utc)
        )

    assert asked == [(tmp_path, tmp_path)]
