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
import pytest  # noqa: E402

# These tests measure the budgets themselves; every other test gets the slow-machine bound.
pytestmark = pytest.mark.shipped_append_budgets

BREADCRUMB_EVENTS = ("UserPromptSubmit", "PostToolUse")
# Starting the interpreter through `uv`, and writing the failure line afterwards.
ROOM_SECONDS = 2.0


def _breadcrumb_handlers(path: Path) -> list[tuple[str, dict]]:
    hooks = json.loads(path.read_text(encoding="utf-8"))["hooks"]
    groups = [(event, group) for event in BREADCRUMB_EVENTS for group in hooks[event]]
    return [(event, handler) for event, group in groups for handler in group["hooks"]]


def _host_timeout(path: Path, event: str, handler: dict) -> float:
    # Official host references checked 2026-10-05; Codex async still uses timeout.
    if path.parent.name == "codex":
        return handler.get("timeout", 600)
    from tests.test_a_claude_prompt_takes_the_ingest_path import _effective_host_timeout

    return _effective_host_timeout(event, handler)


def _our_timeouts(path: Path, script: str) -> list[float]:
    """The timeouts a shipped hook file gives the breadcrumb hooks that run `script`."""
    handlers = _breadcrumb_handlers(path)
    return [float(_host_timeout(path, event, h)) for event, h in handlers if script in h["command"]]


def test_codex_host_default_and_explicit_ceiling_are_both_measured():
    path = REPOSITORY / "integrations/codex/hooks.json"
    assert _host_timeout(path, "UserPromptSubmit", {"type": "command"}) == 600
    assert _host_timeout(path, "PostToolUse", {"type": "command", "async": True, "timeout": 5}) == 5


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
