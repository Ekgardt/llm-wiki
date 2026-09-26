#!/usr/bin/env python3
"""Reclaim what the hook path is too impatient to finish.

Two jobs, both of them recovery rather than routine work, and both of them
impossible from a session hook:

**Drain the project-checkpoint backlog.** A hook drains with a 0.5 s state-lock
budget because a person is waiting on it. That is correct, and it is also why a
backlog outlives every hook: measured on this vault 2026-08-30, `run/state.json`
had grown to 6.7 MB, hooks held the lock almost continuously, and eight
consecutive forced drains each lost it in 0.6 s with the queue unmoved at 2 485
checkpoints. This pass is unattended, so it can wait properly, and it is bounded
so it can never hang the nightly run.

**Sweep abandoned state temporaries.** `atomic_write` stages content in
`.<name>.<nonce>.tmp`, fsyncs it, then renames. A process killed in between —
a hook that hit its timeout, most often — leaves the staged file forever and
nothing collects it. Measured the same day: 39 orphans, 272 MB, every one of
them complete JSON, the oldest from 08-26. Only files older than an hour are
removed, which no live write can be, and the nonce means a sweep can never
race a writer for a name it is about to use.

See `docs/research/2026-08-30-a-backlog-that-prevents-its-own-drain.md`.
"""

from __future__ import annotations

import argparse
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from integration_adapter import BACKLOG_DRAIN_SECONDS, drain_pending_backlog  # noqa: E402
from memory_state import ROOT, STATE_ROOT  # noqa: E402

# An atomic write of the largest state seen here takes well under a second. An
# hour is not a tuning parameter; it is a margin wide enough that a file this
# old cannot belong to a writer that is still running.
ORPHAN_TEMP_SECONDS = 3600.0


# Staged state temporaries, and SQLite lock probes left before the probe cleaned
# up after itself (26 of them, 2026-08-28..09-07). See
# `docs/research/2026-09-24-every-store-has-a-bound.md`.
ORPHAN_PATTERNS = (".*.tmp", ".llm-wiki-lock-probe-*.sqlite3*")


def _orphan_temporaries(directory: Path, now: float) -> list[Path]:
    """Staged files old enough that no live writer can still own them."""
    if not directory.exists():
        return []
    return [path for path in _staged_files(directory) if _is_orphan(path, now)]


def _staged_files(directory: Path) -> list[Path]:
    return [path for pattern in ORPHAN_PATTERNS for path in directory.glob(pattern)]


def _is_orphan(path: Path, now: float) -> bool:
    try:
        return now - path.stat().st_mtime > ORPHAN_TEMP_SECONDS
    except OSError:
        return False


def _remove(path: Path) -> int:
    """The bytes reclaimed, or zero when the file went away or would not."""
    try:
        size = path.stat().st_size
        path.unlink()
    except OSError:
        return 0
    return size


def sweep_orphan_temporaries(directory: Path | None = None) -> dict[str, int]:
    root = directory if directory is not None else STATE_ROOT / "run"
    reclaimed = [_remove(path) for path in _orphan_temporaries(root, time.time())]
    return {"removed": sum(1 for size in reclaimed if size), "bytes": sum(reclaimed)}


# A write into `knowledge/` stages `.<name>.<nonce>.tmp` beside its target and
# renames it over; a writer killed in between leaves the staged copy there. On
# this vault (2026-09-26): three, the oldest from 08-26, two of them whole
# project journals. Only this exact shape is swept — a dot, a name, a hex nonce
# of at least 16 digits, `.tmp` — so a person's own file there is never taken;
# `tests/test_a_killed_write_leaves_nothing_behind.py` holds every staging name
# in `scripts/` to it. Research:
# docs/research/2026-09-26-a-killed-write-leaves-nothing-behind.md
STAGED_WRITE_NAME = re.compile(r"^\..+[.-][0-9a-f]{16,}\.tmp$")
MAX_KNOWLEDGE_ENTRIES = 200_000


def _staged_knowledge_writes(root: Path, now: float) -> list[Path]:
    found: list[Path] = []
    for count, path in enumerate(root.rglob(".*.tmp")):
        if count >= MAX_KNOWLEDGE_ENTRIES:
            break
        if STAGED_WRITE_NAME.match(path.name) and _is_orphan(path, now):
            found.append(path)
    return found


