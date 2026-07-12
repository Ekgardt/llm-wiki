"""Rebuild LanceDB vector index from Markdown source files.

Reads all knowledge/notes/*.md, generates embeddings, and stores in
LanceDB table for HNSW vector search. This is the Markdown → LanceDB
pipeline that enables hybrid search at scale.

Usage:
    uv run python scripts/rebuild_lance_index.py             # rebuild
    uv run python scripts/rebuild_lance_index.py --status     # show stats

Requires: uv sync --extra hybrid
"""
from __future__ import annotations

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
PROJECT_RE = re.compile(r"^project:\s*[\"']?([^\"'\n]+)[\"']?\s*$", re.MULTILINE)
TIMESTAMP_RE = re.compile(r"^timestamp:\s*(.+?)\s*$", re.MULTILINE)

EMBEDDING_MODEL = "BAAI/bge-small-en-v1.5"


def _extract_fm(content: str, pattern: re.Pattern) -> str | None:
    fm = FRONTMATTER_RE.match(content)
    if not fm:
        return None
    m = pattern.search(fm.group(1))
    return m.group(1).strip() if m else None


def rebuild_lance(verbose: bool = True) -> dict:
    """Full rebuild: Markdown → embeddings → LanceDB. Returns stats."""
    if not KNOWLEDGE_DIR.exists():
        return {"pages": 0, "error": "no knowledge dir"}

    try:
        from lance_store import have_lancedb, upsert_vectors
    except ImportError:
        if verbose:
            print("LanceDB not installed. Run: uv sync --extra hybrid")
        return {"pages": 0, "error": "lancedb_not_installed"}

    if not have_lancedb():
        # LanceDB might be importable but not initialized — that's OK,
        # upsert_vectors will create the table.
        pass

    # Check for embedding model.
    try:
        from sentence_transformers import SentenceTransformer
    except ImportError:
        if verbose:
            print("sentence-transformers not installed. Run: uv sync --extra semantic")
        return {"pages": 0, "error": "no_sentence_transformers"}

    embedder = SentenceTransformer(EMBEDDING_MODEL)

    pages = []
    for md in sorted(KNOWLEDGE_DIR.rglob("*.md")):
        if md.name in SKIP_NAMES or "archive" in md.parts:
            continue
        pages.append(md)

    if verbose:
        print(f"Rebuilding LanceDB index from {len(pages)} Markdown pages...")

    paths, titles, summaries, projects, timestamps, texts = [], [], [], [], [], []

    for md in pages:
        content = md.read_text(encoding="utf-8", errors="ignore")
        title_match = H1_RE.search(content)
        title = title_match.group(1).strip() if title_match else md.stem
        summary_match = SUMMARY_RE.search(content)
        summary = summary_match.group(1).strip() if summary_match else ""
        body = FRONTMATTER_RE.sub("", content, count=1)
        project = _extract_fm(content, PROJECT_RE) or ""
        timestamp = _extract_fm(content, TIMESTAMP_RE) or ""

        paths.append(str(md.relative_to(ROOT).as_posix()))
        titles.append(title)
        summaries.append(summary)
        projects.append(project.lower())
        timestamps.append(timestamp[:10] if timestamp else "")
        texts.append(f"{title}. {summary}. {body[:300]}")

    if not texts:
        return {"pages": 0, "error": "no_pages"}

    t0 = time.time()
    vectors = embedder.encode(texts, show_progress_bar=False, convert_to_numpy=True).tolist()
    count = upsert_vectors(paths, titles, summaries, projects, timestamps, vectors, EMBEDDING_MODEL)
    elapsed = time.time() - t0

    stats = {"pages": count, "elapsed_s": round(elapsed, 2)}
    if verbose:
        print(f"  Done in {elapsed:.2f}s: {count} vectors stored in LanceDB.")
    return stats


def main() -> int:
    import argparse
    p = argparse.ArgumentParser(description="Rebuild LanceDB vector index.")
    p.add_argument("--status", action="store_true", help="Show index statistics.")
    args = p.parse_args()

    if args.status:
        from lance_store import have_lancedb, vector_count
        if not have_lancedb():
            print("LanceDB not available.")
            return 1
        print(f"LanceDB index: {vector_count()} vectors.")
        return 0

    stats = rebuild_lance()
    return 0 if "error" not in stats else 1


if __name__ == "__main__":
    raise SystemExit(main())
