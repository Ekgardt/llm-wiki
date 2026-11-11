"""Argument-binding flows and service paths walked through the generation."""
from __future__ import annotations

import sqlite3
from pathlib import Path

try:
    from ..code_hints import in_node_chunks
except ImportError:
    from code_hints import in_node_chunks
from . import code_graph_dependencies, code_graph_generation, runtime_roots
from .code_graph_generation import (
    check_generation_stop,
    stored_qualified_name,
)
from .code_graph_reports import (
    dependency_reach,
    reported_answer,
    seed_cut_report,
    store_report,
)

# Issue #24, section B: `data_flow` is argument binding, not a data-flow graph.
# The extractor records, per resolved call, which caller-visible name lands on
# which parameter of the callee (`BINDS_ARGUMENTS`, literal
# `argument->parameter`, joined to its `CALLS` edge by the span of the call);
# this walk follows those bindings hop by hop and says so in its own answer.
# Research: docs/research/2026-09-11-argument-bindings-and-route-calls.md.
FLOW_MAX_DEPTH = 8
FLOW_DEFAULT_DEPTH = 2
# Rows one argument-flow answer returns, a display cut stated in it as
# `flow_count` / `flows_truncated`; each hop's query refuses more than this.
FLOW_MAX_ROWS = 1000
FLOW_MAX_SEEDS = 20
FLOW_NOTE = (
    "argument bindings of resolved calls, not data-flow analysis: "
    "each hop names which argument binds which parameter"
)


def _flow_depth(max_depth: int | None) -> int:
    if max_depth is None:
        return FLOW_DEFAULT_DEPTH
    return max(1, min(int(max_depth), FLOW_MAX_DEPTH))


def _flow_row(graph, edge: dict, depth: int) -> dict | None:
    target = graph.node(edge["target_node_id"])
    source = graph.node(edge["source_node_id"])
    if target is None or source is None:
        return None
    return {
        "depth": depth,
        "from": stored_qualified_name(source),
        "to": stored_qualified_name(target),
        "bindings": edge["literal"],
        "symbol_id": target["node_id"],
    }


def _flow_hop(graph, frontier: list[str], seen: set[str], depth: int) -> tuple[list[dict], list[str]]:
    """One hop of binding edges, and the nodes it opens for the next hop."""
    edges = in_node_chunks(
        frontier,
        lambda ids: graph.argument_bindings(source_node_ids=ids, max_rows=FLOW_MAX_ROWS),
    )
    return _flow_hop_rows(graph, edges, depth), _flow_hop_reached(edges, seen)


def _flow_hop_rows(graph, edges: list[dict], depth: int) -> list[dict]:
    rows = [_flow_row(graph, edge, depth) for edge in edges]
    return [row for row in rows if row is not None]


def _flow_hop_reached(edges: list[dict], seen: set[str]) -> list[str]:
    return [str(edge["target_node_id"]) for edge in edges if str(edge["target_node_id"]) not in seen]


def _flow_rows(
    graph, seeds: list[str], depth: int, deadline=None, cancelled=None
) -> tuple[list[dict], bool]:
    """Every row the walk found, and whether a hop's frontier had to be narrowed."""
    rows: list[dict] = []
    seen = set(seeds)
    frontier = list(seeds)
    narrowed = False
    for hop in range(1, depth + 1):
        check_generation_stop(deadline, cancelled)
        if not frontier:
            break
        hop_rows, reached = _flow_hop(graph, frontier, seen, hop)
        rows.extend(hop_rows)
        seen.update(reached)
        following = sorted(set(reached))
        narrowed = narrowed or len(following) > FLOW_MAX_ROWS
        frontier = following[:FLOW_MAX_ROWS]
    return rows, narrowed


def flow_cut_report(rows: list[dict], narrowed: bool) -> dict:
    """What the row cut and a narrowed frontier left out of the answer."""
    return {"flow_count": len(rows), "flows_truncated": narrowed or len(rows) > FLOW_MAX_ROWS}


def _flow_report(graph, symbol: str, seeds: list[str], rows: list[dict], depth: int) -> dict:
    return {
        **store_report(graph),
        "symbol": symbol,
        "symbol_resolved": bool(seeds),
        "resolved_symbol_nodes": len(seeds),
        "note": FLOW_NOTE,
        **dependency_reach(rows, depth),
    }


