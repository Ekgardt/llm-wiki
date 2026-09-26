"""get_decisions reports its retrieval as recall does (audit 2026-09-26 C-10).

docs/research/2026-09-26-decisions-report-their-retrieval.md
"""
from __future__ import annotations

import ast
import time
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def _fake_search(query, *, trace_sink=None, **_kwargs):
    trace_sink.update(
        {"requested_mode": "HYBRID", "effective_mode": "BASE", "signals_used": ["lexical"],
         "corpus_generation": "generation-x", "fallback_reason": "generation_vectors_unavailable",
         "partial": False}
    )
    return [{"path": "knowledge/notes/a-decision.md", "type": "decision", "candidate_id": "c",
             "title": "A", "summary": "s", "score": 1.0, "fused_score": 1.0}]


def test_a_decision_answer_says_it_fell_back(monkeypatch) -> None:
    import mcp_server
    import search_memory

    monkeypatch.setattr(search_memory, "search", _fake_search)
    data, _clamped = mcp_server._tool_get_decisions({"query": "choice"}, time.monotonic() + 30)
    quality = mcp_server._quality_of_results("get_decisions", data, {}, False)

    assert data["retrieval_trace"]["fallback_reason"] == "generation_vectors_unavailable"
    assert (quality.get("fallback"), [row["path"] for row in data["results"]]) == (
        True, ["knowledge/notes/a-decision.md"]
    )
    assert mcp_server._answer_generation("get_decisions", data) == "generation-x"


def _imports_search(node: ast.AST) -> bool:
    if not isinstance(node, ast.ImportFrom) or node.module != "search_memory":
        return False
    return "search" in {alias.name for alias in node.names}


def _calls_search_itself(function: ast.FunctionDef) -> bool:
    return any(map(_imports_search, ast.walk(function)))


def _direct_search_callers() -> set[str]:
    """Functions of mcp_server that import `search` from search_memory themselves."""
    tree = ast.parse((SCRIPTS / "mcp_server.py").read_text(encoding="utf-8"))
    functions = [node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)]
    return {function.name for function in functions if _calls_search_itself(function)}


def test_every_tool_reaches_the_corpus_through_the_one_planner() -> None:
    # The warm-up is not an answer; every tool goes through `_search_vault`.
    assert _direct_search_callers() == {"_run_vault_search", "_warmup_pass"}
