"""L0/L1/L2 tiered knowledge loading — progressive disclosure.

Generates multi-level summaries for each knowledge page so agents can
load the right amount of context without overpaying for tokens.

Levels (OpenViking model):
- L0: one-sentence summary (~100 tokens) — quick relevance check
- L1: structured overview (~500-1000 tokens) — planning decisions
- L2: full page content — deep reading (already exists in Markdown)

L0 already exists as the "One-sentence summary:" line in each page.
This module generates L1 overviews via LLM, cached in cache/tiers/.

The SessionStart advisory (build_advisory.py) can use L0 to decide
which pages to inject, then pull L1 for the top candidates, and only
read L2 (full page) when truly needed — cutting token usage 50-90%.

Usage:
    uv run python scripts/build_tiers.py              # generate L1 for all pages
    uv run python scripts/build_tiers.py --slug auth  # generate for one page
    uv run python scripts/build_tiers.py --status     # show cache stats
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from memory_state import ROOT, STATE_ROOT, atomic_write  # noqa: E402

KNOWLEDGE_DIR = ROOT / "knowledge" / "notes"
TIERS_DIR = STATE_ROOT / "cache" / "tiers"
SKIP_NAMES = {"index.md", "log.md", "README.md", "state.md", "context.md"}

FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?", re.DOTALL)
H1_RE = re.compile(r"^#\s+(.+?)\s*$", re.MULTILINE)
SUMMARY_RE = re.compile(
    r"^One-sentence summary:\s*(.+?)\s*$", re.MULTILINE | re.IGNORECASE
)
STATUS_RE = re.compile(r"^status:\s*(.+?)\s*$", re.MULTILINE)


def get_l0(slug: str) -> str:
    """Get L0 (one-sentence summary) for a page. ~100 tokens.

    Reads from the page's 'One-sentence summary:' line.
    Falls back to first sentence of body or slug.
    """
    page_path = KNOWLEDGE_DIR / f"{slug}.md"
    if not page_path.exists():
        return ""

    content = page_path.read_text(encoding="utf-8", errors="ignore")
    body = FRONTMATTER_RE.sub("", content, count=1)

    m = SUMMARY_RE.search(body)
    if m:
        return m.group(1).strip()

    # Fallback: first sentence after H1.
    lines = body.splitlines()
    for line in lines[1:]:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and not stripped.startswith("---"):
            return stripped[:200]

    return slug.replace("-", " ")


def get_l1(slug: str) -> str | None:
    """Get L1 (structured overview) for a page. ~500-1000 tokens.

    Reads from cache/tiers/<slug>.l1.md. Returns None if not generated.
    """
    l1_path = TIERS_DIR / f"{slug}.l1.md"
    if not l1_path.exists():
        return None
    return l1_path.read_text(encoding="utf-8", errors="ignore")


def get_l2(slug: str) -> str:
    """Get L2 (full page content). Just reads the markdown file."""
    page_path = KNOWLEDGE_DIR / f"{slug}.md"
    if not page_path.exists():
        return ""
    return page_path.read_text(encoding="utf-8", errors="ignore")


def _needs_l1_regeneration(slug: str, page_path: Path) -> bool:
    """Check if L1 needs to be (re)generated for this page."""
    l1_path = TIERS_DIR / f"{slug}.l1.md"
    if not l1_path.exists():
        return True
    # Check if page changed since L1 was generated.
    try:
        return page_path.stat().st_mtime > l1_path.stat().st_mtime
    except OSError:
        return True


def generate_l1(slug: str, use_llm: bool = True) -> str | None:
    """Generate L1 overview for a page.

    If use_llm=True and LLM available: LLM generates a structured overview.
    If use_llm=False or no LLM: deterministic extraction (first N paragraphs).
    """
    page_path = KNOWLEDGE_DIR / f"{slug}.md"
    if not page_path.exists():
        return None

    content = page_path.read_text(encoding="utf-8", errors="ignore")
    body = FRONTMATTER_RE.sub("", content, count=1)
    l0 = get_l0(slug)

    if not use_llm or os_env_fake():
        return _deterministic_l1(slug, body, l0)

    # LLM-based L1 generation.
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    try:
        from llm_client import call_llm
    except ImportError:
        return _deterministic_l1(slug, body, l0)

    # Skip for fake provider (tests).
    if os_env_fake():
        return _deterministic_l1(slug, body, l0)

    prompt = f"""Summarize this knowledge page into a structured overview.
Keep it under 500 words. Include:
- Key points (bulleted)
- Important decisions or constraints
- Links to related concepts

=== PAGE ===
{body[:3000]}

