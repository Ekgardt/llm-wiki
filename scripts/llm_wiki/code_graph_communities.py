"""Community detection over the call graph (Louvain) and the community answers."""
from __future__ import annotations

from pathlib import Path

from . import code_graph_generation, code_graph_workspace
from .code_graph_generation import (
    NO_GENERATION,
    stored_or_reason,
    stored_qualified_name,
)
from .code_graph_reports import (
    live_report,
    named_node_ids,
    reported_answer,
    store_report,
)

# Community partitions cached per graph reader; one key is used today, so 4 only
# keeps an old entry from lingering. A miss recomputes, it never answers less.
MAX_DERIVED_COMMUNITY_CACHE = 4
# Bound for the folded call-pair aggregate that feeds community detection.
# 200,000 against this repository's measured 29,868 pairs — 6.7x headroom.
MAX_CALL_PAIR_ROWS = 200_000


def _add_undirected_edge(graph: dict, source: str, target: str, weight: float) -> None:
    if source == target:
        return
    graph.setdefault(source, {})[target] = graph.setdefault(source, {}).get(target, 0) + weight
    graph.setdefault(target, {})[source] = graph.setdefault(target, {}).get(source, 0) + weight


def undirected_call_graph(edges: list[dict[str, object]]) -> dict:
    """Fold resolved call edges into one weighted undirected graph.

    Accepts either a row of record from `EvidenceGraph.edges()`, which carries
    no weight and counts as one, or a pair already folded in SQL by
    `EvidenceGraph.edge_weights()`, which carries how many assertions joined it.
    """
    graph: dict[str, dict[str, float]] = {}
    for edge in edges:
        _add_undirected_edge(
            graph,
            str(edge["source_node_id"]),
            str(edge["target_node_id"]),
            float(edge.get("weight", 1)),
        )
    return graph


def _stored_call_pairs(graph_reader) -> list[dict[str, object]]:
    """The whole call graph as distinct weighted undirected pairs, folded in SQL.

    Measured on this repository: 35,313 resolved CALLS assertions fold into
    29,868 pairs, 0.30 s, 4.07 MB as a Python adjacency, 1.93 s of Louvain.
    The bound is `MAX_CALL_PAIR_ROWS`; above it the reader refuses by name.
    """
    return graph_reader.edge_weights(
        edge_types=("CALLS",), max_rows=MAX_CALL_PAIR_ROWS
    )


def _derived_cache(graph_reader) -> dict:
    cache = getattr(graph_reader, "_derived_code_graph_cache", None)
    if cache is None:
        cache = {}
        graph_reader._derived_code_graph_cache = cache
    return cache


def stored_communities(graph_reader, edges: list[dict[str, object]]) -> list[list[str]]:
    cache = _derived_cache(graph_reader)
    cache_key = "communities/calls/v1"
    if cache_key in cache:
        return cache[cache_key]
    communities = louvain_communities(undirected_call_graph(edges))
    if len(cache) >= MAX_DERIVED_COMMUNITY_CACHE:
        cache.pop(next(iter(cache)))
    cache[cache_key] = communities
    return communities


# NEW-125. Communities used to answer as bare `code:node:<md5>` lists, naming no
# symbol, no file and no line, so `answer_budget` classified the whole field as
# an opaque collection and dropped it - `mode=community` returned nothing at
# all. Naming the members costs bytes, so the listing is bounded here rather
# than left to be cut somewhere downstream.
#
# The bounds are measured on this repository, not chosen: the call graph folds
# into 4 078 communities over 17 194 members, and naming every one of them costs
# 899 071 estimated tokens - 36x the 25 000-token client ceiling `answer_budget`
# documents. 30 communities at 10 members each cost 11 836, which leaves room
# for the entry points, routes and hotspots that share the `summary` answer
# (~9 000 tokens measured). The counts below state everything the bound left
# out, so the answer is short, never quietly partial.
COMMUNITY_LIMIT = 30

