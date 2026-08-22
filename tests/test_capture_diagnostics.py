"""A lost capture must leave a trace (OPEN-013).

Prompt and post-tool capture stay fail-open — the hook still exits 0 — but the
failure is now recorded durably: one line in `logs/capture-failures.jsonl` and
one counter in `state.json`, surfaced in the SessionStart block.
"""
from __future__ import annotations

import json

import pytest


@pytest.fixture
def diagnostics(tmp_path, monkeypatch):
    import capture_diagnostics

    reports = tmp_path / "logs"
    monkeypatch.setattr(capture_diagnostics, "REPORTS_DIR", reports)
    monkeypatch.setattr(capture_diagnostics, "FAILURE_LOG", reports / "capture-failures.jsonl")

    state: dict = {}

    def _update_state(mutator, **kwargs):
        mutator(state)

    monkeypatch.setattr(capture_diagnostics, "update_state", _update_state)
    return capture_diagnostics, state


def test_failure_is_recorded_in_trail_and_counter(diagnostics):
    module, state = diagnostics

    module.record_capture_failure(
        "user_prompt_append",
        "OSError: disk full",
        slug="demo",
        session_id="0123456789",
    )

    entry = json.loads(module.FAILURE_LOG.read_text(encoding="utf-8").strip())
    assert entry["kind"] == "user_prompt_append"
    assert entry["reason"] == "OSError: disk full"
    assert entry["slug"] == "demo"
    assert entry["session"] == "01234567"
    assert state["capture_failures"]["user_prompt_append"]["count"] == 1


def test_counter_accumulates_and_surfaces_a_session_line(diagnostics):
    module, state = diagnostics

    for _ in range(3):
        module.record_capture_failure("post_tool_append", "ValueError: bad target")

    assert module.capture_failure_totals(state) == {"post_tool_append": 3}
    line = module.capture_failure_line(state)
    assert "3 capture(s) lost" in line
    assert "post_tool_append 3" in line


def test_clean_state_produces_no_session_line(diagnostics):
    module, _ = diagnostics

    assert module.capture_failure_line({}) == ""
    assert module.capture_failure_totals({"capture_failures": "broken"}) == {}


def test_trail_is_bounded(diagnostics, monkeypatch):
    module, _ = diagnostics
    monkeypatch.setattr(module, "MAX_FAILURE_LOG_BYTES", 400)

    for number in range(50):
        module.record_capture_failure("user_prompt_hook", f"RuntimeError: {number}")

    written = module.FAILURE_LOG.read_text(encoding="utf-8")
    assert len(written.encode("utf-8")) <= 400 + 1
    assert "RuntimeError: 49" in written
    assert "RuntimeError: 0\"" not in written


def test_reason_is_redacted(diagnostics):
    module, _ = diagnostics

    module.record_capture_failure(
        "user_prompt_append",
        "RuntimeError: token sk-ant-api03-AAAABBBBCCCCDDDDEEEEFFFFGGGGHHHH rejected",
    )

    written = module.FAILURE_LOG.read_text(encoding="utf-8")
    assert "AAAABBBBCCCC" not in written


def test_prompt_append_failure_is_recorded(monkeypatch):
    import user_prompt_capture

    recorded: list[tuple] = []
    monkeypatch.setattr(
        user_prompt_capture,
        "record_capture_failure",
        lambda kind, reason, **fields: recorded.append((kind, reason, fields)),
    )
    monkeypatch.setitem(
        __import__("sys").modules,
        "daily_log_append",
        type("M", (), {"append_daily": staticmethod(_raise_disk_full)}),
    )

    assert user_prompt_capture._append_prompt_tag("demo", "session", "hello") is False
    assert recorded and recorded[0][0] == "user_prompt_append"
    assert "OSError" in recorded[0][1]


def test_tool_append_failure_is_recorded(monkeypatch):
    import post_tool_capture

    recorded: list[tuple] = []
    monkeypatch.setattr(
        post_tool_capture,
        "record_capture_failure",
        lambda kind, reason, **fields: recorded.append((kind, reason, fields)),
    )
    monkeypatch.setitem(
        __import__("sys").modules,
        "daily_log_append",
        type("M", (), {"append_daily": staticmethod(_raise_disk_full)}),
    )

    assert post_tool_capture._append_tool_tag("demo", "session", "Edit", "a.py") is False
    assert recorded and recorded[0][0] == "post_tool_append"


