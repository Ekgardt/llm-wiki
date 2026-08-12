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
import math
import os
import re
import sqlite3
import sys
import time
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from memory_state import (  # noqa: E402
    ROOT,
    STATE_ROOT,
    atomic_write,
    decode_json_object_strict,
    parse_frontmatter_scalar,
)
from vault_editorial import (  # noqa: E402
    AUTHORITY_RANKS,
    CONFIDENCE_RANKS,
    MAX_ACTIVE_NOTE_ENTRIES,
    ActiveNote,
    ActiveNoteSelection,
    active_note_generation_manifest,
    parse_active_note_metadata,
    read_bounded_note,
    read_bounded_note_snapshot,
    select_active_notes,
)

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
EMBEDDING_MODEL_VERSION = 1
VECTOR_CACHE_SCHEMA = "llm-wiki-vector-cache"
VECTOR_CACHE_VERSION = 1
MAX_VECTOR_CACHE_BYTES = 64 * 1024 * 1024
MAX_VECTOR_CACHE_CHARS = 64 * 1024 * 1024
MAX_VECTOR_CACHE_JSON_DEPTH = 8
MAX_VECTOR_CACHE_METADATA_CHARS = 16 * 1024 * 1024
MAX_VECTOR_CACHE_VECTOR_VALUES = MAX_ACTIVE_NOTE_ENTRIES * EMBEDDING_DIM
MAX_VECTOR_CACHE_PAGES = MAX_ACTIVE_NOTE_ENTRIES
_VECTOR_CACHE_PARALLEL_ARRAY_KEYS = (
    "paths",
    "hashes",
    "titles",
    "summaries",
    "projects",
    "timestamps",
)
_VECTOR_CACHE_GENERATION_KEYS = {
    "version",
    "artifact",
    "canonical_sha256",
    "paths",
}
_VECTOR_CACHE_KEYS = {
    "schema",
    "version",
    "generation",
    "model",
    "model_version",
    "dimensions",
    "page_count",
    "paths",
    "hashes",
    "titles",
    "summaries",
    "projects",
    "timestamps",
    "vectors",
}
# The strict decoder counts every mapping member and sequence item as one node.
MAX_VECTOR_CACHE_JSON_NODES = min(
    MAX_VECTOR_CACHE_BYTES,
    len(_VECTOR_CACHE_KEYS)
    + len(_VECTOR_CACHE_GENERATION_KEYS)
    + MAX_VECTOR_CACHE_PAGES
    * (EMBEDDING_DIM + len(_VECTOR_CACHE_PARALLEL_ARRAY_KEYS) + 2),
)
MAX_VECTOR_CACHE_JSON_MEMBERS = MAX_VECTOR_CACHE_JSON_NODES
# Compact JSON adds one structural token per node plus container delimiters.
_VECTOR_CACHE_FIXED_LEXICAL_TOKENS = (
    2 * len(_VECTOR_CACHE_KEYS)
    + 1
    + 2 * len(_VECTOR_CACHE_GENERATION_KEYS)
    + 1
    + len(_VECTOR_CACHE_PARALLEL_ARRAY_KEYS)
    + 2
)
MAX_VECTOR_CACHE_JSON_LEXICAL_TOKENS = min(
    MAX_VECTOR_CACHE_BYTES,
    _VECTOR_CACHE_FIXED_LEXICAL_TOKENS
    + MAX_VECTOR_CACHE_PAGES
    * (EMBEDDING_DIM + len(_VECTOR_CACHE_PARALLEL_ARRAY_KEYS) + 3),
)
MAX_STDIN_QUERY_BYTES = 64 * 1024


