"""The summary and the other generation-reading modes carry the freshness block.

Only the graph modes compared the generation's commit with the checkout's and
started a refresh. See
docs/research/2026-09-25-every-structural-answer-carries-its-freshness.md.
"""

from __future__ import annotations

from pathlib import Path

import mcp_server

STALE = {"generation_commit": "a" * 40, "checkout_commit": "b" * 40, "stale_by_commit": True, "refresh": "started"}


def test_the_search_mode_and_the_summary_carry_the_block(tmp_path: Path, monkeypatch) -> None:
    import code_graph
    import symbol_search

    monkeypatch.setattr(mcp_server, "_repository_freshness", lambda _resolved: STALE)
    monkeypatch.setattr(symbol_search, "search_symbols", lambda *_args, **_options: {"results": []})
    monkeypatch.setattr(code_graph, "get_architecture", lambda *_args, **_options: {"modules": []})

    searched = mcp_server._architecture_tool_call(
        {"directory": str(tmp_path), "mode": "search", "symbol": "x"}, deadline=mcp_server._operation_deadline(None)
    )
    summary = mcp_server._get_architecture(str(tmp_path))

    assert (searched.get("freshness"), summary.get("freshness")) == (STALE, STALE)


def test_an_error_answer_gets_no_block(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(mcp_server, "_repository_freshness", lambda _resolved: STALE)

    assert mcp_server._with_generation_freshness(tmp_path, {"error": "x"}) == {"error": "x"}
