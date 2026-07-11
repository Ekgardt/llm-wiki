"""Built-in hybrid search over the vault — zero external dependencies.

Uses Python's built-in sqlite3 + FTS5 for BM25 full-text search.
Optionally uses sentence-transformers for semantic (vector) search
when the library is installed. Results are fused via Reciprocal
Rank Fusion (RRF) for hybrid ranking.

For solo-developer vaults (<500 pages):
- BM25 only: <10ms, zero deps, good for keyword-precise queries
- BM25 + Vector: <50ms, needs `pip install sentence-transformers`,
  finds semantically related pages ("database performance" → "N+1 query fix")

Usage:
    uv run python scripts/search_memory.py "auth decision"
    uv run python scripts/search_memory.py "database performance" --semantic
    uv run python scripts/search_memory.py "hook timing gotcha" --limit 5
    uv run python scripts/search_memory.py "JWT" --scope wiki --project your-project
    uv run python scripts/search_memory.py --rebuild  # force index rebuild
    uv run python scripts/search_memory.py --status   # show index stats
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from memory_state import ROOT, STATE_ROOT, atomic_write  # noqa: E402

INDEX_DIR = STATE_ROOT / "cache"
INDEX_FILE = INDEX_DIR / "index.sqlite"
INDEX_MANIFEST = INDEX_DIR / ".paths-manifest"
VECTOR_CACHE = INDEX_DIR / "vectors.json"  # JSON embedding cache (no pickle)

KNOWLEDGE_DIR = ROOT / "knowledge" / "notes"
# Legacy alias retained for tests and external callers. Post-three-zone
# consolidation both names resolve to the same single knowledge/notes tree.
WIKI_DIR = KNOWLEDGE_DIR

# Files to skip (editorial / operational, not knowledge)
SKIP_NAMES = {"index.md", "log.md", "README.md", "state.md", "context.md"}
SKIP_DIRS = {"projects", "gaps", "raw-sources"}

FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?", re.DOTALL)
H1_RE = re.compile(r"^#\s+(.+?)\s*$", re.MULTILINE)
SUMMARY_RE = re.compile(
    r"^One-sentence summary:\s*(.+?)\s*$", re.MULTILINE | re.IGNORECASE
)

# Embedding model — lightweight, CPU-friendly, ~90MB download on first use
EMBEDDING_MODEL = "all-MiniLM-L6-v2"
EMBEDDING_DIM = 384


def _have_sentence_transformers() -> bool:
    """Check if sentence-transformers is importable."""
    try:
        import sentence_transformers  # noqa: F401
        return True
    except ImportError:
        return False


def _get_embedder():
    """Lazily load the embedding model. Returns None if unavailable.

    The model is cached at module level — loading ~90MB model once,
    not per-query. This is critical for benchmark latency.
    """
    global _embedder_cache
    if _embedder_cache is not None:
        return _embedder_cache
    try:
        from sentence_transformers import SentenceTransformer
        _embedder_cache = SentenceTransformer(EMBEDDING_MODEL)
        return _embedder_cache
    except Exception:
        return None


_embedder_cache = None


def _embed_texts(texts: list[str]) -> list[list[float]] | None:
    """Embed a list of texts. Returns None if model unavailable."""
    embedder = _get_embedder()
    if not embedder:
        return None
    try:
        vectors = embedder.encode(texts, show_progress_bar=False, convert_to_numpy=True)
        return vectors.tolist()
    except Exception:
        return None


def _cosine_similarity(query_vec: list[float], doc_vecs: list[list[float]]) -> list[float]:
    """Compute cosine similarity between query and all documents."""
    import numpy as np
    q = np.array(query_vec)
    docs = np.array(doc_vecs)
    # Normalize
    q_norm = q / (np.linalg.norm(q) + 1e-10)
    docs_norm = docs / (np.linalg.norm(docs, axis=1, keepdims=True) + 1e-10)
    return (docs_norm @ q_norm).tolist()


def _collect_pages(scope: str = "all") -> list[Path]:
    """Collect all searchable markdown pages."""
    pages: list[Path] = []
    seen: set[Path] = set()

    roots: list[Path] = []
    # All scope values resolve to the single knowledge/notes tree after the
    # three-zone consolidation; "wiki" and "memory" are kept as legacy aliases.
    if scope in ("wiki", "memory", "knowledge", "all"):
        roots.append(KNOWLEDGE_DIR)

    for root in roots:
        if not root.exists():
            continue
        for md in sorted(root.rglob("*.md")):
            if not md.is_file() or md in seen:
                continue
            if md.name in SKIP_NAMES:
                continue
            if any(skip in md.relative_to(root).parts for skip in SKIP_DIRS):
                continue
            # Skip superseded/archived pages from active search results
            try:
                content = md.read_text(encoding="utf-8", errors="ignore")
                fm = re.match(r"^---\s*\n(.*?)\n---", content, re.DOTALL)
                if fm:
                    status_m = re.search(r"^status:\s*(.+?)\s*$", fm.group(1), re.MULTILINE)
                    if status_m and status_m.group(1).strip() in ("superseded", "archived"):
                        continue
            except OSError:
                continue
            seen.add(md)
            pages.append(md)
    return pages


def _extract_frontmatter_field(content: str, pattern: re.Pattern) -> str | None:
    fm = FRONTMATTER_RE.match(content)
    if not fm:
        return None
    m = pattern.search(fm.group(1))
    return m.group(1).strip() if m else None


# Patterns for metadata extraction
PROJECT_FIELD_RE = re.compile(r"^project:\s*[\"']?([^\"'\n]+)[\"']?\s*$", re.MULTILINE)
TIMESTAMP_FIELD_RE = re.compile(r"^timestamp:\s*(.+?)\s*$", re.MULTILINE)
AUTHORITY_FIELD_RE = re.compile(
    r"^source_authority:\s*[\"']?([^\"'\n]+)[\"']?\s*$", re.MULTILINE
)
VALID_TO_FIELD_RE = re.compile(r"^valid_to:\s*(.+?)\s*$", re.MULTILINE)

# Higher weight = preferred in ranking (typed provenance).
AUTHORITY_WEIGHTS = {
    "user": 1.35,
    "human": 1.35,
    "ai-derived": 1.0,
    "ai": 1.0,
    "web": 0.9,
    "inferred": 0.8,
    "unknown": 1.0,
}


def _extract_title_and_summary(content: str, fallback_stem: str) -> tuple[str, str]:
    title = fallback_stem
    summary = ""
    # Strip frontmatter for cleaner search
    body = FRONTMATTER_RE.sub("", content, count=1)
    m = H1_RE.search(body)
    if m:
        title = m.group(1).strip()
    m = SUMMARY_RE.search(body)
    if m:
        summary = m.group(1).strip()
    return title, summary


def _strip_frontmatter(content: str) -> str:
    """Remove YAML frontmatter — it shouldn't pollute search results."""
    return FRONTMATTER_RE.sub("", content, count=1)


