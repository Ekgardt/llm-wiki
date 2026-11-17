"""A reclaim sweep names what it could not remove and stops at its step's deadline (audit 2026-09-27 C-2, C-3).

docs/research/2026-09-27-a-sweep-says-what-it-could-not-do.md
"""
from __future__ import annotations

import os
import time
from pathlib import Path

import reclaim_runtime_state as reclaim

HOUR_AND_MORE = reclaim.ORPHAN_TEMP_SECONDS + 60


def _aged(path: Path) -> None:
    stamp = time.time() - HOUR_AND_MORE
    os.utime(path, (stamp, stamp))


def test_an_orphan_that_cannot_be_removed_is_a_named_failure(tmp_path: Path) -> None:
    (tmp_path / ".stuck.tmp").mkdir()
    (tmp_path / ".gone.tmp").write_bytes(b"x" * 10)
    _aged(tmp_path / ".stuck.tmp")
    _aged(tmp_path / ".gone.tmp")

    result = reclaim.sweep_orphan_temporaries(tmp_path)

    assert (result["removed"], result["bytes"], result["failed"], str(result["reason"]).endswith(".stuck.tmp")) == (
        1, 10, 1, True
    )


def test_the_knowledge_walk_stops_at_the_step_deadline_and_says_so(tmp_path: Path) -> None:
    staged = tmp_path / f".journal.md.{'a' * 32}.tmp"
    staged.write_bytes(b"x")
    _aged(staged)

    late = reclaim.sweep_staged_knowledge_writes(tmp_path, deadline=time.monotonic() - 1)
    done = reclaim.sweep_staged_knowledge_writes(tmp_path)

    assert (late["removed"], late["unfinished"], done["removed"], done["unfinished"]) == (0, True, 1, False)


def test_a_sweep_failure_reaches_the_night_report() -> None:
    result = {key: {} for key in ("transactions", "history", "staged_writes")}
    result["temporaries"] = {"failed": 1, "reason": "PermissionError: .x.tmp"}
    result["snapshot"] = {"status": "ok"}
    result["backlog"] = {"failed": []}

    assert reclaim.failures(result) == ["temporaries: PermissionError: .x.tmp"]
