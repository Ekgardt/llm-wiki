"""Access tracking for knowledge pages — records retrieval analytics.

Tracks how often each page is accessed (search hits, advisory injection,
direct reads). This data powers:
- Forgetting curve (Ebbinghaus decay): pages with no access decay over time.
- Quality scoring: frequently-accessed pages are validated as useful.
- Advisory ranking: recently-accessed pages get boost in SessionStart.

The access log is stored in:
1. cache/access_log.jsonl (always) — append-only JSONL, survives restarts.

Frontmatter fields updated on page files:
- access_count: int (how many times accessed)
- last_accessed: ISO timestamp (when last accessed)

These are low-frequency updates (batched, not per-query) to avoid
frontmatter churn in git diffs.
"""
from __future__ import annotations

import json
import re
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from markdown_transaction import mutate_knowledge, stable_operation_id  # noqa: E402
from memory_state import ROOT, STATE_ROOT  # noqa: E402

KNOWLEDGE_DIR = ROOT / "knowledge" / "notes"
ACCESS_LOG_FILE = STATE_ROOT / "cache" / "access_log.jsonl"
BATCH_THRESHOLD = 5  # Flush frontmatter after N accesses per page.

# In-memory batch: slug → count. Flushed to frontmatter periodically.
_batch: dict[str, int] = {}

FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?", re.DOTALL)


def record_access(slug: str, source: str = "search", query: str | None = None,
                  rank: int | None = None) -> None:
    """Record a page access. In-memory batch only (instant, no I/O).

    JSONL logging and frontmatter flush happen in scheduled_nightly.py.
    This keeps search latency unaffected by access tracking.

    Args:
        slug: The page slug (filename without .md).
        source: 'search' | 'session-start' | 'compile' | 'direct'.
        query: The search query that surfaced this page (if applicable).
        rank: The position this page was shown at (if applicable).
    """
    # In-memory batch only — no disk I/O during search.
    _batch[slug] = _batch.get(slug, 0) + 1


def flush_access_to_frontmatter(slug: str | None = None) -> int:
    """Flush batched access counts to page frontmatter.

    Args:
        slug: If provided, flush only this page. If None, flush all pending.

    Returns:
        Number of pages updated.
    """
    updated = 0
    slugs = [slug] if slug else list(_batch.keys())

    for s in slugs:
        count = _batch.get(s, 0)
        if count <= 0:
            continue

        page_path = KNOWLEDGE_DIR / f"{s}.md"
        if not page_path.exists():
            continue

        try:
            content = page_path.read_text(encoding="utf-8")
            now = datetime.now().isoformat(timespec="seconds")

            fm_match = FRONTMATTER_RE.match(content)
            if fm_match:
                fm = fm_match.group(1)
                # Update or add access_count.
                if re.search(r"^access_count:\s*\d+", fm, re.MULTILINE):
                    existing = int(
                        re.search(r"^access_count:\s*(\d+)", fm, re.MULTILINE).group(1)
                    )
                    fm = re.sub(
                        r"^access_count:\s*\d+.*$",
                        f"access_count: {existing + count}",
                        fm,
                        count=1,
                        flags=re.MULTILINE,
                    )
                else:
                    fm += f"\naccess_count: {count}"

                # Update or add last_accessed.
                if re.search(r"^last_accessed:", fm, re.MULTILINE):
                    fm = re.sub(
                        r"^last_accessed:.*$",
                        f"last_accessed: {now}",
                        fm,
                        count=1,
                        flags=re.MULTILINE,
                    )
                else:
                    fm += f"\nlast_accessed: {now}"

                new_content = f"---\n{fm}\n---\n" + content[fm_match.end():]
            else:
                # No frontmatter — add one.
                new_content = (
                    f"---\naccess_count: {count}\nlast_accessed: {now}\n---\n\n{content}"
                )

            encoded = new_content.encode("utf-8")
            mutate_knowledge(
                stable_operation_id("access", f"{s}:{now}", encoded),
                {page_path: encoded},
            )
            _batch.pop(s, None)
            updated += 1
        except Exception:
            continue

    return updated


