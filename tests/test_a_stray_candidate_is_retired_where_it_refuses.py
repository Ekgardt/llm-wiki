"""A provably stray candidate is moved aside where writers are refused.

`doctor --repair` could retire a stray coordinator candidate, but nothing ran it:
the nightly, the weekly and every hook were refused for six days (2026-09-17..23)
until a person did. Writers now apply the same rule themselves, and the queue
candidate is covered too. See
docs/research/2026-09-24-a-stray-candidate-is-retired-where-it-refuses.md.
"""

from __future__ import annotations

import os
import stat
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import markdown_transaction  # noqa: E402
import memory_queue  # noqa: E402
import operational_ownership  # noqa: E402
from installed_memory_repair import (  # noqa: E402
    ReliabilityV3ValidationError,
    require_reliability_v3_adopted,
    retire_stray_candidates,
)

from tests.test_a_stray_candidate_after_adoption import _adopted_vault, _stray  # noqa: E402

QUEUE_CANDIDATE = "run/queue-v3.candidate.sqlite3"


def _queue_stray(state_root: Path) -> Path:
    candidate = state_root / QUEUE_CANDIDATE
    memory_queue.initialize_queue_v3_candidate(candidate, source_v2=None)
    return candidate


def _now() -> datetime:
    return datetime.now(timezone.utc)


def test_a_writer_retires_an_empty_coordinator_stray_and_is_admitted(tmp_path: Path) -> None:
    root, state_root, _home = _adopted_vault(tmp_path)
    candidate = _stray(state_root)
    markdown_transaction._ADOPTION_VALIDATION_CACHE.clear()

    coordinator = markdown_transaction.active_markdown_coordinator(root, state_root)

    quarantined = list((state_root / "run" / "coordinator-quarantine").iterdir())
    assert (candidate.exists(), len(quarantined), coordinator.vault) == (False, 1, root.resolve())


def test_an_empty_queue_stray_is_retired_into_the_queue_quarantine(tmp_path: Path) -> None:
    _root, state_root, _home = _adopted_vault(tmp_path)
    candidate = _queue_stray(state_root)

    outcome = retire_stray_candidates(state_root, _now())

    assert (candidate.exists(), outcome.kept) == (False, ())
    assert outcome.retired[0].startswith("run/queue-quarantine/")


def test_a_queue_stray_holding_a_task_is_kept_and_still_refuses(tmp_path: Path) -> None:
    root, state_root, _home = _adopted_vault(tmp_path)
    candidate = _queue_stray(state_root)
    memory_queue._QueueV3CandidateReader(candidate).enqueue("flush", 1, {"n": 1})

    outcome = retire_stray_candidates(state_root, _now())

    assert candidate.exists()
    assert any("holds a row in tasks" in reason for reason in outcome.kept)
    with pytest.raises(ReliabilityV3ValidationError):
        require_reliability_v3_adopted(root=root, state_root=state_root)


def test_a_coordinator_stray_with_a_live_owner_is_kept(tmp_path: Path) -> None:
    _root, state_root, _home = _adopted_vault(tmp_path)
    candidate = _stray(state_root)
    operational_ownership.OwnershipRegistry(state_root).acquire(
        "project", scope="project:a", actor_id="actor-a"
    )

    outcome = retire_stray_candidates(state_root, _now())

    assert candidate.exists()
    assert any("maintenance owner actor-a is live" in reason for reason in outcome.kept)


def test_nothing_is_retired_while_an_adoption_is_in_flight(tmp_path: Path) -> None:
    _root, state_root, _home = _adopted_vault(tmp_path)
    candidate = _stray(state_root)
    (state_root / "run" / f".reliability-v3-{'a' * 64}-coordinator.candidate.sqlite3").write_bytes(b"")

    outcome = retire_stray_candidates(state_root, _now())

    assert candidate.exists()
    assert any("adoption operation is in flight" in reason for reason in outcome.kept)


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_the_quarantine_directory_is_owner_only(tmp_path: Path) -> None:
    _root, state_root, _home = _adopted_vault(tmp_path)
    _stray(state_root)
    loose = state_root / "run" / "coordinator-quarantine"
    loose.mkdir(mode=0o775)
    os.chmod(loose, 0o775)

    retire_stray_candidates(state_root, _now())

    assert stat.S_IMODE(loose.stat().st_mode) == 0o700


def test_no_candidate_costs_nothing(tmp_path: Path) -> None:
    _root, state_root, _home = _adopted_vault(tmp_path)

    assert retire_stray_candidates(state_root, _now()) == ((), ())
