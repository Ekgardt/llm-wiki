"""Every hook that appends to the daily log gives up before its host cancels it.

A writer with no deadline retried until it was killed and left no reason: 250 lost
tool breadcrumbs on the owner's vault before the first of the three was bounded.
See `docs/research/2026-09-17-every-hook-writer-gives-up-before-its-host-does.md`.
"""
from __future__ import annotations

import json
import math
import sys
import time
from pathlib import Path

REPOSITORY = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY / "scripts"))

import daily_log_append  # noqa: E402
import integration_adapter  # noqa: E402

BREADCRUMB_EVENTS = ("UserPromptSubmit", "PostToolUse")
# Starting the interpreter through `uv`, and writing the failure line afterwards.
ROOM_SECONDS = 2.0


def _breadcrumb_handlers(path: Path) -> list[dict]:
    hooks = json.loads(path.read_text(encoding="utf-8"))["hooks"]
    groups = [group for event in BREADCRUMB_EVENTS for group in hooks[event]]
    return [handler for group in groups for handler in group["hooks"]]


def _our_timeouts(path: Path, script: str) -> list[float]:
    """The timeouts a shipped hook file gives the breadcrumb hooks that run `script`."""
    handlers = _breadcrumb_handlers(path)
    return [float(h["timeout"]) for h in handlers if script in h["command"]]


def test_a_breadcrumb_budget_fits_inside_every_shipped_host_timeout():
    shipped = _our_timeouts(
        REPOSITORY / "integrations/claude-code/settings.json", "integration_adapter.py"
    ) + _our_timeouts(REPOSITORY / "integrations/codex/hooks.json", "integration_adapter.py")

    assert len(shipped) >= 4
    assert daily_log_append.BREADCRUMB_APPEND_BUDGET_SECONDS + ROOM_SECONDS <= min(shipped)


def test_both_budgets_leave_the_delegate_room_to_say_why():
    largest = max(
        daily_log_append.BREADCRUMB_APPEND_BUDGET_SECONDS,
        daily_log_append.LIFECYCLE_APPEND_BUDGET_SECONDS,
    )

    assert integration_adapter.DELEGATE_TIMEOUT_SECONDS - largest >= ROOM_SECONDS


def _remaining(deadline: float) -> float:
    return deadline - time.monotonic()


def test_the_prompt_breadcrumb_is_given_the_breadcrumb_budget(monkeypatch):
    import user_prompt_capture

    seen: list[float] = []
    monkeypatch.setattr(
        daily_log_append,
        "append_daily",
        lambda *_a, deadline=math.inf, **_k: seen.append(_remaining(deadline)),
    )

    user_prompt_capture._append_prompt_tag("slug", "session", "what did we decide")

    assert 0 < seen[0] <= daily_log_append.BREADCRUMB_APPEND_BUDGET_SECONDS


def test_the_tool_breadcrumb_is_given_the_breadcrumb_budget(monkeypatch):
    import post_tool_capture

    seen: list[float] = []
    monkeypatch.setattr(
        daily_log_append,
        "append_daily",
        lambda *_a, deadline=math.inf, **_k: seen.append(_remaining(deadline)),
    )

    post_tool_capture._append_tool_tag("slug", "session", "Bash", "ls")

    assert 0 < seen[0] <= daily_log_append.BREADCRUMB_APPEND_BUDGET_SECONDS


def test_the_session_end_tag_is_given_the_lifecycle_budget(monkeypatch, tmp_path):
    import session_end_project_tag

    seen: list[float] = []
    monkeypatch.setattr(
        session_end_project_tag,
        "locked_append",
        lambda *_a, deadline=math.inf, **_k: seen.append(_remaining(deadline)),
    )

    session_end_project_tag._append_entry(tmp_path / "2026-09-17.md", "## entry\n")

    assert 0 < seen[0] <= daily_log_append.LIFECYCLE_APPEND_BUDGET_SECONDS
