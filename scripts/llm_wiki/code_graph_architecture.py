"""The architecture summary: entry points, hotspots, modules and listing bounds."""
from __future__ import annotations

import re
from pathlib import Path

from . import code_graph_generation, code_graph_workspace
from .code_graph_communities import (
    COMMUNITY_LIMIT,
    _live_community_answer,
    _stored_community_answer,
)
from .code_graph_generation import (
    NO_GENERATION,
    stored_architecture_node,
    stored_location,
    stored_or_reason,
)
from .code_graph_reports import (
    live_report,
    store_report,
)

# The hotspot list is a ranking, not a dump. This repository has 10,607 nodes
# with at least one incoming call; a caller reads the head of that ranking. The
# bound is stated in the answer as `hotspot_limit` / `hotspots_truncated`, so a
# reader is never left to mistake the top of the list for the whole of it.
HOTSPOT_LIMIT = 100


def _main_entry_points(path: Path, result: dict) -> list[dict]:
    return [
        {"kind": "main", "name": "main", "file": str(path), "line": function["line"]}
        for function in result["functions"]
        if function["name"] == "main"
    ]


def _live_architecture_points(parsed) -> tuple[list, list]:
    entry_points: list[dict] = []
    routes: list[dict] = []
    for path, result in parsed:
        source = path.read_text(encoding="utf-8", errors="ignore")
        entry_points.extend(_main_entry_points(path, result))
        entry_points.extend(_listen_entry_points(path, source))
        routes.extend(_framework_routes(path, source))
    return entry_points, routes


def _live_hotspots(incoming: dict, definitions: dict) -> list[dict]:
    hotspots = [
        {
            "name": definitions[symbol_id]["name"],
            "symbol_id": symbol_id,
            "owner": definitions[symbol_id]["owner"],
            "file": definitions[symbol_id]["file"],
            "line": definitions[symbol_id]["line"],
            "incoming_callers": len(callers),
        }
        for symbol_id, callers in incoming.items()
        if symbol_id in definitions
    ]
    hotspots.sort(
        key=lambda item: (-item["incoming_callers"], item["name"], item["file"])
    )
    return hotspots


def _live_incoming(edges: list[dict]) -> dict:
    incoming: dict[str, set[str]] = {}
    for edge in edges:
        incoming.setdefault(edge["target"], set()).add(edge["source"])
    return incoming


def get_architecture(
    directory: Path,
    *,
    live: bool = False,
    with_report: bool = False,
    limit: int | None = None,
) -> dict:
    """Summarize statically visible entry points, routes, hotspots, and modules."""
    bound = summary_listing_limit(limit)
    stored, reason = None, NO_GENERATION
    if not live:
        stored, reason = stored_or_reason(
            lambda: store_get_architecture(directory, bound)
        )
    if stored is not None:
        return stored
    parsed, definitions, edges = code_graph_workspace.workspace_call_graph(directory)
    entry_points, routes = _live_architecture_points(parsed)
    communities, counts = _live_community_answer(definitions, edges)
    architecture = {
        **listing_fields(entry_points, "entry_points", bound),
        **listing_fields(routes, "routes", bound),
        **hotspot_fields(*_bounded_hotspots(
            _live_hotspots(_live_incoming(edges), definitions)
        ), bound),
        "communities": communities,
        **counts,
        "graph_complete": False,
    }
    return {**architecture, **live_report(directory, parsed, reason)}


# Entry points and routes are listed the way hotspots already are: with the
# bound stated in the answer rather than implied. Both were fetched at
# `max_rows=10_000` and returned whole, with no count and no truncation flag,
# so a reader could not tell a complete listing from a clipped one.
#
# 30 is `COMMUNITY_LIMIT`, this summary's existing answer to "how much of a
# list belongs in a summary", rather than a fourth number invented here.
#
# Measured on this repository 2026-08-29: 97 entry points, every one of them a
# script's `main`, cost 7 367 of the summary's 24 430 characters — 30% — while
# the summary stated no count at all. The count says 97 in two characters.
SUMMARY_LISTING_LIMIT = COMMUNITY_LIMIT


def summary_listing_limit(limit: int | None) -> int:
    """How many rows of each summary listing to return.

    `None` is the summary's own number. A caller who wants the whole ranking
    asks for it, up to the ceiling the store query already fetches — the same
    shape the dependency walk's `depth` takes, and the answer to the objection
    that a ranking cannot be cut because nothing restores it. Now something
    does.
    """
    if limit is None:
        return SUMMARY_LISTING_LIMIT
    return max(1, min(int(limit), HOTSPOT_LIMIT))


def listing_fields(rows: list[dict], key: str, limit: int) -> dict:
    """One bounded listing, with what was cut said out loud."""
    return {
        key: rows[:limit],
        f"{key}_count": len(rows),
        f"{key}_limit": limit,
        f"{key}_truncated": len(rows) > limit,
    }


def _bounded_hotspots(hotspots: list[dict]) -> tuple[list[dict], bool]:
    return hotspots[:HOTSPOT_LIMIT], len(hotspots) > HOTSPOT_LIMIT


def hotspot_fields(hotspots: list[dict], truncated: bool, limit: int) -> dict:
    """The hotspot ranking with its bound stated in the answer, never implied.

    The query fetches `HOTSPOT_LIMIT` and the caller asks for `limit`, so the
    bound that actually applied is the smaller of the two. Stating the caller's
    number alone would have claimed a bound of 30 while returning two rows.
    `truncated` stays true when either bound bit.

    No count here, deliberately, unlike the listings beside it. The ranking has
    10 607 members on this repository and the query never sees past the first
    hundred, so any number stated would be the size of the fetch rather than of
    the ranking. `hotspots_truncated` says there is more without claiming to
    know how much.
    """
    applied = min(limit, HOTSPOT_LIMIT)
    return {
        "hotspots": hotspots[:applied],
        "hotspot_limit": applied,
        "hotspots_truncated": truncated or len(hotspots) > applied,
    }