# Three, not ten. A community is identified by its size and a few of its names;
# the rest of the roster answers a question nobody asked in a summary. Measured
# 2026-09-06 on this repository: the summary cost 17 149 tokens, of which
# **13 331 were community members** — 30 communities of 10 rows, each row a
# qualified name, an absolute path and a line. At three the same answer costs
# about a third of that, and `members_truncated` and `members_omitted` still say
# exactly what was left out, so the answer is shorter and never quietly partial.
#
# The metric this serves is the owner's, stated 2026-09-06: not tokens saved but
# right answers bought per token spent. At identical correctness we were buying
# 1.20 per thousand against codebase-memory-mcp's 5.20.
COMMUNITY_MEMBER_LIMIT = 3


def _community_order_key(community: list[str]) -> tuple[int, str]:
    """Largest first - the bound has to keep the communities that carry most."""
    return (-len(community), str(community[0]))


def _named_community(community: list[str], member_row) -> dict[str, object]:
    members = [member_row(node_id) for node_id in community[:COMMUNITY_MEMBER_LIMIT]]
    return {
        "size": len(community),
        "members": members,
        "members_truncated": len(community) > len(members),
        "members_omitted": len(community) - len(members),
    }


def _communities_holding(communities: list[list[str]], node_ids) -> list[list[str]]:
    """Only the communities a named symbol belongs to, or all of them.

    A whole-graph listing cannot answer "which community does X belong to" at
    this repository's scale: naming all 17 194 members costs 899 071 estimated
    tokens, 36x the client ceiling, and `fuse_rrf`'s community is 729th by size,
    so no honest bound reaches it. Anchoring on the symbol turns a listing that
    must be cut into an answer that fits.
    """
    if node_ids is None:
        return communities
    return [item for item in communities if node_ids.intersection(item)]


def _community_answer(
    communities: list[list[str]], member_row, node_ids=None
) -> tuple[list, dict]:
    """Named communities, plus the counts that state what the bound left out."""
    selected = _communities_holding(communities, node_ids)
    shown = sorted(selected, key=_community_order_key)[:COMMUNITY_LIMIT]
    counts = {
        "community_count": len(selected),
        "community_member_count": sum(len(item) for item in selected),
        "community_limit": COMMUNITY_LIMIT,
        "community_member_limit": COMMUNITY_MEMBER_LIMIT,
        "communities_truncated": len(selected) > len(shown),
    }
    return [_named_community(item, member_row) for item in shown], counts


def _stored_community_member(graph, node_id: str, locations: dict) -> dict:
    node = graph.node(node_id)
    location = locations.get(node_id, {})
    source_root = Path(graph.repository_scope.checkout_root)
    relative = str(location.get("relative_path", ""))
    return {
        "qualified_name": "" if node is None else stored_qualified_name(node),
        "file": str(source_root / relative) if relative else "",
        "line": location.get("line", 0),
    }


def _shown_community_members(communities: list[list[str]]) -> list[str]:
    """Exactly the members the bound will name, so nothing else is looked up."""
    shown = sorted(communities, key=_community_order_key)[:COMMUNITY_LIMIT]
    return [node_id for item in shown for node_id in item[:COMMUNITY_MEMBER_LIMIT]]


def _stored_symbol_node_ids(graph, symbol: str | None):
    """Every function or method of this name, asked up to the reader's ceiling.

    A bound of 512 refused `symbol=__init__` on a repository with more
    definitions of it (audit C-36,
    docs/research/2026-09-25-a-common-name-is-asked-up-to-the-reader-ceiling.md).
    """
    if symbol is None:
        return None
    return set(named_node_ids(graph, ("function", "method"), symbol))


def _stored_community_answer(graph, symbol: str | None = None) -> tuple[list, dict]:
    communities = stored_communities(graph, _stored_call_pairs(graph))
    node_ids = _stored_symbol_node_ids(graph, symbol)
    selected = _communities_holding(communities, node_ids)
    locations = graph.node_locations(_shown_community_members(selected))
    return _community_answer(
        communities,
        lambda node_id: _stored_community_member(graph, node_id, locations),
        node_ids,
    )


