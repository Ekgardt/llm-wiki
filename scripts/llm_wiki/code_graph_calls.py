"""Callers and callees of a function, from the generation or a live parse."""
from __future__ import annotations

from pathlib import Path

try:
    from ..code_hints import (
        LOCATION_CHUNK,
        in_node_chunks,
    )
except ImportError:
    from code_hints import (
        LOCATION_CHUNK,
        in_node_chunks,
    )
from . import code_graph_generation
from .code_graph_generation import (
    NO_GENERATION,
    stored_edge_location,
    stored_or_reason,
    stored_qualified_name,
)
from .code_graph_parse import parsed_live_sources
from .code_graph_reports import (
    dependency_reach,
    live_report,
    named_node_ids,
    reported_answer,
    seed_cut_report,
    store_report,
)


def stored_callers(
    function_name: str, directory: Path, with_report: bool, max_depth: int | None = None
) -> list[dict] | dict | None:
    depth = _call_walk_depth(max_depth)
    if depth > 1:
        return _store_walk_calls(function_name, directory, "in", depth, with_report)
    if with_report:
        return store_find_callers(function_name, directory, with_report=True)
    return store_find_callers(function_name, directory)


# Issue #24, B4: `callers`/`callees` with a depth walk the generation's bounded
# CALLS closure (`EvidenceGraph.neighbors`, a recursive CTE with depth, work
# and row ceilings). Depth 1 is the unchanged one-hop path; the walk reports
# `depth_applied` and `depth_frontier_open` exactly as `dependencies` does. The
# depth, row, work and seed bounds are the dependency walk's (DEPENDENCY_* below);
# past rows or work the engine refuses rather than truncates. Basis unknown beyond
# that: values predate measurement.
CALL_WALK_MAX_DEPTH = 8
CALL_WALK_MAX_ROWS = 10_000
CALL_WALK_MAX_WORK = 100_000
CALL_WALK_MAX_SEEDS = 20
def _call_walk_depth(max_depth: int | None) -> int:
    if max_depth is None:
        return 1
    return max(1, min(int(max_depth), CALL_WALK_MAX_DEPTH))


def _walk_seeds(graph, function_name: str) -> list[str]:
    """Every node the name resolves to, sorted; the walk cuts and reports."""
    return sorted(set(named_node_ids(graph, ("function", "method"), function_name)))


def _keep_shallowest(merged: dict, row: dict) -> None:
    current = merged.get(row["node_id"])
    if current is None or row["depth"] < current["depth"]:
        merged[row["node_id"]] = row


def _walked_nodes(graph, seeds: list[str], direction: str, depth: int) -> dict:
    """The CALLS closure of every seed, breadth-first, shallowest depth kept.

    `reachable` and not `neighbors`: the recursive CTE carries its visited set
    as a string down every path, and measured 2026-09-10 on a 1 020-file
    fixture (20 seeds) it took 801 ms at depth 3 and 4.0 s at depth 8, where
    the breadth-first walk expands each node once.
    """
    merged: dict = {}
    for seed in seeds:
        rows = graph.reachable(
            seed,
            edge_types=("CALLS",),
            reverse=direction == "in",
            max_depth=depth,
            max_rows=CALL_WALK_MAX_ROWS,
            max_work=CALL_WALK_MAX_WORK,
        )
        for row in rows:
            _keep_shallowest(merged, row)
    return merged


def _walk_locations(graph, node_ids: list[str]) -> dict:
    found: dict = {}
    for index in range(0, len(node_ids), LOCATION_CHUNK):
        found.update(graph.node_locations(node_ids[index : index + LOCATION_CHUNK]))
    return found


def _walked_row(graph, node: dict, location: dict | None, function_name: str) -> dict:
    source_root = Path(graph.repository_scope.checkout_root)
    found = location or {}
    relative = found.get("relative_path")
    return {
        "file": str(source_root / relative) if relative else "",
        "line": found.get("line", 0),
        "function": function_name,
        "name": node["metadata"].get("name", node["identity_key"]),
        "qualified_name": stored_qualified_name(node),
        "depth": node["depth"],
        "symbol_id": node["node_id"],
    }


