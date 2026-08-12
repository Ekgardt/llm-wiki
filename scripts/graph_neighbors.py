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

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from memory_state import (  # noqa: E402
    ROOT,
    STATE_ROOT,
    atomic_write,
    parse_frontmatter_scalar,
    parse_project_scope,
    read_json_object_file_bounded,
)
from vault_editorial import ActiveNoteSelection, select_active_notes  # noqa: E402

KNOWLEDGE_DIR = ROOT / "knowledge" / "notes"
GRAPH_CACHE = STATE_ROOT / "cache" / "link-graph.json"
MAX_GRAPH_CACHE_BYTES = 16 * 1024 * 1024

WIKILINK_RE = re.compile(r"\[\[([^\]|#]+?)(?:\|[^\]]+)?\]\]")


def _is_inactive(content: str) -> bool:
    """Exclude inactive or ambiguously scoped pages from the active graph."""
    status = parse_frontmatter_scalar(content, "status")
    project = parse_project_scope(content)
    return bool(
        status.present
        and status.value is None
        or project.present
        and project.value is None
        or status.value is not None
        and status.value.casefold() in {"superseded", "archived"}
    )


def _resolve_snapshot_wikilink(
    target: str,
    by_path: dict[str, str],
    by_stem: dict[str, str],
) -> str | None:
    value = target.strip()
    if not value or "\\" in value:
        return None
    if "/" not in value:
        return by_stem.get(Path(value).stem.casefold())
    candidate = value if value.endswith(".md") else f"{value}.md"
    direct = by_path.get(candidate.casefold())
    if direct is not None:
        return direct
    prefix = KNOWLEDGE_DIR.relative_to(ROOT).as_posix()
    return by_path.get(f"{prefix}/{candidate}".casefold())


def _build_link_graph(
    selection: ActiveNoteSelection | None = None,
) -> dict[str, list[str]]:
    """Build canonical adjacency from one immutable note selection."""
    if selection is None:
        selection = select_active_notes(KNOWLEDGE_DIR, root=ROOT)
    graph: dict[str, list[str]] = {}
    by_path = {
        note.relative_path.casefold(): note.relative_path for note in selection.notes
    }
    by_stem: dict[str, str] = {}
    for note in selection.notes:
        by_stem.setdefault(note.path.stem.casefold(), note.relative_path)
    for note in selection.notes:
        links: set[str] = set()
        for target in WIKILINK_RE.findall(note.content):
            target = target.strip()
            if not target:
                continue
            resolved = _resolve_snapshot_wikilink(target, by_path, by_stem)
            if resolved:
                links.add(resolved)
        if links:
            graph[note.relative_path] = sorted(links, key=lambda value: (value.casefold(), value))
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
        if KNOWLEDGE_DIR.exists():
            for found in KNOWLEDGE_DIR.rglob(f"{t}.md"):
                candidates.append(found)
                break

    for c in candidates:
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
            return resolved.relative_to(ROOT).as_posix()
    return None


# Cache the graph (rebuilt when the canonical generation changes).
_link_graph_cache: tuple[str, dict[str, list[str]]] | dict[str, list[str]] | None = None


def _valid_cached_graph(
    payload: dict,
    selection: ActiveNoteSelection,
) -> dict[str, list[str]] | None:
    if (
        payload.get("version") != selection.generation.version
        or payload.get("canonical_sha256")
        != selection.generation.canonical_sha256
        or not isinstance(payload.get("graph"), dict)
    ):
        return None
    allowed = {note.relative_path for note in selection.notes}
    graph: dict[str, list[str]] = {}
    for source, targets in payload["graph"].items():
        if (
            not isinstance(source, str)
            or source not in allowed
            or not isinstance(targets, list)
            or any(not isinstance(target, str) or target not in allowed for target in targets)
        ):
            return None
        graph[source] = list(targets)
    return graph


def get_link_graph(
    selection: ActiveNoteSelection | None = None,
) -> dict[str, list[str]]:
    """Get a graph bound to one canonical selection generation."""
    global _link_graph_cache
    if selection is None and isinstance(_link_graph_cache, dict):
        return _link_graph_cache
    if selection is None:
        selection = select_active_notes(KNOWLEDGE_DIR, root=ROOT)
    generation = selection.generation.canonical_sha256
    generation_key = f"v{selection.generation.version}:{generation}"
    if isinstance(_link_graph_cache, tuple) and _link_graph_cache[0] == generation_key:
        return _link_graph_cache[1]
    payload = read_json_object_file_bounded(
        GRAPH_CACHE,
        max_bytes=MAX_GRAPH_CACHE_BYTES,
        max_depth=8,
    )
    graph = _valid_cached_graph(payload, selection) if payload is not None else None
    if graph is None:
        graph = _build_link_graph(selection)
        payload = {
            "version": selection.generation.version,
            "canonical_sha256": generation,
            "graph": graph,
        }
        try:
            GRAPH_CACHE.parent.mkdir(parents=True, exist_ok=True)
            atomic_write(GRAPH_CACHE, json.dumps(payload, sort_keys=True))
        except OSError:
            pass
    _link_graph_cache = (generation_key, graph)
    return graph


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
    *,
    selection: ActiveNoteSelection | None = None,
) -> list[dict]:
    """Add graph-neighbor boost to existing results.

    For each page in BM25 top-K, its wikilink neighbors get a
    score boost. This surfaces pages that are semantically connected
    through the link graph even if their text doesn't match the query.

    The boost is added to the 'graph_score' field and combined
    into the final fused_score via RRF.
    """
    graph = get_link_graph(selection)

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
    selection = select_active_notes(KNOWLEDGE_DIR, root=ROOT)
    graph = _build_link_graph(selection)
    generation_key = (
        f"v{selection.generation.version}:"
        f"{selection.generation.canonical_sha256}"
    )
    _link_graph_cache = (generation_key, graph)
    return sum(len(v) for v in graph.values())


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
