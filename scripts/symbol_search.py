"""Ranked symbol search over the whole active generation (issue #24, B2).

Parity with codebase-memory-mcp's `search_graph` name search: a name pattern
answers qualified names with in/out degree, an exact `total` and a
`has_more` flag, ranked exact > prefix > substring > in-degree. The search is
one SQL statement over the generation's `node` table
(`EvidenceGraph.search_nodes`); no artifact is added and no generation format
changes. Not done, by design: BM25 identifier splitting and semantic search
over symbols — both need a symbol-level artifact the generation does not
hold. Research:
`docs/research/2026-09-10-a-query-surface-that-answers-the-whole-graph.md`.
"""

from __future__ import annotations

from pathlib import Path

DEFAULT_KINDS = ("class", "function", "method")
MAX_LIMIT = 100
DEFAULT_LIMIT = 10


def _qualified_name(row: dict) -> str:
    metadata = row["metadata"]
    owner = str(metadata.get("owner", ""))
    name = str(metadata.get("name", row["identity_key"]))
    return f"{owner}.{name}" if owner else name


def _located_row(row: dict, location: dict | None) -> dict:
    found = location or {}
    return {
        "qualified_name": _qualified_name(row),
        "name": row["metadata"].get("name"),
        "kind": row["kind"],
        "path": found.get("relative_path", row["metadata"].get("path")),
        "line": found.get("line"),
        "in_degree": row["in_degree"],
        "out_degree": row["out_degree"],
        "match": ("exact", "prefix", "substring")[row["rank"]],
        "symbol_id": row["node_id"],
    }


def _bounded_limit(limit: int | None) -> int:
    if limit is None:
        return DEFAULT_LIMIT
    return max(1, min(int(limit), MAX_LIMIT))


def _search_answer(graph, pattern: str, path_prefix: str | None, limit: int, deadline: float) -> dict:
    found = graph.search_nodes(
        pattern,
        kinds=DEFAULT_KINDS,
        path_prefix=path_prefix,
        max_rows=limit,
        deadline=deadline,
    )
    rows = found["rows"]
    locations = graph.node_locations([row["node_id"] for row in rows], deadline=deadline)
    return {
        "pattern": pattern,
        "kinds": list(DEFAULT_KINDS),
        "path_prefix": path_prefix,
        "results": [_located_row(row, locations.get(row["node_id"])) for row in rows],
        "total": found["total"],
        "has_more": found["truncated"],
        "limit": limit,
        "ranking": "exact, prefix, substring; then in-degree, then name",
        "degree_note": "resolved edges of every served type; not caller counts",
        "generation_id": str(graph.generation_id),
    }


def search_symbols(
    directory: Path,
    pattern: str,
    *,
    path_prefix: str | None = None,
    limit: int | None = None,
    deadline: float,
) -> dict:
    """Ranked qualified names matching `pattern`, or a named refusal."""
    from code_graph import _active_evidence_graph

    graph = _active_evidence_graph(directory)
    if graph is None:
        return {"pattern": pattern, "results": [], "graph": "no_active_generation"}
    try:
        return _search_answer(graph, pattern, path_prefix, _bounded_limit(limit), deadline)
    finally:
        graph.close()