def _walked_rows(graph, merged: dict, function_name: str, direction: str) -> list[dict]:
    ordered = sorted(
        merged.values(),
        key=lambda item: (item["depth"], stored_qualified_name(item), item["node_id"]),
    )
    locations = _walk_locations(graph, [item["node_id"] for item in ordered])
    rows = [
        _walked_row(graph, item, locations.get(item["node_id"]), function_name)
        for item in ordered
    ]
    if direction == "out":
        for row in rows:
            row["callee"] = row["name"]
    return rows


def _walk_report(graph, function_name: str, direction: str, seeds: list, rows: list, depth: int) -> dict:
    report = {
        "symbol_resolved": bool(seeds),
        "resolved_symbol_nodes": len(seeds),
        **dependency_reach(rows, depth),
    }
    if direction == "in":
        report.update(_unresolved_caller_fields(graph, function_name))
    return report


def _store_walk_calls(
    function_name: str, directory: Path, direction: str, depth: int, with_report: bool
) -> list[dict] | dict | None:
    graph = code_graph_generation.active_evidence_graph(directory)
    if graph is None:
        return None
    try:
        matched = _walk_seeds(graph, function_name)
        seeds = matched[:CALL_WALK_MAX_SEEDS]
        rows = _walked_rows(
            graph, _walked_nodes(graph, seeds, direction, depth), function_name, direction
        )
        report = {
            **store_report(graph),
            **_walk_report(graph, function_name, direction, seeds, rows, depth),
            **seed_cut_report(matched, CALL_WALK_MAX_SEEDS),
        }
        key = "callers" if direction == "in" else "callees"
        return reported_answer(key, rows, report, with_report)
    finally:
        graph.close()


def _named_function(result: dict, function_name: str) -> dict | None:
    return next(
        (item for item in result["functions"] if item["name"] == function_name), None
    )


def _callees_in_function(path: Path, result: dict, func_def: dict) -> list[dict]:
    callees = []
    for call in result["calls"]:
        end_line = func_def.get("end_line", call["line"])
        if func_def["line"] <= call["line"] <= end_line:
            callees.append(
                {"file": str(path), "line": call["line"], "callee": call["name"]}
            )
    return callees


# NEW-124. The bound on the *sample* of unresolved call sites one answer names.
# `unresolved_caller_count` stays exact above it, so a cut list still states how
# much it is missing rather than implying it is whole.
UNRESOLVED_CALLER_LIMIT = 200


def _unresolved_caller_row(graph, row: dict, function_name: str) -> dict:
    caller = graph.node(row["source_node_id"])
    source_root = Path(graph.repository_scope.checkout_root)
    return {
        "file": str(source_root / row["relative_path"]),
        "line": row["line"],
        "function": function_name,
        "qualified_name": "" if caller is None else stored_qualified_name(caller),
        "call_text": row["target_text"],
        "reason": row["reason"],
    }


def _unresolved_caller_fields(graph, function_name: str) -> dict[str, object]:
    """The calls this answer could not bind, so a zero never reads as an absence.

    NEW-124. `queue.recover_expired_leases()` reaches a method through a
    variable the extractor cannot type, so it leaves an *observation* rather
    than a CALLS assertion. An answer built only from assertions said
    `0 callers` for `recover_expired_leases`, which has two - a silently wrong
    answer, and a worse failure than the ceiling refusal it replaced, because a
    refusal says "I cannot" and this said "nobody does".

    These rows are candidates matched by the attribute the call names. The
    receiver's type is exactly what could not be established, so they are never
    counted as callers and never merged into `callers`.
    """
    found = graph.unresolved_calls_naming(
        function_name, max_rows=UNRESOLVED_CALLER_LIMIT
    )
    return {
        "unresolved_callers": [
            _unresolved_caller_row(graph, row, function_name) for row in found["calls"]
        ],
        "unresolved_caller_count": found["count"],
        "unresolved_callers_truncated": found["truncated"],
    }


