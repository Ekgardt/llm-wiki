"""Auto-generate per-project context summary for SessionStart injection.

Reads all knowledge pages tagged with `project: <slug>` in their
frontmatter, plus recent daily-log breadcrumbs for that slug, plus
the project's state.md handoff note. Produces a compact markdown
block that gets injected at SessionStart so the agent immediately
knows: what decisions were made, what patterns are known, what
gotchas exist, what's currently open — for THIS specific project.

Without this: the agent sees global vault inventory but doesn't know
which knowledge applies to the project you just opened.

With this: the agent sees a tailored brief — "you decided JWT last
week, you have 3 known gotchas about hook timing, you left off at
refresh tokens".
"""
from __future__ import annotations

import argparse
import re
from datetime import datetime
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))
from memory_state import ROOT, load_state  # noqa: E402

KNOWLEDGE = ROOT / "knowledge" / "notes"
DAILY_DIR = ROOT / "knowledge" / "daily"
PROJECTS_DIR = ROOT / "knowledge" / "projects"

FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?", re.DOTALL)
PROJECT_FIELD_RE = re.compile(r"^project:\s*[\"']?([^\"'\n]+)[\"']?\s*$", re.MULTILINE)
TYPE_FIELD_RE = re.compile(r"^type:\s*(.+?)\s*$", re.MULTILINE)
H1_RE = re.compile(r"^#\s+(.+?)\s*$", re.MULTILINE)
SUMMARY_RE = re.compile(r"^One-sentence summary:\s*(.+?)\s*$", re.MULTILINE | re.IGNORECASE)
STATUS_FIELD_RE = re.compile(r"^status:\s*(.+?)\s*$", re.MULTILINE)


def _extract_frontmatter_field(content: str, pattern: re.Pattern) -> str | None:
    fm = FRONTMATTER_RE.match(content)
    if not fm:
        return None
    m = pattern.search(fm.group(1))
    return m.group(1).strip() if m else None


def _find_project_pages(slug: str) -> list[dict]:
    """Find all knowledge pages tagged with `project: <slug>`."""
    results = []
    if not KNOWLEDGE.exists():
        return results
    for md in sorted(KNOWLEDGE.rglob("*.md")):
        try:
            content = md.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        project = _extract_frontmatter_field(content, PROJECT_FIELD_RE)
        if project and project.lower().strip() == slug.lower().strip():
            page_type = _extract_frontmatter_field(content, TYPE_FIELD_RE) or "unknown"
            status = _extract_frontmatter_field(content, STATUS_FIELD_RE) or "active"
            title_match = H1_RE.search(content)
            title = title_match.group(1).strip() if title_match else md.stem
            summary_match = SUMMARY_RE.search(content)
            summary = summary_match.group(1).strip() if summary_match else ""
            results.append({
                "path": md.relative_to(ROOT).as_posix(),
                "type": page_type,
                "status": status,
                "title": title,
                "summary": summary,
            })
    return results


def _find_recent_daily_activity(slug: str, days: int = 7) -> list[str]:
    """Find recent daily-log breadcrumbs mentioning this slug."""
    results = []
    if not DAILY_DIR.exists():
        return results
    cutoff = datetime.now().timestamp() - (days * 86400)
    for md in sorted(DAILY_DIR.glob("*.md"), reverse=True):
        try:
            if md.stat().st_mtime < cutoff:
                continue
            content = md.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if slug.lower() in content.lower():
            # Extract lines mentioning the slug
            for line in content.splitlines():
                if slug.lower() in line.lower() and line.strip():
                    results.append(f"{md.stem}: {line.strip()[:120]}")
                    if len(results) >= 10:
                        return results
    return results


def _read_state_handoff(slug: str) -> str:
    """Read the 'Where we left off' section from project state.md."""
    state_path = PROJECTS_DIR / slug / "state.md"
    if not state_path.exists():
        return ""
    try:
        content = state_path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""
    # Extract "## Where we left off" section
    match = re.search(
        r"^##\s*Where we left off\s*$\n(.*?)(?=\n##\s|\Z)",
        content,
        re.MULTILINE | re.DOTALL,
    )
    if match:
        return match.group(1).strip()
    return ""


