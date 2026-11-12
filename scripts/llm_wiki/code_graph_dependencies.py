"""Dependency reach and paths between symbols, from the generation or a live parse."""
from __future__ import annotations

import sqlite3
from pathlib import Path

from . import code_graph_generation, code_graph_workspace
from .code_graph_generation import (
    NO_GENERATION,
    stored_or_reason,
)
from .code_graph_reports import (
    dependency_reach,
    live_report,
    named_node_ids,
    reported_answer,
    seed_cut_report,
    store_report,
)

#: What a dependency is, in edges the extractor actually writes. `IMPORTS` is
#: what a module depends on; `CALLS` is what a symbol depends on.
DEPENDENCY_EDGE_TYPES = ("CALLS", "IMPORTS")
DEPENDENCY_SEED_KINDS = ("class", "function", "method", "module")
# A dependency walk's bounds. Seeds past 20 are cut and the answer reports it
# (`seed_cut_report`); the walk stops at depth 8, the architecture tools' depth
# (mcp_server.ARCHITECTURE_MAX_DEPTH); more than 1 000 reached nodes or 100 000 units of
# work is refused by evidence_graph rather than truncated. Basis unknown beyond that:
# values predate measurement; review if real symbols are refused.
DEPENDENCY_SEED_LIMIT = 20
DEPENDENCY_MAX_DEPTH = 8
DEPENDENCY_MAX_ROWS = 1000
DEPENDENCY_MAX_WORK = 100_000


def _dependency_seed_by_id(graph, symbol: str) -> list[str]:
    try:
        node = graph.node(symbol)
    except (ValueError, sqlite3.Error):
        return []
    return [] if node is None else [str(node["node_id"])]


def _dependency_seed_by_path(graph, symbol: str) -> list[str]:
    """A repository-relative file path names the module the file defines."""
    by_path = graph.find_nodes(
        kinds=("module",), path=symbol, max_rows=DEPENDENCY_SEED_LIMIT
    )
    if by_path:
        return [str(item["node_id"]) for item in by_path]
    return _dependency_seed_by_id(graph, symbol)


def dependency_seed_nodes(graph, symbol: str) -> list[str]:
    """The nodes a dependency question is about: a name, a path, or a node id.

    CODE-07, loud face: `get_architecture` forwards `request["symbol"]` — a
    *name* — where a node id was expected, and the node-id syntax starts with
    `[A-Za-z0-9]`, so every private name raised rather than answered. Names are
    resolved the way `find_callers` and `find_callees` already resolve them.
    A path is accepted too, because "what does this file depend on" is the
    question this mode is asked with — `scripts/retrieval.py`, not
    `scripts.retrieval` — and a module node carries both.

    Every match is returned; the caller cuts to its seed limit and reports the
    cut (`seed_cut_report`). Asking the reader for only that many rows made it
    refuse every common name (audit 3, A6).
    """
    by_name = named_node_ids(graph, DEPENDENCY_SEED_KINDS, symbol)
    if by_name:
        return by_name
    return _dependency_seed_by_path(graph, symbol)


def _dependency_sort_key(item: dict) -> tuple:
    return (
        item["depth"],
        str(item["kind"]),
        str(item["identity_key"]),
        str(item["node_id"]),
    )


def stored_dependency_rows(
    graph, seeds: list[str], reverse: bool, max_depth: int | None = None
) -> list[dict]:
    depth = _dependency_depth(max_depth)
    merged: dict[str, dict] = {}
    for seed in seeds:
        _merge_dependency_rows(merged, graph, seed, reverse, depth)
    return sorted(merged.values(), key=_dependency_sort_key)


# One hop, because that is the question and because it is already this
# product's answer to the same question elsewhere: `graph_neighbors.neighbors`
# takes `max_hops: int = 1` over the same range. The convention outside is the
# same shape — codebase-memory's `trace_path` defaults to a bounded 3, and an
# IDE call hierarchy (JetBrains Rider, LSP `callHierarchy/incomingCalls`) shows
# one level and expands a node when asked. Returning the whole reachable set by
# default was the outlier, and it arrived by omission rather than by decision.
#
# Measured on the active generation for `scripts/retrieval.py`: one hop is 7
# modules and ~338 tokens; the whole set is 346 rows and 20 735 tokens after
# shaping, 287 of them individual functions and classes nobody asked about.
#
# Nothing is silently lost: `depth_applied` says how far this answer walked and
# `depth_frontier_open` says whether the walk stopped at that edge with more
# beyond it, so a caller who wants the closure knows to ask for it.
DEPENDENCY_DEFAULT_DEPTH = 1


def _dependency_depth(max_depth: int | None) -> int:
    """How far to walk: what the caller asked, bounded by what the store allows.

    A depth below one would ask for nothing and is read as one hop rather than
    refused, because "no hops" is not a question anyone means to ask.
    """
    if max_depth is None:
        return DEPENDENCY_DEFAULT_DEPTH
    return max(1, min(int(max_depth), DEPENDENCY_MAX_DEPTH))


