#!/usr/bin/env python3
"""Re-create pages a refused transaction wrote and nothing wrote since.

A compile that plans several files applies them as one transaction. When one
of the shared targets — `knowledge/log.md`, most often — is appended to by
another writer between the plan and the apply, the precondition fails and the
whole transaction is refused. The pages the compile had already produced are
never written, and the compile does not run again: the daily log it read is
already marked compiled.

Measured on this vault on 2026-09-06: of 105 quarantined transactions, doctor
proves 96 were retried and committed. Of the remaining nine, one is a compile
from 2026-09-02 that lost two pages this way, and both were about the owner's
own stated preferences. The bytes were never gone — the undo trail keeps the
after-image of every planned operation, hashed — so this restores them from
there rather than asking a model to write them again.

The walk is deliberately narrow, in the shape of `repair_journal_gap.py`. A
page is re-created only when all of it holds: its transaction is quarantined,
the operation is a `create` (a `replace` would overwrite whatever is there
now), the target does not exist today, the recorded after-image still hashes
to what the plan recorded, and the path is under `knowledge/`. Anything else
is left alone and reported. On a vault with nothing owed it does nothing.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from markdown_transaction import (  # noqa: E402
    _image_bytes,
    active_or_legacy_coordinator,
    mutate_knowledge,
)
from memory_state import ROOT, STATE_ROOT  # noqa: E402
from reliable_memory import sha256_bytes  # noqa: E402

KNOWLEDGE = "knowledge/"
# A race is an accident and may be replayed. A DLP refusal is a decision: the
# fail-closed boundary looked at those bytes and declined to publish them, and
# a repair that re-created them would quietly overrule it. Two of the three
# refused transactions holding owed pages on this vault are exactly that, so
# the distinction is not hypothetical.
REPLAYABLE = "precondition_failed"


def _quarantined_ids(database: sqlite3.Connection) -> list[str]:
    return [
        str(row[0])
        for row in database.execute(
            'SELECT id FROM "transaction" WHERE state=\'quarantined\' '
            "AND error_code = ? ORDER BY created_at",
            (REPLAYABLE,),
        )
    ]


def _plan_of(directory: Path) -> dict | None:
    plan = directory / "plan.json"
    if not plan.is_file():
        return None
    try:
        return json.loads(plan.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _recorded_content(directory: Path, operation: dict) -> bytes | None:
    """The after-image this operation planned, if it is still intact."""
    after = operation.get("after")
    if not isinstance(after, dict):
        return None
    artifact = directory / str(after.get("artifact", ""))
    if not artifact.is_file():
        return None
    content = _image_bytes(artifact)
    if sha256_bytes(content) != after.get("sha256"):
        return None
    return content


def _is_owed(operation: dict, vault: Path) -> bool:
    """A create, under `knowledge/`, whose target nothing wrote since."""
    path = str(operation.get("path", ""))
    if operation.get("kind") != "create" or not path.startswith(KNOWLEDGE):
        return False
    return not (vault / path).exists()


def _owed_pages(directory: Path, vault: Path) -> list[tuple[str, bytes]]:
    plan = _plan_of(directory)
    operations = plan.get("operations", []) if isinstance(plan, dict) else []
    owed = []
    for operation in operations:
        content = _recorded_content(directory, operation)
        if content is not None and _is_owed(operation, vault):
            owed.append((str(operation["path"]), content))
    return owed


def _collect(coordinator, vault: Path) -> list[tuple[str, str, bytes]]:
    with coordinator._connect() as database:  # noqa: SLF001
        identifiers = _quarantined_ids(database)
    found = []
    for identifier in identifiers:
        directory = coordinator.transaction_root / identifier
        for path, content in _owed_pages(directory, vault):
            found.append((identifier, path, content))
    return found


def _restore(vault: Path, owed: list[tuple[str, str, bytes]]) -> None:
    for identifier, path, content in owed:
        record = mutate_knowledge(
            f"repair-refused-create:{identifier}:{sha256_bytes(content)}",
            {vault / path: content},
        )
        print(f"restored {path} ({record.state})")


def _report(owed: list[tuple[str, str, bytes]]) -> None:
    if not owed:
        print("nothing owed: no refused transaction is missing a page")
        return
    print(f"{len(owed)} page(s) a refused transaction wrote and nothing wrote since:")
    for identifier, path, content in owed:
        print(f"  {path}  ({len(content)} bytes, from {identifier})")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply", action="store_true", help="write the pages; otherwise only report"
    )
    args = parser.parse_args(argv)
    vault = Path(ROOT)
    coordinator = active_or_legacy_coordinator(vault, Path(STATE_ROOT))
    owed = _collect(coordinator, vault)
    _report(owed)
    if args.apply:
        _restore(vault, owed)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
