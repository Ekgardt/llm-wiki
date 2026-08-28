"""Bounded multi-hop structural queries over the active Evidence Graph generation.

CODE-01, roadmap 2026-08-18 section 12: parity with codebase-memory-mcp's
`query_graph`, without exposing a query language. The query is a closed JSON
pipeline — one start filter, at most `MAX_HOPS` edge hops, one limit — so
there is no injection surface and every step runs against the engine's own
row, work, and deadline ceilings. The engine refuses silent truncation by
raising (`query row ceiling exceeded`, measured 2026-08-28); this module
turns every such refusal into a named part of the answer.
Research: `docs/research/2026-08-28-bounded-graph-query-mode.md`.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

MAX_HOPS = 3
MAX_LIMIT = 200
DEFAULT_LIMIT = 50
MAX_QUERY_BYTES = 4096
HOP_ROW_CEILING = 200
HOP_WORK_CEILING = 1000

EDGE_TYPES = {
    "calls": "CALLS",
    "defines": "DEFINES",
    "imports": "IMPORTS",
    "contains": "CONTAINS",
    "inherits": "INHERITS",
    "links": "LINKS_TO",
    "exposes": "EXPOSES",
}

QUERY_NOTE = (
    "bounded structural reachability over the active generation; "
    "ceilings refuse by name, nothing is truncated silently"
)

_DIRECTIONS = frozenset({"in", "out"})
_START_KEYS = ("name", "path", "kind")
_QUERY_KEYS = frozenset({"start", "hops", "limit"})
_HOP_KEYS = frozenset({"edge", "direction"})


def _parsed_document(query_text: str) -> dict:
    if len(query_text.encode("utf-8")) > MAX_QUERY_BYTES:
        raise ValueError(f"graph query exceeds {MAX_QUERY_BYTES} bytes")
    try:
        document = json.loads(query_text)
    except json.JSONDecodeError as error:
        raise ValueError(f"graph query is not valid JSON: {error.msg}") from None
    if not isinstance(document, dict):
        raise ValueError("graph query must be a JSON object")
    return document


def _require_closed_keys(mapping: dict, allowed: frozenset, label: str) -> None:
    unknown = sorted(set(mapping) - allowed)
    if unknown:
        raise ValueError(f"{label} has unknown keys: {', '.join(unknown)}")


def _optional_text(value: object, label: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")
    return value


def _validated_start(document: dict) -> dict:
    start = document.get("start")
    if not isinstance(start, dict):
        raise ValueError("graph query needs a 'start' object")
    _require_closed_keys(start, frozenset(_START_KEYS), "start")
    fields = {
        key: _optional_text(start.get(key), f"start.{key}") for key in _START_KEYS
    }
    if not any(fields.values()):
        raise ValueError("start must name at least one of: name, path, kind")
    return fields


def _hop_edge(edge: object, index: int) -> str:
    resolved = EDGE_TYPES.get(edge.lower()) if isinstance(edge, str) else None
    if resolved is None:
        served = ", ".join(sorted(EDGE_TYPES))
        raise ValueError(
            f"hop {index} edge {edge!r} is not served; served edges: {served}"
        )
    return resolved


def _validated_hop(hop: object, index: int) -> dict:
    if not isinstance(hop, dict):
        raise ValueError(f"hop {index} must be an object")
    _require_closed_keys(hop, _HOP_KEYS, f"hop {index}")
    direction = hop.get("direction")
    if direction not in _DIRECTIONS:
        raise ValueError(f"hop {index} direction must be 'in' or 'out'")
    return {"edge": _hop_edge(hop.get("edge"), index), "direction": direction}


def _validated_hops(document: dict) -> list[dict]:
    hops = document.get("hops", [])
    if not isinstance(hops, list):
        raise ValueError("'hops' must be a list")
    if len(hops) > MAX_HOPS:
        raise ValueError(f"at most {MAX_HOPS} hops are served")
    return [_validated_hop(hop, index) for index, hop in enumerate(hops)]


def _validated_limit(document: dict) -> int:
    limit = document.get("limit", DEFAULT_LIMIT)
    if not isinstance(limit, int) or isinstance(limit, bool):
        raise ValueError("'limit' must be an integer")
    if not 1 <= limit <= MAX_LIMIT:
        raise ValueError(f"'limit' must be between 1 and {MAX_LIMIT}")
    return limit


def parse_graph_query(query_text: str) -> dict:
    """The validated closed pipeline, or a ValueError naming the refusal."""
    document = _parsed_document(query_text)
    _require_closed_keys(document, _QUERY_KEYS, "graph query")
    return {
        "start": _validated_start(document),
        "hops": _validated_hops(document),
        "limit": _validated_limit(document),
    }


def _check_deadline(deadline: float) -> None:
    if time.monotonic() >= deadline:
        raise TimeoutError("graph query deadline reached")


def _row_view(row: dict) -> dict:
    metadata = row.get("metadata") or {}
    return {
        "node_id": row["node_id"],
        "kind": row["kind"],
        "name": metadata.get("name"),
        "path": metadata.get("path"),
        "owner": metadata.get("owner"),
    }


def _is_ceiling_refusal(error: ValueError) -> bool:
    return "ceiling exceeded" in str(error)


def _start_nodes(graph, start: dict, limit: int, deadline: float) -> list[dict]:
    kinds = [start["kind"]] if start["kind"] else None
    try:
        return graph.find_nodes(
            kinds=kinds,
            name=start["name"],
            path=start["path"],
            max_rows=limit,
            deadline=deadline,
        )
    except ValueError as error:
        if not _is_ceiling_refusal(error):
            raise
        raise ValueError(
            f"start matches more than {limit} nodes; "
            "narrow it with name or path, or raise limit"
        ) from None


def _expand_one(graph, node: dict, hop: dict, deadline: float) -> list[dict] | None:
    """One node's neighbors, or None when the engine refused by ceiling."""
    try:
        return graph.neighbors(
            node["node_id"],
            direction=hop["direction"],
            edge_types=(hop["edge"],),
            max_depth=1,
            max_rows=HOP_ROW_CEILING,
            max_work=HOP_WORK_CEILING,
            deadline=deadline,
        )
    except ValueError as error:
        if not _is_ceiling_refusal(error):
            raise
        return None


