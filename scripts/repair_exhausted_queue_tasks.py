#!/usr/bin/env python3
"""Retire a task that used every attempt and was left looking ready.

`_failure_state` used to send an ordinary failure back to `ready` whatever the
attempt count was, so a task that failed on its last allowed attempt kept the
state of work still to do and the attempt count of work already given up on.
The worker will not claim it, because the budget is spent. `redrive` will not
take it, because redrive requires a dead task. Nothing else looks at it.

Twenty-three session classifications sat in that gap on this vault, each eight
failed attempts old, every one of them failing for a reason since fixed: the
classifier was reading backup manifests instead of the conversation.

The code no longer creates this state. This moves the tasks already in it to
the state the fixed code would have given them, so `memory_queue.py redrive`
can pick them up. It touches a task only when all of it holds: the state is
`ready`, no lease is held, `attempts` has reached the limit, and an error code
is recorded. On a vault with none it does nothing.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from memory_queue import active_or_legacy_memory_queue  # noqa: E402
from memory_state import ROOT, STATE_ROOT  # noqa: E402
from reliable_memory import DEFAULTS, begin_immediate  # noqa: E402

STRANDED = (
    "SELECT id, kind, attempts, error_code, updated_at FROM tasks "
    "WHERE state='ready' AND lease_token IS NULL AND attempts >= ? "
    "AND error_code IS NOT NULL ORDER BY updated_at"
)


def stranded_tasks(
    database: sqlite3.Connection, attempt_limit: int
) -> list[sqlite3.Row]:
    return list(database.execute(STRANDED, (attempt_limit,)))


def _retire(database: sqlite3.Connection, identifier: str) -> int:
    return database.execute(
        "UPDATE tasks SET state='dead' WHERE id=? AND state='ready' "
        "AND lease_token IS NULL",
        (identifier,),
    ).rowcount


def _report(rows: list[sqlite3.Row]) -> None:
    if not rows:
        print("nothing stranded: no task has spent its attempts and stayed ready")
        return
    print(f"{len(rows)} task(s) out of attempts and still marked ready:")
    for row in rows:
        print(
            f"  {row['id'][:12]}  {row['kind']:10s} attempts={row['attempts']}"
            f"  {row['error_code']}  {row['updated_at'][:19]}"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply", action="store_true", help="retire them; otherwise only report"
    )
    parser.add_argument(
        "--max-attempts", type=int, default=DEFAULTS.queue_max_attempts
    )
    args = parser.parse_args(argv)
    queue = active_or_legacy_memory_queue(Path(ROOT), Path(STATE_ROOT))
    with queue._connect() as database:  # noqa: SLF001
        database.row_factory = sqlite3.Row
        rows = stranded_tasks(database, args.max_attempts)
        _report(rows)
        if not args.apply:
            return 0
        with begin_immediate(database):
            retired = sum(_retire(database, row["id"]) for row in rows)
    print(f"retired {retired} task(s); `memory_queue.py redrive <id>` can take them")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