def _live_qualified_name(definition: dict) -> str:
    owner = str(definition.get("owner", ""))
    name = str(definition.get("name", ""))
    return name if owner in {"", "<module>"} else f"{owner}.{name}"


def _live_community_member(definitions: dict, symbol_id: str) -> dict:
    definition = definitions.get(symbol_id, {})
    return {
        "qualified_name": _live_qualified_name(definition),
        "file": definition.get("file", ""),
        "line": definition.get("line", 0),
    }


def _live_symbol_ids(definitions: dict, symbol: str | None):
    if symbol is None:
        return None
    return {
        symbol_id
        for symbol_id, definition in definitions.items()
        if definition.get("name") == symbol
    }


def _live_community_answer(
    definitions: dict, edges: list[dict], symbol: str | None = None
) -> tuple[list, dict]:
    return _community_answer(
        communities_from_edges(edges),
        lambda symbol_id: _live_community_member(definitions, symbol_id),
        _live_symbol_ids(definitions, symbol),
    )


def detect_communities(
    directory: Path,
    *,
    symbol: str | None = None,
    live: bool = False,
    with_report: bool = False,
) -> list[list[str]] | dict:
    """Detect functional modules with deterministic weighted Louvain.

    `symbol` narrows the answer to the communities that symbol belongs to. It
    exists because the whole-graph listing cannot be both complete and bounded
    here — see `_communities_holding` for the measurement — so "which module
    does X belong to" needs an anchor, exactly as who-calls does.
    """
    stored, reason = None, NO_GENERATION
    if not live:
        stored, reason = stored_or_reason(
            lambda: _stored_detect_communities(directory, symbol, with_report)
        )
    if stored is not None:
        return stored
    parsed, definitions, edges = code_graph_workspace.workspace_call_graph(directory)
    communities, counts = _live_community_answer(definitions, edges, symbol)
    report = {**live_report(directory, parsed, reason), **counts}
    return reported_answer("communities", communities, report, with_report)


def communities_from_edges(edges: list[dict]) -> list[list[str]]:
    graph: dict[str, dict[str, float]] = {}
    for edge in edges:
        caller, callee = edge["source"], edge["target"]
        if caller == callee:
            continue
        graph.setdefault(caller, {})[callee] = graph.setdefault(caller, {}).get(callee, 0) + 1
        graph.setdefault(callee, {})[caller] = graph.setdefault(callee, {}).get(caller, 0) + 1
    return louvain_communities(graph)


def _stored_detect_communities(directory: Path, symbol: str | None, with_report: bool):
    """Preserve the historical one-argument call form, which tests substitute."""
    if symbol is None and not with_report:
        return store_detect_communities(directory)
    return store_detect_communities(directory, symbol=symbol, with_report=with_report)


def store_detect_communities(
    directory: Path, *, symbol: str | None = None, with_report: bool = False
) -> list[list[str]] | dict | None:
    graph = code_graph_generation.active_evidence_graph(directory)
    if graph is None:
        return None
    try:
        communities, counts = _stored_community_answer(graph, symbol)
        report = {**store_report(graph), **counts}
        return reported_answer("communities", communities, report, with_report)
    finally:
        graph.close()


def _best_louvain_target(
    node: object,
    weights: dict[object, float],
    totals: dict[object, float],
    degree: dict[object, float],
    old: object,
    m2: float,
) -> object:
    best = old
    best_gain = 0.0
    for target in sorted(weights, key=str):
        gain = weights[target] - totals[target] * degree[node] / m2
        if gain > best_gain + 1e-12:
            best, best_gain = target, gain
    return best


def _neighbor_community_weights(
    neighbors: dict[object, float], community: dict[object, object]
) -> dict[object, float]:
    weights: dict[object, float] = {}
    for neighbor, weight in neighbors.items():
        target = community[neighbor]
        weights[target] = weights.get(target, 0.0) + weight
    return weights


