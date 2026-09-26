"""An estimate never inherits the envelope's full confidence (audit 2026-09-26 C-10).

docs/research/2026-09-26-no-estimate-inherits-full-confidence.md
"""
from __future__ import annotations

import mcp_server
import pytest


def test_a_vault_never_compiled_is_not_reported_with_full_confidence() -> None:
    status = {"last_compile": "never", "last_compile_status": "unknown", "compile_backlog": 3,
              "warmup": {"status": "not_started"}, "retrieval_degradations": {}}

    quality = mcp_server._quality_for("vault_status", status, {})

    assert (quality["confidence"] < 1.0, quality["partial"]) == (True, True)


@pytest.mark.parametrize("name", sorted(mcp_server._TOOL_HANDLERS))
def test_every_tool_is_claimed_by_a_quality_rule_or_declared_exact(name) -> None:
    claimed = mcp_server._quality_for(name, {}, {"probe": 1}) != {}

    assert claimed or name in mcp_server.EXACT_ANSWER_TOOLS


def test_the_exact_tools_are_tools() -> None:
    assert mcp_server.EXACT_ANSWER_TOOLS <= set(mcp_server._TOOL_HANDLERS)
