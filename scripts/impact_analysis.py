"""LINK Layer — connects code changes to wiki knowledge pages.

When code changes, this module identifies which wiki pages might become
stale because they reference the changed code. This is the unique feature
that connects the code graph to the knowledge graph — no other system
in the world does this.

Usage:
    # Check what wiki pages are affected by uncommitted changes:
    uv run python scripts/impact_analysis.py

    # Check what's affected by a specific commit range:
    uv run python scripts/impact_analysis.py --range main..HEAD

    # Output for SessionStart advisory (machine-readable):
    uv run python scripts/impact_analysis.py --json

The module works by:
1. Getting changed files from git diff
2. Extracting symbol names (functions, classes) from changed files
3. Searching wiki pages for mentions of those symbols
4. Returning potentially stale pages with confidence levels
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from memory_state import ROOT  # noqa: E402

KNOWLEDGE_DIR = ROOT / "knowledge" / "notes"
SKIP_NAMES = {"index.md", "log.md", "README.md", "state.md", "context.md"}


def get_changed_files(git_range: str | None = None) -> list[str]:
    """Get list of changed files from git diff.

    Args:
        git_range: e.g. "main..HEAD" or None for uncommitted changes.
    """
    try:
        if git_range:
            cmd = ["git", "diff", "--name-only", git_range]
        else:
            cmd = ["git", "diff", "--name-only"]
        result = subprocess.run(
            cmd, capture_output=True, text=True, cwd=str(ROOT),
        )
        if result.returncode != 0:
            # Try staged changes
            result = subprocess.run(
                ["git", "diff", "--name-only", "--cached"],
                capture_output=True, text=True, cwd=str(ROOT),
            )
        files = [f.strip() for f in result.stdout.splitlines() if f.strip()]
        return files
    except Exception:
        return []


def extract_symbols_from_file(file_path: Path) -> list[str]:
    """Extract function/class names from a source file.

    Uses code_graph module if available, falls back to regex.
    """
    if not file_path.exists():
        return []

    try:
        from code_graph import parse_file
        result = parse_file(file_path)
        symbols = []
        symbols.extend(f["name"] for f in result.get("functions", []))
        symbols.extend(c["name"] for c in result.get("classes", []))
        return list(set(symbols))  # Deduplicate
    except ImportError:
        pass

    # Regex fallback: extract def/class/function names.
    content = file_path.read_text(encoding="utf-8", errors="ignore")
    symbols = set()
    for m in re.finditer(r"\bdef\s+(\w+)", content):
        symbols.add(m.group(1))
    for m in re.finditer(r"\bclass\s+(\w+)", content):
        symbols.add(m.group(1))
    for m in re.finditer(r"\bfunction\s+(\w+)", content):
        symbols.add(m.group(1))
    return list(symbols)


def find_stale_wiki_pages(changed_symbols: list[str]) -> list[dict]:
    """Find wiki pages that mention any of the changed symbols.

    Returns list of:
        {slug, matched_symbols, confidence, reason}
    """
    if not changed_symbols or not KNOWLEDGE_DIR.exists():
        return []

    results = []

    for md in sorted(KNOWLEDGE_DIR.rglob("*.md")):
        if md.name in SKIP_NAMES or "archive" in md.parts:
            continue
        try:
            content = md.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue

        # Skip superseded pages.
        if "status: superseded" in content:
            continue

        # Check for symbol mentions.
        matched = []
        for symbol in changed_symbols:
            # Word boundary match (avoid partial matches).
            if re.search(r"\b" + re.escape(symbol) + r"\b", content):
                matched.append(symbol)

        if matched:
            # Confidence: high if many symbols matched, medium if few.
            if len(matched) >= 3:
                confidence = "high"
            elif len(matched) >= 1:
                confidence = "medium"

            try:
                rel_path = str(md.relative_to(ROOT))
            except ValueError:
                rel_path = str(md)

            results.append({
                "slug": md.stem,
                "path": rel_path,
                "matched_symbols": matched,
                "confidence": confidence,
                "reason": f"mentions {len(matched)} changed symbol(s): {', '.join(matched[:5])}",
            })

    # Sort by confidence (high first) then by number of matched symbols.
    results.sort(key=lambda x: (0 if x["confidence"] == "high" else 1, -len(x["matched_symbols"])))
    return results


def apply_significance_budget(pages: list[dict], threshold: float = 0.8) -> list[dict]:
    """Pareto 80% cover — return only pages covering threshold% of total impact.

    Sorts pages by number of matched symbols (descending), accumulates
    until threshold is reached, returns only those. Prevents information
    overload when many pages are affected.

    (Memtrace pattern: "surface the minimum set covering ≥80% of significance".)
    """
    if not pages or len(pages) <= 5:
        return pages

    total = sum(len(p.get("matched_symbols", [])) for p in pages)
    if total == 0:
        return pages

    sorted_pages = sorted(
        pages,
        key=lambda p: len(p.get("matched_symbols", [])),
        reverse=True,
    )

    cumulative = 0
    result = []
    for p in sorted_pages:
        count = len(p.get("matched_symbols", []))
        cumulative += count
        result.append(p)
        if cumulative / total >= threshold:
            break

    return result


def analyze_impact(git_range: str | None = None) -> dict:
    """Full impact analysis: git diff → changed symbols → stale wiki pages.

    Returns:
        {
            changed_files: list[str],
            changed_symbols: list[str],
            stale_pages: list[dict],
            summary: str,
        }
    """
    changed_files = get_changed_files(git_range)

    # Filter to code files only.
    code_extensions = {".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".rs", ".java", ".rb"}
    code_files = [ROOT / f for f in changed_files if Path(f).suffix in code_extensions]

    # Extract symbols from changed files.
    all_symbols = set()
    for cf in code_files:
        if cf.exists():
            symbols = extract_symbols_from_file(cf)
            all_symbols.update(symbols)

    # Find stale wiki pages (with significance budgeting).
    stale_pages = find_stale_wiki_pages(list(all_symbols))
    stale_pages = apply_significance_budget(stale_pages)

    summary = (
        f"{len(changed_files)} file(s) changed, "
        f"{len(all_symbols)} symbol(s) affected, "
        f"{len(stale_pages)} wiki page(s) potentially stale."
    )

    return {
        "changed_files": changed_files,
        "changed_symbols": sorted(all_symbols),
        "stale_pages": stale_pages,
        "summary": summary,
    }


def format_for_advisory(impact: dict, max_pages: int = 3) -> str:
    """Format impact analysis for SessionStart advisory injection.

    Returns a short text block (~200 tokens max) for the agent.
    """
    stale = impact.get("stale_pages", [])
    if not stale:
        return ""

    lines = ["### Code-Knowledge Impact"]
    lines.append(f"{impact['summary']}")
    lines.append("")

    for page in stale[:max_pages]:
        confidence_marker = "!!!" if page["confidence"] == "high" else "!"
        lines.append(
            f"{confidence_marker} **{page['slug']}** — {page['reason']}"
        )

    if len(stale) > max_pages:
        lines.append(f"... and {len(stale) - max_pages} more.")

    return "\n".join(lines)


def main() -> int:
    import argparse
    import json

    p = argparse.ArgumentParser(description="LINK Layer — code-knowledge impact analysis.")
    p.add_argument("--range", type=str, default=None, help="Git range (e.g. main..HEAD).")
    p.add_argument("--json", action="store_true", help="Output as JSON.")
    args = p.parse_args()

    impact = analyze_impact(args.range)

    if args.json:
        print(json.dumps(impact, indent=2, ensure_ascii=False))
        return 0

    print(impact["summary"])
    print()

    if impact["stale_pages"]:
        print("Potentially stale wiki pages:")
        for page in impact["stale_pages"]:
            marker = "!!!" if page["confidence"] == "high" else "  !"
            print(f"  {marker} {page['slug']} ({page['confidence']}): {page['reason']}")
    else:
        print("No stale wiki pages detected.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