def _read_stdin_query_bounded(stream, *, max_bytes: int) -> str | None:
    try:
        binary_stream = getattr(stream, "buffer", None)
        if binary_stream is not None:
            raw = binary_stream.read(max_bytes + 1)
            if len(raw) > max_bytes:
                return None
            return raw.decode("utf-8", errors="strict")
        text = stream.read(max_bytes + 1)
        if len(text) > max_bytes:
            return None
        if len(text.encode("utf-8", errors="strict")) > max_bytes:
            return None
        return text
    except (OSError, UnicodeError, TypeError):
        return None


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
    """Return the shared set after archived/superseded filtering and dedup."""
    return list(_collect_note_selection(scope).paths)


def _collect_note_selection(scope: str = "all") -> ActiveNoteSelection:
    if scope not in ("wiki", "memory", "knowledge", "all"):
        return ActiveNoteSelection((), ())
    return select_active_notes(KNOWLEDGE_DIR, root=ROOT)


def _extract_frontmatter_field(content: str, pattern: re.Pattern) -> str | None:
    fm = FRONTMATTER_RE.match(content)
    if not fm:
        return None
    m = pattern.search(fm.group(1))
    return m.group(1).strip() if m else None


# Patterns for non-sensitive metadata extraction
TIMESTAMP_FIELD_RE = re.compile(r"^timestamp:\s*(.+?)\s*$", re.MULTILINE)
AUTHORITY_FIELD_RE = re.compile(
    r"^source_authority:\s*[\"']?([^\"'\n]+)[\"']?\s*$", re.MULTILINE
)
VALID_TO_FIELD_RE = re.compile(r"^valid_to:\s*(.+?)\s*$", re.MULTILINE)
ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _active_search_metadata(content: str) -> tuple[bool, str]:
    metadata = parse_active_note_metadata(content)
    if metadata is None:
        return False, ""
    return True, metadata.project


def _require_iso_date(value: str, field: str) -> str:
    if not isinstance(value, str) or ISO_DATE_RE.fullmatch(value) is None:
        raise ValueError(f"{field} must be a valid YYYY-MM-DD date")
    try:
        date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{field} must be a valid YYYY-MM-DD date") from exc
    return value


def _timestamp_date(content: str) -> str:
    field = parse_frontmatter_scalar(content, "timestamp")
    if not field.present:
        return ""
    if field.value is None:
        return ""
    value = field.value
    candidate = value[:10]
    try:
        _require_iso_date(candidate, "timestamp")
        if len(value) == 10:
            return candidate
        if value[10] not in {"T", " "}:
            return ""
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return ""
    return candidate

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


def _needs_rebuild(selection: ActiveNoteSelection) -> bool:
    """Return whether FTS metadata differs from the immutable selection."""
    if not INDEX_FILE.exists():
        return True
    if not isinstance(selection, ActiveNoteSelection):
        return True
    expected = active_note_generation_manifest(selection, "fts-v1")
    try:
        connection = sqlite3.connect(
            f"file:{INDEX_FILE.as_posix()}?mode=ro",
            uri=True,
        )
        try:
            stored = dict(connection.execute("SELECT key, value FROM index_metadata"))
        finally:
            connection.close()
    except (OSError, sqlite3.DatabaseError):
        return True
    return stored != {
        "artifact": str(expected["artifact"]),
        "canonical_sha256": str(expected["canonical_sha256"]),
        "version": str(expected["version"]),
    }


def _build_index(selection: ActiveNoteSelection) -> None:
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
        manifest = active_note_generation_manifest(selection, "fts-v1")
        conn.execute("CREATE TABLE index_metadata (key TEXT PRIMARY KEY, value TEXT)")
        conn.executemany(
            "INSERT INTO index_metadata (key, value) VALUES (?, ?)",
            (
                ("artifact", str(manifest["artifact"])),
                ("canonical_sha256", str(manifest["canonical_sha256"])),
                ("version", str(manifest["version"])),
            ),
        )
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

        for note in selection.notes:
            content = note.content
            active, project = _active_search_metadata(content)
            if not active:
                raise OSError(
                    f"canonical search snapshot became inactive or invalid: {note.path}"
                )
            title, summary = _extract_title_and_summary(content, note.path.stem)
            body = _strip_frontmatter(content)
            rel_path = note.relative_path
            timestamp = _timestamp_date(content)
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

    # The database metadata is authoritative; this sidecar is inspectable.
    try:
        atomic_write(
            INDEX_MANIFEST,
            json.dumps(
                active_note_generation_manifest(selection, "fts-v1"),
                sort_keys=True,
            ),
        )
    except OSError:
        pass  # best-effort


