"""Doctor's maintenance fence on an adopted Reliability V3 vault (NEW-109).

The adopted coordinator holds maintenance owners in the v3 schema
(role/scope/actor_id — no `owner_name` column). Doctor's fence used to speak
the legacy schema by raw SQL at whatever coordinator it was handed, so on an
adopted vault every generation refresh died with
`sqlite3.OperationalError: no such column: owner_name`. The fix routes the
adopted path through `operational_ownership.OwnershipRegistry` — the same
registry the capture worker and the project store use — and keeps the legacy
SQL untouched for a legacy root.
"""

from __future__ import annotations

import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import doctor  # noqa: E402
from installed_memory_repair import repair_installed_vault  # noqa: E402
from operational_ownership import OwnershipRegistry  # noqa: E402

_V3_DATABASE = "run/markdown-transactions-v3.sqlite3"


def _adopted_vault(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "vault"
    state_root = tmp_path / "state"
    (root / "scripts").mkdir(parents=True)
    (root / "scripts/integration_adapter.py").write_bytes(
        (SCRIPTS_DIR / "integration_adapter.py").read_bytes()
    )
    notes = root / "knowledge" / "notes"
    notes.mkdir(parents=True)
    (notes / "fixture-page.md").write_text(
        "---\ntype: concept\n---\n# Fixture Page\n\nOne page so the corpus is not empty.\n",
        encoding="utf-8",
    )
    report = repair_installed_vault(
        root=root,
        state_root=state_root,
        adopt_ownership_v3=True,
        confirm_all_agents_stopped=True,
    )
    assert report["overall_status"] == "ok"
    return root, state_root


def _v3_registry(state_root: Path) -> OwnershipRegistry:
    return OwnershipRegistry._from_adopted_database(  # noqa: SLF001
        state_root, state_root / _V3_DATABASE
    )


def _v3_owner_rows(state_root: Path) -> list[tuple]:
    with sqlite3.connect(state_root / _V3_DATABASE) as database:
        return database.execute(
            "SELECT role, scope FROM maintenance_owners"
        ).fetchall()


def test_generation_maintenance_runs_on_an_adopted_vault(tmp_path: Path) -> None:
    """The refresh must not raise; it runs or defers with a named outcome."""
    root, state_root = _adopted_vault(tmp_path)

    outcome = doctor.run_generation_maintenance(
        root, state_root, time_budget_seconds=60, max_sources=200
    )

    assert outcome["status"] in {"built", "ok", "deferred"}, outcome
    assert outcome.get("reason") != "OperationalError", outcome


def test_the_adopted_fence_is_released_after_the_pass(tmp_path: Path) -> None:
    root, state_root = _adopted_vault(tmp_path)

    doctor.run_generation_maintenance(
        root, state_root, time_budget_seconds=60, max_sources=200
    )

    assert _v3_owner_rows(state_root) == []


def test_a_live_v3_owner_defers_the_refresh_as_busy(tmp_path: Path) -> None:
    root, state_root = _adopted_vault(tmp_path)
    registry = _v3_registry(state_root)
    owner = registry.acquire(
        doctor._V3_MAINTENANCE_ROLE, scope=doctor._V3_MAINTENANCE_SCOPE
    )
    try:
        outcome = doctor.run_generation_maintenance(
            root, state_root, time_budget_seconds=20, max_sources=5
        )
    finally:
        registry.release(owner)

    assert outcome["status"] == "deferred", outcome
    assert outcome["reason"] == "maintenance_owner_busy", outcome


def test_the_v3_lease_survives_heartbeat_require_and_release(tmp_path: Path) -> None:
    root, state_root = _adopted_vault(tmp_path)
    now = datetime.now(timezone.utc)

    acquired = doctor._acquire_maintenance_owner(root, state_root, now)
    assert acquired is not None
    coordinator, lease = acquired

    doctor._heartbeat_maintenance_owner(coordinator, lease)
    doctor._require_maintenance_owner(coordinator, lease)
    doctor._release_maintenance_owner(coordinator, lease)
    assert _v3_owner_rows(state_root) == []


def test_a_lost_v3_fence_raises_the_named_fence_loss(tmp_path: Path) -> None:
    """Downstream `_fence_lost_outcome` keys on this exact message."""
    root, state_root = _adopted_vault(tmp_path)
    now = datetime.now(timezone.utc)
    coordinator, lease = doctor._acquire_maintenance_owner(root, state_root, now)
    doctor._release_maintenance_owner(coordinator, lease)

    with pytest.raises(doctor.MaintenanceFenceLost) as caught:
        doctor._heartbeat_maintenance_owner(coordinator, lease)

    assert str(caught.value) == "maintenance_owner_fence_lost"
    assert caught.value.where == "heartbeat"
    assert caught.value.observed["present"] is False


def _legacy_owner_row(state_root: Path) -> tuple | None:
    with sqlite3.connect(state_root / "run/markdown-transactions.sqlite3") as database:
        return database.execute(
            "SELECT owner_name, owner_token, process_id FROM maintenance_owners"
            " WHERE owner_name='doctor'"
        ).fetchone()


def test_a_legacy_root_still_writes_the_owner_name_row(tmp_path: Path) -> None:
    """The pre-adoption path keeps its schema, lease shape, and lifecycle."""
    root = tmp_path / "vault"
    (root / "knowledge" / "notes").mkdir(parents=True)
    now = datetime.now(timezone.utc)

    acquired = doctor._acquire_maintenance_owner(root, root, now)
    assert acquired is not None
    coordinator, lease = acquired
    assert set(lease) == {"token", "epoch"}

    row = _legacy_owner_row(root)
    assert row == ("doctor", lease["token"], os.getpid())

    doctor._heartbeat_maintenance_owner(coordinator, lease)
    doctor._require_maintenance_owner(coordinator, lease)
    doctor._release_maintenance_owner(coordinator, lease)
    assert _legacy_owner_row(root) == ("doctor", "", 0)
