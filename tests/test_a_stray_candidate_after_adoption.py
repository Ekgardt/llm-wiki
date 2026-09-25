"""A stray pre-adoption candidate is named by doctor and retired by `--repair`.

On 2026-09-17 a pytest session whose state root resolved to the live vault left an
empty `run/markdown-transactions-v3.candidate.sqlite3` there; the adoption boundary
refused every Markdown writer for six days and doctor named only the symptoms. See
`docs/research/2026-09-23-a-stray-candidate-stopped-the-memory-for-six-days.md`.
"""

from __future__ import annotations

import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import doctor  # noqa: E402
import markdown_transaction  # noqa: E402
import operational_ownership  # noqa: E402
from installed_memory_repair import (  # noqa: E402
    ReliabilityV3ValidationError,
    require_reliability_v3_adopted,
)

from tests import conftest  # noqa: E402
from tests.slow_machine import LONG_TIMEOUT  # noqa: E402
from tests.test_doctor import GENEROUS_BUDGET_SECONDS, _adopt, _build_root  # noqa: E402

CANDIDATE = "run/markdown-transactions-v3.candidate.sqlite3"


def _adopted_vault(tmp_path: Path) -> tuple[Path, Path, Path]:
    root, state_root, home = _build_root(tmp_path)
    _adopt(root, state_root)
    return root, state_root, home


def _stray(state_root: Path) -> Path:
    candidate = state_root / CANDIDATE
    markdown_transaction.initialize_coordinator_v3_candidate(candidate, source_v2=None)
    return candidate


def _doctor(root: Path, state_root: Path, home: Path, *, repair: bool = False) -> dict:
    return doctor.run_doctor(
        root=root,
        state_root=state_root,
        home=home,
        repair=repair,
        time_budget_seconds=GENEROUS_BUDGET_SECONDS,
    )


def _check(report: dict, check_id: str) -> dict:
    return next(check for check in report["checks"] if check["id"] == check_id)


def test_doctor_names_the_refusal_and_the_stray_file(tmp_path: Path) -> None:
    root, state_root, home = _adopted_vault(tmp_path)
    _stray(state_root)

    adoption = _check(_doctor(root, state_root, home), "adoption")

    assert (adoption["status"], adoption["details"]["code"], adoption["details"]["stray_candidates"]) == (
        "error",
        "reliability_v3_record_invalid",
        [CANDIDATE],
    )
    assert "candidate artifacts remain after adoption" in adoption["message"]
    assert "--repair" in adoption["message"]


def test_a_healthy_adopted_vault_passes_the_adoption_check(tmp_path: Path) -> None:
    root, state_root, home = _adopted_vault(tmp_path)

    adoption = _check(_doctor(root, state_root, home), "adoption")

    assert (adoption["status"], adoption["details"]) == (
        "ok",
        {"adopted": True, "stray_candidates": [], "quarantined_candidates": 0},
    )


def test_repair_retires_an_empty_stray_and_writers_are_admitted_again(tmp_path: Path) -> None:
    root, state_root, home = _adopted_vault(tmp_path)
    candidate = _stray(state_root)

    report = _doctor(root, state_root, home, repair=True)

    retired = [entry for entry in report["repaired"] if entry["action"] == "retire_stray_candidate"]
    quarantined = state_root / retired[0]["path"]
    assert (candidate.exists(), quarantined.parent.name, quarantined.name.endswith(candidate.name)) == (
        False,
        "coordinator-quarantine",
        True,
    )
    require_reliability_v3_adopted(root=root, state_root=state_root)
    assert _check(report, "adoption")["status"] == "ok"


def test_the_quarantined_file_is_retained_evidence_for_the_deletion_contract(tmp_path: Path) -> None:
    root, state_root, home = _adopted_vault(tmp_path)
    _stray(state_root)
    _doctor(root, state_root, home, repair=True)

    queue = _check(_doctor(root, state_root, home), "queue")

    assert "coordinator_quarantine_retained" in queue["details"]["deletion_codes"]


def test_a_candidate_with_a_live_maintenance_owner_is_kept(tmp_path: Path) -> None:
    root, state_root, home = _adopted_vault(tmp_path)
    candidate = _stray(state_root)
    registry = operational_ownership.OwnershipRegistry(state_root)
    registry.acquire("project", scope="project:a", actor_id="actor-a")

    report = _doctor(root, state_root, home, repair=True)

    assert candidate.exists()
    assert any("maintenance owner actor-a is live" in line for line in _check(report, "runtime")["details"]["repair_errors"])


def test_a_candidate_whose_owners_have_expired_is_retired(tmp_path: Path) -> None:
    root, state_root, home = _adopted_vault(tmp_path)
    candidate = _stray(state_root)
    past = datetime.now(timezone.utc) - timedelta(days=6)
    registry = operational_ownership.OwnershipRegistry(state_root, clock=lambda: past)
    registry.acquire("project", scope="project:a", actor_id="actor-a")

    _doctor(root, state_root, home, repair=True)

    assert not candidate.exists()


def test_a_candidate_holding_a_transaction_is_kept(tmp_path: Path) -> None:
    root, state_root, home = _adopted_vault(tmp_path)
    candidate = _stray(state_root)
    coordinator = markdown_transaction.MarkdownCoordinator._from_v3_candidate(
        candidate, state_root=state_root
    )
    coordinator.vault = root
    (root / "knowledge" / "daily").mkdir(parents=True, exist_ok=True)
    markdown_transaction._append_until_committed(
        coordinator,
        "stray-write",
        "knowledge/daily/2026-09-23.md",
        b"# header\n",
        cancelled=None,
        deadline=time.monotonic() + LONG_TIMEOUT,
    )

    report = _doctor(root, state_root, home, repair=True)

    assert candidate.exists()
    assert any("holds a row in transaction" in line for line in _check(report, "runtime")["details"]["repair_errors"])
    with pytest.raises(ReliabilityV3ValidationError):
        require_reliability_v3_adopted(root=root, state_root=state_root)


def test_the_harness_refuses_a_state_root_that_is_the_vault_or_inside_it(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    (vault / "run").mkdir(parents=True)
    outside = tmp_path / "state"

    verdicts = (
        conftest._external_state_root_problem(vault, vault) is not None,
        conftest._external_state_root_problem(vault / "run", vault) is not None,
        conftest._external_state_root_problem(outside, vault),
    )

    assert verdicts == (True, True, None)


def test_the_progress_file_gets_its_directory_before_the_first_test(tmp_path: Path) -> None:
    path = tmp_path / "pytest-timings" / "progress.txt"

    armed = conftest._arm_progress_file(str(path))

    assert (armed, path.parent.is_dir(), path.exists()) == (str(path), True, False)
