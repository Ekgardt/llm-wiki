"""A repair that raises is its own failure; only a lost fence ends the pass.

Finding M-A10 of the third audit. See
`docs/research/2026-09-17-one-failed-repair-does-not-cancel-the-others.md`.
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import doctor  # noqa: E402


def _context(tmp_path: Path) -> doctor._RepairContext:
    return doctor._RepairContext(
        root_path=tmp_path,
        state_path=tmp_path / "state",
        generated_at=datetime(2026, 9, 17, tzinfo=timezone.utc),
        deadline=float("inf"),
        rebuild_generation=False,
        selected_repairs={"runtime", "generations", "queue", "indexes", "archives"},
        repaired=[],
        repair_errors={},
        repair_deferred=set(),
    )


def _raises(error: BaseException):
    def action() -> None:
        raise error

    return action


def test_the_repairs_after_a_failed_one_still_run(tmp_path: Path) -> None:
    context = _context(tmp_path)
    ran: list[str] = []
    ordered = (
        ("generations", _raises(ValueError("catalog is unreadable"))),
        ("queue", lambda: ran.append("queue")),
        ("archives", lambda: ran.append("archives")),
    )

    doctor._run_selected_repairs(context.selected_repairs, ordered, context)

    assert ran == ["queue", "archives"]
    assert list(context.repair_errors) == ["generations"]
    assert "catalog is unreadable" in context.repair_errors["generations"][0]


def test_the_failed_repair_is_named_and_its_checks_marked_deferred(
    tmp_path: Path,
) -> None:
    """Filed under `runtime`, a generation fault read as a runtime fault."""
    context = _context(tmp_path)
    ordered = (("generations", _raises(ValueError("catalog is unreadable"))),)

    doctor._run_selected_repairs(context.selected_repairs, ordered, context)

    assert (
        "runtime" in context.repair_errors,
        context.repair_deferred,
    ) == (False, {"generation"})


@pytest.mark.parametrize(
    "error",
    [doctor.MaintenanceFenceLost("heartbeat", {}), TimeoutError("deadline")],
)
def test_a_lost_fence_or_a_passed_deadline_ends_the_pass(
    tmp_path: Path, error: BaseException
) -> None:
    context = _context(tmp_path)
    ran: list[str] = []
    ordered = (
        ("generations", _raises(error)),
        ("queue", lambda: ran.append("queue")),
    )

    with pytest.raises(type(error)):
        doctor._run_selected_repairs(context.selected_repairs, ordered, context)

    assert (ran, context.repair_errors) == ([], {})


def test_an_archive_repair_that_recovered_nothing_records_nothing(
    tmp_path: Path,
) -> None:
    context = _context(tmp_path)

    doctor._record_recovered_archives([], context)
    empty = list(context.repaired)
    doctor._record_recovered_archives(["one", "two"], context)

    assert (empty, context.repaired) == (
        [],
        [{"action": "recover_archives", "count": 2}],
    )


class _Coordinator:
    """A fence that was ours when the work began and is not ours now."""

    def __init__(self) -> None:
        self.checks = 0


def test_the_index_lock_is_released_even_when_the_fence_was_lost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    released: list[str] = []

    def require(coordinator, lease) -> None:
        raise doctor.MaintenanceFenceLost("require", {})

    guard = doctor._MaintenanceHeartbeat(
        _Coordinator(), {"owner": None}, deadline=float("inf")
    )
    monkeypatch.setattr(doctor, "_require_maintenance_owner", require)

    with pytest.raises(doctor.MaintenanceFenceLost):
        guard.cleanup(lambda: released.append("lock"))

    assert released == ["lock"]