def _raise_disk_full(*args, **kwargs):
    raise OSError("no space left on device")


def _recent_moment(days_ago: float = 0.0) -> str:
    from datetime import datetime, timedelta

    return (datetime.now() - timedelta(days=days_ago)).isoformat(timespec="seconds")


def test_session_start_shows_lost_captures(monkeypatch):
    import session_start_context

    monkeypatch.setattr(
        session_start_context,
        "_load_state_safe",
        lambda: {
            "capture_failures": {
                "user_prompt_append": {"count": 2, "last_at": _recent_moment()}
            }
        },
    )

    block = session_start_context.metacognitive_block()

    assert "2 capture(s) lost" in block


def test_session_start_stops_naming_a_loss_that_stopped_happening(monkeypatch):
    """A warning nobody can clear is a warning nobody reads.

    The counter keeps the evidence; the line covers the last seven days, so a
    quiet week takes it away by itself and a new loss brings it back at once.
    """
    import session_start_context

    monkeypatch.setattr(
        session_start_context,
        "_load_state_safe",
        lambda: {
            "capture_failures": {
                "user_prompt_append": {"count": 2, "last_at": _recent_moment(30)}
            }
        },
    )

    block = session_start_context.metacognitive_block()

    assert "capture(s) lost" not in block


def test_a_capture_lost_today_is_live_and_one_lost_last_month_is_not():
    import capture_diagnostics

    live = {"capture_failures": {"session_end": {"count": 1, "last_at": _recent_moment(1)}}}
    old = {"capture_failures": {"session_end": {"count": 1, "last_at": _recent_moment(30)}}}
    undated = {"capture_failures": {"session_end": {"count": 1}}}

    assert capture_diagnostics.capture_failure_is_live(live) is True
    assert capture_diagnostics.capture_failure_is_live(old) is False
    assert capture_diagnostics.capture_failure_is_live(undated) is False
    assert capture_diagnostics.capture_failure_totals(old) == {"session_end": 1}


def test_the_line_says_so_when_the_trail_is_missing(diagnostics):
    """The counters live in state.json; the trail is separate and best effort.

    This vault has counters and no trail. Pointing the operator at a file that
    is not there wastes the one moment they are paying attention.
    """
    module, state = diagnostics
    module.record_capture_failure("mcp_tool", "ValueError: bad path")
    module.FAILURE_LOG.unlink()

    line = module.capture_failure_line(state)

    assert "is missing" in line
    assert "run/state.json" in line


def test_the_line_points_at_the_trail_when_it_is_there(diagnostics):
    module, state = diagnostics
    module.record_capture_failure("mcp_tool", "ValueError: bad path")

    line = module.capture_failure_line(state)

    assert "see `logs/capture-failures.jsonl`." in line
    assert "missing" not in line


def test_a_trail_writer_that_explodes_does_not_break_the_hook(
    diagnostics, monkeypatch
):
    """`record_capture_failure` promises never to raise. The trail writer only
    catches OSError, and it used to be called outside that promise."""
    module, state = diagnostics

    def exploding_append(_record):
        raise RuntimeError("the trail writer itself failed")

    monkeypatch.setattr(module, "_append_failure_line", exploding_append)

    module.record_capture_failure("mcp_tool", "ValueError: bad path")

    assert state["capture_failures"]["mcp_tool"]["count"] == 1


def test_the_line_says_when_it_last_happened(diagnostics):
    """Nothing clears these counters, so an old loss must read as old."""
    module, state = diagnostics
    module.record_capture_failure("compile_oversized_daily", "too big")

    line = module.capture_failure_line(state)
    recorded = state["capture_failures"]["compile_oversized_daily"]["last_at"]

    assert f"last at {recorded}" in line
    assert "--clear" in line


def test_clearing_retires_the_counters_and_names_them(diagnostics):
    module, state = diagnostics
    module.record_capture_failure("mcp_tool", "one")
    module.record_capture_failure("mcp_tool", "two")
    module.record_capture_failure("post_tool_append", "three")

    retired = module.clear_capture_failures()

    assert retired == {"mcp_tool": 2, "post_tool_append": 1}
    assert module.capture_failure_totals(state) == {}
    assert module.capture_failure_line(state) == ""


def test_clearing_an_empty_counter_reports_nothing(diagnostics):
    module, _state = diagnostics

    assert module.clear_capture_failures() == {}