def _needs_rebuild(pages: list[Path]) -> bool:
    """Check if any page is newer than the index, or if pages were added/removed."""
    if not INDEX_FILE.exists():
        return True
    # Manifest check: if the set of indexed paths differs from the
    # current set (e.g. a page was deleted), trigger rebuild.
    current_paths = sorted(p.relative_to(ROOT).as_posix() for p in pages)
    if INDEX_MANIFEST.exists():
        try:
            manifest_paths = json.loads(
                INDEX_MANIFEST.read_text(encoding="utf-8")
            )
            if manifest_paths != current_paths:
                return True
        except (OSError, json.JSONDecodeError):
            return True
    else:
        # No manifest from a prior build — rebuild to create one.
        return True
    index_mtime = INDEX_FILE.stat().st_mtime
    for p in pages:
        try:
            if p.stat().st_mtime > index_mtime:
                return True
        except OSError:
            continue
    return False


def _build_index(pages: list[Path]) -> None:
    """Build the FTS5 index from scratch (atomically).

    Builds into a temporary database file, then atomically replaces the
    live index via ``os.replace``. This ensures concurrent searches never
    see a partially-built index or a missing-index window.
    """
    INDEX_DIR.mkdir(parents=True, exist_ok=True)
    tmp_file = INDEX_FILE.with_suffix(".sqlite.tmp")

    # Clean up any stale temp file from a previous failed build.
    if tmp_file.exists():
        tmp_file.unlink()

    conn = sqlite3.connect(str(tmp_file))
    try:
        conn.execute(
            """
            CREATE VIRTUAL TABLE pages USING fts5(
                path UNINDEXED,
                title,
                summary,
                body,
                project UNINDEXED,
                timestamp UNINDEXED,
                tokenize = 'porter unicode61'
            )
            """
        )

        for p in pages:
            try:
                content = p.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            title, summary = _extract_title_and_summary(content, p.stem)
            body = _strip_frontmatter(content)
            rel_path = p.relative_to(ROOT).as_posix()
            project = _extract_frontmatter_field(content, PROJECT_FIELD_RE) or ""
            timestamp = _extract_frontmatter_field(content, TIMESTAMP_FIELD_RE) or ""
            # Truncate timestamp to date only for filtering
            timestamp = timestamp[:10] if timestamp else ""
            conn.execute(
                "INSERT INTO pages (path, title, summary, body, project, timestamp) VALUES (?, ?, ?, ?, ?, ?)",
                (rel_path, title, summary, body, project.lower(), timestamp),
            )

        conn.commit()
    except Exception:
        # Build failed — remove the temp file so the live index (if any)
        # remains untouched and usable for searches.
        conn.close()
        try:
            tmp_file.unlink()
        except OSError:
            pass
        raise
    finally:
        try:
            conn.close()
        except Exception:
            pass

    # Atomic swap: rename temp → live. On the same filesystem this is
    # atomic at the OS level, so concurrent readers never see a gap.
    os.replace(str(tmp_file), str(INDEX_FILE))

    # Write paths manifest so deletions are detected on the next check.
    try:
        atomic_write(
            INDEX_MANIFEST,
            json.dumps(
                sorted(p.relative_to(ROOT).as_posix() for p in pages)
            ),
        )
    except OSError:
        pass  # best-effort


