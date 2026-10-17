"""A sync that did its work exits 1 on a vault finding, and 2 only when it failed.

The live update of 2026-09-28 finished every install step, then stopped with
`[FAIL] Runtime synchronization failed` on 183 refused writes from the three days
before it: a finding the sync reports and no install can settle. The installers
already fail the smoke only on the checks the install owns; the sync now agrees.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location("sync_memory_exit", ROOT / "scripts" / "sync_memory.py")
sync_memory = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(sync_memory)


def _report(*actions: dict) -> dict:
    statuses = {action["status"] for action in actions}
    overall = "error" if "error" in statuses else "ok"
    return {"overall_status": overall, "actions": list(actions)}


def _action(action_id: str, status: str, **details: object) -> dict:
    return {"id": action_id, "status": status, "details": details}


def test_refused_writes_in_the_vault_are_a_warning_not_a_failed_sync() -> None:
    report = _report(
        _action("transactions", "error"),
        _action("doctor", "error", errors=["transactions", "scheduler"]),
    )

    assert sync_memory._exit_code(report) == 1


def test_an_error_in_a_check_the_install_owns_still_fails() -> None:
    report = _report(_action("doctor", "error", errors=["adoption"]))

    assert sync_memory._exit_code(report) == 2


def test_an_action_that_could_not_run_fails_even_when_it_only_reports() -> None:
    report = _report(_action("queue", "error", error="OSError", failed=True))

    assert sync_memory._exit_code(report) == 2


def test_the_sync_s_own_work_failing_fails() -> None:
    report = _report(_action("dependencies", "error"))

    assert sync_memory._exit_code(report) == 2
