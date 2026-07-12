"""Contextual Retrieval — prepend LLM-generated context to pages before indexing.

Anthropic technique (provider-agnostic): for each page, generate a one-line
context that disambiguates it. This context is added to the FTS5 index,
making search more precise (-49% retrieval failures per Anthropic data).

Example:
  Page: "# Auth Decision"
  Context: "This page is about choosing JWT over sessions for the
            llm-wiki project, decided March 2026."

The context is stored in cache/contextual/ and merged into the search
index at build time. No changes to Markdown source files.

Usage:
    uv run python scripts/contextual_retrieval.py --all     # generate for all
    uv run python scripts/contextual_retrieval.py --status   # show stats
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from memory_state import ROOT, STATE_ROOT, atomic_write  # noqa: E402

KNOWLEDGE_DIR = ROOT / "knowledge" / "notes"
CONTEXT_DIR = STATE_ROOT / "cache" / "contextual"
SKIP_NAMES = {"index.md", "log.md", "README.md", "state.md", "context.md"}

FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?", re.DOTALL)
H1_RE = re.compile(r"^#\s+(.+?)\s*$", re.MULTILINE)
SUMMARY_RE = re.compile(
    r"^One-sentence summary:\s*(.+?)\s*$", re.MULTILINE | re.IGNORECASE
)
PROJECT_RE = re.compile(r"^project:\s*[\"']?([^\"'\n]+)[\"']?\s*$", re.MULTILINE)
TYPE_RE = re.compile(r"^type:\s*(.+?)\s*$", re.MULTILINE)


def get_context(slug: str) -> str | None:
    """Get cached contextual prefix for a page. Returns None if not generated."""
    ctx_file = CONTEXT_DIR / f"{slug}.ctx"
    if not ctx_file.exists():
        return None
    return ctx_file.read_text(encoding="utf-8", errors="ignore").strip()


def generate_context(slug: str, use_llm: bool = True) -> str:
    """Generate a one-line context for a page.

    If use_llm and LLM available: LLM generates context.
    Otherwise: deterministic extraction from title + summary + project.
    """
    page_path = KNOWLEDGE_DIR / f"{slug}.md"
    if not page_path.exists():
        return ""

    content = page_path.read_text(encoding="utf-8", errors="ignore")
    body = FRONTMATTER_RE.sub("", content, count=1)

    title_match = H1_RE.search(body)
    title = title_match.group(1).strip() if title_match else slug

    summary_match = SUMMARY_RE.search(body)
    summary = summary_match.group(1).strip() if summary_match else ""

    project = ""
    type_val = ""
    fm = FRONTMATTER_RE.match(content)
    if fm:
        proj_m = PROJECT_RE.search(fm.group(1))
        project = proj_m.group(1).strip() if proj_m else ""
        type_m = TYPE_RE.search(fm.group(1))
        type_val = type_m.group(1).strip() if type_m else ""

    # Deterministic fallback (no LLM).
    if not use_llm or os.environ.get("MEMORY_LLM_PROVIDER", "").lower() == "fake":
        parts = []
        if project:
            parts.append(f"Project: {project}.")
        if type_val:
            parts.append(f"Type: {type_val}.")
        parts.append(f"Topic: {title}.")
        if summary:
            parts.append(summary)
        return " ".join(parts)

    # LLM-based context generation.
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    try:
        from llm_client import call_llm
    except ImportError:
        return f"Topic: {title}. {summary}"

    prompt = f"""Generate ONE sentence of context for this knowledge page.
The context should disambiguate the page — what project, what topic,
what decision. Keep it under 100 characters.

Title: {title}
Summary: {summary}
Project: {project}
Type: {type_val}

Return ONLY the context sentence. No preamble."""

    result = call_llm(prompt, "You are a context generator.", max_tokens=100)
    if result and result.strip():
        return result.strip()

    return f"Topic: {title}. {summary}"


def build_all_contexts(use_llm: bool = True, verbose: bool = True) -> dict:
    """Generate contexts for all pages that need them."""
    CONTEXT_DIR.mkdir(parents=True, exist_ok=True)
    stats = {"generated": 0, "skipped": 0, "errors": 0}

    if not KNOWLEDGE_DIR.exists():
        return stats

    for md in sorted(KNOWLEDGE_DIR.rglob("*.md")):
        if md.name in SKIP_NAMES or "archive" in md.parts:
            continue

        content = md.read_text(encoding="utf-8", errors="ignore")
        if "status: superseded" in content or "status: archived" in content:
            stats["skipped"] += 1
            continue

        slug = md.stem
        ctx_file = CONTEXT_DIR / f"{slug}.ctx"

        # Skip if context exists and page hasn't changed.
        if ctx_file.exists():
            try:
                if md.stat().st_mtime <= ctx_file.stat().st_mtime:
                    stats["skipped"] += 1
                    continue
            except OSError:
                pass

        try:
            ctx = generate_context(slug, use_llm=use_llm)
            if ctx:
                atomic_write(ctx_file, ctx)
                stats["generated"] += 1
                if verbose:
                    print(f"  Generated context: {slug}")
            else:
                stats["errors"] += 1
        except Exception:
            stats["errors"] += 1

    if verbose:
        print(f"\nContext generation: {stats['generated']} generated, "
              f"{stats['skipped']} skipped, {stats['errors']} errors.")
    return stats


def main() -> int:
    import argparse
    p = argparse.ArgumentParser(description="Contextual Retrieval — page context generation.")
    p.add_argument("--all", action="store_true", help="Generate for all pages.")
    p.add_argument("--slug", type=str, default=None, help="Generate for one page.")
    p.add_argument("--no-llm", action="store_true", help="Use deterministic extraction.")
    p.add_argument("--status", action="store_true", help="Show cache stats.")
    args = p.parse_args()

    if args.status:
        if not CONTEXT_DIR.exists():
            print("No context cache. Run --all to generate.")
            return 0
        ctx_files = list(CONTEXT_DIR.glob("*.ctx"))
        print(f"Context cache: {len(ctx_files)} pages")
        return 0

    if args.slug:
        ctx = generate_context(args.slug, use_llm=not args.no_llm)
        print(ctx)
        return 0

    build_all_contexts(use_llm=not args.no_llm)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
