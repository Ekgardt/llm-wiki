"""The Windows weekly task's limit sits above the weekly pass's own worst case.

It was two hours against a pass that runs the whole nightly one (2.4 hours) first, and
reflection had no bound at all. Research:
`docs/research/2026-09-14-the-weekly-task-outlasts-its-pass.md`.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

import scheduled_weekly  # noqa: E402


def _weekly_limit_seconds() -> float:
    from tests.test_the_scheduler_outlasts_the_pass import script_limit_hours

    return float(script_limit_hours()["weekly"]) * 3600


def test_the_weekly_task_is_not_killed_inside_its_own_bounds():
    assert scheduled_weekly.worst_case_seconds() < _weekly_limit_seconds()


def test_reflection_stops_starting_pages_when_its_budget_is_spent(monkeypatch, tmp_path):
    import reflection

    pages = [{"path": tmp_path / f"{name}.md"} for name in ("a", "b", "c")]
    reflected: list[str] = []
    clock = iter([0.0, 0.0, scheduled_weekly.REFLECTION_BUDGET_SECONDS, 10**6])
    monkeypatch.setattr(reflection, "find_reflection_candidates", lambda: pages)
    monkeypatch.setattr(reflection, "reflect_page", lambda path, apply: reflected.append(path.stem) or "ok")
    monkeypatch.setattr(scheduled_weekly.time, "monotonic", lambda: next(clock))
    lines: list[str] = []

    scheduled_weekly._reflect_candidates(lines.append)

    assert (reflected, lines[-1]) == (["a"], "  reflection budget spent: 2 page(s) wait for next week")
