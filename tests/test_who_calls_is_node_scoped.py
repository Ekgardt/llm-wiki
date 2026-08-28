"""NEW-116 — a who-calls question must not read the whole CALLS set.

`_store_find_callers`/`_store_find_callees` used to pull every resolved CALLS
assertion out of the active generation and filter it in a Python loop. On this
repository's live generation that is 35,313 rows against a 10,000-row ceiling,
so `EvidenceGraph._execute` raised `Evidence Graph query row ceiling exceeded`
and every who-calls / what-calls question was refused.

The fix anchors the question in SQL: `EvidenceGraph.edges()` takes optional
`source_node_ids` / `target_node_ids` filters, so the query reads the handful of
rows it needs and still returns `assertion_id` per edge (which `callers()` and
`callees()` cannot give, because they return nodes with depth).

The refusal contract is unchanged: the ceiling still refuses, never truncates.
What changes is that ordinary questions stop reaching it.
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

_SOURCE = b"def caller():\n    callee()\n"
_CALLEES = 6


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _fan_out_records() -> dict:
    """One caller with `_CALLEES` outgoing CALLS edges, so a small ceiling bites."""
    nodes = [
        {
            "node_id": "caller",
            "kind": "function",
            "identity_scheme": "python-qualified-name/v1",
            "identity_key": "app:caller",
            "metadata": {"name": "caller"},
        }
    ]
    assertions = []
    evidence = []
    for index in range(_CALLEES):
        evidence.append(
            {
                "evidence_id": f"ev-{index}",
                "assertion_id": f"call-{index}",
                "observation_id": None,
                "source_id": "src",
                "byte_start": 18,
                "byte_end": 24,
                "span_sha256": _sha(_SOURCE[18:24]),
            }
        )
        nodes.append(
            {
                "node_id": f"callee-{index}",
                "kind": "function",
                "identity_scheme": "python-qualified-name/v1",
                "identity_key": f"app:callee_{index}",
                "metadata": {"name": f"callee_{index}"},
            }
        )
        assertions.append(
            {
                "assertion_id": f"call-{index}",
                "source_node_id": "caller",
                "edge_type": "CALLS",
                "target_node_id": f"callee-{index}",
                "literal": None,
                "confidence": "high",
                "authority": "ai-derived",
                "resolution": "resolved",
                "extractor": "python/v1",
            }
        )
    return {
        "sources": [
            {
                "source_id": "src",
                "relative_path": "app.py",
                "sha256": _sha(_SOURCE),
                "size": len(_SOURCE),
                "media_type": "text/x-python",
                "language": "python",
                "git_oid": None,
            }
        ],
        "source_bytes": {"src": _SOURCE},
        "nodes": nodes,
        "occurrences": [
            {
                "occurrence_id": "occ-caller",
                "node_id": "caller",
                "source_id": "src",
                "role": "definition",
                "byte_start": 0,
                "byte_end": 13,
                "line_start": 1,
                "line_end": 1,
            }
        ],
        "assertions": assertions,
        "observations": [],
        "evidence": evidence,
        "dependencies": [],
    }


def _fan_out_graph(tmp_path: Path):
    import evidence_graph

    path = tmp_path / "evidence.sqlite3"
    evidence_graph.create_generation_database(path, **_fan_out_records())
    return evidence_graph.EvidenceGraph(path, state_root=tmp_path)


def test_a_node_scoped_edge_query_answers_where_the_whole_edge_set_refuses(tmp_path):
    """The defect proper: the ceiling is reached only because nothing narrowed."""
    graph = _fan_out_graph(tmp_path)
    ceiling = _CALLEES - 2

    try:
        with pytest.raises(ValueError, match="row ceiling exceeded"):
            graph.edges(edge_types=("CALLS",), max_rows=ceiling)

        scoped = graph.edges(
            edge_types=("CALLS",), target_node_ids=["callee-3"], max_rows=ceiling
        )
    finally:
        graph.close()

    assert [item["assertion_id"] for item in scoped] == ["call-3"]
    assert scoped[0]["source_node_id"] == "caller"


def test_a_source_scoped_edge_query_returns_only_that_nodes_outgoing_calls(tmp_path):
    graph = _fan_out_graph(tmp_path)
    try:
        outgoing = graph.edges(
            edge_types=("CALLS",), source_node_ids=["caller"], max_rows=_CALLEES
        )
        none_outgoing = graph.edges(
            edge_types=("CALLS",), source_node_ids=["callee-0"], max_rows=_CALLEES
        )
    finally:
        graph.close()

    assert len(outgoing) == _CALLEES
    assert none_outgoing == []


def test_both_ends_may_be_named_at_once(tmp_path):
    graph = _fan_out_graph(tmp_path)
    try:
        both = graph.edges(
            edge_types=("CALLS",),
            source_node_ids=["caller"],
            target_node_ids=["callee-1", "callee-2"],
            max_rows=_CALLEES,
        )
    finally:
        graph.close()

    assert [item["assertion_id"] for item in both] == ["call-1", "call-2"]


def test_an_oversized_node_filter_is_refused_by_name_not_truncated(tmp_path):
    """A caller handing over thousands of ids is told so; the set is never cut."""
    import evidence_graph

    graph = _fan_out_graph(tmp_path)
    oversized = [f"node-{index}" for index in range(evidence_graph.MAX_NODE_FILTER + 1)]
    try:
        with pytest.raises(ValueError, match="target_node_ids cannot contain more"):
            graph.edges(edge_types=("CALLS",), target_node_ids=oversized)
        with pytest.raises(ValueError, match="source_node_ids cannot contain more"):
            graph.edges(edge_types=("CALLS",), source_node_ids=oversized)
    finally:
        graph.close()


def test_an_empty_filter_selects_nothing_and_none_means_no_filter(tmp_path):
    graph = _fan_out_graph(tmp_path)
    try:
        unfiltered = graph.edges(
            edge_types=("CALLS",), target_node_ids=None, max_rows=_CALLEES
        )
        empty = graph.edges(
            edge_types=("CALLS",), target_node_ids=[], max_rows=_CALLEES
        )
    finally:
        graph.close()

    assert len(unfiltered) == _CALLEES
    assert empty == []


def _refuse_unanchored_calls(monkeypatch):
    """Stand in for the live generation's ceiling: refuse an unnarrowed CALLS read."""
    import evidence_graph

    real = evidence_graph.EvidenceGraph.edges
    seen: list[dict] = []

    def guarded(self, **kwargs):
        seen.append(kwargs)
        anchored = kwargs.get("source_node_ids") or kwargs.get("target_node_ids")
        if kwargs.get("edge_types") == ("CALLS",) and not anchored:
            raise ValueError("Evidence Graph query row ceiling exceeded")
        return real(self, **kwargs)

    monkeypatch.setattr(evidence_graph.EvidenceGraph, "edges", guarded)
    return seen


def test_who_calls_no_longer_reads_the_whole_calls_set(tmp_path, monkeypatch):
    import code_graph

    from tests.test_code_graph import _activate_graph

    catalog = _activate_graph(tmp_path)
    monkeypatch.setattr(code_graph, "_generation_catalog", lambda directory: catalog)
    seen = _refuse_unanchored_calls(monkeypatch)

    callers = code_graph.find_callers("callee", tmp_path)

    assert [item["qualified_name"] for item in callers] == ["caller"]
    assert seen[0]["target_node_ids"] == ["callee"]


def test_what_x_calls_no_longer_reads_the_whole_calls_set(tmp_path, monkeypatch):
    import code_graph

    from tests.test_code_graph import _activate_graph

    catalog = _activate_graph(tmp_path)
    monkeypatch.setattr(code_graph, "_generation_catalog", lambda directory: catalog)
    seen = _refuse_unanchored_calls(monkeypatch)

    callees = code_graph.find_callees("caller", tmp_path)

    assert [item["callee"] for item in callees] == ["callee"]
    assert seen[0]["source_node_ids"] == ["caller"]
