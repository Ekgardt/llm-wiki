"""A leftover v2 fence does not lock a vault out of adoption, and a refusal says why.

`_require_unambiguous_v2_owners` refused while any `source_fences` row existed at
all. A fence row outlives the process that took it — the live v2 queue sweeps
expired ones on every open — so one crash during an archive run meant the vault
could never adopt, and the operator saw only `reliability_v3_adoption_failed`.

See `docs/research/2026-09-18-an-expired-fence-is-not-an-obstacle-to-adoption.md`.
"""

from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import installed_memory_repair  # noqa: E402
import memory_queue  # noqa: E402
from memory_queue import MemoryQueue  # noqa: E402
from reliable_memory import OperationalDatabaseContractError  # noqa: E402

_DAY = "2026-01-01"
_DIGEST = "d" * 64


def _stamp(offset: timedelta) -> str:
    return memory_queue._timestamp(datetime.now(timezone.utc) + offset)


def _queue_with_fence(tmp_path: Path, offset: timedelta) -> MemoryQueue:
    """A v2 queue holding one fence row whose lease ends at `offset` from now."""
    queue = MemoryQueue(tmp_path)
    queue.acquire_source_fence(_DAY, _DIGEST)
    with sqlite3.connect(queue.db_path) as connection:
        connection.execute(
            "UPDATE source_fences SET expires_at=?", (_stamp(offset),)
        )
    return queue


def _adopt(tmp_path: Path, queue: MemoryQueue) -> None:
    memory_queue.initialize_queue_v3_candidate(
        tmp_path / "run" / "queue-v3.candidate.sqlite3", source_v2=queue.db_path
    )


def test_a_fence_left_by_a_dead_run_does_not_block_the_candidate(
    tmp_path: Path,
) -> None:
    queue = _queue_with_fence(tmp_path, timedelta(hours=-1))

    _adopt(tmp_path, queue)

    assert (tmp_path / "run" / "queue-v3.candidate.sqlite3").is_file()


def test_a_fence_that_is_still_held_still_refuses(tmp_path: Path) -> None:
    queue = _queue_with_fence(tmp_path, timedelta(hours=1))

    with pytest.raises(OperationalDatabaseContractError) as raised:
        _adopt(tmp_path, queue)

    assert raised.value.code == "queue_v2_source_fence_ambiguous"


def test_the_operator_is_told_which_precondition_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(**_kwargs: object) -> bool:
        raise memory_queue._migration_error(
            "queue_v2_source_fence_ambiguous", "some message that stays private"
        )

    monkeypatch.setattr(
        installed_memory_repair, "_apply_reliability_v3_adoption", refuse
    )

    report = installed_memory_repair.repair_installed_vault(
        root=tmp_path,
        state_root=tmp_path,
        adopt_ownership_v3=True,
        confirm_all_agents_stopped=True,
    )

    assert [blocker["code"] for blocker in report["blockers"]] == [
        "reliability_v3_adoption_failed",
        "queue_v2_source_fence_ambiguous",
    ]
