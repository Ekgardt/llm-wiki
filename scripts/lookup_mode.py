"""Print the retrieval mode search uses on this vault, and what it searches.

  HYBRID  — the active evidence generation has complete vectors: BM25 + dense
            retrieval + reranking, the default of `search_memory.py` and `recall`.
  BASE    — a generation without complete vectors: BM25 alone.
  DIRECT  — no active generation: search reads Markdown directly, bounded by its
            deadline, and every hit says `no_active_generation`.

This used to recommend a mode by page count (under 50, 50–300, over 300), a rule
from before the generation existed that nothing followed: search always ran in
HYBRID. It now reports what search does. See
`docs/research/2026-09-24-an-answer-says-how-old-its-index-is.md`.

Usage:
    python scripts/lookup_mode.py            # mode, counts, generation
    python scripts/lookup_mode.py --json     # machine-readable output
"""
from __future__ import annotations

import argparse
import io
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

# Force utf-8 on stdout so the en-dash / em-dash don't mojibake on Windows cp1252.
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, io.UnsupportedOperation):
        pass

sys.path.insert(0, str(Path(__file__).resolve().parent))
from memory_state import ROOT  # noqa: E402
from vault_editorial import EDITORIAL_NAMES, editorial_parents_to_skip  # noqa: E402

WIKI = ROOT / "knowledge" / "notes"


def count_wiki_pages() -> int:
    """Count curated content pages under `knowledge/notes/`.

    Exempts editorial metadata (index/log/state/etc. — see
    `vault_editorial.EDITORIAL_NAMES`) and skeleton directories like
    `knowledge/projects/_template/`. Superseded pages are counted here; search
    excludes them (rule 12), so `search_memory.py --status` counts fewer.
    """
    if not WIKI.exists():
        return 0
    skip_parents = editorial_parents_to_skip(WIKI)
    return sum(1 for p in WIKI.rglob("*.md") if _curated_page(p, skip_parents))


def _curated_page(p: Path, skip_parents: object) -> bool:
    if not p.is_file() or p.name in EDITORIAL_NAMES:
        return False
    return not any(sp in p.parents for sp in skip_parents)


def index_status() -> dict:
    """The evidence generation the search reads, from the catalog, without a search.

    Since 2026-09-23 the generation is the only index; without one a search
    reads Markdown directly (`docs/research/2026-09-23-the-generation-is-the-only-index.md`).
    """
    try:
        import search_memory

        catalog = search_memory._active_generation_catalog()
        manifest = None if catalog is None else catalog.get_active()
    except (ImportError, OSError, ValueError, RuntimeError):
        return {"available": False}
    if not isinstance(manifest, dict):
        return {"available": False}
    return {
        "available": True,
        "generation_id": manifest.get("generation_id"),
        "vector_state": manifest.get("vector_state"),
    }


def search_mode(index: dict) -> str:
    """The mode a default search runs in, given what `index_status` found."""
    if not index.get("available"):
        return "DIRECT"
    if index.get("vector_state") == "complete":
        return "HYBRID"
    return "BASE"


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--json", action="store_true")
    args = p.parse_args()

    count = count_wiki_pages()
    index = index_status()
    mode = search_mode(index)

    payload = {
        "wiki_pages": count,
        "search_mode": mode,
        "index": index,
        "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    if args.json:
        print(json.dumps(payload, indent=2))
        return 0
    _print_summary(count, mode, index)
    return 0


def _print_summary(count: int, mode: str, index: dict) -> None:
    print(f"Wiki pages (curated, excl. editorial, superseded included): {count}")
    print(f"Search mode: {mode}")
    if not index.get("available"):
        print("No active evidence generation; search reads Markdown directly.")
        print("Build one: uv run python scripts/search_memory.py --rebuild")
        return
    print(f"Evidence generation: {index.get('generation_id')} (vectors {index.get('vector_state')})")


if __name__ == "__main__":
    raise SystemExit(main())