def _detect_agent_strengths(agent: str) -> list[str] | None:
    """Auto-detect what an agent is good at from its history.

    Instead of hardcoding "codex=codegen, opencode=research", we look at:
    1. Which knowledge page types this agent has contributed to most
    2. Which feedback types it receives (corrections = weakness,
       preferences = engagement area)

    Returns: ordered list of knowledge types the agent excels at,
    or None if no data (use balanced view).
    """
    import json as _json

    type_counts: dict[str, int] = {}

    # Count knowledge pages by source_authority or detected agent
    if KNOWLEDGE.exists():
        for md in KNOWLEDGE.rglob("*.md"):
            try:
                content = md.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            # Check if this page was contributed by this agent
            if agent.lower() not in content.lower():
                continue
            # Extract type
            fm_match = re.match(r"^---\s*\n(.*?)\n---", content, re.DOTALL)
            if fm_match:
                type_match = re.search(r"^type:\s*(.+?)\s*$", fm_match.group(1), re.MULTILINE)
                if type_match:
                    t = type_match.group(1).strip()
                    type_counts[t] = type_counts.get(t, 0) + 1

    # Also check feedback: types where agent gets corrections = weakness
    feedback_dir = ROOT / "knowledge" / "feedback"
    if feedback_dir.exists():
        for f in feedback_dir.glob("*.json"):
            try:
                fb = _json.loads(f.read_text(encoding="utf-8"))
            except (OSError, _json.JSONDecodeError):
                continue
            if agent.lower() in fb.get("text", "").lower() or agent.lower() in fb.get("project", "").lower():
                fb_type = fb.get("type", "")
                # Corrections indicate the agent is active in this area
                # but makes mistakes — still engagement signal
                type_counts[f"feedback_{fb_type}"] = type_counts.get(f"feedback_{fb_type}", 0) + 1

    if not type_counts:
        return None  # no data → balanced view

    # Rank types by frequency (most contributions = strongest area)
    ranked = sorted(type_counts.items(), key=lambda x: x[1], reverse=True)
    return [t for t, _ in ranked[:5]]


def build_context(slug: str, max_chars: int = 2000, agent: str | None = None) -> str:
    """Build the project-context injection block.

    Args:
        slug: Project slug to scope the context.
        max_chars: Maximum output length.
        agent: Agent name. Context is auto-tailored based on the agent's
               demonstrated strengths (derived from feedback history),
               NOT hardcoded assumptions about which tool is "better at X".
    """
    parts = [f"## Project context: {slug}\n"]

    # 1. Handoff note from state.md
    handoff = _read_state_handoff(slug)
    if handoff:
        parts.append(f"### Where you left off\n{handoff[:500]}\n")

    # 2. Knowledge pages tagged for this project
    pages = _find_project_pages(slug)
    active_pages = [p for p in pages if p["status"] != "superseded"]

    # Per-agent filtering (Dorabotka C v2: auto-detect, not hardcoded)
    if agent:
        agent = agent.lower()
        # Auto-detect agent strengths from feedback history.
        # Instead of hardcoding "codex=codegen, opencode=research",
        # we look at which knowledge types each agent has contributed
        # successfully (via feedback_capture + knowledge page source).
        agent_priority = _detect_agent_strengths(agent)
        if not agent_priority:
            # Fallback: balanced view (all types)
            agent_priority = None
        if agent_priority:
            active_pages.sort(
                key=lambda p: agent_priority.index(p["type"]) if p["type"] in agent_priority else 99
            )

    if active_pages:
        parts.append(f"### Known knowledge ({len(active_pages)} pages)")
        by_type: dict[str, list[dict]] = {}
        for p in active_pages:
            by_type.setdefault(p["type"], []).append(p)
        for ptype in sorted(by_type.keys()):
            parts.append(f"**{ptype}s:**")
            for p in by_type[ptype][:5]:
                summary = p["summary"][:80] if p["summary"] else p["title"]
                parts.append(f"- {summary}")
            parts.append("")

    # 3. Recent activity
    activity = _find_recent_daily_activity(slug)
    if activity:
        parts.append(f"### Recent activity (last 7 days)")
        for line in activity[:5]:
            parts.append(f"- {line}")
        parts.append("")

    # 4. Heartbeat from state.json
    try:
        state = load_state()
        hb = state.get("codex_heartbeats", {}).get(slug, {})
        if hb:
            parts.append(f"### Last seen")
            parts.append(f"- {hb.get('reason', 'unknown')} at {hb.get('at', '?')}")
    except Exception:
        pass

    text = "\n".join(parts).strip()
    if len(text) > max_chars:
        text = text[:max_chars - 20].rstrip() + "\n… (truncated)\n"
    return text


def main() -> int:
    p = argparse.ArgumentParser(description="Build per-project context for SessionStart.")
    p.add_argument("slug", help="Project slug (e.g. 'your-project')")
    p.add_argument("--max-chars", type=int, default=2000)
    p.add_argument("--write", action="store_true", help="Write to knowledge/projects/<slug>/context.md")
    args = p.parse_args()

    context = build_context(args.slug, args.max_chars)
    if args.write:
        out = PROJECTS_DIR / args.slug / "context.md"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            "---\n"
            f"type: project-context\ntitle: \"{args.slug} context\"\n"
            f"description: \"Auto-generated project context for {args.slug}\"\n"
            f"timestamp: {datetime.now().isoformat(timespec='seconds')}\n"
            "---\n\n"
            f"# {args.slug} — Auto-Context\n\n"
            f"Generated by `scripts/build_context.py`. Do not edit manually — "
            f"this file is regenerated on each compile pass.\n\n"
            f"{context}\n",
            encoding="utf-8",
        )
        print(f"Written: {out.relative_to(ROOT)}")
    else:
        print(context)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
