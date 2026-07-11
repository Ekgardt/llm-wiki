"""Proactive advisory generator — the "navigator" layer.

Unlike the metacognitive block (which shows inventory/backlog = dashboard),
this module surfaces ACTIONABLE intelligence: open threads, last decisions,
potential contradictions, cross-project insights. It's what makes the
system feel "smart" rather than just a filing cabinet.

Called from session_start_context.py on every SessionStart. Non-LLM, <100ms.
Output is injected as "## Advisory" block in the additionalContext payload.

Inspired by:
- ReMe's "proactive" feature (surfaces topics from auto_dream)
- Supermemory's static-profile vs dynamic-context split
- VEP's "knowledge state" metacognitive injection
"""
from __future__ import annotations

import re
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from memory_state import REPORTS_DIR, ROOT  # noqa: E402

PROJECTS_DIR = ROOT / "knowledge" / "projects"
KNOWLEDGE = ROOT / "knowledge" / "notes"
DAILY_DIR = ROOT / "knowledge" / "daily"

FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?", re.DOTALL)
TIMESTAMP_RE = re.compile(r"^timestamp:\s*(.+?)\s*$", re.MULTILINE)
TYPE_RE = re.compile(r"^type:\s*(.+?)\s*$", re.MULTILINE)
STATUS_RE = re.compile(r"^status:\s*(.+?)\s*$", re.MULTILINE)
PROJECT_RE = re.compile(r"^project:\s*[\"']?([^\"'\n]+)[\"']?\s*$", re.MULTILINE)
H1_RE = re.compile(r"^#\s+(.+?)\s*$", re.MULTILINE)
SUMMARY_RE = re.compile(
    r"^One-sentence summary:\s*(.+?)\s*$", re.MULTILINE | re.IGNORECASE
)


def _fm_field(content: str, pattern: re.Pattern) -> str | None:
    fm = FRONTMATTER_RE.match(content)
    if not fm:
        return None
    m = pattern.search(fm.group(1))
    return m.group(1).strip() if m else None


def _read_open_threads(slug: str) -> list[str]:
    """Extract open threads from project state.md."""
    # Validate by containment rather than ASCII slug format —
    # session_start_project_state.py may generate Unicode slugs.
    state_path = (PROJECTS_DIR / slug / "state.md").resolve()
    try:
        state_path.relative_to(PROJECTS_DIR.resolve())
    except ValueError:
        return []
    if not state_path.exists():
        return []
    try:
        content = state_path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return []
    match = re.search(
        r"^##\s*Open threads\s*$\n(.*?)(?=\n##\s|\Z)",
        content,
        re.MULTILINE | re.DOTALL,
    )
    if not match:
        return []
    threads = []
    for line in match.group(1).strip().splitlines():
        line = line.strip()
        if line.startswith("- ") and len(line) > 3:
            threads.append(line[2:].strip()[:120])
    return threads[:5]


def _find_last_decision(slug: str | None = None) -> dict | None:
    """Find the most recent decision page (optionally filtered by project).

    The compiler writes decisions FLAT under knowledge/notes/ (not in a
    decisions/ subdir), so we scan all .md files and filter by
    frontmatter `type: decision`.
    """
    if not KNOWLEDGE.exists():
        return None
    candidates = []
    for md in KNOWLEDGE.rglob("*.md"):
        try:
            content = md.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        page_type = _fm_field(content, TYPE_RE)
        if not page_type or page_type.strip().strip("\"'").lower() != "decision":
            continue
        status = _fm_field(content, STATUS_RE) or "active"
        if status == "superseded":
            continue
        ts = _fm_field(content, TIMESTAMP_RE)
        if not ts:
            continue
        project = _fm_field(content, PROJECT_RE)
        if slug and project and project.lower() != slug.lower():
            continue
        title_match = H1_RE.search(content)
        title = title_match.group(1).strip() if title_match else md.stem
        summary_match = SUMMARY_RE.search(content)
        summary = summary_match.group(1).strip()[:100] if summary_match else ""
        candidates.append({
            "title": title,
            "summary": summary,
            "timestamp": ts[:10],
            "path": md.relative_to(ROOT).as_posix(),
        })
    if not candidates:
        return None
    candidates.sort(key=lambda x: x["timestamp"], reverse=True)
    return candidates[0]


def _find_contradictions() -> list[str]:
    """Check lint report for contradiction findings."""
    # Check if the last lint report exists and has findings
    reports_dir = REPORTS_DIR
    if not reports_dir.exists():
        return []
    reports = sorted(reports_dir.glob("lint-*.md"), reverse=True)
    if not reports:
        return []
    try:
        report = reports[0].read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return []
    # Extract broken_wikilinks findings (actionable)
    hits = []
    in_section = False
    for line in report.splitlines():
        if line.startswith("## Broken Wikilinks"):
            in_section = True
            continue
        if line.startswith("## "):
            in_section = False
        if in_section and line.strip().startswith("- ") and "(none)" not in line:
            hits.append(line.strip()[2:][:120])
    return hits[:3]


