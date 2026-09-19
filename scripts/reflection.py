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
# A rewrite shorter than this is a refusal or a fragment, not a page.
MIN_REFLECTED_WORDS = 40
SUMMARY_PREFIX = "One-sentence summary:"
# The heading of the block this pass appends; the page's live body ends where it begins.
HISTORY_MARKER = "\n## History (pre-reflection"
_UNTOUCHED_FRONTMATTER = ("status: superseded", "status: archived", "type: decision")

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
    content = _reflectable_text(md)
    if content is None:
        return None
    updates = UPDATE_SECTION_RE.findall(_live_body(content))
    if len(updates) < REFLECTION_THRESHOLD:
        return None
    return {
        "path": md,
        "slug": md.stem,
        "title": _title_of(content, md.stem),
        "update_count": len(updates),
    }


def _title_of(content: str, fallback: str) -> str:
    title_match = H1_RE.search(content)
    if title_match is None:
        return fallback
    return title_match.group(1)


def _live_body(content: str) -> str:
    """The page before this pass's own history block: what a reader sees as the page.

    The preserved original inside the block still holds its `## Update` sections; counting
    them would reflect a page again every week. See
    `docs/research/2026-09-17-a-reflection-is-checked-before-it-is-written.md`.
    """
    return content.split(HISTORY_MARKER, 1)[0]


def _reflectable_text(md: Path) -> str | None:
    """The text of a page this pass may rewrite; None otherwise."""
    if md.name in SKIP_NAMES or "archive" in md.parts:
        return None
    try:
        content = _page_source(md).decode("utf-8")
    except (OSError, ValueError):
        # Too large, unstable, or not UTF-8 (`UnicodeDecodeError` is a `ValueError`):
        # the rewriter would refuse it, and one refusal ended the whole weekly loop.
        # Research: docs/research/2026-09-17-a-page-the-rewriter-cannot-read-is-not-a-candidate.md
        return None
    if _never_rewritten(content):
        return None
    return content


def _page_source(md: Path) -> bytes:
    """The one way this pass reads a page: bounded and stable, for finder and rewriter."""
    return read_stable_bytes(md, MAX_REFLECTION_PAGE_BYTES, label="reflection page")


def _never_rewritten(content: str) -> bool:
    """Retired pages, and decisions: "Decisions are immutable: supersede, never edit in place"."""
    frontmatter, _body = _split_frontmatter(content)
    return any(marker in frontmatter for marker in _UNTOUCHED_FRONTMATTER)


def reflect_page(md: Path, apply: bool = False) -> str:
    """Rewrite a page by folding Update sections into the main narrative.

    Returns a summary of what was done (or would be done if dry-run).
    """
    source_bytes = _page_source(md)
    content = source_bytes.decode("utf-8")
    if _never_rewritten(content):
        return f"  {md.stem}: a decision or a retired page is never rewritten, skipping."
    frontmatter, body = _split_frontmatter(content)
    live, earlier = _live_and_earlier(body)
    updates = UPDATE_SECTION_RE.findall(live)
    rewritten, message = _reflection(md, live, len(updates), apply)
    if rewritten is None:
        return message
    page = _reflected_page(md, frontmatter, live, rewritten) + earlier
    encoded = redact_secrets(page).encode("utf-8")
    mutate_knowledge(
        stable_operation_id("reflection", md.relative_to(ROOT).as_posix(), encoded),
        {md: encoded},
        preconditions={
            md.relative_to(ROOT).as_posix(): sha256_bytes(source_bytes)
        },
    )
    return f"  {md.stem}: reflected ({len(updates)} updates integrated)."


def _live_and_earlier(body: str) -> tuple[str, str]:
    """The body a reader sees, and the history blocks of earlier passes, kept as they are."""
    live, marker, earlier = body.partition(HISTORY_MARKER)
    return live, marker + earlier


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
    refused = _why_not_written(body, rewritten or "")
    if refused is not None:
        return None, f"  {md.stem}: rewrite not written — {refused}."
    return rewritten, ""


def _why_not_written(body: str, rewritten: str) -> str | None:
    """Why this reply must not replace the page, or None when it may.

    The weekly pass runs unattended, and a provider's refusal once became the whole body of
    a page. A rewrite has to keep what the page is known by. Research:
    `docs/research/2026-09-17-a-reflection-is-checked-before-it-is-written.md`.
    """
    checks = (
        (len(rewritten.split()) < MIN_REFLECTED_WORDS, "the reply is too short to be a page"),
        (SUMMARY_PREFIX in body and SUMMARY_PREFIX not in rewritten, "the summary line is gone"),
        (bool(UPDATE_SECTION_RE.search(rewritten)), "an update section was left unintegrated"),
        (HISTORY_MARKER.strip() in rewritten, "the reply carries its own history block"),
    )
    return next((reason for failed, reason in checks if failed), None)


def _reflection_prompt(body: str) -> str:
    return f"""You are a knowledge editor. Rewrite the page below by integrating
all Update sections into the main narrative. The result should read as a
single coherent page, not a series of patches.

Rules:
1. PRESERVE all factual claims — do not invent new information.
2. INTEGRATE updates into the main text — don't just concatenate; leave no
   "## Update" section behind.
3. Keep the same title, the "One-sentence summary:" line, and evidence sections.
4. Do NOT add a history section: the original is preserved for you.
5. Target 150-400 words.

=== PAGE TO REWRITE ===
{body}

=== OUTPUT ===
Return the COMPLETE rewritten page body (starting after the H1 title).
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
