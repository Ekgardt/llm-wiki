"""NEW-121: the whole-graph aggregates answer in SQL instead of pulling the graph.

`NEW-116` anchored the symbol-scoped questions (who calls / what calls) in SQL.
These three could not be anchored by any symbol — "which functions have no
caller", "how many callers has each function", "what are the modules" — and so
they pulled every node and every edge at ``max_rows=10_000`` and refused on this
repository's 35,313 resolved CALLS assertions over 19,153 function+method nodes.

The regression is fixture-sized but the shape is the live one: the fixture's
budget is smaller than its graph, exactly as the repository's ceiling is smaller
than its graph. Each test below fails on the code before the fix.
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

SOURCE = b"def hub(): pass\ndef warm(): hub()\ndef lonely(): pass\n"

# Twelve resolved CALLS assertions — one of them a self-loop — that fold into
# five distinct undirected pairs. A budget of five refuses the raw rows and
# admits the folded answer, which is the whole point of folding in SQL.
_CALLS = (
    ("a1", "warm", "hub"),
    ("a2", "warm", "hub"),
    ("a3", "warm", "hub"),
    ("a4", "gate", "hub"),
    ("a5", "gate", "hub"),
    ("a6", "loop", "hub"),
    ("a7", "hub", "warm"),
    ("a8", "gate", "warm"),
    ("a9", "gate", "warm"),
    ("a10", "loop", "gate"),
    ("a11", "loop", "gate"),
    ("a12", "loop", "loop"),
)

_FUNCTIONS = (
    ("hub", "app.py"),
    ("warm", "app.py"),
    ("gate", "app.py"),
    ("loop", "app.py"),
    ("lonely", "app.py"),
    ("stale", "lib/util.py"),
    ("exposed", "app.py"),
    ("test_only", "app.py"),
    ("helper", "tests/test_app.py"),
)


def _node(name: str, path: str) -> dict:
    return {
        "node_id": name,
        "kind": "function",
        "identity_scheme": "python/v1",
        "identity_key": f"app:{name}",
        "metadata": {"name": name, "owner": "app", "path": path},
    }


def _assertion(identifier: str, source: str, target: str, edge_type: str) -> dict:
    return {
        "assertion_id": identifier,
        "source_node_id": source,
        "edge_type": edge_type,
        "target_node_id": target,
        "literal": None,
        "confidence": "high",
        "authority": "ai-derived",
        "resolution": "resolved",
        "extractor": "python/v1",
    }


def _graph_records() -> dict:
    assertions = [
        _assertion(identifier, source, target, "CALLS")
        for identifier, source, target in _CALLS
    ]
    assertions.append(_assertion("expose", "exposed", "hub", "EXPOSES"))
    return {
        "sources": [
            {
                "source_id": "source",
                "relative_path": "app.py",
                "sha256": hashlib.sha256(SOURCE).hexdigest(),
                "size": len(SOURCE),
                "media_type": "text/x-python",
                "language": "python",
                "git_oid": None,
            }
        ],
        "source_bytes": {"source": SOURCE},
        "nodes": [_node(name, path) for name, path in _FUNCTIONS],
        "occurrences": [
            {
                "occurrence_id": "occurrence",
                "node_id": "lonely",
                "source_id": "source",
                "role": "definition",
                "byte_start": 34,
                "byte_end": 52,
                "line_start": 3,
                "line_end": 3,
            }
        ],
        "assertions": assertions,
        "evidence": [
            {
                "evidence_id": f"evidence-{record['assertion_id']}",
                "assertion_id": record["assertion_id"],
                "observation_id": None,
                "source_id": "source",
                "byte_start": 16,
                "byte_end": 33,
                "span_sha256": hashlib.sha256(SOURCE[16:33]).hexdigest(),
            }
            for record in assertions
        ],
        "observations": [],
        "dependencies": [],
    }


@pytest.fixture()
def graph(tmp_path):
    from generation_catalog import GenerationCatalog
    from repository_scope import resolve_repository_scope

    from tests.test_evidence_graph_recovery import _publish

    catalog = GenerationCatalog(tmp_path / "state")
    _publish(
        catalog,
        "active",
        graph_records=_graph_records(),
        repository_scope=resolve_repository_scope(tmp_path).as_dict(),
    )
    catalog.register("active")
    catalog.activate("active", expected_active=None)
    opened = _open(catalog, tmp_path)
    try:
        yield opened
    finally:
        opened.close()


def _open(catalog, directory):
    import evidence_graph
    from repository_scope import resolve_repository_scope

    return evidence_graph.EvidenceGraph.open_active_for_repository(
        catalog, resolve_repository_scope(directory)
    )


@pytest.fixture()
def repository(tmp_path, monkeypatch):
    """An active generation wired into `code_graph`, with the live scan forbidden."""
    import code_graph
    from generation_catalog import GenerationCatalog
    from repository_scope import resolve_repository_scope

    from tests.test_evidence_graph_recovery import _publish

    catalog = GenerationCatalog(tmp_path / "state")
    _publish(
        catalog,
        "active",
        graph_records=_graph_records(),
        repository_scope=resolve_repository_scope(tmp_path).as_dict(),
    )
    catalog.register("active")
    catalog.activate("active", expected_active=None)
    monkeypatch.setattr(code_graph, "_generation_catalog", lambda directory: catalog)
    monkeypatch.setattr(
        code_graph,
        "_workspace_call_graph",
        lambda directory: (_ for _ in ()).throw(AssertionError("live scan used")),
    )
    return tmp_path


def _forbid_whole_graph_reads(monkeypatch):
    """Refuse exactly the two reads that made these questions hit the ceiling."""
    import evidence_graph

    def refuse_edges(self, **_kwargs):
        raise AssertionError("the whole edge set was pulled into Python")

    original = evidence_graph.EvidenceGraph.find_nodes

    def guarded_find_nodes(self, **kwargs):
        if tuple(kwargs.get("kinds") or ()) == ("function", "method"):
            raise AssertionError("every function node was pulled into Python")
        return original(self, **kwargs)

    monkeypatch.setattr(evidence_graph.EvidenceGraph, "edges", refuse_edges)
    monkeypatch.setattr(evidence_graph.EvidenceGraph, "find_nodes", guarded_find_nodes)


def test_the_dead_code_anti_join_answers_under_a_budget_smaller_than_the_graph(graph):
    """Nine nodes, a budget of three, and the answer is still returned."""
    with pytest.raises(ValueError, match="row ceiling exceeded"):
        graph.find_nodes(kinds=("function", "method"), max_rows=3)

    dead = graph.nodes_without_edges(
        kinds=("function", "method"),
        incoming_edge_types=("CALLS",),
        outgoing_edge_types=("EXPOSES",),
        exclude_name_prefixes=("test_",),
        max_rows=3,
    )

    assert sorted(node["metadata"]["name"] for node in dead) == [
        "helper",
        "lonely",
        "stale",
    ]


def test_the_caller_ranking_is_counted_in_sql_and_reports_its_own_bound(graph):
    """`gate` and `loop` are hidden by the bound, and the bound says so."""
    top, truncated = graph.top_incoming_edge_counts(
        edge_types=("CALLS",), kinds=("function",), max_rows=2
    )

    assert [(row["node_id"], row["incoming"]) for row in top] == [("hub", 3), ("warm", 2)]
    assert truncated is True
    assert graph.top_incoming_edge_counts(edge_types=("CALLS",), max_rows=10)[1] is False


def test_call_pairs_fold_in_sql_so_the_raw_rows_never_reach_python(graph):
    """Twelve assertions, five undirected pairs, one budget that separates them."""
    with pytest.raises(ValueError, match="row ceiling exceeded"):
        graph.edges(edge_types=("CALLS",), max_rows=5)

    pairs = graph.edge_weights(edge_types=("CALLS",), max_rows=5)

    assert [
        (pair["source_node_id"], pair["target_node_id"], pair["weight"])
        for pair in pairs
    ] == [
        ("gate", "hub", 2),
        ("gate", "loop", 2),
        ("gate", "warm", 2),
        ("hub", "loop", 1),
        ("hub", "warm", 4),
    ]


def test_the_folded_pairs_build_the_same_community_graph_as_the_raw_rows(graph):
    import code_graph

    raw = code_graph._undirected_call_graph(
        graph.edges(edge_types=("CALLS",), max_rows=100)
    )
    folded = code_graph._undirected_call_graph(
        graph.edge_weights(edge_types=("CALLS",), max_rows=100)
    )

    assert folded == raw
    assert "loop" not in folded.get("loop", {})


def test_find_dead_code_answers_without_pulling_the_graph(repository, monkeypatch):
    import code_graph

    _forbid_whole_graph_reads(monkeypatch)

    report = code_graph.find_dead_code(repository, with_report=True)

    names = sorted(item["name"] for item in report["candidates"])
    # `helper` lives in tests/test_app.py, so the basename convention — the half
    # of the rule that stays in Python — keeps it out of the answer.
    assert names == ["lonely", "stale"]
    assert report["fallback"] is False


def test_get_architecture_answers_without_pulling_the_graph_and_states_its_bound(
    repository, monkeypatch
):
    import code_graph

    _forbid_whole_graph_reads(monkeypatch)
    monkeypatch.setattr(code_graph, "HOTSPOT_LIMIT", 2)

    architecture = code_graph.get_architecture(repository)

    assert [item["name"] for item in architecture["hotspots"]] == ["hub", "warm"]
    assert architecture["hotspots"][0]["incoming_callers"] == 3
    assert architecture["hotspot_limit"] == 2
    assert architecture["hotspots_truncated"] is True


def test_detect_communities_answers_without_pulling_the_graph(repository, monkeypatch):
    import code_graph

    _forbid_whole_graph_reads(monkeypatch)

    communities = code_graph.detect_communities(repository)

    assert communities and all(len(group) >= 2 for group in communities)
    assert {name for group in communities for name in group} <= {
        "hub", "warm", "gate", "loop",
    }