def _find_cross_project_insights(slug: str) -> list[str]:
    """Find knowledge pages in OTHER projects that share concepts with this project."""
    # Get this project's pages' titles
    project_titles: set[str] = set()
    other_pages: list[dict] = []
    if not KNOWLEDGE.exists():
        return []
    for md in sorted(KNOWLEDGE.rglob("*.md")):
        try:
            content = md.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        project = _fm_field(content, PROJECT_RE)
        title_match = H1_RE.search(content)
        title = title_match.group(1).strip().lower() if title_match else ""
        summary_match = SUMMARY_RE.search(content)
        summary = summary_match.group(1).strip()[:80] if summary_match else ""
        entry = {
            "title": title_match.group(1).strip() if title_match else md.stem,
            "summary": summary,
            "project": project or "global",
            "path": md.relative_to(ROOT).as_posix(),
        }
        if project and project.lower() == slug.lower():
            project_titles.add(title)
        elif project and project.lower() != slug.lower():
            other_pages.append(entry)
    # Check if any other-project page shares keywords
    insights = []
    for other in other_pages[:20]:  # limit scan
        other_title_words = set(other["title"].lower().split())
        for pt in project_titles:
            pt_words = set(pt.split())
            overlap = pt_words & other_title_words
            # Need at least 2 meaningful overlapping words (skip common words)
            meaningful = overlap - {"the", "a", "an", "for", "of", "to", "in", "and", "with"}
            if len(meaningful) >= 2:
                insights.append(
                    f"'{other['title']}' ({other['project']}) — shares: {', '.join(meaningful)}"
                )
                break
    return insights[:3]


def _find_stale_pages() -> int:
    """Count pages older than 90 days without supersede."""
    cutoff = (datetime.now().timestamp()) - (90 * 86400)
    count = 0
    if not KNOWLEDGE.exists():
        return 0
    for md in KNOWLEDGE.rglob("*.md"):
        try:
            content = md.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        status = _fm_field(content, STATUS_RE)
        if status == "superseded":
            continue
        if md.stat().st_mtime < cutoff:
            count += 1
    return count


def build_advisory(slug: str | None = None, max_chars: int = 800, use_llm: bool = False) -> str:
    """Build the proactive advisory block for SessionStart injection.

    This is the "navigator" layer — actionable intelligence, not just inventory.
    Non-LLM, <100ms for rule-based. Optional LLM enhancement adds ~5-10s.

    Args:
        slug: Project slug to scope the advisory.
        max_chars: Maximum output length.
        use_llm: If True and LLM available, generate a richer insight paragraph.
    """
    # Always build the rule-based advisory first (fast, reliable)
    rule_based = _build_rule_based_advisory(slug, max_chars)

    if not use_llm:
        return rule_based

    # Optional: enhance with LLM insight
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from llm_client import call_llm
    except ImportError:
        return rule_based

    if not rule_based:
        return ""

    # Ask LLM to synthesize the advisory data into actionable insight
    prompt = f"""You are an advisory engine for a solo developer's memory vault.
Below is structured data about the current state of project '{slug or 'unknown'}'.
Generate a 2-3 sentence ACTIONABLE insight that helps the developer decide what
to focus on next. Be specific, not generic. If there's a contradiction or open
thread, call it out.

=== Advisory data ===
{rule_based}
=== End data ===

Respond with only the insight paragraph (2-3 sentences). No preamble."""

    try:
        llm_insight = call_llm(
            prompt,
            system_prompt="You are a concise technical advisor. 2-3 sentences max. No filler.",
            max_tokens=200,
        )
    except Exception:  # noqa: BLE001
        return rule_based

    if llm_insight and llm_insight.strip():
        # Prepend LLM insight, keep rule-based details below
        combined = f"**Insight:** {llm_insight.strip()}\n\n{rule_based}"
        if len(combined) > max_chars:
            combined = combined[:max_chars - 20].rstrip() + "..."
        return combined

    return rule_based


def _build_rule_based_advisory(slug: str | None, max_chars: int) -> str:
    """Build the fast rule-based advisory (no LLM)."""
    parts: list[str] = []

    # 1. Open threads (most actionable)
    if slug:
        threads = _read_open_threads(slug)
        if threads:
            parts.append(f"**Open threads ({len(threads)}):**")
            for t in threads:
                parts.append(f"- {t}")
            parts.append("")

    # 2. Last decision
    last = _find_last_decision(slug)
    if last:
        parts.append(f"**Last decision** ({last['timestamp']}):")
        parts.append(f"- {last['title']}: {last['summary']}")
        parts.append("")

    # 3. Potential contradictions
    contradictions = _find_contradictions()
    if contradictions:
        parts.append(f"**Lint alerts ({len(contradictions)}):**")
        for c in contradictions:
            parts.append(f"- {c}")
        parts.append("")

    # 4. Cross-project insights
    if slug:
        insights = _find_cross_project_insights(slug)
        if insights:
            parts.append("**Cross-project insights:**")
            for i in insights:
                parts.append(f"- {i}")
            parts.append("")

    # 5. Stale page count (gentle nudge)
    stale = _find_stale_pages()
    if stale > 5:
        parts.append(f"**Vault health:** {stale} pages older than 90 days — consider archiving.")

    if not parts:
        return ""

    text = "\n".join(parts).strip()
    if len(text) > max_chars:
        text = text[:max_chars - 20].rstrip() + "..."
    return text


def main() -> int:
    import argparse
    p = argparse.ArgumentParser(description="Build proactive advisory for SessionStart.")
    p.add_argument("slug", nargs="?", default=None, help="Project slug (optional)")
    p.add_argument("--max-chars", type=int, default=800)
    p.add_argument("--llm", action="store_true", help="Enhance with LLM insight (needs ~5-10s)")
    args = p.parse_args()
    advisory = build_advisory(args.slug, args.max_chars, use_llm=args.llm)
    if advisory:
        print(advisory)
    else:
        print("(no advisory — vault is clean)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