def _valid_as_of(source: ActiveNote | str, as_of: str) -> bool:
    """Return whether strict timestamp and valid_to metadata allow as_of."""
    as_of = _require_iso_date(as_of, "as_of")
    if isinstance(source, ActiveNote):
        content = source.content
    else:
        try:
            p = ROOT / source if not Path(source).is_absolute() else Path(source)
            content = read_bounded_note(p)
        except OSError:
            return True
    timestamp_field = parse_frontmatter_scalar(content, "timestamp")
    if timestamp_field.present:
        timestamp = _timestamp_date(content)
        if not timestamp or timestamp > as_of:
            return False
    valid_to = parse_frontmatter_scalar(content, "valid_to")
    if not valid_to.present:
        return True
    if valid_to.value is None:
        return False
    vt = valid_to.value.lower()
    if vt in ("null", "none", "~", ""):
        return True
    try:
        return _require_iso_date(vt, "valid_to") >= as_of
    except ValueError:
        return False


def _sort_search_results(results: list[dict], *, score_field: str) -> list[dict]:
    """Sort by unrounded relevance, then strict provenance and stable path."""
    return sorted(
        results,
        key=lambda item: (
            -float(item.get(score_field, 0.0)),
            -int(item.get("_authority_rank", AUTHORITY_RANKS["inferred"])),
            -int(item.get("_confidence_rank", CONFIDENCE_RANKS["low"])),
            str(item.get("path", "")).casefold(),
            str(item.get("path", "")),
        ),
    )


def _finalize_result_scores(results: list[dict]) -> list[dict]:
    finalized: list[dict] = []
    for result in results:
        item = result.copy()
        if isinstance(item.get("score"), int | float):
            item["score"] = round(float(item["score"]), 4)
        if isinstance(item.get("fused_score"), int | float):
            item["fused_score"] = round(float(item["fused_score"]), 6)
        item.pop("_authority_rank", None)
        item.pop("_confidence_rank", None)
        finalized.append(item)
    return finalized


