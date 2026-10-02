"""The class-d cuts in the core modules lose nothing silently (law 9, phase 2a).

Malformed claims are reported; valid claims share the response budget. The
navigation answer's CALLS read is anchored on the symbol, so a repository with more
call edges than the fact bound still finds the symbol's calls. See
`docs/research/2026-09-27-a-cut-says-what-it-left-out.md` and
`docs/research/2026-09-27-every-limit-states-its-reason.md`.
"""

from __future__ import annotations

import ast
from pathlib import Path

import compile_memory
import doctor
import mcp_server


def _candidate(index: int) -> dict[str, object]:
    return {
        "evidence_index": 0,
        "subject": f"lease {index}",
        "relation": "ends-at",
        "value": {"type": "number", "value": str(index), "unit": "seconds"},
    }


def test_admission_preserves_valid_claims_and_reports_only_malformed() -> None:
    candidates = [_candidate(index) for index in range(10)]
    before = len(compile_memory.DROPPED_CLAIMS)
    kept = compile_memory._admitted_candidates(candidates + [{"subject": None}], "cut-page")
    dropped = compile_memory.DROPPED_CLAIMS[before:]
    assert kept == candidates
    assert [item["slug"] for item in dropped] == ["cut-page"]


class _AnchoredGraph:
    """Holds more CALLS edges than the fact bound; the symbol's edge sorts last."""

    def __init__(self) -> None:
        self.anchors: list[dict[str, object]] = []

    def find_nodes(self, **_options):
        return [{"node_id": "target-node", "kind": "function"}]

    def edges(self, *, edge_types, max_rows, deadline, source_node_ids=None, target_node_ids=None):
        self.anchors.append({"source": source_node_ids, "target": target_node_ids})
        return []


def test_the_calls_read_is_anchored_on_the_symbols_nodes() -> None:
    graph = _AnchoredGraph()

    mcp_server._collected_call_locations(graph, "target", None, "incoming", None, None)

    assert graph.anchors == [{"source": None, "target": ["target-node"]}]


class _ManyNodeGraph(_AnchoredGraph):
    """A common name: more nodes than one anchored read may name."""

    def find_nodes(self, **_options):
        return [{"node_id": f"node-{index:04d}", "kind": "function"} for index in range(600)]


def test_a_common_name_is_read_in_slices_the_reader_accepts() -> None:
    graph = _ManyNodeGraph()

    mcp_server._collected_call_locations(graph, "main", None, "outgoing", None, None)

    assert [len(anchor["source"]) for anchor in graph.anchors] == [512, 88]


def _is_edge_read(node: ast.AST) -> bool:
    return isinstance(node, ast.Call) and ast.unparse(node.func) == "graph.edges"


def _unanchored_edge_reads(path: Path) -> list[str]:
    """`graph.edges(...)` calls naming neither end, nor passing an anchor mapping."""
    calls = list(filter(_is_edge_read, ast.walk(ast.parse(path.read_text(encoding="utf-8")))))
    return [f"{path.name}:{call.lineno}" for call in calls if not _names_an_anchor(call)]


def _names_an_anchor(call: ast.Call) -> bool:
    keywords = {keyword.arg for keyword in call.keywords}
    return bool(keywords & {"source_node_ids", "target_node_ids", None})


def test_every_graph_edge_read_is_anchored_on_nodes() -> None:
    scripts = sorted((Path(__file__).resolve().parents[1] / "scripts").glob("*.py"))
    assert [name for path in scripts for name in _unanchored_edge_reads(path)] == []


def test_the_claims_finding_counts_the_pages_it_does_not_name() -> None:
    pages = doctor.MAX_CLAIM_PAGES_NAMED + 3
    details: dict = {"codes": []}

    class _Database:
        def execute(self, sql, _parameters=()):
            rows = [("unresolved", f"page-{index}") for index in range(pages)]
            return _Rows(rows if "claim_index_diagnostic" in sql else [(1,)])

    doctor._count_claims(_Database(), details)

    assert (len(details["pages"]), details["pages_omitted"]) == (doctor.MAX_CLAIM_PAGES_NAMED, 3)


class _Rows:
    def __init__(self, rows: list[tuple]) -> None:
        self._rows = rows

    def fetchall(self) -> list[tuple]:
        return self._rows
