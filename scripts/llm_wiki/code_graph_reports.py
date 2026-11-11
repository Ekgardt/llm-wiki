"""The store report and seed-cut fields every structural answer carries."""
from __future__ import annotations

from pathlib import Path

from . import code_graph_workspace
from .code_graph_generation import NO_GENERATION


def _observation_count(graph) -> int:
    """Counted once per generation: the table cannot change after registration."""

    def count() -> int:
        return graph._database.execute("SELECT count(*) FROM observation").fetchone()[0]

    memoized = getattr(graph, "memoized", None)
    if memoized is None:
        return count()
    return memoized("observation_count", count)


def store_report(graph) -> dict[str, object]:
    unresolved_count = _observation_count(graph)
    return {
        "source_generation": graph.generation_id,
        "source_scope": "checkout",
        "source_root": graph.repository_scope.checkout_root,
        "graph_complete": unresolved_count == 0,
        "unresolved_count": unresolved_count,
        "fallback": False,
    }


def _live_unresolved_count(
    directory: Path, parsed: list[tuple[Path, dict]] | None = None
) -> int:
    if parsed is None:
        parsed, _definitions, _edges = code_graph_workspace.workspace_call_graph(directory)
    return sum(
        1
        for _path, result in parsed
        for call in result["calls"]
        if call.get("confidence") not in {"confirmed", "heuristic"}
    )


def live_report(
    directory: Path,
    parsed: list[tuple[Path, dict]] | None = None,
    reason: str = NO_GENERATION,
) -> dict[str, object]:
    return {
        "source_generation": None,
        "graph_complete": False,
        "unresolved_count": _live_unresolved_count(directory, parsed),
        "fallback": True,
        "fallback_reason": reason,
    }


def reported_answer(key: str, value, report: dict[str, object], enabled: bool):
    return {key: value, **report} if enabled else value


# Audit 3, A5-A7: a name is resolved, cut and sliced on this side of the reader,
# whose own bounds refuse by name instead of truncating. Research:
# `docs/research/2026-09-17-a-name-is-resolved-before-the-graph-is-asked.md`.
SEED_LOOKUP_ROWS = 10_000  # evidence_graph.MAX_ROWS


def named_node_ids(graph, kinds: tuple[str, ...], name: str) -> list[str]:
    """Every node of these kinds with this name, in the reader's own order.

    Asked up to the reader's row ceiling: a seed limit passed as `max_rows` is a
    refusal for every common name (`main`, `__init__`), not a cut.
    """
    nodes = graph.find_nodes(kinds=kinds, name=name, max_rows=SEED_LOOKUP_ROWS)
    return [str(item["node_id"]) for item in nodes]


def seed_cut_report(matched: list[str], limit: int) -> dict[str, object]:
    """Nothing for a whole answer; the size of the cut when there was one."""
    if len(matched) <= limit:
        return {}
    return {"matching_symbol_nodes": len(matched), "symbol_nodes_truncated": True}


def dependency_reach(rows: list[dict], depth: int) -> dict[str, object]:
    """How far this answer walked, and whether anything lies past that edge.

    A row sitting exactly at the depth the walk stopped at may have neighbours
    the walk never looked at. That is not a claim there are more — it is the
    difference between "this is all of it" and "this is as far as you asked".
    """
    return {
        "depth_applied": depth,
        "depth_frontier_open": any(row.get("depth") == depth for row in rows),
    }
