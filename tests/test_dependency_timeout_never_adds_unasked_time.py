"""Relative budgets cannot gain time from arithmetic or a timeout floor."""
import subprocess
import time

from tests.test_sync_memory import (
    _load_sync_memory,
)
from tests.test_sync_memory import (
    test_dependency_action_checks_lock_and_baseline_environment as original_scenario,
)


def test_default_budget_keeps_its_exact_upper_bound(tmp_path, monkeypatch):
    monkeypatch.setattr(time, "monotonic", lambda: 4095.02)
    assert (time.monotonic() + 30.0) - time.monotonic() > 30.0
    original_scenario(tmp_path)


def test_positive_remaining_budget_is_not_raised_to_a_millisecond(tmp_path):
    sync = _load_sync_memory()
    calls = []

    def run(command, **kwargs):
        calls.append(kwargs["timeout"])
        return subprocess.CompletedProcess(command, 0, "", "")

    completed, error = sync._run_uv_step(sync._LOCK_STEP, run, root=tmp_path, remaining=lambda: 0.0005)
    assert completed is not None and error is None
    assert calls == [0.0005]


def test_explicit_whole_action_deadline_keeps_the_callers_larger_budget(monkeypatch):
    sync = _load_sync_memory()
    monkeypatch.setattr(time, "monotonic", lambda: 4096.0)
    assert sync._dependency_clock(4224.0, 30.0)() == 128.0


def test_expired_remaining_budget_starts_no_command(tmp_path):
    sync = _load_sync_memory()
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    completed, error = sync._run_uv_step(sync._LOCK_STEP, run, root=tmp_path, remaining=lambda: 0.0)
    assert completed is None and error["status"] == "error"
    assert calls == []


def test_invalid_nan_deadline_cannot_become_an_unbounded_allowance():
    sync = _load_sync_memory()
    assert sync._remaining_clock(float("nan"))() == 0.0