def _caller_edge(path: Path, call: dict, function_name: str) -> dict:
    return {
        "file": str(path),
        "line": call["line"],
        "function": function_name,
        "qualified_name": call.get("qualified_name"),
        "confidence": call.get("confidence", "heuristic"),
    }


def _call_names_target(call: dict, function_name: str, language: str) -> bool:
    """Unknown Python calls are excluded; other languages fall back to the name."""
    resolved = (call.get("qualified_name") or call["name"]).rsplit(".", 1)[-1]
    if resolved != function_name:
        return False
    return call.get("confidence") == "confirmed" or language != "python"


def _live_callers_in_file(path: Path, result: dict, function_name: str) -> list[dict]:
    return [
        _caller_edge(path, call, function_name)
        for call in result["calls"]
        if _call_names_target(call, function_name, result["language"])
    ]


def _call_names_unbound_target(call: dict, function_name: str, language: str) -> bool:
    """A name match the live pass refuses to confirm: the receiver stayed open."""
    resolved = (call.get("qualified_name") or call["name"]).rsplit(".", 1)[-1]
    return (
        resolved == function_name
        and language == "python"
        and call.get("confidence") != "confirmed"
    )


def _live_unresolved_caller(path: Path, call: dict, function_name: str) -> dict:
    return {
        "file": str(path),
        "line": call["line"],
        "function": function_name,
        "qualified_name": call.get("qualified_name"),
        "call_text": call.get("name", function_name),
        "reason": "unconfirmed_receiver",
    }


def _live_unresolved_in_file(path: Path, result: dict, function_name: str) -> list[dict]:
    return [
        _live_unresolved_caller(path, call, function_name)
        for call in result["calls"]
        if _call_names_unbound_target(call, function_name, result["language"])
    ]


def _live_unresolved_fields(unresolved: list[dict]) -> dict[str, object]:
    """The live pass owes the same distinction the stored pass now makes."""
    sample = unresolved[:UNRESOLVED_CALLER_LIMIT]
    return {
        "unresolved_callers": sample,
        "unresolved_caller_count": len(unresolved),
        "unresolved_callers_truncated": len(unresolved) > len(sample),
    }


def _live_caller_scan(
    parsed: list[tuple[Path, dict]], function_name: str
) -> tuple[list[dict], list[dict]]:
    callers: list[dict] = []
    unresolved: list[dict] = []
    for path, result in parsed:
        callers.extend(_live_callers_in_file(path, result, function_name))
        unresolved.extend(_live_unresolved_in_file(path, result, function_name))
    return callers, unresolved


def find_callers(
    function_name: str,
    directory: Path,
    *,
    live: bool = False,
    with_report: bool = False,
    max_depth: int | None = None,
) -> list[dict] | dict:
    """Find callers using strict Python evidence or non-Python name heuristics.

    Unknown Python calls are excluded from `callers`, and named separately in
    `unresolved_callers` with an exact `unresolved_caller_count` (NEW-124), so
    an empty `callers` list distinguishes "nobody calls it" from "the receiver
    could not be resolved". Both fields need `with_report=True`; the bare list
    form is kept for callers that only want confirmed edges.

    `max_depth` above 1 walks the generation's CALLS closure that deep (issue
    #24, B4); rows then carry `depth`, and the report `depth_applied` and
    `depth_frontier_open`. The live fallback answers one hop and says so.
    """
    stored, reason = None, NO_GENERATION
    if not live:
        stored, reason = stored_or_reason(
            lambda: stored_callers(function_name, directory, with_report, max_depth)
        )
    if stored is not None:
        return stored
    parsed = parsed_live_sources(directory)
    callers, unresolved = _live_caller_scan(parsed, function_name)
    report = {
        **live_report(directory, parsed, reason),
        **_live_unresolved_fields(unresolved),
    }
    return reported_answer("callers", callers, report, with_report)


