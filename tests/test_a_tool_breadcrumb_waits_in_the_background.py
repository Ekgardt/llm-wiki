"""A tool breadcrumb whose hook runs in the background outlasts a writer holding the gate.

On 2026-09-28 the live vault lost 738 breadcrumbs: the PostToolUse hook had to
finish inside the host's 5 s, so its append gave up after 2.5 s whenever a writer
held the gate or the CPU was busy. The hook now runs with `async: true` and its
append waits out one full writer lease. A real second writer holds the gate here.
See docs/research/2026-09-29-a-tool-breadcrumb-waits-in-the-background.md.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import daily_log_append
import integration_adapter
import markdown_transaction
import pytest
from installed_memory_repair import repair_installed_vault

from tests.test_reliability_v3_adoption import _vault

ROOT = Path(__file__).resolve().parents[1]
# The budgets are the subject here, so the shipped values stand (tests/conftest.py).
pytestmark = pytest.mark.shipped_append_budgets
# Longer than the foreground budget, well inside the background one.
_HELD_SECONDS = daily_log_append.BREADCRUMB_APPEND_BUDGET_SECONDS + 1.0


@pytest.fixture
def vault(tmp_path: Path, monkeypatch) -> Path:
    root, state_root = _vault(tmp_path)
    repair_installed_vault(
        root=root, state_root=state_root, adopt_ownership_v3=True, confirm_all_agents_stopped=True
    )
    monkeypatch.setenv("LLM_WIKI_ROOT", str(root))
    monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(state_root))
    return root


def _hold_the_gate(held: threading.Event) -> None:
    writer = markdown_transaction._default_coordinator()
    with writer.writer_gate(wait_seconds=10):
        held.set()
        threading.Event().wait(_HELD_SECONDS)


def _append_while_held(vault: Path, budget: float) -> None:
    held = threading.Event()
    writer = threading.Thread(target=_hold_the_gate, args=(held,), daemon=True)
    writer.start()
    held.wait(10)
    try:
        markdown_transaction.append_knowledge(
            "breadcrumb:" + "0" * 52,
            vault / "knowledge" / "daily" / "2026-09-29.md",
            b"- tool breadcrumb\n",
            deadline=daily_log_append.append_deadline(budget),
        )
    finally:
        writer.join(_HELD_SECONDS + 10)


def test_the_background_budget_outlasts_a_writer_the_foreground_budget_does_not(vault: Path) -> None:
    _append_while_held(vault, daily_log_append.BACKGROUND_APPEND_BUDGET_SECONDS)

    assert b"- tool breadcrumb\n" in (vault / "knowledge" / "daily" / "2026-09-29.md").read_bytes()


def test_the_foreground_budget_still_gives_up_inside_its_host(vault: Path) -> None:
    with pytest.raises(TimeoutError):
        _append_while_held(vault, daily_log_append.BREADCRUMB_APPEND_BUDGET_SECONDS)


def test_the_background_delegate_is_stopped_after_its_own_budget() -> None:
    assert (
        integration_adapter._delegate_timeout("post_tool_capture.py", True),
        integration_adapter._delegate_timeout("post_tool_capture.py", False),
        integration_adapter._delivery_arguments(True),
    ) == (
        daily_log_append.BACKGROUND_APPEND_BUDGET_SECONDS + integration_adapter.DELEGATE_STARTUP_SECONDS,
        daily_log_append.BREADCRUMB_APPEND_BUDGET_SECONDS + integration_adapter.DELEGATE_STARTUP_SECONDS,
        ["--background"],
    )


def test_only_the_tool_capture_hooks_run_in_the_background() -> None:
    """A hook that returns context to Claude (the prompt hook) must stay in the foreground."""
    background = sorted(event for event, hook in _shipped_hooks() if _runs_in_background(hook))

    assert background == ["PostToolUse", "PostToolUseFailure"]


def _shipped_hooks() -> list[tuple[str, dict]]:
    hooks = json.loads((ROOT / "integrations" / "claude-code" / "settings.json").read_text())["hooks"]
    return [(event, hook) for event, blocks in hooks.items() for block in blocks for hook in block["hooks"]]


def _runs_in_background(hook: dict) -> bool:
    return bool(hook.get("async")) and "--background" in hook["command"]
