"""CODE-06: the code answer path spends tokens on purpose, and says what it cut.

The measurement behind these tests is in
`docs/research/2026-08-28-token-budgeted-answers.md`: on the ten CODE-07 tasks
both products answered correctly, llm-wiki spent 3.4x codebase-memory-mcp's
tokens, and 15.8-20.6% of its answer bytes were opaque `code:node:<32 hex>`
identifiers with no reader anywhere in the repository.

Every test here fails on the tree before the change. The canned answers are the
real shapes, copied from `benchmark/code-parity-first-pairing-2026-08-28.json`
and from a live baseline run of `benchmark/run_code_parity.py`.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import answer_budget  # noqa: E402
import mcp_server  # noqa: E402

_HASH = "code:node:2efaa60ad36c3d08c1c53ea64c98a9a5"
_OTHER_HASH = "code:node:fb7f00530cc7b93acb3052efd59b2d85"

_QUERY_ARGUMENTS = {"directory": ".", "mode": "query", "query": "{}"}


def _node_row(index: int) -> dict:
    return {
        "node_id": f"code:node:{index:032x}",
        "kind": "function",
        "name": f"caller_number_{index}",
        "path": "scripts/retrieval.py",
        "owner": "scripts.retrieval",
    }


def _query_answer(rows: int = 2) -> dict:
    """The `query` mode's real shape: node_id, kind, name, path, owner."""
    return {
        "status": "ok",
        "generation_id": "generation-18cfd903a7a4e112-3ce112cb",
        "hops_applied": 1,
        "limit": 50,
        "frontier_truncated": False,
        "refused_expansions": [],
        "nodes": [_node_row(index) for index in range(rows)],
        "note": "bounded structural reachability over the active generation",
    }


def _caller_row(path: str, line: int, qualified: str, identifier: str) -> dict:
    return {
        "file": path,
        "line": line,
        "function": "fuse_rrf",
        "qualified_name": qualified,
        "confidence": "high",
        "symbol_id": identifier,
    }


def _callers_answer() -> dict:
    """The `callers` mode's real shape: the same hash, under `symbol_id`."""
    report = {
        "source_generation": "generation-18cfd903a7a4e112-3ce112cb",
        "graph_complete": False,
        "unresolved_count": 76044,
        "fallback": False,
    }
    callers = [
        _caller_row(
            "/home/user/llm-wiki/scripts/retrieval.py",
            2845,
            "scripts.retrieval._fused_candidates",
            _HASH,
        ),
        _caller_row(
            "/home/user/llm-wiki/tests/test_retrieval.py",
            141,
            "tests.test_retrieval.test_rrf_is_rank_only",
            _OTHER_HASH,
        ),
    ]
    return {
        "directory": "/home/user/llm-wiki",
        "mode": "callers",
        "architecture": {"callers": callers, **report},
        **report,
    }


def _candidate_row(name: str, identifier: str, path: str, line: int) -> dict:
    return {
        "name": name,
        "symbol_id": identifier,
        "owner": "scripts.integration_adapter",
        "file": path,
        "line": line,
        "status": "candidate",
        "reason": "zero_confirmed_incoming_calls",
    }


def _dead_code_answer() -> dict:
    """The live fallback's `symbol_id` is readable, not a hash - it must stay."""
    return {
        "directory": "/home/user/llm-wiki",
        "candidates": [
            _candidate_row(
                "_flush_started",
                "scripts.integration_adapter::_flush_started",
                "/home/user/llm-wiki/scripts/integration_adapter.py",
                1845,
            ),
            _candidate_row(
                "_search_backends",
                _HASH,
                "/home/user/llm-wiki/scripts/search_memory.py",
                4831,
            ),
        ],
        "source_generation": "generation-18cfd903a7a4e112-3ce112cb",
        "graph_complete": False,
        "unresolved_count": 76044,
        "fallback": False,
    }


def _architecture(monkeypatch, answer: dict) -> None:
    monkeypatch.setattr(
        mcp_server, "_architecture_tool_call", lambda arguments, deadline: answer
    )


def _call(name: str, arguments: dict) -> dict:
    data, _ = mcp_server._tool_call_data(name, arguments, 1e12)
    return data


def _values(rows: list, key: str) -> list:
    """Present values only, so a dropped field reads as an empty list."""
    return [row[key] for row in rows if key in row]


