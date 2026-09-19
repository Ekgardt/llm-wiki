"""A lineage page is a function of its inputs, so a retry rebuilds it exactly.

Both resumable lineage operations write the page file before the database rows
that record it, which is what lets a retry recognise its own work — provided the
retry produces the same bytes. Both cut the page by `time.monotonic()`, so the
number of links depended on how busy the machine was: a rollback left page N on
disk, the retry built a different page N, and `_write_durable_file` answered
`durable_file_conflict` — `orphan_corrupt_purge_page_conflict` on the purge side,
a blocked state with no resolver.

See `docs/research/2026-09-18-a-page-that-is-rewritten-must-come-out-the-same.md`.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import memory_queue  # noqa: E402
from memory_queue import MemoryQueue  # noqa: E402

_OPERATION = {"page_count": 2, "rolling_root": "0" * 64}


def _candidates(count: int) -> list[dict[str, str]]:
    return [
        {
            "id": f"task-{index:04d}",
            "state": "dead",
            "created_at": "2026-07-01T12:00:00.000000+00:00",
            "updated_at": "2026-07-02T12:00:00.000000+00:00",
            "input_hash": f"{index:064x}",
        }
        for index in range(count)
    ]


@pytest.fixture
def slow_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    """A machine where every step of the loop costs a second of wall clock."""
    ticks = iter(range(0, 10_000))

    monkeypatch.setattr(time, "monotonic", lambda: float(next(ticks)))


def test_the_same_candidates_build_the_same_page_on_any_machine(
    tmp_path: Path, slow_clock: None
) -> None:
    candidate = tmp_path / "run" / "queue-v3.candidate.sqlite3"
    memory_queue.initialize_queue_v3_candidate(candidate, source_v2=None)
    queue = MemoryQueue._from_v3_candidate(candidate, state_root=tmp_path)
    candidates = _candidates(8)

    first = queue._bounded_lineage_links(candidates, _OPERATION, "op-1")
    second = queue._bounded_lineage_links(candidates, _OPERATION, "op-1")

    assert (first, len(first)) == (second, 8)
