"""Rebuild the PostgreSQL index from Markdown source files.

Reads all knowledge/notes/*.md, parses frontmatter, and populates the
PostgreSQL tables (pages, pages_vec, edges). This is the Markdown → PG
pipeline that makes the vault searchable via PostgreSQL + pgvector.

Usage:
    uv run python scripts/rebuild_pg_index.py             # full rebuild
    uv run python scripts/rebuild_pg_index.py --semantic   # include embeddings
    uv run python scripts/rebuild_pg_index.py --status     # show stats

The script is idempotent — safe to re-run. Uses ON CONFLICT upsert.
Requires: uv sync --extra postgres  AND  PostgreSQL running.
"""
from __future__ import annotations

import argparse
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from memory_state import ROOT  # noqa: E402

KNOWLEDGE_DIR = ROOT / "knowledge" / "notes"
SKIP_NAMES = {"index.md", "log.md", "README.md", "state.md", "context.md"}

FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?", re.DOTALL)
H1_RE = re.compile(r"^#\s+(.+?)\s*$", re.MULTILINE)
SUMMARY_RE = re.compile(
    r"^One-sentence summary:\s*(.+?)\s*$", re.MULTILINE | re.IGNORECASE
)
WIKILINK_RE = re.compile(r"\[\[([^\]]+)\]\]")
PROJECT_RE = re.compile(r"^project:\s*[\"']?([^\"'\n]+)[\"']?\s*$", re.MULTILINE)
TYPE_RE = re.compile(r"^type:\s*(.+?)\s*$", re.MULTILINE)
STATUS_RE = re.compile(r"^status:\s*(.+?)\s*$", re.MULTILINE)
CONFIDENCE_RE = re.compile(r"^confidence:\s*(.+?)\s*$", re.MULTILINE)
AUTHORITY_RE = re.compile(r"^source_authority:\s*(.+?)\s*$", re.MULTILINE)
TIMESTAMP_RE = re.compile(r"^timestamp:\s*(.+?)\s*$", re.MULTILINE)
VALID_TO_RE = re.compile(r"^valid_to:\s*(.+?)\s*$", re.MULTILINE)

EMBEDDING_MODEL = "all-MiniLM-L6-v2"
EMBEDDING_DIM = 384


def _extract_frontmatter_field(content: str, pattern: re.Pattern) -> str | None:
    fm = FRONTMATTER_RE.match(content)
    if not fm:
        return None
    m = pattern.search(fm.group(1))
    return m.group(1).strip() if m else None


def _strip_frontmatter(content: str) -> str:
    return FRONTMATTER_RE.sub("", content, count=1)


def _extract_title_and_summary(content: str, fallback_stem: str) -> tuple[str, str]:
    title = fallback_stem
    summary = ""
    body = _strip_frontmatter(content)
    m = H1_RE.search(body)
    if m:
        title = m.group(1).strip()
    m = SUMMARY_RE.search(body)
    if m:
        summary = m.group(1).strip()
    return title, summary


def collect_pages() -> list[Path]:
    """Collect all searchable markdown pages from knowledge/notes/."""
    pages: list[Path] = []
    if not KNOWLEDGE_DIR.exists():
        return pages
    for md in sorted(KNOWLEDGE_DIR.rglob("*.md")):
        if not md.is_file():
            continue
        if md.name in SKIP_NAMES:
            continue
        if "archive" in md.parts:
            continue
        pages.append(md)
    return pages


def parse_page(path: Path) -> dict:
    """Parse a markdown file into a page record."""
    content = path.read_text(encoding="utf-8", errors="ignore")
    slug = path.stem
    title, summary = _extract_title_and_summary(content, slug)
    body = _strip_frontmatter(content)

    status = _extract_frontmatter_field(content, STATUS_RE) or "active"
    page_type = _extract_frontmatter_field(content, TYPE_RE) or "concept"
    project = _extract_frontmatter_field(content, PROJECT_RE)
    confidence = _extract_frontmatter_field(content, CONFIDENCE_RE)
    source_authority = _extract_frontmatter_field(content, AUTHORITY_RE)
    timestamp = _extract_frontmatter_field(content, TIMESTAMP_RE)
    valid_to = _extract_frontmatter_field(content, VALID_TO_RE)

    # Extract wikilinks for graph edges.
    wikilinks = WIKILINK_RE.findall(body)
    # Normalize: [[Display Name|slug]] → slug, [[slug]] → slug
    link_slugs = []
    for wl in wikilinks:
        if "|" in wl:
            link_slugs.append(wl.split("|")[-1].strip())
        else:
            link_slugs.append(wl.strip())

    from memory_state import file_hash
    content_hash = file_hash(path)

    return {
        "slug": slug,
        "title": title,
        "summary": summary,
        "body": body,
        "project": project,
        "page_type": page_type,
        "status": status,
        "confidence": confidence,
        "source_authority": source_authority,
        "timestamp": timestamp,
        "valid_to": valid_to,
        "content_hash": content_hash,
        "link_slugs": link_slugs,
        "path": path,
    }