def _stored_caller_row(graph, edge, function_name: str, directory: Path) -> dict | None:
    caller = graph.node(edge["source_node_id"])
    if caller is None:
        return None
    location = stored_edge_location(graph, edge["assertion_id"], directory)
    return {
        "file": location[0],
        "line": location[1],
        "function": function_name,
        "qualified_name": stored_qualified_name(caller),
        "confidence": edge["confidence"],
        "symbol_id": caller["node_id"],
    }


def _sorted_stored_rows(rows: list, key) -> list[dict]:
    return sorted([row for row in rows if row is not None], key=key)


def _caller_sort_key(item: dict):
    return (item["file"], item["line"], item["symbol_id"])


def store_find_callers(
    function_name: str, directory: Path, *, with_report: bool = False
) -> list[dict] | dict | None:
    graph = code_graph_generation.active_evidence_graph(directory)
    if graph is None:
        return None
    try:
        target_ids = _walk_seeds(graph, function_name)
        edges = in_node_chunks(
            target_ids,
            lambda ids: graph.edges(
                edge_types=("CALLS",), target_node_ids=ids, max_rows=10_000
            ),
        )
        rows = [
            _stored_caller_row(graph, edge, function_name, directory) for edge in edges
        ]
        results = _sorted_stored_rows(rows, _caller_sort_key)
        report = {
            **store_report(graph),
            **_unresolved_caller_fields(graph, function_name),
        }
        return reported_answer("callers", results, report, with_report)
    finally:
        graph.close()


def _stored_callees(
    function_name: str, directory: Path, with_report: bool, max_depth: int | None = None
) -> list[dict] | dict | None:
    depth = _call_walk_depth(max_depth)
    if depth > 1:
        return _store_walk_calls(function_name, directory, "out", depth, with_report)
    if with_report:
        return store_find_callees(function_name, directory, with_report=True)
    return store_find_callees(function_name, directory)


def _live_callees_in_file(path: Path, result: dict, function_name: str) -> list[dict]:
    func_def = _named_function(result, function_name)
    if func_def is None:
        return []
    return _callees_in_function(path, result, func_def)


def find_callees(
    function_name: str,
    directory: Path,
    *,
    live: bool = False,
    with_report: bool = False,
    max_depth: int | None = None,
) -> list[dict] | dict:
    """Find all functions called BY a function (CALLS edge, forward direction).

    Returns list of {file, line, callee}. `max_depth` above 1 walks the
    generation's CALLS closure that deep (issue #24, B4).
    """
    stored, reason = None, NO_GENERATION
    if not live:
        stored, reason = stored_or_reason(
            lambda: _stored_callees(function_name, directory, with_report, max_depth)
        )
    if stored is not None:
        return stored
    callees: list[dict] = []
    parsed = parsed_live_sources(directory)
    for path, result in parsed:
        callees.extend(_live_callees_in_file(path, result, function_name))
    return reported_answer(
        "callees", callees, live_report(directory, parsed, reason), with_report
    )


def _stored_callee_row(graph, edge, directory: Path) -> dict | None:
    callee = graph.node(edge["target_node_id"])
    if callee is None:
        return None
    location = stored_edge_location(graph, edge["assertion_id"], directory)
    return {
        "file": location[0],
        "line": location[1],
        "callee": callee["metadata"].get("name", callee["identity_key"]),
        "symbol_id": callee["node_id"],
        "confidence": edge["confidence"],
    }


def _callee_sort_key(item: dict):
    return (str(item["callee"]), item["symbol_id"])


def store_find_callees(
    function_name: str, directory: Path, *, with_report: bool = False
) -> list[dict] | dict | None:
    graph = code_graph_generation.active_evidence_graph(directory)
    if graph is None:
        return None
    try:
        source_ids = _walk_seeds(graph, function_name)
        edges = in_node_chunks(
            source_ids,
            lambda ids: graph.edges(
                edge_types=("CALLS",), source_node_ids=ids, max_rows=10_000
            ),
        )
        rows = [_stored_callee_row(graph, edge, directory) for edge in edges]
        results = _sorted_stored_rows(rows, _callee_sort_key)
        return reported_answer("callees", results, store_report(graph), with_report)
    finally:
        graph.close()
