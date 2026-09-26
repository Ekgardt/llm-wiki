"""A failed tool call or telemetry write is reported on its own, not as a lost capture.

The live vault showed "53 capture(s) lost (mcp_tool 53)": MCP tool errors shared
the capture counter and kept the capture check red. See
docs/research/2026-09-25-a-tool-failure-is-not-a-lost-capture.md.
"""

from __future__ import annotations

import time

import capture_diagnostics
import doctor
import pytest


@pytest.fixture
def recorded(tmp_path, monkeypatch):
    state: dict = {}

    def mutate(apply, **_options):
        apply(state)

    monkeypatch.setattr(capture_diagnostics, "update_state", mutate)
    monkeypatch.setattr(capture_diagnostics, "FAILURE_LOG", tmp_path / "capture-failures.jsonl")
    monkeypatch.setattr(capture_diagnostics, "REPORTS_DIR", tmp_path)
    return state


def test_tool_and_telemetry_failures_are_not_captures(recorded) -> None:
    capture_diagnostics.record_capture_failure("mcp_tool", "ValueError: bad reference")
    capture_diagnostics.record_capture_failure("telemetry_event", "OSError: disk full")

    assert capture_diagnostics.capture_failure_totals(recorded) == {}
    assert capture_diagnostics.capture_failure_line(recorded) == ""
    assert capture_diagnostics.operational_failure_totals(recorded) == {"mcp_tool": 1, "telemetry_event": 1}


def test_doctor_names_recent_tool_failures_under_tools(recorded, tmp_path, monkeypatch) -> None:
    capture_diagnostics.record_capture_failure("mcp_tool", "ValueError: bad reference")
    monkeypatch.setattr(doctor, "_read_state", lambda _root, _deadline: (recorded, False))

    check = doctor._tool_failure_check(tmp_path, time.monotonic() + 5)

    assert (check["status"], check["details"]["kinds"]) == ("degraded", {"mcp_tool": 1})
