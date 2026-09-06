"""A PostToolUse breadcrumb must give up before its parent kills it.

The delegate is killed at `integration_adapter.DELEGATE_TIMEOUT_SECONDS`.
`append_daily` defaults to no deadline, so the append retried its
compare-and-swap on a contended daily log until the kill arrived: the
breadcrumb was lost, a refused attempt was left behind, and the child never
got to record why. 250 losses on this vault, all of them while a benchmark
held all four cores.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import integration_adapter  # noqa: E402
import post_tool_capture  # noqa: E402


def test_the_append_gives_up_before_the_parent_kills_the_delegate():
    assert (
        post_tool_capture.APPEND_BUDGET_SECONDS
        < integration_adapter.DELEGATE_TIMEOUT_SECONDS
    )


def test_the_budget_leaves_room_to_record_the_failure():
    """Enough margin that the child can write its own diagnostics line."""
    margin = (
        integration_adapter.DELEGATE_TIMEOUT_SECONDS
        - post_tool_capture.APPEND_BUDGET_SECONDS
    )
    assert margin >= 2.0


def test_the_append_is_given_a_deadline(monkeypatch):
    seen = {}

    def _record(slug, session_id, block, *, operation_id=None, deadline=None):
        seen["deadline"] = deadline

    monkeypatch.setitem(
        sys.modules,
        "daily_log_append",
        type(sys)("daily_log_append"),
    )
    sys.modules["daily_log_append"].append_daily = _record

    assert post_tool_capture._append_tool_tag("slug", "session", "Bash", "ls")
    assert seen["deadline"] is not None
