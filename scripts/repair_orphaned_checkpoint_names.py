#!/usr/bin/env python3
"""Re-attempt checkpoints whose names no request can produce any more.

A quarantined or reserved checkpoint sequence blocks every sequence behind it,
and the design clears it the right way: the original request arrives again,
re-derives the same `occurrence_id`, and `_retry_spent_checkpoint` turns the row
into a fresh attempt. `tests/test_project_journal.py` states that contract in
seven tests and nothing here changes it.

On 2026-08-30 a batch's `occurrence_id` stopped being its last event's id and
became a digest of the whole batch, because two batches ending at the same event
were claiming one name and the second was refused forever. That fix left behind
rows named under the old scheme. No future request can bear those names, so for
those rows alone the door the design left open is walled up — and on this vault
three of them were holding 3 338 queued checkpoints, one of them the only
surviving record of 40 events.

This walks exactly those rows: not committed, and named by something other than
the batch naming. It takes the project lease, gives each a fresh attempt through
the same step a returning request would have used, and replays it. On a vault
with no orphaned names it does nothing and says so. Running it twice is safe.

See `docs/research/2026-08-30-a-sequence-nothing-can-settle.md`.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from memory_state import ROOT, STATE_ROOT  # noqa: E402
from project_journal import ProjectLeaseBusy, ProjectStore, _timestamp  # noqa: E402

# Every checkpoint written since the rename is named by `_batch_occurrence_id`.
# Anything else on an unsettled row predates it and can never be re-requested.
BATCH_NAME_PREFIX = "batch:"

# A batch name was assumed re-requestable, because the request that made it would
# arrive again. On 2026-09-05 that assumption failed on this vault: a batch
# checkpoint hit `precondition_failed` — the journal had simply moved between
# planning and applying — and its `occurrence_id` is a digest of the exact events
# in that batch, which were consumed and will never be assembled again. One
# project sat quarantined with 335 events queued behind it, and every subsequent
# attempt logged `ProjectPendingPriorError` instead.
#
# Whether a name will arrive again cannot be read off the row. Age can: a request
# that was going to return has returned within this window, and after it the door
# is walled up whatever the name looks like.
STALE_CHECKPOINT_SECONDS = 30 * 60


def _age_seconds(created_at: object, now: float) -> float:
    from datetime import datetime

    try:
        started = datetime.fromisoformat(str(created_at).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return float("inf")
    return now - started.timestamp()


def _is_orphaned(row: object, now: float) -> bool:
    if not str(row["occurrence_id"]).startswith(BATCH_NAME_PREFIX):
        return True
    return _age_seconds(row["created_at"], now) >= STALE_CHECKPOINT_SECONDS


def orphaned_rows(store: ProjectStore) -> list[tuple[str, int, str, str]]:
    """Unsettled sequences no request is going to settle on its own."""
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc).timestamp()
    with store.coordinator._connect() as database:  # noqa: SLF001
        rows = database.execute(
            "SELECT c.project, c.sequence, c.state, c.occurrence_id, t.created_at "
            "FROM project_checkpoints AS c "
            'LEFT JOIN "transaction" AS t ON t.id = c.transaction_id '
            "WHERE c.state != 'committed' ORDER BY c.project, c.sequence"
        ).fetchall()
    return [
        (row["project"], row["sequence"], row["state"], row["occurrence_id"])
        for row in rows
        if _is_orphaned(row, now)
    ]


def _lease_precondition(project: str, lease) -> dict[str, object]:
    return {
        "project": project,
        "lease_token": lease.token,
        "fencing_epoch": lease.epoch,
        "expires_at": _timestamp(lease.expires_at),
    }


def _repair_under_lease(store: ProjectStore, project: str, sequence: int, lease) -> str:
    reservation = store.coordinator.retry_unsettled_sequence(
        project, sequence, _lease_precondition(project, lease)
    )
    if reservation is None:
        return "already settled"
    receipt = store._replayed_under_lease(reservation, lease, None)  # noqa: SLF001
    return "written" if receipt is not None else "re-attempted, not yet written"


def repair_one(store: ProjectStore, project: str, sequence: int) -> str:
    """One sequence, under the project's own lease, or a reason it was skipped."""
    try:
        lease = store.acquire_lease(project, "checkpoint-name-repair")
    except ProjectLeaseBusy:
        return "project is leased by another invocation"
    try:
        return _repair_under_lease(store, project, sequence, lease)
    finally:
        store._release(lease)  # noqa: SLF001


def _outcome(store: ProjectStore, project: str, sequence: int) -> str:
    try:
        return repair_one(store, project, sequence)
    except Exception as error:  # noqa: BLE001
        return f"failed: {type(error).__name__}: {error}"


def repair(store: ProjectStore) -> list[str]:
    lines: list[str] = []
    for project, sequence, state, occurrence in orphaned_rows(store):
        outcome = _outcome(store, project, sequence)
        lines.append(f"{project} {sequence} ({state}, {occurrence[:16]}…): {outcome}")
    return lines


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vault", default=str(ROOT))
    parser.add_argument("--state-root", default=str(STATE_ROOT))
    parser.add_argument(
        "--list-only",
        action="store_true",
        help="name the orphaned rows and change nothing",
    )
    args = parser.parse_args()
    store = ProjectStore(Path(args.vault), Path(args.state_root))
    if args.list_only:
        rows = orphaned_rows(store)
        print("\n".join(f"{p} {s} ({st}, {o[:16]}…)" for p, s, st, o in rows) or "none")
        return 0
    lines = repair(store)
    print("\n".join(lines) or "no orphaned checkpoint names")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
