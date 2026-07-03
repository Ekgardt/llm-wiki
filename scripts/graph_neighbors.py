"""Graph-neighbor retrieval boost for hybrid search.

When BM25+Vector find page A, pages that A links to via [[wikilinks]]
get a relevance boost. This is the 3rd retrieval signal (after BM25
and Vector) that akitaonrails/ai-memory uses for triple-fusion RRF.

Example: query "JWT auth" → finds decisions/auth-jwt.md → that page
links to patterns/token-refresh.md → refresh page gets boosted even
though "JWT" doesn't appear in its text.

Integrates into search_memory.py's _rrf_fuse() as a 3rd signal.
"""
from __future__ import annotations

import re
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from memory_state import ROOT  # noqa: E402

WIKI_DIR = ROOT / "wiki"
KNOWLEDGE_DIR = ROOT / "memory" / "knowledge"

WIKILINK_RE = re.compile(r"\[\[([^\]|#]+?)(?:\|[^\]]+)?\]\]")


def _build_link_graph() -> dict[str, list[str]]:
    """Build adjacency: page_path → [linked_page_paths].

    Scans all wiki + knowledge markdown files for [[wikilinks]].
    Resolves links to actual file paths.
    """
    graph: dict[str, list[str]] = {}
    search_dirs = [WIKI_DIR, KNOWLEDGE_DIR]

    for d in search_dirs:
        if not d.exists():
            continue
        for md in d.rglob("*.md"):
            if not md.is_file():
                continue
            try:
                content = md.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            rel = md.relative_to(ROOT).as_posix()
            links = []
            for target in WIKILINK_RE.findall(content):
                target = target.strip()
                if not target:
                    continue
                # Try to resolve the wikilink to a file path
                resolved = _resolve_wikilink(target)
                if resolved:
                    links.append(resolved)
            if links:
                graph[rel] = list(set(links))  # dedupe

    return graph


def _resolve_wikilink(target: str) -> str | None:
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
        for d in (WIKI_DIR, KNOWLEDGE_DIR):
            if d.exists():
                for found in d.rglob(f"{t}.md"):
                    candidates.append(found)
                    break  # first match

    for c in candidates:
        if c.exists() and c.is_file():
            return c.relative_to(ROOT).as_posix()
    return None


# Cache the graph (rebuilt on first call, reused for all queries in session)
_link_graph_cache: dict[str, list[str]] | None = None


def get_link_graph() -> dict[str, list[str]]:
    """Get the wikilink graph, cached at module level."""
    global _link_graph_cache
    if _link_graph_cache is None:
        _link_graph_cache = _build_link_graph()
    return _link_graph_cache


def get_neighbors(page_path: str) -> list[str]:
    """Get pages that `page_path` links to."""
    graph = get_link_graph()
    return graph.get(page_path, [])


def get_reverse_neighbors(page_path: str) -> list[str]:
    """Get pages that link TO `page_path`."""
    graph = get_link_graph()
    return [src for src, targets in graph.items() if page_path in targets]


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

    return [{"path": p, "graph_boost": round(b, 4)} for p, b in sorted(boost_paths.items(), key=lambda x: x[1], reverse=True)]


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
