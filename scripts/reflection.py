"""Memory reflection — offline page consolidation (A-MEM evolution pattern).

Runs periodically (weekly via scheduled_weekly.py) to evolve the knowledge
base. Finds pages that have accumulated multiple Update sections and
rewrites them into a clean, integrated narrative — folding the updates
into the main text. Old content is preserved in a ## History section.

This implements the A-MEM "memory evolution" operation (NeurIPS 2025):
historical pages are REWRITTEN as the corpus grows, not just appended to.

Trigger: pages with >= REFLECTION_THRESHOLD Update sections.
Safety: old body is NEVER deleted — moved to ## History.
LLM: one call per page. Content is rewritten from existing text only.

Usage:
    uv run python scripts/reflection.py              # dry-run (show candidates)
    uv run python scripts/reflection.py --apply      # rewrite pages
"""
from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from memory_state import ROOT, atomic_write  # noqa: E402

KNOWLEDGE = ROOT / "knowledge" / "notes"
SKIP_NAMES = {"index.md", "log.md", "README.md", "state.md", "context.md"}

REFLECTION_THRESHOLD = 2  # Minimum Update sections to trigger reflection.

UPDATE_SECTION_RE = re.compile(r"^## Update \(\d{4}-\d{2}-\d{2}\)", re.MULTILINE)
FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?", re.DOTALL)
H1_RE = re.compile(r"^#\s+(.+?)\s*$", re.MULTILINE)


def find_reflection_candidates() -> list[dict]:
    """Find pages with enough Update sections to warrant reflection.

    Returns list of dicts: {path, slug, title, update_count}.
    """
    candidates = []
    if not KNOWLEDGE.exists():
        return candidates

    for md in sorted(KNOWLEDGE.rglob("*.md")):
        if md.name in SKIP_NAMES:
            continue
        if "archive" in md.parts:
            continue
        try:
            content = md.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue

        # Skip superseded/archived pages.
        if "status: superseded" in content or "status: archived" in content:
            continue
        # Skip already-reflected pages (have ## History section).
        if "## History" in content:
            continue

        updates = UPDATE_SECTION_RE.findall(content)
        if len(updates) >= REFLECTION_THRESHOLD:
            title_match = H1_RE.search(content)
            title = title_match.group(1) if title_match else md.stem
            candidates.append({
                "path": md,
                "slug": md.stem,
                "title": title,
                "update_count": len(updates),
            })

    return candidates


def reflect_page(md: Path, apply: bool = False) -> str:
    """Rewrite a page by folding Update sections into the main narrative.

    Returns a summary of what was done (or would be done if dry-run).
    """
    content = md.read_text(encoding="utf-8")
    updates = UPDATE_SECTION_RE.findall(content)
    if len(updates) < REFLECTION_THRESHOLD:
        return f"  {md.stem}: only {len(updates)} updates, skipping."

    # Split content into frontmatter + body.
    fm_match = FRONTMATTER_RE.match(content)
    frontmatter = fm_match.group(0) if fm_match else ""
    body = content[len(frontmatter):]

    # For dry-run, just report.
    if not apply:
        return f"  {md.stem}: {len(updates)} updates, candidate for reflection."

    # Call LLM to rewrite the body.
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    try:
        from llm_client import call_llm
    except ImportError:
        return f"  {md.stem}: llm_client not available."

    prompt = f"""You are a knowledge editor. Rewrite the page below by integrating
all Update sections into the main narrative. The result should read as a
single coherent page, not a series of patches.

Rules:
1. PRESERVE all factual claims — do not invent new information.
2. INTEGRATE updates into the main text — don't just concatenate.
3. Move the OLD body (before your rewrite) into a ## History section.
4. Keep the same title, summary, and evidence sections.
5. Target 150-400 words for the main content (excluding History).

=== PAGE TO REWRITE ===
{body}

=== OUTPUT ===
Return the COMPLETE rewritten page body (starting after the H1 title).
Include a ## History section at the end with the original body.
Return ONLY the rewritten markdown — no commentary.
"""
    system = "You are a knowledge consolidation engine. Output markdown only."
    rewritten = call_llm(prompt, system, max_tokens=3000)

    if not rewritten or not rewritten.strip():
        return f"  {md.stem}: LLM returned empty response."

    # Build the new page: frontmatter + rewritten body.
    # The rewritten body should start with the H1 title.
    if not rewritten.strip().startswith("# "):
        # Extract title from original and prepend.
        title_match = H1_RE.search(body)
        title = title_match.group(0) if title_match else f"# {md.stem}"
        rewritten = f"{title}\n\n{rewritten}"

    new_content = frontmatter + rewritten.rstrip() + "\n"

    # Add History section with original body.
    now = datetime.now().strftime("%Y-%m-%d")
    history_header = f"\n\n## History (pre-reflection {now})\n"
    # Find where the rewritten content ends (before any existing History).
    history_body = body  # The original full body.
    new_content += f"{history_header}<details>\n<summary>Original page before reflection</summary>\n\n{history_body}\n\n</details>\n"

    atomic_write(md, new_content)
    return f"  {md.stem}: reflected ({len(updates)} updates integrated)."


def main() -> int:
    p = argparse.ArgumentParser(description="Memory reflection — page consolidation.")
    p.add_argument("--apply", action="store_true", help="Actually rewrite pages (default: dry-run).")
    args = p.parse_args()

    candidates = find_reflection_candidates()
    if not candidates:
        print("No reflection candidates found. All pages are clean.")
        return 0

    print(f"Found {len(candidates)} reflection candidate(s):\n")
    for c in candidates:
        print(f"  {c['slug']}: {c['update_count']} update sections")

    if not args.apply:
        print("\nDry-run. Use --apply to rewrite.")
        return 0

    print("\nReflecting...\n")
    for c in candidates:
        result = reflect_page(c["path"], apply=True)
        print(result)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
