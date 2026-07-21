"""Graph-neighbor retrieval boost for hybrid search.

When BM25+Vector find page A, pages that A links to via [[wikilinks]]
get a relevance boost. This is the 3rd retrieval signal (after BM25
and Vector) that akitaonrails/ai-memory uses for triple-fusion RRF.

Example: query "JWT auth" → finds decisions/auth-jwt.md → that page
links to patterns/token-refresh.md → refresh page gets boosted even
though "JWT" doesn't appear in its text.

Integrates into search_memory.py's _rrf_fuse_triple() as a 3rd signal.
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


def _build_link_graph(*, deadline: float | None = None) -> dict[str, list[str]]:
    """Build adjacency: page_path → [linked_page_paths].

    Scans all knowledge markdown files for [[wikilinks]].
    Resolves links to actual file paths.
    """
    graph: dict[str, list[str]] = {}

    if not KNOWLEDGE_DIR.exists():
        return graph

    for md in sorted(KNOWLEDGE_DIR.rglob("*.md")):
        if deadline is not None and time.monotonic() >= deadline:
            raise TimeoutError("graph neighbor source-scan deadline reached")
        if not md.is_file():
            continue
        try:
            content = md.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        # Skip superseded/archived pages from the active graph
        if _is_inactive(content):
            continue
        try:
            rel = md.relative_to(ROOT).as_posix()
        except ValueError:
            continue
        links = []
        for target in WIKILINK_RE.findall(content):
            target = target.strip()
            if not target:
                continue
            resolved = _resolve_wikilink(target, deadline=deadline)
            if resolved:
                links.append(resolved)
        if links:
            graph[rel] = sorted(dict.fromkeys(links))

    return graph


def _resolve_wikilink(target: str, *, deadline: float | None = None) -> str | None:
    """Resolve a [[wikilink]] target to a relative file path."""
    # Strip path-like targets
    t = target.strip()
    if "/" in t:
        # Path-style: try as-is and with .md
        candidates = [
            ROOT / (t + ".md"),
            ROOT / t,
        ]
    else:
        # Bare name: search for <name>.md in wiki + knowledge
        candidates = []
        if KNOWLEDGE_DIR.exists():
            candidates.extend(sorted(KNOWLEDGE_DIR.rglob(f"{t}.md")))

    valid: list[str] = []
    for c in candidates:
        if deadline is not None and time.monotonic() >= deadline:
            raise TimeoutError("graph neighbor source-scan deadline reached")
        resolved = c.resolve()
        if resolved.exists() and resolved.is_file():
            try:
                resolved.relative_to(ROOT)
            except ValueError:
                continue
            # Skip superseded/archived targets from the active graph
            try:
                target_content = resolved.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            if _is_inactive(target_content):
                continue
            valid.append(resolved.relative_to(ROOT).as_posix())
    unique = sorted(dict.fromkeys(valid))
    return unique[0] if len(unique) == 1 else None


# Optional explicit source-scan cache populated only by rebuild_graph_cache().
_link_graph_cache: dict[str, list[str]] | None = None


def _read_active_link_graph(
    catalog: object | None = None,
    *,
    deadline: float | None = None,
) -> dict[str, list[str]] | None:
    """Read resolved LINKS_TO edges from the catalog-selected immutable graph."""
    if catalog is None:
        catalog_path = STATE_ROOT / "cache" / "evidence-graph" / "catalog.sqlite3"
        if not catalog_path.is_file():
            return None
        from generation_catalog import GenerationCatalog

        catalog = GenerationCatalog(STATE_ROOT, catalog_path=catalog_path)
    from evidence_graph import EvidenceGraph
    from repository_scope import resolve_repository_scope

    graph = None
    try:
        scope = resolve_repository_scope(ROOT, deadline=deadline)
        graph = EvidenceGraph.open_active_for_repository(
            catalog,
            scope,
            deadline=deadline,
        )
        if graph is None:
            return None
        rows = graph._execute(
            """
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
            """,
            (),
            max_rows=10_000,
            deadline=deadline,
        )
    except TimeoutError:
        raise
    except (FileNotFoundError, PermissionError, TypeError, ValueError, sqlite3.Error):
        return None
    finally:
        if graph is not None:
            graph.close()
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
    if not isinstance(max_hops, int) or isinstance(max_hops, bool) or not 1 <= max_hops <= 8:
        raise ValueError("max_hops must be between 1 and 8")
    graph = get_link_graph(catalog=catalog, deadline=deadline)
    seen = {page_path}
    frontier = [page_path]
    result: list[dict[str, object]] = []
    for hop in range(1, max_hops + 1):
        if deadline is not None and time.monotonic() >= deadline:
            raise TimeoutError("graph neighbor deadline reached")
        next_frontier: list[str] = []
        for source in sorted(frontier):
            for target in sorted(graph.get(source, [])):
                if target in seen:
                    continue
                seen.add(target)
                next_frontier.append(target)
                result.append({"path": target, "hop": hop})
        frontier = sorted(next_frontier)
        if not frontier:
            break
    return sorted(result, key=lambda item: (int(item["hop"]), str(item["path"])))


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

    # Collect all paths that should get a boost
    boost_paths: dict[str, float] = {}
    for r in bm25_results[:10]:  # only top-10 seed the boost
        path = r["path"]
        neighbors = graph.get(path, [])
        for rank, neighbor in enumerate(neighbors):
            # Closer neighbors (rank 0) get more boost
            boost = boost_weight / (1 + rank * 0.2)
            boost_paths[neighbor] = boost_paths.get(neighbor, 0) + boost

    # Also boost from vector results
    if vector_results:
        for r in vector_results[:10]:
            path = r["path"]
            neighbors = graph.get(path, [])
            for rank, neighbor in enumerate(neighbors):
                boost = boost_weight / (1 + rank * 0.2)
                boost_paths[neighbor] = boost_paths.get(neighbor, 0) + boost

    return [
        {"path": path, "graph_boost": round(boost, 4)}
        for path, boost in sorted(boost_paths.items(), key=lambda item: (-item[1], item[0]))
    ]


def rebuild_graph_cache() -> int:
    """Force rebuild the link graph. Returns edge count."""
    global _link_graph_cache
    _link_graph_cache = _build_link_graph()
    return sum(len(v) for v in _link_graph_cache.values())


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description="Graph-neighbor link analysis.")
    p.add_argument("--stats", action="store_true", help="Show graph statistics")
    p.add_argument("--neighbors", type=str, default=None, help="Show neighbors of a page")
    args = p.parse_args()

    if args.stats:
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
    elif args.neighbors:
        neighbors = get_neighbors(args.neighbors)
        rev = get_reverse_neighbors(args.neighbors)
        print(f"Outbound links from {args.neighbors}:")
        for n in neighbors:
            print(f"  → {n}")
        print(f"\nInbound links to {args.neighbors}:")
        for r in rev:
            print(f"  ← {r}")
