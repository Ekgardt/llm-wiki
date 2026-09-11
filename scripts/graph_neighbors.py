"""Graph-neighbor retrieval boost for hybrid search.

When BM25+Vector find page A, pages that A links to via [[wikilinks]]
get a relevance boost. This is the 3rd retrieval signal (after BM25
and Vector) that akitaonrails/ai-memory uses for triple-fusion RRF.

Example: query "JWT auth" → finds decisions/auth-jwt.md → that page
links to patterns/token-refresh.md → refresh page gets boosted even
though "JWT" doesn't appear in its text.

Feeds retrieval.py's fuse_rrf() as a third signal.
"""
from __future__ import annotations

import re
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from memory_state import ROOT, STATE_ROOT  # noqa: E402

KNOWLEDGE_DIR = ROOT / "knowledge" / "notes"

WIKILINK_RE = re.compile(r"\[\[([^\]|#]+?)(?:\|[^\]]+)?\]\]")
STATUS_RE = re.compile(r"^status:\s*(.+?)\s*$", re.MULTILINE)


def _is_inactive(content: str) -> bool:
    """Return True if the page has status: superseded or status: archived."""
    m = STATUS_RE.search(content)
    return bool(m and m.group(1).strip().lower() in ("superseded", "archived"))


def _require_before(deadline: float | None, message: str) -> None:
    if deadline is not None and time.monotonic() >= deadline:
        raise TimeoutError(message)


