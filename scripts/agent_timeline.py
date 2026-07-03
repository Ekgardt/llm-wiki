"""Agent timeline — attribution: who decided what and when.

Reads daily logs + heartbeats + knowledge pages with timestamps
and builds a timeline showing which agent made which decision,
in which project, and when.

Solves: "3 agents worked in project — who contributed what?"

Usage:
    uv run python scripts/agent_timeline.py --project your-project
    uv run python scripts/agent_timeline.py --project your-project --days 7
    uv run python scripts/agent_timeline.py --all --days 30
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from memory_state import ROOT, load_state  # noqa: E402

DAILY_DIR = ROOT / "memory" / "daily"
KNOWLEDGE = ROOT / "memory" / "knowledge"
PROJECTS_DIR = ROOT / "wiki" / "projects"

# Patterns to extract agent identity from daily log entries
AGENT_PATTERNS = [
    (re.compile(r"opencode", re.IGNORECASE), "OpenCode"),
    (re.compile(r"codex", re.IGNORECASE), "Codex"),
    (re.compile(r"claude", re.IGNORECASE), "Claude Code"),
]

# Extract decision/lesson lines from FLUSH blocks
DECISION_RE = re.compile(r"^\*?\*?Decisions? made\*?\*?\s*$", re.IGNORECASE)
LESSON_RE = re.compile(r"^\*?\*?Lessons?\s*/\s*patterns?\*?\*?\s*$", re.IGNORECASE)
GOTCHA_RE = re.compile(r"^\*?\*?Gotchas?\s*/\s*debugging\*?\*?\s*$", re.IGNORECASE)
BULLET_RE = re.compile(r"^-\s+(.+)$")


def _detect_agent(text: str) -> str:
    """Detect which agent produced this block."""
    for pattern, name in AGENT_PATTERNS:
        if pattern.search(text):
            return name
    return "unknown"


def _extract_activity(daily_path: Path, project_slug: str | None, days: int) -> list[dict]:
    """Extract agent activity from a daily log file."""
    cutoff = datetime.now() - timedelta(days=days)
    try:
        content = daily_path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return []

    date_str = daily_path.stem  # YYYY-MM-DD
    try:
        file_date = datetime.strptime(date_str, "%Y-%m-%d")
        if file_date < cutoff:
            return []
    except ValueError:
        return []

    # Split into session blocks
    blocks = re.split(r"^##\s+\[", content, flags=re.MULTILINE)
    activities = []

    for block in blocks[1:]:  # skip preamble
        # Extract timestamp from header
        header_match = re.match(r"(\d{2}:\d{2}:\d{2})\]\s*(.+)", block)
        if not header_match:
            continue
        ts = header_match.group(1)
        event_info = header_match.group(2)

        # Detect agent
        agent = _detect_agent(block)
        # Detect project from slug mention
        if project_slug and project_slug.lower() not in block.lower():
            # Check if this block mentions the target project
            slug_in_block = re.search(r"slug:\s*[`']?(\w+)", block, re.IGNORECASE)
            if slug_in_block and slug_in_block.group(1).lower() != project_slug.lower():
                continue

        # Extract decisions/lessons from the block
        in_section = None
        items = []
        for line in block.splitlines():
            if DECISION_RE.match(line.strip()):
                in_section = "decision"
            elif LESSON_RE.match(line.strip()):
                in_section = "lesson"
            elif GOTCHA_RE.match(line.strip()):
                in_section = "gotcha"
            elif line.strip().startswith("## ") or line.strip().startswith("- Trigger:"):
                in_section = None
            elif in_section and BULLET_RE.match(line.strip()):
                items.append({"type": in_section, "text": BULLET_RE.match(line.strip()).group(1)[:120]})

        # Also capture tool breadcrumbs
        tool_matches = re.findall(r"\[(\d{2}:\d{2}:\d{2})\]\s+tool\s+\|\s+(\w+)\s+\|\s+(\w+)\s+\|\s+(\w+)\s+(.*)", block)
        for tool_ts, session, slug, tool_name, target in tool_matches:
            if project_slug and slug.lower() != project_slug.lower():
                continue
            activities.append({
                "date": date_str,
                "time": tool_ts,
                "agent": agent,
                "type": "tool",
                "tool": tool_name,
                "target": target.strip()[:80],
                "project": slug,
            })

        # Add decisions/lessons
        for item in items:
            activities.append({
                "date": date_str,
                "time": ts,
                "agent": agent,
                "type": item["type"],
                "text": item["text"],
            })

    return activities


def _extract_knowledge_timeline(project_slug: str | None, days: int) -> list[dict]:
    """Extract knowledge page creation timeline from frontmatter timestamps."""
    cutoff = datetime.now() - timedelta(days=days)
    results = []
    if not KNOWLEDGE.exists():
        return results

    for md in sorted(KNOWLEDGE.rglob("*.md")):
        try:
            content = md.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        # Extract timestamp from frontmatter
        fm_match = re.match(r"^---\s*\n(.*?)\n---", content, re.DOTALL)
        if not fm_match:
            continue
        fm = fm_match.group(1)
        ts_match = re.search(r"^timestamp:\s*(.+?)\s*$", fm, re.MULTILINE)
        if not ts_match:
            continue
        try:
            page_date = datetime.fromisoformat(ts_match.group(1).split("T")[0])
            if page_date < cutoff:
                continue
        except (ValueError, IndexError):
            continue

        # Filter by project
        proj_match = re.search(r"^project:\s*(.+?)\s*$", fm, re.MULTILINE)
        if project_slug:
            if not proj_match or proj_match.group(1).strip().lower() != project_slug.lower():
                continue

        type_match = re.search(r"^type:\s*(.+?)\s*$", fm, re.MULTILINE)
        h1_match = re.search(r"^#\s+(.+?)\s*$", content, re.MULTILINE)
        summary_match = re.search(r"^One-sentence summary:\s*(.+?)\s*$", content, re.MULTILINE | re.IGNORECASE)

        results.append({
            "date": ts_match.group(1)[:10],
            "time": ts_match.group(1)[11:19] if "T" in ts_match.group(1) else "",
            "agent": "compile",
            "type": f"knowledge:{type_match.group(1)}" if type_match else "knowledge",
            "text": summary_match.group(1)[:120] if summary_match else (h1_match.group(1) if h1_match else md.stem),
            "path": md.relative_to(ROOT).as_posix(),
        })

    return results


def build_timeline(project_slug: str | None = None, days: int = 30) -> list[dict]:
    """Build a unified agent activity timeline."""
    activities = []

    # From daily logs
    if DAILY_DIR.exists():
        for daily in sorted(DAILY_DIR.glob("*.md"), reverse=True):
            activities.extend(_extract_activity(daily, project_slug, days))

    # From knowledge pages (compiled decisions)
    activities.extend(_extract_knowledge_timeline(project_slug, days))

    # From heartbeats
    try:
        state = load_state()
        heartbeats = state.get("codex_heartbeats", {})
        for slug, hb in heartbeats.items():
            if project_slug and slug.lower() != project_slug.lower():
                continue
            activities.append({
                "date": hb.get("at", "")[:10],
                "time": hb.get("at", "")[11:19] if "T" in hb.get("at", "") else "",
                "agent": _detect_agent(hb.get("reason", "")),
                "type": "heartbeat",
                "text": f"active in {slug}",
            })
    except Exception:
        pass

    # Sort chronologically (newest first)
    activities.sort(key=lambda x: f"{x.get('date', '')}T{x.get('time', '')}", reverse=True)
    return activities


def format_timeline(activities: list[dict]) -> str:
    """Format timeline as readable markdown."""
    if not activities:
        return "(no activity found)"

    lines = [f"## Agent Timeline ({len(activities)} events)\n"]
    current_date = ""

    for a in activities:
        date = a.get("date", "?")
        if date != current_date:
            current_date = date
            lines.append(f"\n### {date}\n")

        time = a.get("time", "?")[:8]
        agent = a.get("agent", "?")
        atype = a.get("type", "?")
        text = a.get("text", a.get("target", ""))

        emoji = {"decision": "[DECISION]", "lesson": "[LESSON]", "gotcha": "[GOTCHA]",
                 "tool": "[TOOL]", "heartbeat": "[ACTIVE]", "compile": "[COMPILED]"}.get(atype, "")
        if atype.startswith("knowledge:"):
            emoji = f"[{atype.split(':')[1].upper()}]"

        lines.append(f"- `{time}` **{agent}** {emoji} {text}")

    return "\n".join(lines)


def main() -> int:
    p = argparse.ArgumentParser(description="Agent activity timeline.")
    p.add_argument("--project", default=None, help="Filter by project slug")
    p.add_argument("--all", action="store_true", help="All projects")
    p.add_argument("--days", type=int, default=30, help="Look back N days")
    p.add_argument("--json", action="store_true", help="JSON output")
    args = p.parse_args()

    slug = None if args.all else args.project
    activities = build_timeline(slug, args.days)

    if args.json:
        print(json.dumps(activities, indent=2, ensure_ascii=False))
    else:
        print(format_timeline(activities))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