def _authority_weight(path: str) -> float:
    """Read source_authority from page frontmatter; default 1.0."""
    try:
        p = ROOT / path if not Path(path).is_absolute() else Path(path)
        content = p.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return 1.0
    auth = _extract_frontmatter_field(content, AUTHORITY_FIELD_RE)
    if not auth:
        return 1.0
    return AUTHORITY_WEIGHTS.get(auth.strip().lower(), 1.0)


def _valid_as_of(path: str, as_of: str) -> bool:
    """True if page is valid at as_of (valid_to empty/null or >= as_of)."""
    try:
        p = ROOT / path if not Path(path).is_absolute() else Path(path)
        content = p.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return True
    valid_to = _extract_frontmatter_field(content, VALID_TO_FIELD_RE)
    if not valid_to:
        return True
    vt = valid_to.strip().strip("\"'").lower()
    if vt in ("null", "none", "~", ""):
        return True
    return vt[:10] >= as_of[:10]


def search(
    query: str,
    scope: str = "all",
    limit: int = 10,
    force_rebuild: bool = False,
    project: str | None = None,
    since: str | None = None,
    as_of: str | None = None,
    semantic: bool = False,
) -> list[dict]:
    """Run a hybrid BM25 + optional vector search.

    Optional filters:
    - project: boost results tagged with `project: <slug>` (x2 score boost)
    - since: only results with timestamp >= YYYY-MM-DD
    - as_of: only results valid on YYYY-MM-DD (timestamp <= as_of and
      valid_to empty or >= as_of); also applies source_authority weights
    - semantic: if sentence-transformers is installed, also run vector
      search and fuse results via RRF. Finds semantically related pages
      even when keywords don't match.
    """
    if not query or not query.strip():
        return []
    pages = _collect_pages(scope)
    if not pages:
        return []

    if force_rebuild or _needs_rebuild(pages):
        _build_index(pages)

    conn = sqlite3.connect(str(INDEX_FILE))

    # BM25 search (always runs)
    # Escape FTS5 special tokens: wrap each word in double quotes to
    # prevent FTS5 from interpreting common words (in, not, and, or,
    # near) as operators or column names. This preserves AND semantics
    # between terms while avoiding syntax errors.
    # "hook errors" → '"hook" "errors"' → AND of two terms
    # (NOT '"hook errors"' which would be exact phrase match)
    # Escape embedded double-quotes so FTS5 does not choke on user input.
    fts_terms = []
    for w in query.split():
        if not w:
            continue
        safe = w.replace('"', '""')
        fts_terms.append(f'"{safe}"')
    fts_query = " ".join(fts_terms)
    bm25_raw = conn.execute(
        """
        SELECT path, title, summary, project, timestamp, bm25(pages) as rank
        FROM pages
        WHERE pages MATCH ?
        ORDER BY rank
        LIMIT ?
        """,
        (fts_query, limit * 3),
    ).fetchall()
    conn.close()

    # TITLE BOOST: if a page's title matches the query, boost its score.
    # This fixes Recall@1 regressions where a duplicate page (promoted
    # wiki copy) outscores the original knowledge page.
    query_lower = query.lower().strip()
    query_words = set(query_lower.split())

    bm25_results = []
    for row in bm25_raw:
        path, title, summary, proj, ts, rank = row
        if since and ts:
            try:
                if ts[:10] < since:
                    continue
            except (IndexError, TypeError):
                pass
        if as_of and ts:
            try:
                if ts[:10] > as_of[:10]:
                    continue
            except (IndexError, TypeError):
                pass
        if as_of and not _valid_as_of(path, as_of):
            continue
        score = -rank
        if project and proj and proj.lower() == project.lower():
            score *= 2.0

        # Title boost (highest impact on Recall@1)
        title_lower = (title or "").lower().strip()
        title_words = set(title_lower.split())
        if title_lower == query_lower:
            # Exact title match → massive boost
            score *= 5.0
        elif query_words and query_words.issubset(title_words):
            # All query words are in the title → strong boost
            score *= 3.0
        elif title_words and title_words.issubset(query_words):
            # Title is a subset of query → moderate boost
            score *= 2.0

        # FILENAME MATCH BOOST: if the query matches the filename slug,
        # this is almost certainly the right page. Strongest precision signal.
        # "hook scripts defense-in-depth" → filename "hook-scripts-defense-in-depth"
        filename_slug = Path(path).stem.lower().replace("-", " ")
        if filename_slug == query_lower:
            score *= 10.0  # near-guaranteed correct match
        elif query_words and query_words.issubset(set(filename_slug.split())):
            score *= 4.0

        # Path preference: knowledge/notes/ is the canonical durable-pages
        # tree. (Pre-three-zone this distinguished wiki/ from memory/; both
        # now resolve to the same knowledge/notes path, so the boost is a
        # no-op kept for forward-compat if a second tree is reintroduced.)
        if "knowledge/notes/" in path:
            score *= 1.3  # increased from 1.2 to break ties more decisively

        # Typed provenance: user-said outranks inferred/ai-derived.
        score *= _authority_weight(path)

        bm25_results.append({
            "path": path,
            "title": title,
            "summary": summary[:120] if summary else "",
            "score": round(score, 2),
            "project": proj or "",
            "timestamp": ts or "",
        })

    # RE-SORT after boosts! FTS5 returns results in bm25() order, but
    # title/filename boosts change the effective score. Without this
    # re-sort, a page boosted to score=300 stays at its original FTS5
    # position (e.g. rank 2) even though it should be rank 1.
    bm25_results.sort(key=lambda x: x["score"], reverse=True)

    # SHORT-CIRCUIT: if any page has exact filename match with the query,
    # return it at rank 1 immediately. This prevents graph-neighbor RRF
    # from pushing a filename-matched page down by promoting a linked
    # but incorrect page (e.g. wiki copy beating the knowledge original).
    # When multiple pages match (duplicates), prefer knowledge/notes/.
    query_normalized = query.lower().strip().replace(" ", "-")
    filename_matches = [
        r for r in bm25_results[:10]
        if Path(r["path"]).stem.lower() == query_normalized
    ]
    if filename_matches:
        # Sort matches: knowledge/notes/ first (primary source),
        # then by score (highest first)
        filename_matches.sort(
            key=lambda r: (
                0 if "knowledge/notes/" in r["path"] else 1,
                -r["score"],
            )
        )
        best = filename_matches[0]
        rest = [x for x in bm25_results if x["path"] != best["path"]][:limit-1]
        return [best] + rest

    # Optional: vector search for semantic matching
    vector_results = None
    if semantic and _have_sentence_transformers():
        try:
            vector_results = _vector_search(query, pages, limit * 3, project, since, as_of)
        except Exception as e:
            print(f"  (vector search failed: {e})", file=sys.stderr)
            vector_results = None

    # Optional: graph-neighbor boost (3rd retrieval signal)
    graph_boosts = None
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from graph_neighbors import boost_graph_neighbors
        graph_boosts = boost_graph_neighbors(bm25_results, vector_results)
    except Exception:
        pass

    # Fuse results: BM25 + Vector + Graph-neighbor via RRF
    if vector_results or graph_boosts:
        fused = _rrf_fuse_triple(bm25_results, vector_results, graph_boosts)
        # Apply project boost on fused results
        if project:
            for r in fused:
                if r.get("project", "").lower() == project.lower():
                    r["fused_score"] = round(r["fused_score"] * 1.5, 4)
            fused.sort(key=lambda x: x.get("fused_score", 0), reverse=True)
        return fused[:limit]

    # BM25 only (fallback)
    bm25_results.sort(key=lambda x: x["score"], reverse=True)
    return bm25_results[:limit]