def _reclaimed(paths: list[Path]) -> dict[str, int]:
    sizes = [_remove(path) for path in paths]
    return {"removed": sum(1 for size in sizes if size), "bytes": sum(sizes)}


def sweep_staged_knowledge_writes(root: Path | None = None) -> dict[str, int]:
    """Staged copies a killed write left beside a Markdown target."""
    base = root or ROOT / "knowledge"
    if not base.is_dir():
        return _reclaimed([])
    return _reclaimed(_staged_knowledge_writes(base, time.time()))


# The nightly kills this step after `RECLAIM_STEP_SECONDS`; the image prune stops
# on its own a margin before that, so the kill never lands inside a prune and the
# steps after it still run. What it did not reach is pruned the next night.
RECLAIM_STEP_SECONDS = 180
RECLAIM_MARGIN_SECONDS = 30


def prune_settled_transactions(deadline: float = float("inf")) -> dict[str, object]:
    """Drop the before and after images of transactions that have settled.

    The machinery existed and nothing ever called it, so on this vault the trail
    had never been pruned: 11 369 transactions and 4.9 GB on 2026-09-02, for a
    journal that grows one line at a time. Databases keep undo data only until
    the write settles; the month-long window was an ad-hoc backup, and a real
    backup replaces it.

    Failure is reported, never raised: a pass that cannot tidy up must still
    drain the queue and sweep the temporaries.
    See `docs/research/2026-09-02-where-undo-belongs-and-for-how-long.md`.
    """
    # Through the adoption rule, never by constructing one: adoption replaces the
    # pre-adoption database with a JSON tombstone, so a writer that opens that
    # path directly dies with `file is not a database`. Which is exactly what my
    # first attempt did on 2026-09-02.
    from markdown_transaction import active_or_legacy_coordinator

    try:
        coordinator = active_or_legacy_coordinator(ROOT, STATE_ROOT)
        return {"pruned": int(coordinator.prune(deadline=deadline)), "failed": 0}
    except TimeoutError:
        return {"pruned": 0, "failed": 0, "unfinished": True}
    except Exception as error:  # noqa: BLE001
        return {"pruned": 0, "failed": 1, "reason": str(error)[:120]}


def prune_transaction_history() -> dict[str, int]:
    """Settled rows past the history window (`HISTORY_RETENTION_DAYS`).

    The image prune above kept the rows, and the table only grew: 23 557 rows,
    55 MB on 2026-09-24. Failure is reported, never raised.
    """
    from markdown_transaction import active_or_legacy_coordinator

    try:
        coordinator = active_or_legacy_coordinator(ROOT, STATE_ROOT)
        return {**coordinator.prune_history(), "failed": 0}
    except Exception as error:  # noqa: BLE001
        return {"attempts": 0, "transactions": 0, "failed": 1, "reason": str(error)[:120]}


def remove_empty_intent_shards(directory: Path | None = None) -> int:
    """Shard directories a capture intent left empty when it moved on.

    Intents move from `pending/` to `ready/` and on; both leave empty shards,
    and only `pending/` was swept (136 empty under `ready/` on 2026-09-25, C-13).
    """
    intents = STATE_ROOT / "run" / "capture-intents"
    roots = [directory] if directory is not None else [intents / "pending", intents / "ready"]
    return sum(_empty_shards_removed(root) for root in roots)


def _empty_shards_removed(root: Path) -> int:
    if not root.is_dir():
        return 0
    return sum(_removed_if_empty(shard) for shard in root.iterdir())


def _removed_if_empty(shard: Path) -> int:
    try:
        shard.rmdir()
    except OSError:
        return 0
    return 1


def snapshot_memory() -> dict[str, object]:
    """A second copy of the memory, outside the vault, kept with its history.

    The undo trail was never a second copy — it held images of individual writes
    and has just been cut from 5.0 GB to 307 MB. Of 116 knowledge pages only 83
    are in git and of 15 daily logs only 3; the rest is private by design and
    lived in exactly one place until now.

    Failure is reported, never raised, like everything else in this pass.
    """
    from snapshot_knowledge import snapshot_root, take_snapshot

    try:
        return {**take_snapshot(), "root": str(snapshot_root())}
    except Exception as error:  # noqa: BLE001
        return {"status": f"failed: {type(error).__name__}", "commit": None}


