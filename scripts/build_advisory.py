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

import hashlib
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


def _read_text_or_none(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None


def _h1_title(content: str, fallback: str) -> str:
    match = H1_RE.search(content)
    if match:
        return match.group(1).strip()
    return fallback


def _summary(content: str, limit: int) -> str:
    match = SUMMARY_RE.search(content)
    if match:
        return match.group(1).strip()[:limit]
    return ""


def _read_open_threads(slug: str) -> list[str]:
    """Extract open threads from project state.md."""
    # Validate by containment rather than ASCII slug format —
    # session_start_project_state.py may generate Unicode slugs.
    state_path = (PROJECTS_DIR / slug / "state.md").resolve()
    content = _contained_state_text(state_path)
    if content is None:
        return []
    return _open_thread_lines(content)


def _contained_state_text(state_path: Path) -> str | None:
    """The state file's text when it stays inside the projects directory and can be read."""
    try:
        state_path.relative_to(PROJECTS_DIR.resolve())
    except ValueError:
        return None
    if not state_path.exists():
        return None
    return _read_text_or_none(state_path)


def _open_thread_lines(content: str) -> list[str]:
    match = re.search(
        r"^##\s*Open threads\s*$\n(.*?)(?=\n##\s|\Z)",
        content,
        re.MULTILINE | re.DOTALL,
    )
    if not match:
        return []
    lines = (line.strip() for line in match.group(1).strip().splitlines())
    return [line[2:].strip()[:120] for line in lines if _thread_line(line)][:5]


def _thread_line(line: str) -> bool:
    return line.startswith("- ") and len(line) > 3


def _find_last_decision(slug: str | None = None) -> dict | None:
    """Find the most recent decision page (optionally filtered by project).

    The compiler writes decisions FLAT under knowledge/notes/ (not in a
    decisions/ subdir), so we scan all .md files and filter by
    frontmatter `type: decision`.

    The returned dict carries ``slug``, ``logical_path``, and ``source_sha256`` so downstream
    callers (e.g. L1 tier lookup) can key caches by source hash per the
    Task 15 cache contract.
    """
    if not KNOWLEDGE.exists():
        return None
    candidates = _decision_entries(slug)
    if not candidates:
        return None
    candidates.sort(key=lambda x: x["timestamp"], reverse=True)
    return candidates[0]


def _decision_entries(slug: str | None) -> list[dict]:
    entries = []
    for md in KNOWLEDGE.rglob("*.md"):
        entry = _decision_entry(md, slug)
        if entry is not None:
            entries.append(entry)
    return entries


def _decision_entry(md: Path, slug: str | None) -> dict | None:
    content = _read_text_or_none(md)
    if content is None:
        return None
    timestamp = _active_decision_timestamp(content)
    if timestamp is None or not _in_project(content, slug):
        return None
    return _decision_record(md, content, timestamp)


def _is_decision(content: str) -> bool:
    page_type = _fm_field(content, TYPE_RE)
    return bool(page_type) and page_type.strip().strip("\"'").lower() == "decision"


def _active_decision_timestamp(content: str) -> str | None:
    """The timestamp of a decision page that is not superseded; None for any other page."""
    if not _is_decision(content):
        return None
    if (_fm_field(content, STATUS_RE) or "active") == "superseded":
        return None
    return _fm_field(content, TIMESTAMP_RE) or None


def _in_project(content: str, slug: str | None) -> bool:
    if not slug:
        return True
    project = _fm_field(content, PROJECT_RE)
    return not project or project.lower() == slug.lower()


def _decision_record(md: Path, content: str, timestamp: str) -> dict:
    source_sha256 = _source_digest(md)
    return {
        "title": _h1_title(content, md.stem),
        "summary": _summary(content, 100),
        "timestamp": timestamp[:10],
        "path": md.relative_to(ROOT).as_posix(),
        "slug": md.stem,
        "logical_path": md.relative_to(KNOWLEDGE).as_posix(),
        "source_sha256": source_sha256,
    }


def _source_digest(md: Path) -> str | None:
    try:
        return hashlib.sha256(md.read_bytes()).hexdigest()
    except OSError:
        return None


def _find_contradictions() -> list[str]:
    """Check lint report for contradiction findings."""
    report = _latest_lint_report()
    if report is None:
        return []
    # Extract broken_wikilinks findings (actionable)
    lines = _section_lines(report, "## Broken Wikilinks")
    return [line.strip()[2:][:120] for line in lines if _listed_finding(line)][:3]


def _latest_lint_report() -> str | None:
    if not REPORTS_DIR.exists():
        return None
    reports = sorted(REPORTS_DIR.glob("lint-*.md"), reverse=True)
    if not reports:
        return None
    return _read_text_or_none(reports[0])


def _section_lines(report: str, heading: str) -> list[str]:
    """Lines under `heading`, up to the next `## ` heading."""
    lines = []
    in_section = False
    for line in report.splitlines():
        if line.startswith(heading):
            in_section = True
            continue
        in_section = in_section and not line.startswith("## ")
        if in_section:
            lines.append(line)
    return lines


def _listed_finding(line: str) -> bool:
    return line.strip().startswith("- ") and "(none)" not in line


_STOP_WORDS = frozenset({"the", "a", "an", "for", "of", "to", "in", "and", "with"})


def _find_cross_project_insights(slug: str) -> list[str]:
    """Find knowledge pages in OTHER projects that share concepts with this project."""
    if not KNOWLEDGE.exists():
        return []
    project_titles, other_pages = _project_pages(slug)
    insights = [_shared_title_insight(other, project_titles) for other in other_pages[:20]]  # limit scan
    return [insight for insight in insights if insight is not None][:3]


def _project_pages(slug: str) -> tuple[set[str], list[dict]]:
    """(lower-cased H1 titles of this project's pages, title and project of other projects' pages)."""
    project_titles: set[str] = set()
    other_pages: list[dict] = []
    for md in sorted(KNOWLEDGE.rglob("*.md")):
        content = _read_text_or_none(md)
        if content is not None:
            _classify_page(md, content, slug, project_titles, other_pages)
    return project_titles, other_pages


def _classify_page(md: Path, content: str, slug: str, project_titles: set[str], other_pages: list[dict]) -> None:
    project = _fm_field(content, PROJECT_RE)
    if not project:
        return
    if project.lower() == slug.lower():
        project_titles.add(_h1_title(content, "").lower())
        return
    other_pages.append({"title": _h1_title(content, md.stem), "project": project})


def _shared_title_insight(other: dict, project_titles: set[str]) -> str | None:
    """The page's insight line when its title shares two meaningful words with a project title."""
    other_title_words = set(other["title"].lower().split())
    for title in project_titles:
        meaningful = (set(title.split()) & other_title_words) - _STOP_WORDS
        if len(meaningful) >= 2:
            return f"'{other['title']}' ({other['project']}) — shares: {', '.join(meaningful)}"
    return None


def _find_stale_pages() -> int:
    """Count pages older than 90 days without supersede."""
    cutoff = (datetime.now().timestamp()) - (90 * 86400)
    if not KNOWLEDGE.exists():
        return 0
    return sum(1 for md in KNOWLEDGE.rglob("*.md") if _stale(md, cutoff))


def _stale(md: Path, cutoff: float) -> bool:
    content = _read_text_or_none(md)
    if content is None or _fm_field(content, STATUS_RE) == "superseded":
        return False
    return md.stat().st_mtime < cutoff


def build_advisory(slug: str | None = None, max_chars: int = 800, use_llm: bool = False) -> str:
    """Build the proactive advisory block for SessionStart injection.

    This is the "navigator" layer — actionable intelligence, not just inventory.
    Non-LLM, <100ms for rule-based. Optional LLM enhancement adds ~5-10s.

    v4.0: Uses L1 tier summaries (from build_tiers.py) when available for
    more compact context injection (progressive disclosure).

    Args:
        slug: Project slug to scope the advisory.
        max_chars: Maximum output length.
        use_llm: If True and LLM available, generate a richer insight paragraph.
    """
    # Always build the rule-based advisory first (fast, reliable)
    rule_based = _build_rule_based_advisory(slug, max_chars)
    if not use_llm:
        return rule_based
    return _llm_advisory(slug, rule_based, max_chars)


def _llm_advisory(slug: str | None, rule_based: str, max_chars: int) -> str:
    """The rule-based advisory with an LLM insight paragraph on top, when one comes back."""
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from llm_client import call_llm
    except ImportError:
        return rule_based
    if not rule_based:
        return ""
    return _with_insight(_llm_insight(call_llm, slug, rule_based), rule_based, max_chars)


def _llm_insight(call_llm, slug: str | None, rule_based: str) -> str | None:
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
        return call_llm(
            prompt,
            system_prompt="You are a concise technical advisor. 2-3 sentences max. No filler.",
            max_tokens=200,
        )
    except Exception:  # noqa: BLE001
        return None


def _with_insight(llm_insight: str | None, rule_based: str, max_chars: int) -> str:
    if not llm_insight or not llm_insight.strip():
        return rule_based
    # Prepend LLM insight, keep rule-based details below
    return _clipped(f"**Insight:** {llm_insight.strip()}\n\n{rule_based}", max_chars)


def _clipped(text: str, max_chars: int) -> str:
    if len(text) > max_chars:
        return text[:max_chars - 20].rstrip() + "..."
    return text


def build_advisory_refresh() -> str:
    """Return a roughly 50-token mid-session vault health refresh."""
    pages = _active_page_count()
    stale = _find_stale_pages()
    return (
        f"Memory refresh: {pages} pages indexed; {stale} stale. "
        "Use durable pages first and verify stale claims before relying on them."
    )


def _active_page_count() -> int:
    if not KNOWLEDGE.exists():
        return 0
    return sum(1 for path in KNOWLEDGE.rglob("*.md") if _active_page(path))


def _active_page(path: Path) -> bool:
    content = _read_text_or_none(path)
    return content is not None and _fm_field(content, STATUS_RE) != "superseded"


def _build_rule_based_advisory(slug: str | None, max_chars: int) -> str:
    """Build the fast rule-based advisory (no LLM)."""
    parts = [
        *_open_threads_section(slug),  # 1. Open threads (most actionable)
        *_last_decision_section(slug),  # 2. Last decision
        *_lint_section(),  # 3. Potential contradictions
        *_insights_section(slug),  # 4. Cross-project insights
        *_stale_section(),  # 5. Stale page count (gentle nudge)
    ]
    if not parts:
        return ""
    return _clipped("\n".join(parts).strip(), max_chars)


def _bullet_section(heading: str, entries: list[str]) -> list[str]:
    if not entries:
        return []
    return [heading, *(f"- {entry}" for entry in entries), ""]


def _open_threads_section(slug: str | None) -> list[str]:
    if not slug:
        return []
    threads = _read_open_threads(slug)
    return _bullet_section(f"**Open threads ({len(threads)}):**", threads)


def _last_decision_section(slug: str | None) -> list[str]:
    last = _find_last_decision(slug)
    if not last:
        return []
    return [f"**Last decision** ({last['timestamp']}):", _decision_line(last), ""]


def _decision_line(last: dict) -> str:
    """The decision's L1 overview when one is cached for its current bytes, else title and summary.

    v4.0: progressive disclosure. Task 15: the cache key includes the source
    SHA-256 so a stale L1 overview cannot survive a content change.
    """
    fallback = f"- {last['title']}: {last['summary']}"
    try:
        from build_tiers import get_l1

        l1 = get_l1(
            last["slug"],
            source_sha256=last.get("source_sha256"),
            logical_path=last.get("logical_path"),
        )
    except Exception:
        return fallback
    if l1:
        return f"- {l1[:200]}"
    return fallback


def _lint_section() -> list[str]:
    contradictions = _find_contradictions()
    return _bullet_section(f"**Lint alerts ({len(contradictions)}):**", contradictions)


def _insights_section(slug: str | None) -> list[str]:
    if not slug:
        return []
    return _bullet_section("**Cross-project insights:**", _find_cross_project_insights(slug))


def _stale_section() -> list[str]:
    stale = _find_stale_pages()
    if stale > 5:
        return [f"**Vault health:** {stale} pages older than 90 days — consider archiving."]
    return []


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
