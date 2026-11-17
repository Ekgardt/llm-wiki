"""Two projectors racing for one project journal append each event exactly once."""
from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from markdown_transaction import ProjectPendingPriorError
from project_journal import JOURNAL_HEADER, ProjectFenceError, ProjectLeaseBusy, ProjectStore
from reliable_memory import canonical_json_bytes

from tests.project_journal_helpers import checkpoint_event
from tests.slow_machine import LONG_TIMEOUT


def journal_records(store: ProjectStore, slug: str = "demo") -> list[dict[str, object]]:
    text = store.read_journal(slug)
    return [json.loads(line) for line in text.removeprefix(JOURNAL_HEADER).splitlines()]


def run_simultaneous_projectors(vault: Path, state_root: Path) -> None:
    barrier = threading.Barrier(2)

    def write(index: int):
        store = ProjectStore(vault, state_root)
        barrier.wait()
        deadline = time.monotonic() + LONG_TIMEOUT
        while time.monotonic() < deadline:
            try:
                return store.checkpoint(
                    "demo",
                    checkpoint_event(f"evt-{index}", f"event-{index}"),
                    f"agent-{index}",
                    writer_wait_seconds=min(LONG_TIMEOUT, max(0, deadline - time.monotonic())),
                )
            except (ProjectLeaseBusy, ProjectFenceError, ProjectPendingPriorError):
                # CI run 33039646439 (Windows py3.12-s2) lost this two-writer
                # race as ProjectFenceError, and a local run on 2026-08-27
                # lost it as ProjectPendingPriorError. All three are legal
                # losses of one round, and the product is right to raise them:
                # ProjectLeaseBusy (a ProjectFenceError subclass) - the rival
                # writer holds the project lease right now;
                # ProjectFenceError - the rival re-acquired the lease between
                # our reservation and apply, so our quarantined attempt must
                # be replayed forward by the next round;
                # ProjectPendingPriorError - the rival reserved the prior
                # sequence and has not committed it yet.
                # The append-once postcondition below is unchanged.
                continue
        raise AssertionError("project lease never became available")

    with ThreadPoolExecutor(max_workers=2) as pool:
        receipts = list(pool.map(write, range(2)))

    assert sorted(receipt.sequence for receipt in receipts) == [1, 2]
    records = journal_records(ProjectStore(vault, state_root))
    assert {record["occurrence_id"] for record in records} == {"evt-0", "evt-1"}
    assert [record["sequence"] for record in records] == [1, 2]
    assert all(
        canonical_json_bytes(record).decode()
        in ProjectStore(vault, state_root).read_journal("demo")
        for record in records
    )