def _rrf_fuse_triple(
    bm25_results: list[dict],
    vector_results: list[dict] | None,
    graph_boosts: list[dict] | None,
    k: int = 60,
) -> list[dict]:
    """Triple-fusion RRF: BM25 + Vector + Graph-neighbor.

    Weighted RRF: BM25 gets weight 2 (most reliable for known-item
    retrieval), Vector gets weight 1 (helps with semantic queries),
    Graph gets weight 0.5 (soft boost through links).

    Standard unweighted RRF can HURT when BM25 is already correct:
    if BM25 has page at rank 1 but Vector has a different page at
    rank 1, the fusion pushes the correct page down. Weighting BM25
    higher prevents this regression.
    """
    scores: dict[str, float] = {}
    metadata: dict[str, dict] = {}

    # BM25 — weight 2.0 (most reliable signal)
    for rank, r in enumerate(bm25_results):
        path = r["path"]
        scores[path] = scores.get(path, 0) + 2.0 / (k + rank + 1)
        metadata[path] = r

    # Vector — weight 1.0 (helps when BM25 misses)
    if vector_results:
        for rank, r in enumerate(vector_results):
            path = r["path"]
            scores[path] = scores.get(path, 0) + 1.0 / (k + rank + 1)
            if path not in metadata:
                metadata[path] = r

    # Graph-neighbor — weight 0.5 (softest signal, boosts through links)
    if graph_boosts:
        for rank, r in enumerate(graph_boosts):
            path = r["path"]
            scores[path] = scores.get(path, 0) + 0.5 * r.get("graph_boost", 0) / (k * 2 + rank + 1)
            if path not in metadata:
                metadata[path] = {
                    "path": path,
                    "title": path.split("/")[-1].replace(".md", ""),
                    "summary": "",
                    "score": 0,
                    "project": "",
                    "timestamp": "",
                }

    sorted_paths = sorted(scores.items(), key=lambda x: x[1], reverse=True)
    results = []
    for path, score in sorted_paths:
        r = metadata[path].copy()
        r["fused_score"] = round(score, 4)
        results.append(r)
    return results


