"""What the current diff touches, transitively, as code symbols (issue #24, B5).

`impact_analysis.analyze_impact` maps a diff to the symbols whose definition
spans it changed and walks confirmed reverse edges from them, but keeps only
decisions, pages, tests and checkpoints: every function, method or class it
reached is dropped. This module answers that missing half from the same
generation — the code symbols that call, import or inherit a changed symbol
within eight hops — and `mode=impact` carries it as `affected_symbols` beside
the unchanged `affected` groups. Bounded seeds, depth, work and rows; a cut
answer says so. The walk follows every resolved edge, including the
extractor's medium-confidence ones, and the answer names that difference
from the confirmed-only `affected` groups.
Research: `docs/research/2026-09-10-a-query-surface-that-answers-the-whole-graph.md`.
"""

from __future__ import annotations

from pathlib import Path

REACH_EDGE_TYPES = ("CALLS", "IMPORTS", "INHERITS")
MAX_DEPTH = 8
MAX_SEEDS = 50
MAX_ROWS = 200
WALK_MAX_ROWS = 2_000
WALK_MAX_WORK = 20_000
CODE_KINDS = frozenset({"class", "function", "method"})
EDGE_NOTE = (
    "walks every resolved CALLS/IMPORTS/INHERITS edge, medium confidence "
    "included; the affected groups use confirmed edges only"
)


def _qualified_name(node: dict) -> str:
    metadata = node["metadata"]
    owner = str(metadata.get("owner", ""))
    name = str(metadata.get("name", node["identity_key"]))
    return f"{owner}.{name}" if owner else name


def _seed_ids(changed_symbols: list[dict]) -> set[str]:
    return {str(item["node_id"]) for item in changed_symbols if item.get("node_id")}


def _reached(graph, seed: str, deadline: float) -> list[dict]:
    return graph.reachable(
        seed,
        edge_types=REACH_EDGE_TYPES,
        reverse=True,
        max_depth=MAX_DEPTH,
        max_rows=WALK_MAX_ROWS,
        max_work=WALK_MAX_WORK,
        deadline=deadline,
    )


def _keep_shallowest(merged: dict, row: dict) -> None:
    current = merged.get(row["node_id"])
    if current is None or row["depth"] < current["depth"]:
        merged[row["node_id"]] = row


def _merge(merged: dict, rows: list[dict], seeds: set[str]) -> None:
    for row in rows:
        if row["node_id"] in seeds or row["kind"] not in CODE_KINDS:
            continue
        _keep_shallowest(merged, row)


def _walk(graph, seeds: set[str], deadline: float) -> tuple[dict, bool]:
    merged: dict = {}
    partial = len(seeds) > MAX_SEEDS
    for seed in sorted(seeds)[:MAX_SEEDS]:
        try:
            rows = _reached(graph, seed, deadline)
        except ValueError:
            partial = True
            continue
        _merge(merged, rows, seeds)
    return merged, partial


def _row(node: dict, location: dict | None) -> dict:
    found = location or {}
    return {
        "qualified_name": _qualified_name(node),
        "kind": node["kind"],
        "path": found.get("relative_path", node["metadata"].get("path")),
        "line": found.get("line"),
        "depth": node["depth"],
        "symbol_id": node["node_id"],
    }


def _symbol_rows(graph, merged: dict, deadline: float) -> list[dict]:
    ordered = sorted(
        merged.values(), key=lambda item: (item["depth"], _qualified_name(item), item["node_id"])
    )[:MAX_ROWS]
    locations = graph.node_locations([item["node_id"] for item in ordered], deadline=deadline)
    return [_row(item, locations.get(item["node_id"])) for item in ordered]


def _answer(rows: list[dict], truncated: bool, note: str) -> dict:
    return {
        "affected_symbols": rows,
        "affected_symbols_truncated": truncated,
        "affected_symbols_depth": MAX_DEPTH,
        "affected_symbols_edges": list(REACH_EDGE_TYPES),
        "affected_symbols_note": note,
    }


def _walked_answer(graph, seeds: set[str], deadline: float) -> dict:
    merged, partial = _walk(graph, seeds, deadline)
    rows = _symbol_rows(graph, merged, deadline)
    return _answer(rows, partial or len(merged) > MAX_ROWS, EDGE_NOTE)


def affected_symbols(directory: Path, changed_symbols: list[dict], deadline: float) -> dict:
    """Code symbols reachable backwards from the changed ones, bounded."""
    from code_graph import _active_evidence_graph

    seeds = _seed_ids(changed_symbols)
    if not seeds:
        return _answer([], False, "no changed symbol resolved in the generation")
    graph = _active_evidence_graph(directory)
    if graph is None:
        return _answer([], False, "no_active_generation")
    try:
        return _walked_answer(graph, seeds, deadline)
    finally:
        graph.close()
