"""Print the recommended local retrieval mode for the current vault size.

  DIRECT  (< 50 wiki pages)    — read knowledge/index.md + target pages.
  BASE    (50–300 wiki pages)  — use the evidence generation's BM25 when direct
                                  navigation is ambiguous.
  HYBRID  (> 300 wiki pages)   — use BM25 + optional local vectors,
                                  graph neighbors, and reranking.

Usage:
    python scripts/lookup_mode.py            # print tier + counts
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

TIERS = [
    (50, "DIRECT"),
    (301, "BASE"),
    (float("inf"), "HYBRID"),
]


def count_wiki_pages() -> int:
    """Count curated content pages under `knowledge/notes/`.

    Exempts editorial metadata (index/log/state/etc. — see
    `vault_editorial.EDITORIAL_NAMES`) and skeleton directories like
    `knowledge/projects/_template/`. The resulting count drives the retrieval
    tier recommendation.
    """
    if not WIKI.exists():
        return 0
    skip_parents = editorial_parents_to_skip(WIKI)
    return sum(1 for p in WIKI.rglob("*.md") if _curated_page(p, skip_parents))


def _curated_page(p: Path, skip_parents: object) -> bool:
    if not p.is_file() or p.name in EDITORIAL_NAMES:
        return False
    return not any(sp in p.parents for sp in skip_parents)


def tier_for(count: int) -> str:
    for cap, name in TIERS:
        if count < cap:
            return name
    return "HYBRID"


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


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--json", action="store_true")
    args = p.parse_args()

    count = count_wiki_pages()
    tier = tier_for(count)
    index = index_status()

    payload = {
        "wiki_pages": count,
        "recommended_tier": tier,
        "thresholds": {"DIRECT": "<50", "BASE": "50–300", "HYBRID": ">300"},
        "index": index,
        "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    if args.json:
        print(json.dumps(payload, indent=2))
        return 0
    _print_summary(count, tier, index)
    return 0


def _print_summary(count: int, tier: str, index: dict) -> None:
    print(f"Wiki pages (curated, excl. editorial): {count}")
    print(f"Recommended tier: {tier}")
    print("Thresholds: DIRECT < 50  |  BASE 50–300  |  HYBRID > 300")
    if not index.get("available"):
        print("No active evidence generation; search reads Markdown directly.")
        print("Build one: uv run python scripts/search_memory.py --rebuild")
        return
    print(f"Evidence generation: {index.get('generation_id')} (vectors {index.get('vector_state')})")


if __name__ == "__main__":
    raise SystemExit(main())