def _vector_search(
    query: str,
    pages: list[Path],
    limit: int,
    project: str | None = None,
    since: str | None = None,
    as_of: str | None = None,
) -> list[dict] | None:
    """Run vector similarity search using sentence-transformers.

    Builds embeddings for all pages (cached) and the query, then
    returns pages ranked by cosine similarity.
    """
    # Load or build vector cache
    vectors_data = _load_or_build_vectors(pages)
    if not vectors_data:
        return None

    paths = vectors_data["paths"]
    titles = vectors_data["titles"]
    summaries = vectors_data["summaries"]
    projects = vectors_data["projects"]
    timestamps = vectors_data["timestamps"]
    vectors = vectors_data["vectors"]

    # Embed the query
    query_vec = _embed_texts([query])
    if not query_vec:
        return None

    # Compute cosine similarity
    sims = _cosine_similarity(query_vec[0], vectors)

    # Build results
    results = []
    for i, sim in enumerate(sims):
        proj = projects[i]
        ts = timestamps[i]
        # Apply temporal filter
        if since and ts:
            try:
                if ts[:10] < since:
                    continue
            except (IndexError, TypeError):
                pass
        # Temporal filter for vector hits (parity with BM25 as_of).
        if as_of and ts:
            try:
                if ts[:10] > as_of[:10]:
                    continue
            except (IndexError, TypeError):
                pass
        if as_of and not _valid_as_of(paths[i], as_of):
            continue
        score = round(sim, 4)
        if project and proj and proj.lower() == project.lower():
            score = round(score * 1.5, 4)
        results.append({
            "path": paths[i],
            "title": titles[i],
            "summary": summaries[i][:120],
            "score": score,
            "project": proj,
            "timestamp": ts,
        })

    results.sort(key=lambda x: x["score"], reverse=True)
    return results[:limit]