def rebuild_pg(semantic: bool = False, verbose: bool = True) -> dict:
    """Full rebuild: Markdown → PostgreSQL. Returns stats dict."""
    from pg_store import (
        db,
        init_schema,
        pg_available,
        upsert_edge,
        upsert_embedding,
        upsert_page,
    )

    if not pg_available():
        if verbose:
            print("PostgreSQL not available. Cannot rebuild PG index.")
        return {"pages": 0, "embeddings": 0, "edges": 0, "error": "pg_unavailable"}

    # Ensure schema exists.
    init_schema()

    pages = collect_pages()
    if verbose:
        print(f"Rebuilding PostgreSQL index from {len(pages)} Markdown pages...")

    embedder = None
    if semantic:
        try:
            from sentence_transformers import SentenceTransformer

            embedder = SentenceTransformer(EMBEDDING_MODEL)
            if verbose:
                print(f"  Embedding model: {EMBEDDING_MODEL}")
        except ImportError:
            if verbose:
                print("  sentence-transformers not installed — skipping embeddings.")

    page_count = 0
    embedding_count = 0
    edge_count = 0
    slug_to_id: dict[str, int] = {}

    t0 = time.time()

    with db() as conn:
        for path in pages:
            rec = parse_page(path)

            page_id = upsert_page(
                conn,
                slug=rec["slug"],
                title=rec["title"],
                summary=rec["summary"],
                body=rec["body"][:5000],  # truncate body for DB
                project=rec["project"],
                page_type=rec["page_type"],
                status=rec["status"],
                confidence=rec["confidence"],
                source_authority=rec["source_authority"],
                timestamp=rec["timestamp"],
                valid_to=rec["valid_to"],
                content_hash=rec["content_hash"],
            )
            slug_to_id[rec["slug"]] = page_id
            page_count += 1

            # Embedding.
            if embedder:
                text = f"{rec['title']}. {rec['summary']}. {rec['body'][:300]}"
                vec = embedder.encode(text, show_progress_bar=False).tolist()
                upsert_embedding(conn, page_id, vec, EMBEDDING_MODEL)
                embedding_count += 1

        # Edges: resolve wikilinks now that all pages have IDs.
        for path in pages:
            rec = parse_page(path)
            src_id = slug_to_id.get(rec["slug"])
            if not src_id:
                continue
            for dst_slug in set(rec["link_slugs"]):
                dst_slug_normalized = re.sub(r"[^a-z0-9-]", "-", dst_slug.lower()).strip("-")
                dst_id = slug_to_id.get(dst_slug_normalized)
                upsert_edge(conn, src_id, dst_slug_normalized, "wikilink", dst_id)
                edge_count += 1

        conn.commit()

    elapsed = time.time() - t0
    stats = {
        "pages": page_count,
        "embeddings": embedding_count,
        "edges": edge_count,
        "elapsed_s": round(elapsed, 2),
    }

    if verbose:
        print(
            f"  Done in {elapsed:.2f}s: "
            f"{page_count} pages, {embedding_count} embeddings, {edge_count} edges."
        )

    return stats


def main() -> int:
    p = argparse.ArgumentParser(description="Rebuild PostgreSQL index from Markdown.")
    p.add_argument("--semantic", action="store_true", help="Include vector embeddings.")
    p.add_argument("--status", action="store_true", help="Show index statistics.")
    args = p.parse_args()

    if args.status:
        from pg_store import page_count, pg_available

        if not pg_available():
            print("PostgreSQL not available.")
            return 1
        count = page_count()
        print(f"PostgreSQL index: {count} active pages.")
        return 0

    stats = rebuild_pg(semantic=args.semantic)
    if "error" in stats:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