# --- the opaque identifier stops being emitted ------------------------------


def test_the_query_mode_stops_paying_for_a_hash_no_caller_reads(monkeypatch):
    _architecture(monkeypatch, _query_answer(rows=3))
    data = _call("get_architecture", dict(_QUERY_ARGUMENTS))
    assert _values(data["nodes"], "node_id") == []
    assert data["answer_budget"]["omitted_fields"] == ["node_id"]
    # The citation the answer exists to deliver is untouched.
    assert _values(data["nodes"], "name") == [f"caller_number_{i}" for i in range(3)]


def test_the_callers_mode_pays_the_same_tax_under_a_different_key(monkeypatch):
    """`node_id` and `symbol_id` are the same hash; one rule must catch both."""
    _architecture(monkeypatch, _callers_answer())
    data = _call(
        "get_architecture",
        {"directory": ".", "mode": "callers", "symbol": "fuse_rrf"},
    )
    callers = data["architecture"]["callers"]
    assert _values(callers, "symbol_id") == []
    assert data["answer_budget"]["omitted_fields"] == ["symbol_id"]
    assert _values(callers, "line") == [2845, 141]


def test_a_readable_symbol_id_survives_because_it_is_not_a_hash(monkeypatch):
    monkeypatch.setattr(
        mcp_server, "_find_dead_code", lambda *args, **kwargs: _dead_code_answer()
    )
    data = _call("find_dead_code", {"directory": "."})
    assert _values(data["candidates"], "symbol_id") == [
        "scripts.integration_adapter::_flush_started"
    ]
    assert data["answer_budget"]["omitted_fields"] == ["symbol_id"]


def test_an_explicit_opt_in_brings_the_identifiers_back(monkeypatch):
    _architecture(monkeypatch, _query_answer(rows=2))
    data = _call(
        "get_architecture", {**_QUERY_ARGUMENTS, "include_node_ids": True}
    )
    assert _values(data["nodes"], "node_id") == [_node_row(0)["node_id"], _node_row(1)["node_id"]]
    assert "answer_budget" not in data


def test_the_refusal_evidence_keeps_its_identifiers(monkeypatch):
    """A refused expansion is the fail-closed evidence; it pays for itself."""
    answer = _query_answer(rows=1)
    answer["refused_expansions"] = [
        {
            "hop": 0,
            "refused_node_ids": [_HASH],
            "refused_count": 1,
            "reason": "engine row or work ceiling exceeded",
        }
    ]
    _architecture(monkeypatch, answer)
    data = _call("get_architecture", dict(_QUERY_ARGUMENTS))
    assert data["refused_expansions"][0]["refused_node_ids"] == [_HASH]


# --- the budget ------------------------------------------------------------


def test_a_budget_trims_rows_and_says_how_many_it_dropped(monkeypatch):
    _architecture(monkeypatch, _query_answer(rows=40))
    data = _call("get_architecture", {**_QUERY_ARGUMENTS, "budget_tokens": 120})
    report = data["answer_budget"]
    assert report["truncated"] is True
    assert len(data["nodes"]) + report["rows_omitted"] == 40
    assert report["body_tokens"] <= 120 - answer_budget.REPORT_TOKEN_ALLOWANCE


def test_a_trimmed_answer_actually_fits_the_budget_it_was_given(monkeypatch):
    """The budget block is part of the answer, so it is part of the promise."""
    _architecture(monkeypatch, _query_answer(rows=40))
    data = _call("get_architecture", {**_QUERY_ARGUMENTS, "budget_tokens": 120})
    assert answer_budget.estimate_tokens(data) <= 120


def test_the_budget_drops_the_derivable_field_before_a_row(monkeypatch):
    """`owner` is recoverable from `path`; a row is not recoverable at all."""
    _architecture(monkeypatch, _query_answer(rows=8))
    data = _call("get_architecture", {**_QUERY_ARGUMENTS, "budget_tokens": 300})
    report = data["answer_budget"]
    assert report["omitted_fields"] == ["node_id", "owner"]
    assert len(data["nodes"]) == 8
    assert "rows_omitted" not in report


def test_a_budget_too_small_for_the_frame_refuses_by_name(monkeypatch):
    _architecture(monkeypatch, _query_answer(rows=40))
    data = _call("get_architecture", {**_QUERY_ARGUMENTS, "budget_tokens": 32})
    assert data["error"] == "answer_budget_too_small"
    assert data["answer_budget"]["minimum_tokens"] > 32
    assert "nodes" not in data