def _load_or_build_vectors(pages: list[Path]) -> dict | None:
    """Load cached embeddings or build them fresh.

    Cache is invalidated when any source file changes (mtime check).
    """
    # Check cache validity via mtime + path set (JSON cache; no pickle).
    needs_rebuild = True
    current_paths = sorted(
        p.relative_to(ROOT).as_posix() for p in pages if p.exists()
    )
    if VECTOR_CACHE.exists():
        try:
            cache_mtime = VECTOR_CACHE.stat().st_mtime
            needs_rebuild = any(
                p.stat().st_mtime > cache_mtime for p in pages if p.exists()
            )
            if not needs_rebuild:
                data = json.loads(VECTOR_CACHE.read_text(encoding="utf-8"))
                if sorted(data.get("paths") or []) != current_paths:
                    needs_rebuild = True
                else:
                    return data
        except Exception:
            needs_rebuild = True

    if needs_rebuild:
        return _build_vectors(pages)
    return None


def _build_vectors(pages: list[Path]) -> dict | None:
    """Build embeddings for all pages. Returns None if model unavailable."""
    embedder = _get_embedder()
    if not embedder:
        return None

    INDEX_DIR.mkdir(parents=True, exist_ok=True)

    paths_list = []
    texts_list = []
    titles_list = []
    summaries_list = []
    projects_list = []
    timestamps_list = []

    for p in pages:
        try:
            content = p.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        title, summary = _extract_title_and_summary(content, p.stem)
        body = _strip_frontmatter(content)[:500]  # truncate for embedding
        project = _extract_frontmatter_field(content, PROJECT_FIELD_RE) or ""
        timestamp = _extract_frontmatter_field(content, TIMESTAMP_FIELD_RE) or ""
        timestamp = timestamp[:10] if timestamp else ""

        text_for_embedding = f"{title}. {summary}. {body[:300]}"
        paths_list.append(p.relative_to(ROOT).as_posix())
        texts_list.append(text_for_embedding)
        titles_list.append(title)
        summaries_list.append(summary)
        projects_list.append(project.lower())
        timestamps_list.append(timestamp)

    if not texts_list:
        return None

    # Embed all texts
    try:
        vectors = embedder.encode(texts_list, show_progress_bar=False, convert_to_numpy=True)
    except Exception:
        return None

    data = {
        "paths": paths_list,
        "titles": titles_list,
        "summaries": summaries_list,
        "projects": projects_list,
        "timestamps": timestamps_list,
        "vectors": vectors.tolist(),
    }

    # Cache to disk as JSON (no pickle — safer if state root is compromised).
    try:
        atomic_write(VECTOR_CACHE, json.dumps(data))
    except Exception:
        pass  # best-effort cache

    return data