def _merge_dependency_rows(
    merged: dict, graph, seed: str, reverse: bool, max_depth: int
) -> None:
    rows = graph.reachable(
        seed,
        edge_types=DEPENDENCY_EDGE_TYPES,
        reverse=reverse,
        max_depth=max_depth,
        max_rows=DEPENDENCY_MAX_ROWS,
        max_work=DEPENDENCY_MAX_WORK,
    )
    for item in rows:
        merged.setdefault(str(item["node_id"]), item)


def _dependency_resolution(symbol: str, seeds: list[str]) -> dict[str, object]:
    """An empty answer must say whether the symbol was found at all.

    CODE-07, silent face: a name that passed the node-id syntax matched no
    node and produced `[]` with no error, byte-identical to a real answer of
    "this depends on nothing". These fields are what tells the two apart.
    """
    return {
        "symbol": symbol,
        "symbol_resolved": bool(seeds),
        "resolved_symbol_nodes": len(seeds),
        "dependency_edge_types": list(DEPENDENCY_EDGE_TYPES),
        "dependency_max_depth": DEPENDENCY_MAX_DEPTH,
    }


def _store_find_dependencies(
    symbol: str,
    directory: Path,
    *,
    reverse: bool,
    with_report: bool,
    max_depth: int | None = None,
) -> list[dict] | dict | None:
    graph = code_graph_generation.active_evidence_graph(directory)
    if graph is None:
        return None
    try:
        matched = dependency_seed_nodes(graph, symbol)
        seeds = matched[:DEPENDENCY_SEED_LIMIT]
        depth = _dependency_depth(max_depth)
        rows = stored_dependency_rows(graph, seeds, reverse, depth)
        report = {
            **store_report(graph),
            **_dependency_resolution(symbol, seeds),
            **dependency_reach(rows, depth),
            **seed_cut_report(matched, DEPENDENCY_SEED_LIMIT),
        }
        return reported_answer("dependencies", rows, report, with_report)
    finally:
        graph.close()


def find_dependencies(
    node_id: str,
    directory: Path,
    *,
    reverse: bool = False,
    live: bool = False,
    with_report: bool = False,
    max_depth: int | None = None,
) -> list[dict] | dict:
    """Find bounded canonical dependencies, preferring the active generation.

    The stored answer walks `CALLS` and `IMPORTS` assertions. It used to walk
    the `dependency` table, which holds 0 rows in the active generation and in
    every generation on this disk, is written by no producer in `scripts/`,
    and therefore answered `[]` for every symbol ever asked — silently, with
    the same `graph_complete: false` caveat a correct answer carries.
    """
    stored, reason = None, NO_GENERATION
    if not live:
        stored, reason = stored_or_reason(
            lambda: _store_find_dependencies(
                node_id,
                directory,
                reverse=reverse,
                with_report=with_report,
                max_depth=max_depth,
            )
        )
    if stored is not None:
        return stored
    parsed, definitions, edges = code_graph_workspace.workspace_call_graph(directory)
    dependencies = _find_live_dependencies(
        node_id, definitions, edges, reverse=reverse
    )
    return reported_answer(
        "dependencies",
        dependencies,
        live_report(directory, parsed, reason),
        with_report,
    )


# Display cuts of one find_paths answer, stated in it: each end resolves to at most
# five nodes (`<end>_nodes_truncated`), and at most ten paths are returned
# (`paths_truncated`). docs/research/2026-09-27-a-cut-says-what-it-left-out.md
PATH_MAX_ENDPOINTS = 5
PATH_MAX_ROWS = 10


def paths_between(graph, sources: list[str], targets: list[str]) -> tuple[list[dict], bool]:
    """Paths for every resolved pair of ends until the answer is full, and whether it was cut."""
    paths: list[dict] = []
    pairs = [(source, target) for source in sources for target in targets]
    for source, target in pairs:
        if len(paths) >= PATH_MAX_ROWS:
            return paths[:PATH_MAX_ROWS], True
        paths.extend(
            graph.path(
                source, target, max_depth=8, max_rows=PATH_MAX_ROWS, max_work=10_000
            )
        )
    return paths[:PATH_MAX_ROWS], len(paths) > PATH_MAX_ROWS


def _path_end_report(label: str, matched: list[str]) -> dict[str, object]:
    report: dict[str, object] = {f"{label}_resolved_nodes": len(matched)}
    if len(matched) > PATH_MAX_ENDPOINTS:
        report[f"{label}_nodes_truncated"] = True
    return report


def _store_find_paths(
    source: str, target: str, directory: Path, with_report: bool
) -> list[dict] | dict | None:
    graph = code_graph_generation.active_evidence_graph(directory)
    if graph is None:
        return None
    try:
        sources = dependency_seed_nodes(graph, source)
        targets = dependency_seed_nodes(graph, target)
        paths, cut = paths_between(
            graph, sources[:PATH_MAX_ENDPOINTS], targets[:PATH_MAX_ENDPOINTS]
        )
        report = {
            **store_report(graph),
            **_path_end_report("source", sources),
            **_path_end_report("target", targets),
            "paths_truncated": cut,
        }
        return reported_answer("paths", paths, report, with_report)
    finally:
        graph.close()