def test_no_budget_means_no_truncation(monkeypatch):
    _architecture(monkeypatch, _query_answer(rows=40))
    data = _call("get_architecture", dict(_QUERY_ARGUMENTS))
    assert len(data["nodes"]) == 40
    assert "truncated" not in data["answer_budget"]


def test_the_budget_argument_is_accepted_on_every_mode():
    """The per-mode closed key set must not refuse the two new arguments."""
    accepted = [
        {"directory": ".", "mode": "summary", "budget_tokens": 500},
        {"directory": ".", "mode": "callers", "symbol": "x", "budget_tokens": 500},
        {**_QUERY_ARGUMENTS, "include_node_ids": True},
        {"directory": ".", "mode": "snippet", "symbol": "x", "budget_tokens": 500},
    ]
    errors = [
        mcp_server._validate_tool_arguments("get_architecture", arguments)
        for arguments in accepted
    ]
    assert errors == [None, None, None, None]


def test_a_budget_outside_the_served_range_is_a_named_argument_error():
    error = mcp_server._validate_tool_arguments(
        "get_architecture",
        {"directory": ".", "mode": "summary", "budget_tokens": 4},
    )
    assert "must be at least" in str(error)


# --- the module on its own -------------------------------------------------


def test_the_module_refuses_a_budget_it_does_not_serve():
    with pytest.raises(ValueError, match="budget_tokens"):
        answer_budget.shape_code_answer({"a": 1}, budget_tokens=0)


def test_a_boolean_is_not_a_budget():
    with pytest.raises(ValueError, match="budget_tokens"):
        answer_budget.shape_code_answer({"a": 1}, budget_tokens=True)


def test_an_answer_with_nothing_to_drop_is_returned_unchanged():
    answer = {"status": "error", "mode": "summary", "error": "row ceiling exceeded"}
    assert answer_budget.shape_code_answer(answer) == answer


# --- a structure of nothing but hashes -------------------------------------


def _summary_answer() -> dict:
    """`mode=summary`'s real shape: 97% of it is bare `code:node:` strings."""
    return {
        "directory": "/home/user/llm-wiki",
        "mode": "summary",
        "architecture": {
            "entry_points": [
                {
                    "kind": "main",
                    "name": "main",
                    "node_id": _HASH,
                    "file": "/home/user/llm-wiki/benchmark/build_flush_corpus.py",
                    "line": 236,
                }
            ],
            "routes": [],
            "communities": [[_HASH, _OTHER_HASH], [_OTHER_HASH, _HASH]],
            "hotspots_truncated": False,
        },
        "source_generation": "generation-18cfd903a7a4e112-3ce112cb",
        "graph_complete": False,
        "unresolved_count": 76044,
        "fallback": False,
    }


def test_a_list_of_nothing_but_hashes_is_itself_an_opaque_field(monkeypatch):
    """Communities name no symbol, no file and no line - only hashes."""
    _architecture(monkeypatch, _summary_answer())
    data = _call("get_architecture", {"directory": ".", "mode": "summary"})
    assert "communities" not in data["architecture"]
    assert data["answer_budget"]["omitted_fields"] == ["communities", "node_id"]
    assert data["architecture"]["entry_points"][0]["line"] == 236


def test_a_summary_keeps_the_structure_a_reader_can_act_on(monkeypatch):
    _architecture(monkeypatch, _summary_answer())
    data = _call("get_architecture", {"directory": ".", "mode": "summary"})
    architecture = data["architecture"]
    assert architecture["routes"] == []
    assert architecture["hotspots_truncated"] is False
    assert data["unresolved_count"] == 76044


def test_the_opt_in_restores_the_communities_too(monkeypatch):
    _architecture(monkeypatch, _summary_answer())
    data = _call(
        "get_architecture",
        {"directory": ".", "mode": "summary", "include_node_ids": True},
    )
    assert data["architecture"]["communities"] == [
        [_HASH, _OTHER_HASH],
        [_OTHER_HASH, _HASH],
    ]


def test_a_readable_group_is_not_an_opaque_collection():
    """Only the `code:<kind>:<hash>` form counts; names are not hashes."""
    answer = {"architecture": {"communities": [["caller", "callee"]]}}
    assert answer_budget.shape_code_answer(answer) == answer
