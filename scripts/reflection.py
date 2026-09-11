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
from bounded_io import read_stable_bytes  # noqa: E402
from markdown_transaction import mutate_knowledge, stable_operation_id  # noqa: E402
from memory_state import ROOT  # noqa: E402
from reliable_memory import sha256_bytes  # noqa: E402
from secret_redact import redact_secrets  # noqa: E402

KNOWLEDGE = ROOT / "knowledge" / "notes"
SKIP_NAMES = {"index.md", "log.md", "README.md", "state.md", "context.md"}

REFLECTION_THRESHOLD = 2  # Minimum Update sections to trigger reflection.
MAX_REFLECTION_PAGE_BYTES = 16 * 1024 * 1024

UPDATE_SECTION_RE = re.compile(r"^## Update \(\d{4}-\d{2}-\d{2}\)", re.MULTILINE)
FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?", re.DOTALL)
H1_RE = re.compile(r"^#\s+(.+?)\s*$", re.MULTILINE)


def find_reflection_candidates() -> list[dict]:
    """Find pages with enough Update sections to warrant reflection.

    Returns list of dicts: {path, slug, title, update_count}.
    """
    if not KNOWLEDGE.exists():
        return []
    candidates = (_reflection_candidate(md) for md in sorted(KNOWLEDGE.rglob("*.md")))
    return [candidate for candidate in candidates if candidate is not None]


def _reflection_candidate(md: Path) -> dict | None:
    content = _unreflected_text(md)
    if content is None:
        return None
    updates = UPDATE_SECTION_RE.findall(content)
    if len(updates) < REFLECTION_THRESHOLD:
        return None
    title_match = H1_RE.search(content)
    return {
        "path": md,
        "slug": md.stem,
        "title": title_match.group(1) if title_match else md.stem,
        "update_count": len(updates),
    }


def _unreflected_text(md: Path) -> str | None:
    """The text of an active page that has not been reflected yet; None otherwise."""
    if md.name in SKIP_NAMES or "archive" in md.parts:
        return None
    try:
        content = md.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None
    if _retired_or_reflected(content):
        return None
    return content


def _retired_or_reflected(content: str) -> bool:
    # Superseded/archived pages, and pages that already carry a ## History section.
    return "status: superseded" in content or "status: archived" in content or "## History" in content


def reflect_page(md: Path, apply: bool = False) -> str:
    """Rewrite a page by folding Update sections into the main narrative.

    Returns a summary of what was done (or would be done if dry-run).
    """
    source_bytes = read_stable_bytes(
        md, MAX_REFLECTION_PAGE_BYTES, label="reflection page"
    )
    content = source_bytes.decode("utf-8")
    updates = UPDATE_SECTION_RE.findall(content)
    frontmatter, body = _split_frontmatter(content)
    rewritten, message = _reflection(md, body, len(updates), apply)
    if rewritten is None:
        return message
    encoded = redact_secrets(_reflected_page(md, frontmatter, body, rewritten)).encode("utf-8")
    mutate_knowledge(
        stable_operation_id("reflection", md.relative_to(ROOT).as_posix(), encoded),
        {md: encoded},
        preconditions={
            md.relative_to(ROOT).as_posix(): sha256_bytes(source_bytes)
        },
    )
    return f"  {md.stem}: reflected ({len(updates)} updates integrated)."


def _split_frontmatter(content: str) -> tuple[str, str]:
    fm_match = FRONTMATTER_RE.match(content)
    frontmatter = fm_match.group(0) if fm_match else ""
    return frontmatter, content[len(frontmatter):]


def _reflection(md: Path, body: str, update_count: int, apply: bool) -> tuple[str | None, str]:
    """(rewritten body, "") when there is one to write; else (None, the line saying why not)."""
    if update_count < REFLECTION_THRESHOLD:
        return None, f"  {md.stem}: only {update_count} updates, skipping."
    # For dry-run, just report.
    if not apply:
        return None, f"  {md.stem}: {update_count} updates, candidate for reflection."
    return _llm_reflection(md, body)


def _llm_reflection(md: Path, body: str) -> tuple[str | None, str]:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    try:
        from llm_client import call_llm
    except ImportError:
        return None, f"  {md.stem}: llm_client not available."
    system = "You are a knowledge consolidation engine. Output markdown only."
    rewritten = call_llm(_reflection_prompt(body), system, max_tokens=3000)
    if not rewritten or not rewritten.strip():
        return None, f"  {md.stem}: LLM returned empty response."
    return rewritten, ""


def _reflection_prompt(body: str) -> str:
    return f"""You are a knowledge editor. Rewrite the page below by integrating
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


def _reflected_page(md: Path, frontmatter: str, body: str, rewritten: str) -> str:
    """Frontmatter, the titled rewrite, then the original body under a dated History section."""
    new_content = frontmatter + _titled(rewritten, body, md.stem).rstrip() + "\n"
    now = datetime.now().strftime("%Y-%m-%d")
    history_header = f"\n\n## History (pre-reflection {now})\n"
    return new_content + f"{history_header}<details>\n<summary>Original page before reflection</summary>\n\n{body}\n\n</details>\n"


def _titled(rewritten: str, body: str, stem: str) -> str:
    """The rewritten body should start with the H1 title; take it from the original if not."""
    if rewritten.strip().startswith("# "):
        return rewritten
    title_match = H1_RE.search(body)
    title = title_match.group(0) if title_match else f"# {stem}"
    return f"{title}\n\n{rewritten}"


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