def _expanded_frontier(
    graph, frontier: list[dict], hop: dict, deadline: float
) -> tuple[list[dict], list[str]]:
    found: dict[str, dict] = {}
    refused: list[str] = []
    for node in frontier:
        _check_deadline(deadline)
        rows = _expand_one(graph, node, hop, deadline)
        _merge_expansion(node, rows, found, refused)
    return list(found.values()), refused


def _merge_expansion(
    node: dict, rows: list[dict] | None, found: dict, refused: list[str]
) -> None:
    if rows is None:
        refused.append(node["node_id"])
        return
    for row in rows:
        found.setdefault(row["node_id"], row)


def _bounded_frontier(nodes: list[dict], limit: int) -> tuple[list[dict], bool]:
    if len(nodes) <= limit:
        return nodes, False
    return nodes[:limit], True


def _executed_hops(
    graph, plan: dict, start_rows: list[dict], deadline: float
) -> tuple[list[dict], list[dict], bool]:
    frontier = start_rows
    refusals: list[dict] = []
    truncated = False
    for index, hop in enumerate(plan["hops"]):
        frontier, refused = _expanded_frontier(graph, frontier, hop, deadline)
        frontier, cut = _bounded_frontier(frontier, plan["limit"])
        truncated = truncated or cut
        _record_refusal(refusals, index, refused)
    return frontier, refusals, truncated


def _record_refusal(refusals: list[dict], index: int, refused: list[str]) -> None:
    if refused:
        refusals.append(
            {
                "hop": index,
                "refused_node_ids": refused[:10],
                "refused_count": len(refused),
                "reason": "engine row or work ceiling exceeded",
            }
        )


def _answer(plan: dict, generation_id: str, executed: tuple) -> dict:
    rows, refusals, truncated = executed
    return {
        "status": "ok",
        "generation_id": generation_id,
        "hops_applied": len(plan["hops"]),
        "limit": plan["limit"],
        "frontier_truncated": truncated,
        "refused_expansions": refusals,
        "nodes": [_row_view(row) for row in rows[: plan["limit"]]],
        "note": QUERY_NOTE,
    }


def run_graph_query(directory: Path, query_text: str, deadline: float) -> dict:
    """CODE-01: one bounded start-then-hops pipeline over the active generation."""
    from code_graph import _active_evidence_graph

    plan = parse_graph_query(query_text)
    graph = _active_evidence_graph(directory)
    if graph is None:
        return {"status": "error", "error": "no_active_generation", "note": QUERY_NOTE}
    try:
        start_rows = _start_nodes(graph, plan["start"], plan["limit"], deadline)
        executed = _executed_hops(graph, plan, start_rows, deadline)
        return _answer(plan, str(graph.generation_id), executed)
    finally:
        graph.close()
