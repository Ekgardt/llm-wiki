"""Print the recommended local retrieval mode for the current vault size.

  DIRECT  (< 50 wiki pages)    — read knowledge/index.md + target pages.
  BASE    (50–300 wiki pages)  — use SQLite FTS5 BM25 when direct navigation
                                  is ambiguous.
  HYBRID  (> 300 wiki pages)   — use BM25 + optional local vectors/LanceDB,
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
    return sum(
        1 for p in WIKI.rglob("*.md")
        if p.is_file()
        and p.name not in EDITORIAL_NAMES
        and not any(sp in p.parents for sp in skip_parents)
    )


def tier_for(count: int) -> str:
    for cap, name in TIERS:
        if count < cap:
            return name
    return "HYBRID"


def index_status() -> dict:
    """Inspect the local SQLite FTS5 index without opening it."""
    import os

    info: dict = {"available": False}
    state_root_env = os.environ.get("LLM_WIKI_STATE_ROOT")
    candidates: list[Path] = []
    if state_root_env:
        candidates.append(Path(state_root_env) / "cache" / "index.sqlite")
    candidates.append(ROOT / "cache" / "index.sqlite")
    for c in candidates:
        try:
            if c and c.exists() and c.is_file():
                mtime = datetime.fromtimestamp(c.stat().st_mtime, tz=timezone.utc)
                age_h = (datetime.now(timezone.utc) - mtime).total_seconds() / 3600
                info.update({
                    "available": True,
                    "index_path": str(c),
                    "index_size_mb": round(c.stat().st_size / (1024 * 1024), 2),
                    "index_age_hours": round(age_h, 1),
                    "index_stale": age_h > 24,
                })
                break
        except OSError:
            continue
    return info


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

    print(f"Wiki pages (curated, excl. editorial): {count}")
    print(f"Recommended tier: {tier}")
    print("Thresholds: DIRECT < 50  |  BASE 50–300  |  HYBRID > 300")
    if index.get("available"):
        print(f"FTS5 index: {index.get('index_path')} ({index.get('index_size_mb')} MB)")
        age_h = index.get("index_age_hours")
        if age_h is not None:
            stale = " [STALE >24h]" if index.get("index_stale") else ""
            print(f"FTS5 index age: {age_h} hours{stale}")
        if index.get("index_stale") and tier != "DIRECT":
            print("Tip: run `search_memory.py --rebuild` before querying.")
    else:
        print("FTS5 index not found; search_memory.py will build it on demand.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