def find_paths(
    source_node_id: str,
    target_node_id: str,
    directory: Path,
    *,
    live: bool = False,
    with_report: bool = False,
) -> list[dict] | dict:
    """Find bounded canonical graph paths, preferring the active generation.

    Both ends may be a name, a repository-relative path or a node id, resolved
    the way `dependencies` resolves its symbol (audit 3, A5).
    """
    stored, reason = None, NO_GENERATION
    if not live:
        stored, reason = stored_or_reason(
            lambda: _store_find_paths(
                source_node_id, target_node_id, directory, with_report
            )
        )
    if stored is not None:
        return stored
    parsed, definitions, edges = code_graph_workspace.workspace_call_graph(directory)
    paths = _find_live_paths(source_node_id, target_node_id, definitions, edges)
    return reported_answer(
        "paths", paths, live_report(directory, parsed, reason), with_report
    )


def _live_node_ids(value: str, definitions: dict[str, dict]) -> list[str]:
    if value in definitions:
        return [value]
    by_name: dict[str, list[str]] = {}
    for identifier, definition in definitions.items():
        by_name.setdefault(str(definition["name"]), []).append(identifier)
    return sorted(by_name.get(value, []))


def _dependency_direction(reverse: bool) -> tuple[str, str]:
    if reverse:
        return ("target", "source")
    return ("source", "target")


def _dependency_adjacency(edges, start_key: str, target_key: str) -> dict:
    adjacency: dict[str, list[str]] = {}
    for edge in edges:
        adjacency.setdefault(edge[start_key], []).append(edge[target_key])
    return adjacency


def _dependency_record(target: str, definition: dict, depth: int) -> dict:
    return {
        "node_id": target,
        "kind": "function",
        "identity_scheme": "live-code/v1",
        "identity_key": target,
        "metadata": {
            "name": definition["name"],
            "owner": definition.get("owner", ""),
            "path": definition.get("file", ""),
        },
        "depth": depth,
    }


def _visit_dependency(target, depth, definitions, seen, results, pending) -> None:
    if target in seen:
        return
    seen.add(target)
    definition = definitions.get(target)
    if definition is not None:
        results.append(_dependency_record(target, definition, depth))
    pending.append((target, depth))


def _expand_dependency(
    current, depth, adjacency, definitions, seen, results, pending
) -> None:
    if depth >= 8:
        return
    for target in sorted(adjacency.get(current, [])):
        _visit_dependency(target, depth + 1, definitions, seen, results, pending)


def _dependency_budget_left(results: list, work: int) -> bool:
    return len(results) < 10_000 and work < 10_000


def _find_live_dependencies(
    node_id: str,
    definitions: dict[str, dict],
    edges: list[dict],
    *,
    reverse: bool,
) -> list[dict]:
    start_key, target_key = _dependency_direction(reverse)
    adjacency = _dependency_adjacency(edges, start_key, target_key)
    pending = [(identifier, 0) for identifier in _live_node_ids(node_id, definitions)]
    seen = {identifier for identifier, _depth in pending}
    results: list[dict] = []
    work = 0
    while pending and _dependency_budget_left(results, work):
        current, depth = pending.pop(0)
        work += 1
        _expand_dependency(
            current, depth, adjacency, definitions, seen, results, pending
        )
    return results


def _path_outgoing(edges) -> dict:
    outgoing: dict[str, list[str]] = {}
    for edge in edges:
        outgoing.setdefault(edge["source"], []).append(edge["target"])
    return outgoing


def _reached_target(node, path, targets) -> bool:
    return node in targets and len(path) > 1


def _queue_path(target, path, pending) -> None:
    if target in path:
        return
    pending.append((target, [*path, target]))


def _extend_paths(node, path, outgoing, pending) -> None:
    if len(path) > 8:
        return
    for target in sorted(outgoing.get(node, [])):
        _queue_path(target, path, pending)


def _path_budget_left(paths: list, work: int) -> bool:
    return len(paths) < 10 and work < 10_000


def _find_live_paths(
    source_node_id: str,
    target_node_id: str,
    definitions: dict[str, dict],
    edges: list[dict],
) -> list[dict]:
    sources = _live_node_ids(source_node_id, definitions)
    targets = set(_live_node_ids(target_node_id, definitions))
    outgoing = _path_outgoing(edges)
    pending = [(source, [source]) for source in sorted(sources)]
    paths: list[dict] = []
    work = 0
    while pending and _path_budget_left(paths, work):
        node, path = pending.pop(0)
        work += 1
        if _reached_target(node, path, targets):
            paths.append(
                {"node_ids": path, "assertion_ids": [], "depth": len(path) - 1}
            )
            continue
        _extend_paths(node, path, outgoing, pending)
    return paths