=== OUTPUT ===
Return ONLY the overview markdown (no title, no commentary).
"""

    result = call_llm(prompt, "You are a knowledge summarizer.", max_tokens=1000)
    if not result or not result.strip():
        return _deterministic_l1(slug, body, l0)

    return result.strip()


def _deterministic_l1(slug: str, body: str, l0: str) -> str:
    """Generate L1 without LLM — extract first sections."""
    lines = body.splitlines()
    overview_lines = [l0, ""]
    char_count = len(l0)

    for line in lines[1:]:  # Skip H1
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("## History"):
            break  # Stop at history section
        # Check limit BEFORE adding.
        if char_count + len(line) >= 2000:
            overview_lines.append("\n...(truncated, see full page for more)")
            break
        if stripped.startswith("## "):
            overview_lines.append("")
            overview_lines.append(stripped)
            char_count += len(stripped)
        else:
            overview_lines.append(line)
            char_count += len(line)

    return "\n".join(overview_lines)


def os_env_fake() -> bool:
    """Check if running with fake LLM provider (tests)."""
    import os
    return os.environ.get("MEMORY_LLM_PROVIDER", "").lower() == "fake"


def build_all_tiers(use_llm: bool = True, verbose: bool = True) -> dict:
    """Generate L1 overviews for all pages that need it.

    Returns stats: {generated, skipped, errors}
    """
    TIERS_DIR.mkdir(parents=True, exist_ok=True)

    stats = {"generated": 0, "skipped": 0, "errors": 0}

    if not KNOWLEDGE_DIR.exists():
        return stats

    for md in sorted(KNOWLEDGE_DIR.rglob("*.md")):
        if md.name in SKIP_NAMES or "archive" in md.parts:
            continue

        # Skip superseded pages.
        try:
            content = md.read_text(encoding="utf-8", errors="ignore")
            if "status: superseded" in content or "status: archived" in content:
                stats["skipped"] += 1
                continue
        except OSError:
            stats["errors"] += 1
            continue

        slug = md.stem

        if not _needs_l1_regeneration(slug, md):
            stats["skipped"] += 1
            continue

        try:
            l1 = generate_l1(slug, use_llm=use_llm)
            if l1:
                l1_path = TIERS_DIR / f"{slug}.l1.md"
                atomic_write(l1_path, l1)
                stats["generated"] += 1
                if verbose:
                    print(f"  Generated L1: {slug}")
            else:
                stats["errors"] += 1
        except Exception:
            stats["errors"] += 1

    if verbose:
        print(f"\nL1 tier generation: {stats['generated']} generated, "
              f"{stats['skipped']} skipped, {stats['errors']} errors.")

    return stats


def get_tier(slug: str, level: str = "auto") -> dict:
    """Get content at the specified tier level.

    Args:
        slug: Page slug.
        level: 'l0', 'l1', 'l2', or 'auto' (returns l0 + l1 if available).

    Returns:
        Dict with level, content, and available levels.
    """
    l0 = get_l0(slug)
    l1 = get_l1(slug)
    l2 = get_l2(slug)

    if level == "l0":
        return {"level": "l0", "content": l0, "available": ["l0"]}
    elif level == "l1":
        return {"level": "l1", "content": l1 or l0, "available": ["l0"] + (["l1"] if l1 else [])}
    elif level == "l2":
        return {"level": "l2", "content": l2, "available": ["l0"] + (["l1"] if l1 else []) + ["l2"]}
    else:  # auto
        content = l0
        if l1:
            content = l1
        return {
            "level": "l1" if l1 else "l0",
            "content": content,
            "l0": l0,
            "available": ["l0"] + (["l1"] if l1 else []) + (["l2"] if l2 else []),
        }


def main() -> int:
    import argparse
    p = argparse.ArgumentParser(description="L0/L1/L2 tiered knowledge loading.")
    p.add_argument("--slug", type=str, default=None, help="Generate L1 for one page.")
    p.add_argument("--all", action="store_true", help="Generate L1 for all pages.")
    p.add_argument("--no-llm", action="store_true", help="Use deterministic extraction (no LLM).")
    p.add_argument("--status", action="store_true", help="Show cache statistics.")
    p.add_argument("--get", type=str, default=None, help="Get content at tier level.")
    args = p.parse_args()

    if args.status:
        if not TIERS_DIR.exists():
            print("No L1 cache. Run --all to generate.")
            return 0
        l1_files = list(TIERS_DIR.glob("*.l1.md"))
        pages = list(KNOWLEDGE_DIR.rglob("*.md")) if KNOWLEDGE_DIR.exists() else []
        page_count = sum(1 for p in pages if p.name not in SKIP_NAMES and "archive" not in p.parts)
        print(f"L1 cache: {len(l1_files)} / {page_count} pages")
        return 0

    if args.slug:
        l1 = generate_l1(args.slug, use_llm=not args.no_llm)
        if l1:
            l1_path = TIERS_DIR / f"{args.slug}.l1.md"
            TIERS_DIR.mkdir(parents=True, exist_ok=True)
            atomic_write(l1_path, l1)
            print(f"Generated L1 for {args.slug}: {len(l1)} chars.")
        else:
            print(f"Failed to generate L1 for {args.slug}.")
        return 0

    if args.get:
        result = get_tier(args.get)
        print(f"Level: {result['level']}")
        print(f"Available: {result['available']}")
        print(f"Content ({len(result['content'])} chars):")
        print(result['content'][:500])
        return 0

    if args.all or True:  # Default: build all
        build_all_tiers(use_llm=not args.no_llm)
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
