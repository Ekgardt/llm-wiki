"""OPS-02: every tool answer states what it cost, and the telemetry costs what it says.

These tests fail before `scripts/answer_cost.py` exists and before
`mcp_server._execute_tool_call` attaches the block.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from answer_cost import (  # noqa: E402
    COST_KEY,
    MODE_ENV,
)

# --- the number itself -------------------------------------------------------

# --- fail closed: missing must read as missing, never as zero ----------------

# --- which optional stages ran ----------------------------------------------

# --- the telemetry obeys the law it measures --------------------------------

# --- the wiring into the answer path ----------------------------------------

def test_a_real_tool_answer_carries_its_cost(monkeypatch) -> None:
    import mcp_server

    monkeypatch.setenv(MODE_ENV, "always")
    text = mcp_server._execute_tool_call("vault_status", {}, _deadline())
    envelope = json.loads(text)
    block = envelope["data"][COST_KEY]
    assert isinstance(block["tokens_estimated"], int)
    assert isinstance(block["duration_ms"], int)
    assert block["estimate_method"] == "utf8_bytes/2"


def test_the_cost_block_does_not_break_the_declared_envelope_schema(monkeypatch) -> None:
    """A thirteenth top-level key would fail every tool's own outputSchema."""
    import mcp_server
    from mcp_contract import envelope_schema

    monkeypatch.setenv(MODE_ENV, "always")
    envelope = json.loads(mcp_server._execute_tool_call("vault_status", {}, _deadline()))
    allowed = set(envelope_schema()["properties"])
    assert set(envelope) <= allowed
    assert COST_KEY in envelope["data"]


def test_a_telemetry_failure_never_costs_the_caller_the_answer(monkeypatch) -> None:
    import mcp_server

    def explode(*_args, **_kwargs):
        raise RuntimeError("telemetry is broken")

    monkeypatch.setattr(mcp_server, "attach_answer_cost", explode)
    envelope = json.loads(mcp_server._execute_tool_call("vault_status", {}, _deadline()))
    assert COST_KEY not in envelope["data"]
    assert envelope["data"]


def _deadline() -> float:
    import time

    return time.monotonic() + 60.0
