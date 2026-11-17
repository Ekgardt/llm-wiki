"""The data_flow and cross_service modes hand the walk the caller's bounds.

Audit 3, K-B3: both modes dropped the deadline, so a slow graph kept one of the
four MCP worker slots after the caller had timed out. The walk's own side is
`docs/research/2026-09-18-graph-a-flow-walk-stops-when-its-caller-has.md`.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))


def _recorded_call(monkeypatch, name: str) -> list:
    """Replace one code_graph walk and keep the keywords it was given."""
    import code_graph

    calls: list = []

    def recorded(symbol, directory, **options):
        calls.append(options)
        return None

    monkeypatch.setattr(sys.modules[getattr(code_graph, name).__module__], name, recorded)
    return calls


def _cancelled_flag(monkeypatch) -> None:

    monkeypatch.setattr("llm_wiki.mcp_server_workers.operation_cancelled", lambda: _stopped)


def _stopped() -> bool:
    return True


def test_the_data_flow_mode_passes_the_callers_deadline_and_cancellation(
    tmp_path, monkeypatch
):
    import mcp_server

    calls = _recorded_call(monkeypatch, "find_argument_flows")
    _cancelled_flag(monkeypatch)
    deadline = time.monotonic() + 3

    answer = mcp_server.data_flow_architecture_call(
        {"directory": str(tmp_path), "symbol": "alpha"}, deadline
    )

    assert (calls[0]["deadline"], calls[0]["cancelled"](), answer["reason"]) == (
        deadline,
        True,
        "no_active_generation",
    )


def test_the_cross_service_mode_passes_the_callers_deadline_and_cancellation(
    tmp_path, monkeypatch
):
    import mcp_server

    calls = _recorded_call(monkeypatch, "find_service_paths")
    _cancelled_flag(monkeypatch)
    deadline = time.monotonic() + 3

    answer = mcp_server.cross_service_architecture_call(
        {"directory": str(tmp_path), "symbol": "alpha"}, deadline
    )

    assert (calls[0]["deadline"], calls[0]["cancelled"](), answer["reason"]) == (
        deadline,
        True,
        "no_active_generation",
    )


def test_an_expired_deadline_stops_the_walk_instead_of_holding_the_slot(
    tmp_path, monkeypatch
):
    """The walk itself raises; the mode does not swallow it into an empty answer."""
    import pytest

    def expired(symbol, directory, **options):
        raise TimeoutError("generation catalog deadline reached")

    monkeypatch.setattr("llm_wiki.code_graph_flows.find_argument_flows", expired)
    import mcp_server

    with pytest.raises(TimeoutError):
        mcp_server.data_flow_architecture_call(
            {"directory": str(tmp_path), "symbol": "alpha"}, time.monotonic() - 1
        )