def rebuild_co_activation() -> dict[str, object]:
    """Rebuild what was mentioned together, from the entries the vault holds.

    Derived and disposable: the table is read at query time when it is there and
    ignored when it is not, so a failure here costs a signal and never an answer.
    See `docs/research/2026-09-06-what-was-mentioned-together.md`.
    """
    try:
        from co_activation import build, save

        table = build()
        save(table)
        return {"co_activation_pages": len(table)}
    except Exception as error:  # noqa: BLE001 - a derived table is never fatal
        return {"co_activation_error": type(error).__name__}


def reclaim(budget_seconds: float) -> dict[str, object]:
    deadline = time.monotonic() + RECLAIM_STEP_SECONDS - RECLAIM_MARGIN_SECONDS
    return {
        "backlog": drain_pending_backlog(budget_seconds),
        "transactions": prune_settled_transactions(deadline),
        "history": prune_transaction_history(),
        "temporaries": sweep_orphan_temporaries(),
        "staged_writes": sweep_staged_knowledge_writes(),
        "empty_shards": remove_empty_intent_shards(),
        "snapshot": snapshot_memory(),
        "co_activation": rebuild_co_activation(),
    }


def _report(result: dict[str, object]) -> str:
    backlog = result["backlog"]
    temporaries = result["temporaries"]
    drained = sum(int(count) for count in backlog["drained"].values())
    transactions = result["transactions"]
    snapshot = result["snapshot"]
    return (
        f"snapshot {snapshot['status']} ({snapshot['commit']}); "
        f"pruned {transactions['pruned']} settled transaction(s){_unfinished_note(transactions)}; "
        f"dropped {result['history']['transactions']} transaction row(s) and "
        f"{result['history']['attempts']} attempt row(s) past the history window; "
        f"drained {drained} checkpoint(s); "
        f"{len(backlog['remaining'])} project(s) still queued; "
        f"{len(backlog['failed'])} project(s) failed; "
        f"removed {temporaries['removed']} orphaned temporary file(s), "
        f"{temporaries['bytes']} byte(s), {result['staged_writes']['removed']} staged "
        f"knowledge write(s), and {result['empty_shards']} empty intent shard(s)"
        f"{_failure_note(result)}"
    )


def _unfinished_note(transactions: dict) -> str:
    return " (stopped at its deadline; the next night continues)" if transactions.get("unfinished") else ""


def _failures(result: dict[str, object]) -> list[str]:
    """What this pass could not do; the report printed counts and hid these (audit 2026-09-26 B-23)."""
    failed = [f"{key}: {result[key].get('reason', 'failed')}" for key in ("transactions", "history") if result[key].get("failed")]
    failed.extend(_snapshot_failure(result["snapshot"]))
    failed.extend(_backlog_failure(result["backlog"]))
    return failed


def _snapshot_failure(snapshot: dict) -> list[str]:
    status = str(snapshot.get("status"))
    return [f"snapshot: {status}"] if status.startswith("failed") else []


def _backlog_failure(backlog: dict) -> list[str]:
    count = len(backlog["failed"])
    return [f"backlog: {count} project(s) failed"] if count else []


def _failure_note(result: dict[str, object]) -> str:
    failed = _failures(result)
    derived = result["co_activation"].get("co_activation_error")
    notes = [*failed, *([f"co-activation: {derived}"] if derived else [])]
    return f"; FAILED: {'; '.join(notes)}" if notes else ""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--budget-seconds",
        type=float,
        default=BACKLOG_DRAIN_SECONDS,
        help="how long the backlog drain may run before it stops",
    )
    args = parser.parse_args()
    result = reclaim(args.budget_seconds)
    print(_report(result))
    # A derived table that failed is named and costs nothing else; a real
    # maintenance failure fails the step, so the night records it.
    return 1 if _failures(result) else 0


if __name__ == "__main__":
    raise SystemExit(main())