def _read_text_or_none(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None


def _build_link_graph(*, deadline: float | None = None) -> dict[str, list[str]]:
    """Build adjacency: page_path → [linked_page_paths].

    Scans all knowledge markdown files for [[wikilinks]].
    Resolves links to actual file paths.
    """
    graph: dict[str, list[str]] = {}
    if not KNOWLEDGE_DIR.exists():
        return graph
    for md in sorted(KNOWLEDGE_DIR.rglob("*.md")):
        _require_before(deadline, "graph neighbor source-scan deadline reached")
        entry = _page_links(md, deadline)
        if entry is not None:
            graph[entry[0]] = entry[1]
    return graph


def _page_links(md: Path, deadline: float | None) -> tuple[str, list[str]] | None:
    """(vault path, resolved link targets) of one active page that links somewhere."""
    page = _active_page(md)
    if page is None:
        return None
    links = _resolved_links(page[1], deadline)
    if not links:
        return None
    return page[0], sorted(dict.fromkeys(links))


def _active_page(md: Path) -> tuple[str, str] | None:
    """(vault path, text) of a readable page that is not superseded or archived."""
    content = _active_text(md)
    if content is None:
        return None
    try:
        return md.relative_to(ROOT).as_posix(), content
    except ValueError:
        return None


def _active_text(md: Path) -> str | None:
    if not md.is_file():
        return None
    content = _read_text_or_none(md)
    # Skip superseded/archived pages from the active graph
    if content is None or _is_inactive(content):
        return None
    return content


def _resolved_links(content: str, deadline: float | None) -> list[str]:
    links = []
    for target in WIKILINK_RE.findall(content):
        resolved = _resolve_target(target.strip(), deadline)
        if resolved:
            links.append(resolved)
    return links


def _resolve_target(target: str, deadline: float | None) -> str | None:
    if not target:
        return None
    return _resolve_wikilink(target, deadline=deadline)


def _resolve_wikilink(target: str, *, deadline: float | None = None) -> str | None:
    """Resolve a [[wikilink]] target to a relative file path."""
    valid: list[str] = []
    for candidate in _wikilink_candidates(target.strip()):
        _require_before(deadline, "graph neighbor source-scan deadline reached")
        path = _active_vault_file(candidate)
        if path is not None:
            valid.append(path)
    unique = sorted(dict.fromkeys(valid))
    return unique[0] if len(unique) == 1 else None


def _wikilink_candidates(target: str) -> list[Path]:
    if "/" in target:
        # Path-style: try as-is and with .md
        return [ROOT / (target + ".md"), ROOT / target]
    # Bare name: search for <name>.md in wiki + knowledge
    if not KNOWLEDGE_DIR.exists():
        return []
    return sorted(KNOWLEDGE_DIR.rglob(f"{target}.md"))


def _active_vault_file(candidate: Path) -> str | None:
    """The vault-relative path of an existing, active page inside the vault."""
    resolved = candidate.resolve()
    if not (resolved.exists() and resolved.is_file()):
        return None
    try:
        relative = resolved.relative_to(ROOT).as_posix()
    except ValueError:
        return None
    return _when_active(resolved, relative)


def _when_active(resolved: Path, relative: str) -> str | None:
    # Skip superseded/archived targets from the active graph
    target_content = _read_text_or_none(resolved)
    if target_content is None or _is_inactive(target_content):
        return None
    return relative


# Optional explicit source-scan cache populated only by rebuild_graph_cache().
_link_graph_cache: dict[str, list[str]] | None = None


_LINKS_SQL = """
            WITH pages AS (
              SELECT o.node_id, min(s.relative_path) AS relative_path
              FROM occurrence o JOIN source s USING(source_id)
              JOIN node n USING(node_id)
              WHERE n.kind IN ('knowledge-page', 'decision', 'debugging-note')
              GROUP BY o.node_id
            )
            SELECT src.relative_path AS source_path,
                   dst.relative_path AS target_path,
                   a.assertion_id
            FROM assertion a
            JOIN pages src ON src.node_id = a.source_node_id
            JOIN pages dst ON dst.node_id = a.target_node_id
            WHERE a.edge_type = 'LINKS_TO' AND a.resolution = 'resolved'
            ORDER BY source_path, target_path, a.assertion_id
            LIMIT ?
            """


def _read_active_link_graph(
    catalog: object | None = None,
    *,
    deadline: float | None = None,
) -> dict[str, list[str]] | None:
    """Read resolved LINKS_TO edges from the catalog-selected immutable graph."""
    catalog = _catalog_or_default(catalog)
    if catalog is None:
        return None
    rows = _link_rows(catalog, deadline)
    if rows is None:
        return None
    return _adjacency(rows)


def _catalog_or_default(catalog: object | None) -> object | None:
    if catalog is not None:
        return catalog
    catalog_path = STATE_ROOT / "cache" / "evidence-graph" / "catalog.sqlite3"
    if not catalog_path.is_file():
        return None
    from generation_catalog import GenerationCatalog

    return GenerationCatalog(STATE_ROOT, catalog_path=catalog_path)


def _link_rows(catalog: object, deadline: float | None) -> list | None:
    """Resolved LINKS_TO rows of the active generation; None when there is none to read."""
    try:
        return _query_links(catalog, deadline)
    except TimeoutError:
        raise
    except (FileNotFoundError, PermissionError, TypeError, ValueError, sqlite3.Error):
        return None


def _query_links(catalog: object, deadline: float | None) -> list | None:
    from evidence_graph import EvidenceGraph
    from repository_scope import resolve_repository_scope

    scope = resolve_repository_scope(ROOT, deadline=deadline)
    graph = EvidenceGraph.open_active_for_repository(
        catalog,
        scope,
        deadline=deadline,
    )
    if graph is None:
        return None
    try:
        return graph._execute(_LINKS_SQL, (), max_rows=10_000, deadline=deadline)
    finally:
        graph.close()


def _adjacency(rows: list) -> dict[str, list[str]]:
    adjacency: dict[str, list[str]] = {}
    for row in rows:
        adjacency.setdefault(str(row["source_path"]), []).append(str(row["target_path"]))
    return {
        source: sorted(dict.fromkeys(targets))
        for source, targets in sorted(adjacency.items())
    }


def get_link_graph(
    *,
    catalog: object | None = None,
    deadline: float | None = None,
) -> dict[str, list[str]]:
    """Prefer the active immutable graph and honestly source-scan if absent."""
    global _link_graph_cache
    active = _read_active_link_graph(catalog, deadline=deadline)
    if active is not None:
        _link_graph_cache = None
        return active
    if _link_graph_cache is not None:
        return _link_graph_cache
    return _build_link_graph(deadline=deadline)


def get_neighbor_records(
    page_path: str,
    *,
    max_hops: int = 1,
    catalog: object | None = None,
    deadline: float | None = None,
) -> list[dict[str, object]]:
    """Return deterministic outbound neighbors ordered by hop then path."""
    if not _valid_hops(max_hops):
        raise ValueError("max_hops must be between 1 and 8")
    graph = get_link_graph(catalog=catalog, deadline=deadline)
    seen = {page_path}
    frontier = [page_path]
    result: list[dict[str, object]] = []
    for hop in range(1, max_hops + 1):
        _require_before(deadline, "graph neighbor deadline reached")
        frontier = _next_frontier(graph, frontier, seen)
        result.extend({"path": target, "hop": hop} for target in frontier)
        if not frontier:
            break
    return sorted(result, key=lambda item: (int(item["hop"]), str(item["path"])))


def _valid_hops(max_hops: object) -> bool:
    return isinstance(max_hops, int) and not isinstance(max_hops, bool) and 1 <= max_hops <= 8


def _next_frontier(graph: dict[str, list[str]], frontier: list[str], seen: set[str]) -> list[str]:
    """Targets one hop past the frontier that were not seen before, sorted."""
    candidates = [target for source in sorted(frontier) for target in sorted(graph.get(source, []))]
    discovered: list[str] = []
    for target in candidates:
        if target not in seen:
            seen.add(target)
            discovered.append(target)
    return sorted(discovered)


def get_neighbors(page_path: str) -> list[str]:
    """Get pages that `page_path` links to."""
    return [str(item["path"]) for item in get_neighbor_records(page_path)]


def get_reverse_neighbors(page_path: str) -> list[str]:
    """Get pages that link TO `page_path`."""
    graph = get_link_graph()
    return sorted(src for src, targets in graph.items() if page_path in targets)


def boost_graph_neighbors(
    bm25_results: list[dict],
    vector_results: list[dict] | None,
    boost_weight: float = 0.15,
) -> list[dict]:
    """Add graph-neighbor boost to existing results.

    For each page in BM25 top-K, its wikilink neighbors get a
    score boost. This surfaces pages that are semantically connected
    through the link graph even if their text doesn't match the query.

    The boost is added to the 'graph_score' field and combined
    into the final fused_score via RRF.
    """
    graph = get_link_graph()
    boost_paths: dict[str, float] = {}
    # Only the top-10 of each list seed the boost, BM25 first, then vector.
    for r in [*bm25_results[:10], *(vector_results or [])[:10]]:
        _add_neighbor_boosts(boost_paths, graph.get(r["path"], []), boost_weight)
    return [
        {"path": path, "graph_boost": round(boost, 4)}
        for path, boost in sorted(boost_paths.items(), key=lambda item: (-item[1], item[0]))
    ]


def _add_neighbor_boosts(boost_paths: dict[str, float], neighbors: list[str], boost_weight: float) -> None:
    for rank, neighbor in enumerate(neighbors):
        # Closer neighbors (rank 0) get more boost
        boost = boost_weight / (1 + rank * 0.2)
        boost_paths[neighbor] = boost_paths.get(neighbor, 0) + boost


def rebuild_graph_cache() -> int:
    """Force rebuild the link graph. Returns edge count."""
    global _link_graph_cache
    _link_graph_cache = _build_link_graph()
    return sum(len(v) for v in _link_graph_cache.values())


def _print_stats() -> None:
    graph = get_link_graph()
    total_edges = sum(len(v) for v in graph.values())
    print(f"Pages with outbound links: {len(graph)}")
    print(f"Total edges: {total_edges}")
    avg = total_edges / len(graph) if graph else 0
    print(f"Average links per page: {avg:.1f}")
    # Top-5 most-connected pages
    top = sorted(graph.items(), key=lambda x: len(x[1]), reverse=True)[:5]
    print("\nTop-5 most-connected pages:")
    for path, links in top:
        print(f"  {path}: {len(links)} links")


def _print_neighbors(page: str) -> None:
    neighbors = get_neighbors(page)
    rev = get_reverse_neighbors(page)
    print(f"Outbound links from {page}:")
    for n in neighbors:
        print(f"  → {n}")
    print(f"\nInbound links to {page}:")
    for r in rev:
        print(f"  ← {r}")


def main() -> int:
    import argparse

    p = argparse.ArgumentParser(description="Graph-neighbor link analysis.")
    p.add_argument("--stats", action="store_true", help="Show graph statistics")
    p.add_argument("--neighbors", type=str, default=None, help="Show neighbors of a page")
    args = p.parse_args()
    if args.stats:
        _print_stats()
        return 0
    if args.neighbors:
        _print_neighbors(args.neighbors)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