def main() -> int:
    p = argparse.ArgumentParser(description="Built-in FTS5 search over the vault.")
    p.add_argument("query", nargs="?", default=None, help="Search query")
    p.add_argument("--scope", choices=["all", "wiki", "memory", "knowledge"], default="all")
    p.add_argument("--limit", type=int, default=10)
    p.add_argument("--project", default=None, help="Boost results from this project slug")
    p.add_argument("--since", default=None, help="Only results since YYYY-MM-DD")
    p.add_argument("--as-of", dest="as_of", default=None, help="Only results valid on YYYY-MM-DD")
    p.add_argument("--semantic", action="store_true", help="Enable vector search (needs sentence-transformers)")
    p.add_argument("--rebuild", action="store_true", help="Force index rebuild")
    p.add_argument("--status", action="store_true", help="Show index stats")
    p.add_argument("--stdin", action="store_true", help="Read query from stdin (injection-safe)")
    args = p.parse_args()

    if args.stdin:
        args.query = sys.stdin.read().strip()

    if args.status:
        pages = _collect_pages("all")
        if INDEX_FILE.exists():
            conn = sqlite3.connect(str(INDEX_FILE))
            count = conn.execute("SELECT COUNT(*) FROM pages").fetchone()[0]
            conn.close()
            print(f"Index: {INDEX_FILE}")
            print(f"  Pages indexed: {count}")
            print(f"  Pages on disk: {len(pages)}")
            print(f"  Index size: {INDEX_FILE.stat().st_size} bytes")
            print(f"  Needs rebuild: {_needs_rebuild(pages)}")
        else:
            print(f"Index: not built ({len(pages)} pages would be indexed)")
        return 0

    if args.rebuild:
        pages = _collect_pages(args.scope)
        print(f"Rebuilding index with {len(pages)} pages...")
        t0 = time.time()
        _build_index(pages)
        print(f"Done in {time.time() - t0:.2f}s")
        return 0

    if not args.query:
        print("Usage: python search_memory.py \"<query>\"", file=sys.stderr)
        return 1

    t0 = time.time()
    results = search(
        args.query, args.scope, args.limit,
        force_rebuild=args.rebuild,
        project=args.project,
        since=args.since,
        as_of=args.as_of,
        semantic=args.semantic,
    )
    elapsed = time.time() - t0

    if not results:
        print(f"No results for '{args.query}' ({elapsed:.3f}s)")
        return 0

    print(f"Found {len(results)} result(s) for '{args.query}' ({elapsed:.3f}s):\n")
    for i, r in enumerate(results, 1):
        proj_tag = f" [{r['project']}]" if r["project"] else ""
        ts_tag = f" ({r['timestamp']})" if r["timestamp"] else ""
        print(f"{i}. [{r['score']}] {r['title']}{proj_tag}{ts_tag}")
        print(f"   {r['path']}")
        if r["summary"]:
            print(f"   {r['summary']}")
        print()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