def _query_fts(
    fts_query: str,
    limit: int,
    allowed_paths: set[str],
    expected_generation: dict[str, object],
) -> list[tuple]:
    connection = sqlite3.connect(
        f"file:{INDEX_FILE.as_posix()}?mode=ro",
        uri=True,
    )
    try:
        connection.execute(
            "CREATE TEMP TABLE allowed_search_paths "
            "(path TEXT PRIMARY KEY) WITHOUT ROWID"
        )
        connection.executemany(
            "INSERT INTO allowed_search_paths (path) VALUES (?)",
            ((path,) for path in sorted(allowed_paths)),
        )
        objects = dict(
            connection.execute(
                "SELECT name, type FROM sqlite_schema "
                "WHERE name IN ('index_metadata', 'pages')"
            )
        )
        metadata_columns = [
            (row[1], str(row[2]).upper(), row[5])
            for row in connection.execute("PRAGMA table_info(index_metadata)")
        ]
        page_columns = [
            row[1] for row in connection.execute("PRAGMA table_info(pages)")
        ]
        stored_generation = dict(
            connection.execute("SELECT key, value FROM index_metadata")
        )
        expected_metadata = {
            "artifact": str(expected_generation.get("artifact", "")),
            "canonical_sha256": str(
                expected_generation.get("canonical_sha256", "")
            ),
            "version": str(expected_generation.get("version", "")),
        }
        if (
            objects != {"index_metadata": "table", "pages": "table"}
            or metadata_columns != [("key", "TEXT", 1), ("value", "TEXT", 0)]
            or page_columns
            != ["path", "title", "summary", "body", "project", "timestamp"]
            or stored_generation != expected_metadata
        ):
            raise sqlite3.DatabaseError("FTS index schema or generation mismatch")
        return connection.execute(
            """
            SELECT pages.path, pages.title, pages.summary, pages.project,
                   pages.timestamp, bm25(pages) as rank
            FROM pages
            INNER JOIN allowed_search_paths AS allowed
                ON allowed.path = pages.path
            WHERE pages MATCH ?
            ORDER BY rank
            LIMIT ?
            """,
            (fts_query, limit),
        ).fetchall()
    finally:
        connection.close()


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
      valid_to empty or >= as_of)
    - semantic: if sentence-transformers is installed, also run vector
      search and fuse results via RRF. Finds semantically related pages
      even when keywords don't match.
    """
    if not query or not query.strip():
        return []
    if since is not None:
        since = _require_iso_date(since, "since")
    if as_of is not None:
        as_of = _require_iso_date(as_of, "as_of")
    selection = _collect_note_selection(scope)
    pages = list(selection.paths)
    if not pages:
        return []
    notes_by_path = {note.relative_path: note for note in selection.notes}
    allowed_paths = set(notes_by_path)

    if force_rebuild or _needs_rebuild(selection):
        _build_index(selection)

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
    expected_fts_generation = active_note_generation_manifest(selection, "fts-v1")
    try:
        bm25_raw = _query_fts(
            fts_query,
            limit * 3,
            allowed_paths,
            expected_fts_generation,
        )
    except sqlite3.DatabaseError:
        _build_index(selection)
        bm25_raw = _query_fts(
            fts_query,
            limit * 3,
            allowed_paths,
            expected_fts_generation,
        )

    # TITLE BOOST: if a page's title matches the query, boost its score.
    # This fixes Recall@1 regressions where a duplicate page (promoted
    # wiki copy) outscores the original knowledge page.
    query_lower = query.lower().strip()
    query_words = set(query_lower.split())

    bm25_results = []
    for row in bm25_raw:
        path, title, summary, proj, ts, rank = row
        if path not in allowed_paths:
            continue
        note = notes_by_path[path]
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
        if as_of and not _valid_as_of(notes_by_path[path], as_of):
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
        bm25_results.append({
            "path": path,
            "title": title,
            "summary": summary[:120] if summary else "",
            "score": score,
            "project": proj or "",
            "timestamp": ts or "",
            "_authority_rank": note.authority_rank,
            "_confidence_rank": note.confidence_rank,
        })

    # RE-SORT after boosts! FTS5 returns results in bm25() order, but
    # title/filename boosts change the effective score. Without this
    # re-sort, a page boosted to score=300 stays at its original FTS5
    # position (e.g. rank 2) even though it should be rank 1.
    bm25_results = _sort_search_results(bm25_results, score_field="score")

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
                -r["_authority_rank"],
                -r["_confidence_rank"],
                r["path"].casefold(),
                r["path"],
            )
        )
        best = filename_matches[0]
        rest = [x for x in bm25_results if x["path"] != best["path"]][:limit-1]
        return _finalize_result_scores([best] + rest)

    # Optional: vector search for semantic matching
    vector_results = None
    if semantic and _have_sentence_transformers():
        try:
            vector_results = _vector_search(
                query,
                selection,
                limit * 3,
                project,
                since,
                as_of,
                notes_by_path=notes_by_path,
                allowed_paths=allowed_paths,
            )
        except Exception as e:
            print(f"  (vector search failed: {e})", file=sys.stderr)
            vector_results = None

    # Optional: graph-neighbor boost (3rd retrieval signal)
    graph_boosts = None
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from graph_neighbors import boost_graph_neighbors
        graph_boosts = boost_graph_neighbors(
            bm25_results,
            vector_results,
            selection=selection,
        )
    except Exception:
        pass

    # Fuse results: BM25 + Vector + Graph-neighbor via RRF
    if vector_results or graph_boosts:
        fused = _rrf_fuse_triple(
            bm25_results,
            vector_results,
            graph_boosts,
            allowed_paths=allowed_paths,
        )
        for result in fused:
            note = notes_by_path[result["path"]]
            result["_authority_rank"] = note.authority_rank
            result["_confidence_rank"] = note.confidence_rank
        # Apply project boost on fused results
        if project:
            for r in fused:
                if r.get("project", "").lower() == project.lower():
                    r["fused_score"] *= 1.5
        fused = _sort_search_results(fused, score_field="fused_score")
        return _finalize_result_scores(fused[:limit])

    # BM25 only (fallback)
    bm25_results = _sort_search_results(bm25_results, score_field="score")
    return _finalize_result_scores(bm25_results[:limit])


def _rrf_fuse_triple(
    bm25_results: list[dict],
    vector_results: list[dict] | None,
    graph_boosts: list[dict] | None,
    k: int = 60,
    *,
    allowed_paths: set[str] | None = None,
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
    allowed_bm25 = (
        bm25_results
        if allowed_paths is None
        else [result for result in bm25_results if result["path"] in allowed_paths]
    )
    for rank, r in enumerate(allowed_bm25):
        path = r["path"]
        scores[path] = scores.get(path, 0) + 2.0 / (k + rank + 1)
        metadata[path] = r

    # Vector — weight 1.0 (helps when BM25 misses)
    if vector_results:
        allowed_vector = (
            vector_results
            if allowed_paths is None
            else [result for result in vector_results if result["path"] in allowed_paths]
        )
        for rank, r in enumerate(allowed_vector):
            path = r["path"]
            scores[path] = scores.get(path, 0) + 1.0 / (k + rank + 1)
            if path not in metadata:
                metadata[path] = r

    # Graph-neighbor — weight 0.5 (softest signal, boosts through links)
    if graph_boosts:
        allowed_graph = (
            graph_boosts
            if allowed_paths is None
            else [result for result in graph_boosts if result["path"] in allowed_paths]
        )
        for rank, r in enumerate(allowed_graph):
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

    results = []
    for path, score in scores.items():
        r = metadata[path].copy()
        r["fused_score"] = score
        results.append(r)
    return _sort_search_results(results, score_field="fused_score")


def _vector_search(
    query: str,
    selection: ActiveNoteSelection,
    limit: int,
    project: str | None = None,
    since: str | None = None,
    as_of: str | None = None,
    *,
    notes_by_path: dict | None = None,
    allowed_paths: set[str] | None = None,
) -> list[dict] | None:
    """Run vector similarity search using sentence-transformers.

    Builds embeddings for all pages (cached) and the query, then
    returns pages ranked by cosine similarity.
    """
    # Load or build vector cache
    vectors_data = _load_or_build_vectors(selection)
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
        path = paths[i]
        if allowed_paths is not None and path not in allowed_paths:
            continue
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
        note = notes_by_path.get(path) if notes_by_path is not None else None
        if as_of and note is not None and not _valid_as_of(note, as_of):
            continue
        score = sim
        if project and proj and proj.lower() == project.lower():
            score *= 1.5
        result = {
            "path": path,
            "title": titles[i],
            "summary": summaries[i][:120],
            "score": score,
            "project": proj,
            "timestamp": ts,
        }
        if note is not None:
            result["_authority_rank"] = note.authority_rank
            result["_confidence_rank"] = note.confidence_rank
        results.append(result)

    results = _sort_search_results(results, score_field="score")
    return results[:limit]


def _load_or_build_vectors(selection: ActiveNoteSelection) -> dict | None:
    """Load embeddings only when bound to the current canonical generation."""
    cached = _read_vector_cache_payload()
    validated = _validate_vector_cache_payload(cached, selection)
    if validated is not None:
        return validated
    rebuilt = _build_vectors(selection)
    return _validate_vector_cache_payload(rebuilt, selection)


def _read_vector_cache_payload() -> dict | None:
    try:
        snapshot = read_bounded_note_snapshot(
            VECTOR_CACHE,
            MAX_VECTOR_CACHE_BYTES,
        )
        return _decode_vector_cache_payload(snapshot.source_bytes)
    except (
        OSError,
        UnicodeError,
        ValueError,
        TypeError,
        RecursionError,
        MemoryError,
        OverflowError,
    ):
        return None


def _decode_vector_cache_payload(raw: bytes | bytearray | str) -> dict:
    return decode_json_object_strict(
        raw,
        max_bytes=MAX_VECTOR_CACHE_BYTES,
        max_chars=MAX_VECTOR_CACHE_CHARS,
        max_depth=MAX_VECTOR_CACHE_JSON_DEPTH,
        max_members=MAX_VECTOR_CACHE_JSON_MEMBERS,
        max_lexical_tokens=MAX_VECTOR_CACHE_JSON_LEXICAL_TOKENS,
    )


def _validate_vector_cache_payload(
    data: object,
    selection: ActiveNoteSelection,
) -> dict | None:
    if not isinstance(data, dict) or set(data) != _VECTOR_CACHE_KEYS:
        return None
    page_count = data.get("page_count")
    dimensions = data.get("dimensions")
    if (
        data.get("schema") != VECTOR_CACHE_SCHEMA
        or type(data.get("version")) is not int
        or data.get("version") != VECTOR_CACHE_VERSION
        or data.get("generation")
        != active_note_generation_manifest(selection, "vectors-v1")
        or data.get("model") != EMBEDDING_MODEL
        or type(data.get("model_version")) is not int
        or data.get("model_version") != EMBEDDING_MODEL_VERSION
        or type(dimensions) is not int
        or dimensions != EMBEDDING_DIM
        or type(page_count) is not int
        or page_count != len(selection.notes)
        or page_count > MAX_VECTOR_CACHE_PAGES
        or page_count * dimensions > MAX_VECTOR_CACHE_VECTOR_VALUES
    ):
        return None

    names = _VECTOR_CACHE_PARALLEL_ARRAY_KEYS
    arrays = [data.get(name) for name in names]
    vectors = data.get("vectors")
    if (
        any(not isinstance(items, list) or len(items) != page_count for items in arrays)
        or not isinstance(vectors, list)
        or len(vectors) != page_count
    ):
        return None
    paths, hashes, titles, summaries, projects, timestamps = arrays
    if any(
        not isinstance(value, str)
        for items in arrays
        for value in items
    ):
        return None
    if len(set(paths)) != page_count:
        return None

    expected_paths = [note.relative_path for note in selection.notes]
    expected_hashes = [note.content_sha256 for note in selection.notes]
    expected_titles: list[str] = []
    expected_summaries: list[str] = []
    for note in selection.notes:
        title, summary = _extract_title_and_summary(note.content, note.path.stem)
        expected_titles.append(title)
        expected_summaries.append(summary)
    if (
        paths != expected_paths
        or hashes != expected_hashes
        or titles != expected_titles
        or summaries != expected_summaries
        or projects != [note.project.lower() for note in selection.notes]
        or timestamps != [_timestamp_date(note.content) for note in selection.notes]
        or sum(len(value) for items in arrays for value in items)
        > MAX_VECTOR_CACHE_METADATA_CHARS
    ):
        return None

    for vector in vectors:
        if not isinstance(vector, list) or len(vector) != dimensions:
            return None
        for value in vector:
            if isinstance(value, bool) or not isinstance(value, int | float):
                return None
            try:
                if not math.isfinite(value):
                    return None
            except (TypeError, OverflowError):
                return None
    return data


def _build_vectors(selection: ActiveNoteSelection) -> dict | None:
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

    for note in selection.notes:
        content = note.content
        title, summary = _extract_title_and_summary(content, note.path.stem)
        body = _strip_frontmatter(content)[:500]  # truncate for embedding
        timestamp = _timestamp_date(content)

        text_for_embedding = f"{title}. {summary}. {body[:300]}"
        paths_list.append(note.relative_path)
        texts_list.append(text_for_embedding)
        titles_list.append(title)
        summaries_list.append(summary)
        projects_list.append(note.project.lower())
        timestamps_list.append(timestamp)

    if not texts_list:
        return None

    # Embed all texts
    try:
        vectors = embedder.encode(texts_list, show_progress_bar=False, convert_to_numpy=True)
    except Exception:
        return None
    try:
        vector_values = vectors.tolist()
    except (AttributeError, TypeError, ValueError, MemoryError, OverflowError, RecursionError):
        return None

    data = {
        "schema": VECTOR_CACHE_SCHEMA,
        "version": VECTOR_CACHE_VERSION,
        "generation": active_note_generation_manifest(selection, "vectors-v1"),
        "model": EMBEDDING_MODEL,
        "model_version": EMBEDDING_MODEL_VERSION,
        "dimensions": EMBEDDING_DIM,
        "page_count": len(selection.notes),
        "paths": paths_list,
        "hashes": [note.content_sha256 for note in selection.notes],
        "titles": titles_list,
        "summaries": summaries_list,
        "projects": projects_list,
        "timestamps": timestamps_list,
        "vectors": vector_values,
    }
    validated = _validate_vector_cache_payload(data, selection)
    if validated is None:
        return None

    # Cache to disk as JSON (no pickle — safer if state root is compromised).
    try:
        encoded = json.dumps(
            validated,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        if (
            len(encoded) > MAX_VECTOR_CACHE_CHARS
            or len(encoded.encode("utf-8", errors="strict")) > MAX_VECTOR_CACHE_BYTES
        ):
            return None
        roundtrip = _decode_vector_cache_payload(encoded)
        if _validate_vector_cache_payload(roundtrip, selection) is None:
            return None
        del roundtrip
        atomic_write(VECTOR_CACHE, encoded)
    except (
        OSError,
        UnicodeError,
        ValueError,
        TypeError,
        RecursionError,
        MemoryError,
        OverflowError,
    ):
        pass  # best-effort cache

    return validated


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
        query = _read_stdin_query_bounded(
            sys.stdin,
            max_bytes=MAX_STDIN_QUERY_BYTES,
        )
        if query is None:
            print("search_memory: stdin query is oversized or invalid", file=sys.stderr)
            return 2
        args.query = query.strip()

    if args.status:
        selection = _collect_note_selection("all")
        pages = list(selection.paths)
        if INDEX_FILE.exists():
            conn = sqlite3.connect(str(INDEX_FILE))
            count = conn.execute("SELECT COUNT(*) FROM pages").fetchone()[0]
            conn.close()
            print(f"Index: {INDEX_FILE}")
            print(f"  Pages indexed: {count}")
            print(f"  Pages on disk: {len(pages)}")
            print(f"  Index size: {INDEX_FILE.stat().st_size} bytes")
            print(f"  Needs rebuild: {_needs_rebuild(selection)}")
        else:
            print(f"Index: not built ({len(pages)} pages would be indexed)")
        return 0

    if args.rebuild:
        selection = _collect_note_selection(args.scope)
        print(f"Rebuilding index with {len(selection.notes)} pages...")
        t0 = time.time()
        _build_index(selection)
        print(f"Done in {time.time() - t0:.2f}s")
        return 0

    if not args.query:
        print("Usage: python search_memory.py \"<query>\"", file=sys.stderr)
        return 1

    t0 = time.time()
    try:
        results = search(
            args.query, args.scope, args.limit,
            force_rebuild=args.rebuild,
            project=args.project,
            since=args.since,
            as_of=args.as_of,
            semantic=args.semantic,
        )
    except ValueError as exc:
        print(f"search_memory: {exc}", file=sys.stderr)
        return 2
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