def find_argument_flows(
    symbol: str,
    directory: Path,
    *,
    with_report: bool = True,
    max_depth: int | None = None,
    deadline: float | None = None,
    cancelled=None,
) -> list[dict] | dict | None:
    """Argument bindings reachable from `symbol`, hop by hop, or None with no generation.

    `deadline` is a `time.monotonic()` value and `cancelled` a callable; either
    stops the walk between hops with `TimeoutError` instead of letting it run to
    completion after its caller has given up (audit 3, B3). Research:
    `docs/research/2026-09-18-graph-a-flow-walk-stops-when-its-caller-has.md`.
    """
    graph = code_graph_generation.active_evidence_graph(directory, deadline=deadline, cancelled=cancelled)
    if graph is None:
        return None
    try:
        matched = code_graph_dependencies.dependency_seed_nodes(graph, symbol)
        seeds = matched[:FLOW_MAX_SEEDS]
        depth = _flow_depth(max_depth)
        found, narrowed = _flow_rows(graph, seeds, depth, deadline, cancelled)
        rows = found[:FLOW_MAX_ROWS]
        report = {
            **_flow_report(graph, symbol, seeds, rows, depth),
            **seed_cut_report(matched, FLOW_MAX_SEEDS),
            **flow_cut_report(found, narrowed),
        }
        return reported_answer("flows", rows, report, with_report)
    finally:
        graph.close()


# Issue #24, section B: `cross_service` crosses the HTTP boundary the graph
# already holds. A client call reaches a `route` node (`HTTP_CALLS`) and the
# handler that serves it declares the same node (`EXPOSES`), so a walk that
# turns around at a route follows a request into the service that answers it.
# Research: docs/research/2026-09-11-argument-bindings-and-route-calls.md.
SERVICE_EDGES = ("CALLS", "HTTP_CALLS")
SERVICE_NOTE = (
    "calls and literal-path HTTP client calls: a route is crossed into the "
    "handler that exposes it, never into a caller the graph cannot prove. A "
    "route this repository does not serve is matched against the exported "
    "routes of every other indexed checkout, and such a hop names the "
    "repository it lands in but is not walked further"
)


def _service_row(graph, edge: dict, depth: int, relation: str) -> dict | None:
    target = graph.node(str(edge["target_node_id"]))
    source = graph.node(str(edge["source_node_id"]))
    if target is None or source is None:
        return None
    return {
        "depth": depth,
        "from": stored_qualified_name(source),
        "to": stored_qualified_name(target),
        "relation": relation,
        "symbol_id": target["node_id"],
    }


def _service_rows_of(graph, edges: list[dict], depth: int) -> list[dict]:
    rows = [
        _service_row(graph, edge, depth, str(edge["edge_type"]).lower())
        for edge in edges
    ]
    return [row for row in rows if row is not None]


def _handler_rows(graph, routes: list[str], depth: int) -> list[dict]:
    """Turn around at a route: the handler that exposes it serves the request."""
    if not routes:
        return []
    edges = in_node_chunks(
        routes,
        lambda ids: graph.edges(
            edge_types=("EXPOSES",), target_node_ids=ids, max_rows=FLOW_MAX_ROWS
        ),
    )
    rows = [_service_row(graph, _reversed_edge(edge), depth, "handled_by") for edge in edges]
    return [row for row in rows if row is not None]


def _reversed_edge(edge: dict) -> dict:
    return {
        **edge,
        "source_node_id": edge["target_node_id"],
        "target_node_id": edge["source_node_id"],
    }


def _route_node_ids(graph, rows: list[dict]) -> list[str]:
    reached = [graph.node(str(row["symbol_id"])) for row in rows]
    return [str(node["node_id"]) for node in reached if node is not None and node["kind"] == "route"]


def _hints_state_root() -> Path:
    return runtime_roots.state_root()


def _foreign_route_rows(graph, observation: dict, depth: int) -> list[dict]:
    """A route no local generation holds may be served by another checkout (#24, D2)."""
    from code_hints import find_routes

    method, _, path = str(observation["target_text"] or "").partition(" ")
    source = graph.node(str(observation["source_node_id"]))
    if not path or source is None:
        return []
    matches = find_routes(
        _hints_state_root(), method, path,
        exclude_checkout_id=str(graph.repository_scope.checkout_id),
    )
    return [_foreign_row(source, observation, match, depth) for match in matches]


def _foreign_row(source: dict, observation: dict, match: dict, depth: int) -> dict:
    return {
        "depth": depth,
        "from": stored_qualified_name(source),
        "to": str(match["handler"]),
        "relation": "handled_by_repository",
        "symbol_id": "",
        "route": str(observation["target_text"] or ""),
        "repository_id": match["repository_id"],
        "checkout_root": match["checkout_root"],
        "file": match["file"],
        "line": match["line"],
    }


