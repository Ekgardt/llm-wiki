"""A path search that ran out of work refuses; it does not answer "no path".

Research: `docs/research/2026-09-17-a-cut-walk-is-not-no-path.md`.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parent
for directory in (TESTS.parent / "scripts", TESTS):
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))

from test_evidence_graph import _create, _records, _sha  # noqa: E402

EDGES = (("callee", "branch-c"), ("caller", "branch-b"), ("caller", "branch-a"), ("branch-a", "caller"))


def _function(identifier: str) -> dict:
    return {"node_id": identifier, "kind": "function", "identity_scheme": "test/v1", "identity_key": identifier, "metadata": {}}


def _call(number: int, source: str, target: str) -> tuple[dict, dict]:
    assertion = {
        "assertion_id": f"edge-{number}",
        "source_node_id": source,
        "edge_type": "CALLS",
        "target_node_id": target,
        "literal": None,
        "confidence": "high",
        "authority": "ai-derived",
        "resolution": "resolved",
        "extractor": "test/v1",
    }
    evidence = {
        "evidence_id": f"evidence-{number}",
        "assertion_id": f"edge-{number}",
        "observation_id": None,
        "source_id": "src-code",
        "byte_start": 18,
        "byte_end": 26,
        "span_sha256": _sha(b"callee()"),
    }
    return assertion, evidence


def _branching_graph(tmp_path: Path):
    records = _records()
    records["nodes"].extend(_function(name) for name in ("branch-a", "branch-b", "branch-c"))
    for number, (source, target) in enumerate(EDGES):
        assertion, evidence = _call(number, source, target)
        records["assertions"].append(assertion)
        records["evidence"].append(evidence)
    return _create(tmp_path, **records)


def test_a_path_is_found_inside_the_work_limit_and_refused_when_the_walk_is_cut(tmp_path):
    graph = _branching_graph(tmp_path)
    try:
        found = graph.path("caller", "branch-c", max_work=20)
        with pytest.raises(ValueError, match="work ceiling"):
            graph.path("caller", "branch-c", max_work=2)
    finally:
        graph.close()

    assert [row["node_ids"] for row in found] == [["caller", "callee", "branch-c"]]


def test_a_finished_walk_that_finds_nothing_still_answers_no_path(tmp_path):
    graph = _branching_graph(tmp_path)
    try:
        found = graph.path("branch-c", "caller", max_work=20)
    finally:
        graph.close()

    assert found == []