# The summary's answer to the first half of "what are its main modules and
# entry points", which it did not answer at all. Measured 2026-08-29: the only
# reason the summary named `scripts/mcp_server.py` — the documented agent
# surface — was that one of its functions ranked between 31st and 100th in the
# call hotspots. No entry point names it, because it has no `main`. Bounding
# the ranking to thirty therefore lost it, and the loss was luck either way.
#
# Ranked by how many distinct modules import each one, which is the same
# GROUP BY the hotspot ranking already uses, over IMPORTS instead of CALLS.
def stored_modules(graph, limit: int) -> tuple[list[dict], bool]:
    counts, truncated = graph.top_incoming_edge_counts(
        edge_types=("IMPORTS",), kinds=("module",), max_rows=limit
    )
    return [_stored_module_row(graph, row) for row in counts if row], truncated


def _stored_module_row(graph, row: dict) -> dict | None:
    node = graph.node(str(row["node_id"]))
    if node is None:
        return None
    metadata = node["metadata"]
    return {
        "name": metadata.get("name", ""),
        "path": metadata.get("path", ""),
        "importing_modules": int(row["incoming"]),
    }


def module_fields(modules: list[dict], truncated: bool, limit: int) -> dict:
    """The module ranking, bounded and saying so, like every listing beside it."""
    return {
        "modules": [row for row in modules if row is not None],
        "modules_limit": limit,
        "modules_truncated": truncated,
    }


def _stored_hotspot(graph, node_id: str, node, incoming: int, directory: Path) -> dict:
    location = stored_location(graph, node_id, directory)
    return {
        "name": node["metadata"].get("name", node["identity_key"]),
        "symbol_id": node_id,
        "owner": node["metadata"].get("owner", ""),
        "file": location[0],
        "line": location[1],
        "incoming_callers": incoming,
    }


def _stored_hotspot_row(graph, row: dict, directory: Path) -> dict | None:
    node_id = str(row["node_id"])
    node = graph.node(node_id)
    if node is None:
        return None
    return _stored_hotspot(graph, node_id, node, int(row["incoming"]), directory)


def _stored_hotspots(graph, directory: Path) -> tuple[list[dict], bool]:
    """The top of the caller ranking, counted by a GROUP BY rather than in Python.

    Measured on this repository: the ranking has 10,607 members and the top 100
    of it costs 0.10 s, against a refusal for the old shape, which pulled every
    function node and every call edge to count in Python.
    """
    counts, truncated = graph.top_incoming_edge_counts(
        edge_types=("CALLS",), kinds=("function", "method"), max_rows=HOTSPOT_LIMIT
    )
    found = [_stored_hotspot_row(graph, row, directory) for row in counts]
    hotspots = [item for item in found if item is not None]
    hotspots.sort(
        key=lambda item: (-item["incoming_callers"], str(item["name"]), item["file"])
    )
    return hotspots, truncated


def _stored_architecture_nodes(graph, nodes, directory: Path) -> list[dict]:
    return [stored_architecture_node(graph, node, directory) for node in nodes]


def store_get_architecture(directory: Path, limit: int) -> dict | None:
    graph = code_graph_generation.active_evidence_graph(directory)
    if graph is None:
        return None
    try:
        entries = graph.find_nodes(kinds=("entry-point",), max_rows=10_000)
        routes = graph.find_nodes(kinds=("route",), max_rows=10_000)
        report = store_report(graph)
        communities, counts = _stored_community_answer(graph)
        return {
            **listing_fields(
                _stored_architecture_nodes(graph, entries, directory),
                "entry_points",
                limit,
            ),
            **listing_fields(
                _stored_architecture_nodes(graph, routes, directory), "routes", limit
            ),
            **module_fields(*stored_modules(graph, limit), limit),
            **hotspot_fields(*_stored_hotspots(graph, directory), limit),
            "communities": communities,
            **counts,
            **report,
        }
    finally:
        graph.close()


def _listen_entry_points(path: Path, source: str) -> list[dict]:
    return [
        {
            "kind": "listen", "name": match.group(1), "file": str(path),
            "line": source.count("\n", 0, match.start()) + 1,
        }
        for match in re.finditer(r"\b((?:app|server)\.listen)\s*\(", source)
    ]


def _framework_routes(path: Path, source: str) -> list[dict]:
    patterns = [
        (r"@\w+\.route\s*\(\s*['\"]([^'\"]+)", "ROUTE"),
        (r"\b(?:app|router)\.(get|post|put|patch|delete|all)\s*\(\s*['\"]([^'\"]+)", None),
        (r"@(Get|Post|Put|Patch|Delete|Request)Mapping\s*\(\s*['\"]([^'\"]+)", None),
    ]
    routes = []
    for pattern, fixed_method in patterns:
        for match in re.finditer(pattern, source, re.IGNORECASE):
            method = fixed_method or match.group(1).upper().removesuffix("MAPPING")
            route_path = match.group(1 if fixed_method else 2)
            routes.append({
                "method": method, "path": route_path, "file": str(path),
                "line": source.count("\n", 0, match.start()) + 1,
            })
    return routes
