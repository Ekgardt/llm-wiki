"""recall and get_decisions give the reranker the window it was measured to need (B-9).

On a 10 s MCP budget the rerank was offered about 3.5 s while its warm cost was a
median 3.46 s and a p95 5.68 s, so about half the answers came back without it. See
`docs/research/2026-09-27-a-rerank-has-the-window-it-was-measured-to-need.md`.
"""

from __future__ import annotations

import time

import mcp_server
import retrieval
import settings

# The warm p95 of the product reranker (depth 10, int8, real vault notes), measured on
# four idle cores on 2026-09-27; the default budget must leave at least this much.
MEASURED_WARM_P95_SECONDS = 5.68
# Lexical and dense legs before the rerank starts, measured well under this.
LEGS_BEFORE_RERANK_SECONDS = 0.5


def _rerank_window(budget: float) -> float:
    """What `optional_stage_deadline` offers the rerank inside one MCP search."""
    now = time.monotonic()
    hybrid_deadline = now + budget - mcp_server.LEXICAL_FALLBACK_RESERVE_SECONDS
    started = now + LEGS_BEFORE_RERANK_SECONDS
    remaining = hybrid_deadline - started
    share = started + remaining * retrieval.OPTIONAL_STAGE_BUDGET_SHARE
    return min(share, hybrid_deadline - retrieval.OPTIONAL_STAGE_TAIL_RESERVE_SECONDS) - started


def test_recall_and_get_decisions_take_the_retrieval_budget(monkeypatch) -> None:
    settings.clear_cache()
    monkeypatch.delenv("LLM_WIKI_MCP_RETRIEVAL_SECONDS", raising=False)
    expected = float(settings.setting_value("mcp.retrieval_seconds"))
    assert mcp_server.tool_operation_seconds("recall", {"query": "q"}) == expected
    assert mcp_server.tool_operation_seconds("get_decisions", {"query": "q"}) == expected
    assert mcp_server.tool_operation_seconds("read_page", {"slug": "x"}) == mcp_server.MCP_OPERATION_SECONDS


def test_the_default_budget_fits_the_measured_rerank() -> None:
    default = next(item for item in settings.REGISTRY if item.name == "mcp.retrieval_seconds").default
    assert _rerank_window(float(default)) >= MEASURED_WARM_P95_SECONDS
    assert _rerank_window(mcp_server.MCP_OPERATION_SECONDS) < MEASURED_WARM_P95_SECONDS


def test_an_operator_on_a_slower_machine_raises_it(monkeypatch) -> None:
    settings.clear_cache()
    monkeypatch.setenv("LLM_WIKI_MCP_RETRIEVAL_SECONDS", "20")
    assert mcp_server.tool_operation_seconds("recall", {"query": "q"}) == 20.0
    settings.clear_cache()