def _move_one_node(
    node: object,
    current: dict,
    community: dict[object, object],
    totals: dict[object, float],
    degree: dict[object, float],
    m2: float,
) -> bool:
    """Move one node to its best community; True when it actually moved."""
    old = community[node]
    totals[old] -= degree[node]
    weights = _neighbor_community_weights(current[node], community)
    best = _best_louvain_target(node, weights, totals, degree, old, m2)
    community[node] = best
    totals[best] += degree[node]
    return best != old


def _local_louvain_pass(
    nodes: list,
    current: dict,
    community: dict[object, object],
    totals: dict[object, float],
    degree: dict[object, float],
    m2: float,
) -> None:
    moved = True
    while moved:
        moved = False
        for node in nodes:
            moved |= _move_one_node(node, current, community, totals, degree, m2)


def _grouped_communities(nodes: list, community: dict[object, object]) -> list[list]:
    groups: dict[object, list[object]] = {}
    for node in nodes:
        groups.setdefault(community[node], []).append(node)
    return sorted(
        (sorted(group, key=str) for group in groups.values()),
        key=lambda group: str(group[0]),
    )


def _aggregated_round(current: dict, members: dict, ordered_groups: list):
    group_of = {
        node: index for index, group in enumerate(ordered_groups) for node in group
    }
    grouped_members = {
        index: set().union(*(members[node] for node in group))
        for index, group in enumerate(ordered_groups)
    }
    return aggregate_louvain_graph(current, group_of), grouped_members


def _louvain_round(current: dict, members: dict):
    """One aggregation round, or None when the partition has settled."""
    nodes = sorted(current, key=str)
    degree = {node: sum(current[node].values()) for node in nodes}
    m2 = sum(degree.values())
    if not m2:
        return None
    community = {node: node for node in nodes}
    _local_louvain_pass(nodes, current, community, dict(degree), degree, m2)
    ordered_groups = _grouped_communities(nodes, community)
    if len(ordered_groups) == len(nodes):
        return None
    return _aggregated_round(current, members, ordered_groups)


def _louvain_seed(graph: dict) -> tuple[dict, dict]:
    current = {node: dict(neighbors) for node, neighbors in graph.items()}
    return current, {node: {node} for node in current}


def _louvain_result(members: dict) -> list[list[str]]:
    communities = [sorted(group) for group in members.values() if len(group) >= 2]
    return sorted(communities, key=lambda group: group[0])


def louvain_communities(graph: dict[str, dict[str, float]]) -> list[list[str]]:
    """Optimize modularity on a weighted undirected graph without dependencies."""
    current, members = _louvain_seed(graph)
    while current:
        advanced = _louvain_round(current, members)
        if advanced is None:
            break
        current, members = advanced
    return _louvain_result(members)


def _add_self_weight(reduced: dict, group: int, weight: float) -> None:
    reduced[group][group] = reduced[group].get(group, 0.0) + weight


def _add_group_weight(reduced: dict, left: int, right: int, weight: float) -> None:
    if left == right:
        _add_self_weight(reduced, left, 2 * weight)
        return
    reduced[left][right] = reduced[left].get(right, 0.0) + weight
    reduced[right][left] = reduced[right].get(left, 0.0) + weight


def _fold_louvain_pair(
    reduced: dict, group_of: dict, source, target, weight: float
) -> None:
    if source == target:
        _add_self_weight(reduced, group_of[source], weight)
        return
    if str(source) > str(target):
        return
    _add_group_weight(reduced, group_of[source], group_of[target], weight)


def aggregate_louvain_graph(
    graph: dict[object, dict[object, float]], group_of: dict[object, int]
) -> dict[int, dict[int, float]]:
    """Aggregate an undirected weighted graph using adjacency degree weights."""
    reduced: dict[int, dict[int, float]] = {
        group: {} for group in sorted(set(group_of.values()))
    }
    for source in sorted(graph, key=str):
        for target, weight in graph[source].items():
            _fold_louvain_pair(reduced, group_of, source, target, weight)
    return reduced