def _foreign_rows(graph, frontier: list[str], depth: int) -> list[dict]:
    observations = in_node_chunks(
        frontier,
        lambda ids: graph.unresolved_edges(
            edge_types=("HTTP_CALLS",), source_node_ids=ids, max_rows=FLOW_MAX_ROWS
        ),
    )
    return [row for item in observations for row in _foreign_route_rows(graph, item, depth)]


def _quiet_foreign_rows(graph, frontier: list[str], depth: int) -> list[dict]:
    """A hint table is a disposable projection: its absence is not an error."""
    try:
        return _foreign_rows(graph, frontier, depth)
    except (OSError, ValueError, TypeError, sqlite3.Error):
        return []


def _service_hop(graph, frontier: list[str], depth: int) -> list[dict]:
    edges = in_node_chunks(
        frontier,
        lambda ids: graph.edges(
            edge_types=SERVICE_EDGES, source_node_ids=ids, max_rows=FLOW_MAX_ROWS
        ),
    )
    rows = _service_rows_of(graph, edges, depth)
    rows += _handler_rows(graph, _route_node_ids(graph, rows), depth)
    return rows + _quiet_foreign_rows(graph, frontier, depth)


def _service_frontier(rows: list[dict], seen: set[str]) -> list[str]:
    """Every node this hop reached first; a foreign handler carries no node id here."""
    identifiers = [str(row["symbol_id"]) for row in rows if row["symbol_id"]]
    reached = [identity for identity in identifiers if identity not in seen]
    seen.update(reached)
    return sorted(set(reached))


def _service_walk(
    graph, seeds: list[str], depth: int, deadline=None, cancelled=None
) -> tuple[list[dict], bool]:
    """Every row the walk found, and whether a hop's frontier had to be narrowed.

    Like `_flow_rows`: the frontier and the answer are cut at FLOW_MAX_ROWS, and
    both cuts are reported instead of silently shortening the walk
    (docs/research/2026-09-27-a-cut-says-what-it-left-out.md).
    """
    rows: list[dict] = []
    seen = set(seeds)
    frontier = list(seeds)
    narrowed = False
    for hop in range(1, depth + 1):
        check_generation_stop(deadline, cancelled)
        if not frontier:
            break
        hop_rows = _service_hop(graph, frontier, hop)
        rows.extend(hop_rows)
        following = _service_frontier(hop_rows, seen)
        narrowed = narrowed or len(following) > FLOW_MAX_ROWS
        frontier = following[:FLOW_MAX_ROWS]
    return rows, narrowed


def _service_cut_report(rows: list[dict], narrowed: bool) -> dict:
    """What the row cut and a narrowed frontier left out of the answer."""
    return {"hop_count": len(rows), "hops_truncated": narrowed or len(rows) > FLOW_MAX_ROWS}


def _service_report(graph, symbol: str, seeds: list[str], rows: list[dict], depth: int) -> dict:
    return {
        **store_report(graph),
        "symbol": symbol,
        "symbol_resolved": bool(seeds),
        "resolved_symbol_nodes": len(seeds),
        "note": SERVICE_NOTE,
        "routes_crossed": len([row for row in rows if row["relation"] == "handled_by"]),
        "repositories_crossed": len(
            {row["repository_id"] for row in rows if row["relation"] == "handled_by_repository"}
        ),
        **dependency_reach(rows, depth),
    }


def find_service_paths(
    symbol: str,
    directory: Path,
    *,
    with_report: bool = True,
    max_depth: int | None = None,
    deadline: float | None = None,
    cancelled=None,
) -> list[dict] | dict | None:
    """Calls and HTTP hops reachable from `symbol`, or None with no generation.

    `deadline` and `cancelled` bound the walk in time the way they bound every
    other reader here — see `find_argument_flows`.
    """
    graph = code_graph_generation.active_evidence_graph(directory, deadline=deadline, cancelled=cancelled)
    if graph is None:
        return None
    try:
        matched = code_graph_dependencies.dependency_seed_nodes(graph, symbol)
        seeds = matched[:FLOW_MAX_SEEDS]
        depth = _flow_depth(max_depth)
        found, narrowed = _service_walk(graph, seeds, depth, deadline, cancelled)
        rows = found[:FLOW_MAX_ROWS]
        report = {
            **_service_report(graph, symbol, seeds, rows, depth),
            **seed_cut_report(matched, FLOW_MAX_SEEDS),
            **_service_cut_report(found, narrowed),
        }
        return reported_answer("hops", rows, report, with_report)
    finally:
        graph.close()