def flush_all() -> int:
    """Flush all pending access counts to JSONL + frontmatter.

    Called by scheduled_nightly.py. Writes JSONL entries for batched
    accesses, then updates frontmatter for pages above threshold.
    """
    # Write JSONL entries for all batched accesses.
    ACCESS_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    now = datetime.now().isoformat(timespec="seconds")
    try:
        with open(ACCESS_LOG_FILE, "a", encoding="utf-8") as f:
            for slug, count in _batch.items():
                for _ in range(count):
                    f.write(json.dumps({
                        "slug": slug, "source": "search",
                        "timestamp": now,
                    }) + "\n")
    except Exception:
        pass

    # Flush to frontmatter.
    return flush_access_to_frontmatter(None)


def get_access_stats(slug: str) -> dict:
    """Get access statistics for a page from the JSONL log.

    Returns:
        Dict with: total_count, last_accessed, sources (dict).
    """
    if not ACCESS_LOG_FILE.exists():
        return {"total_count": 0, "last_accessed": None, "sources": {}}

    total = 0
    last_ts = None
    sources: dict[str, int] = {}

    try:
        with open(ACCESS_LOG_FILE, encoding="utf-8") as f:
            for line in f:
                try:
                    entry = json.loads(line.strip())
                    if entry.get("slug") == slug:
                        total += 1
                        ts = entry.get("timestamp", "")
                        if ts and (last_ts is None or ts > last_ts):
                            last_ts = ts
                        src = entry.get("source", "unknown")
                        sources[src] = sources.get(src, 0) + 1
                except json.JSONDecodeError:
                    continue
    except OSError:
        pass

    return {"total_count": total, "last_accessed": last_ts, "sources": sources}


def decay_score(slug: str, page_type: str = "concept",
                confidence: str = "medium") -> float:
    """Calculate Ebbinghaus-inspired decay score for a page.

    Score 0.0-1.0. Higher = more relevant/alive. Low scores are archive candidates.

    Formula: base_importance * exp(-delta_t / half_life) + reinforcement * access_count

    - base_importance: from confidence (high=1.0, medium=0.7, low=0.4)
    - delta_t: days since last access (or creation if never accessed)
    - half_life: per type (debugging=30d, pattern=90d, concept=365d, decision=inf)
    - reinforcement: access_count * 0.05 (capped at 0.3)
    """
    import math

    base_map = {"high": 1.0, "medium": 0.7, "low": 0.4}
    base = base_map.get(confidence, 0.7)

    half_life_map = {
        "debugging": 30,
        "pattern": 90,
        "gap": 60,
        "qa": 180,
        "concept": 365,
        "decision": 99999,  # effectively never decay
        "entity": 99999,
        "synthesis": 365,
    }
    half_life = half_life_map.get(page_type, 180)

    stats = get_access_stats(slug)
    access_count = stats["total_count"]
    last_accessed = stats["last_accessed"]

    if last_accessed:
        try:
            last_dt = datetime.fromisoformat(last_accessed)
            delta_days = max(0, (datetime.now() - last_dt).days)
        except (ValueError, TypeError):
            delta_days = 0
    else:
        delta_days = 0  # Never accessed — treat as "just created" for decay

    # Decay component.
    decay = base * math.exp(-delta_days / half_life) if half_life < 99999 else base

    # Reinforcement from access.
    reinforcement = min(0.3, access_count * 0.05)

    return round(min(1.0, decay + reinforcement), 4)


def main() -> int:
    """CLI: show access stats or flush pending."""
    import argparse
    p = argparse.ArgumentParser(description="Access tracking for knowledge pages.")
    p.add_argument("--flush", action="store_true", help="Flush pending frontmatter updates.")
    p.add_argument("--stats", type=str, default=None, help="Show stats for a slug.")
    p.add_argument("--decay", type=str, default=None, help="Show decay score for a slug.")
    args = p.parse_args()

    if args.flush:
        n = flush_all()
        print(f"Flushed access counts to {n} page(s).")
        return 0

    if args.stats:
        stats = get_access_stats(args.stats)
        print(json.dumps(stats, indent=2))
        return 0

    if args.decay:
        score = decay_score(args.decay)
        print(f"Decay score for {args.decay}: {score}")
        return 0

    print(f"Pending frontmatter updates: {sum(_batch.values())}")
    print(f"Access log: {ACCESS_LOG_FILE}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
