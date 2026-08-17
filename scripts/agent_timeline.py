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
from bounded_io import read_stable_bytes  # noqa: E402
from event_envelope import canonical_agent  # noqa: E402
from evidence_resolver import (  # noqa: E402
    MAX_DAILY_BYTES,
    EvidenceRef,
    EvidenceResolutionError,
    EvidenceResolver,
)
from memory_state import ROOT, load_state  # noqa: E402

DAILY_DIR = ROOT / "knowledge" / "daily"
KNOWLEDGE = ROOT / "knowledge" / "notes"
PROJECTS_DIR = ROOT / "knowledge" / "projects"

AGENT_META_RE = re.compile(r"(?im)^-\s*Agent:\s*`?([^`\r\n]+)`?\s*$")
TOOL_LINE_RE = re.compile(
    r"^\s*-\s+`\[(\d{2}:\d{2}:\d{2})\]\s+tool\s+\|\s+([^`]+)`\s*(.*)$",
    re.IGNORECASE,
)
EVIDENCE_REF_RE = re.compile(
    r"`(daily:\d{4}-\d{2}-\d{2} sha256:[0-9a-f]{64} "
    r"block:[A-Za-z0-9][A-Za-z0-9._:-]{0,199} bytes:\d+-\d+)`"
)

# Extract decision/lesson lines from FLUSH blocks
DECISION_RE = re.compile(r"^\*?\*?Decisions? made\*?\*?\s*$", re.IGNORECASE)
LESSON_RE = re.compile(r"^\*?\*?Lessons?\s*/\s*patterns?\*?\*?\s*$", re.IGNORECASE)
GOTCHA_RE = re.compile(r"^\*?\*?Gotchas?\s*/\s*debugging\*?\*?\s*$", re.IGNORECASE)
BULLET_RE = re.compile(r"^-\s+(.+)$")
SECTION_RE = re.compile(
    r"^\*?\*?(?:(?P<decision>Decisions? made)|"
    r"(?P<lesson>Lessons?\s*/\s*patterns?)|"
    r"(?P<gotcha>Gotchas?\s*/\s*debugging))\*?\*?\s*$",
    re.IGNORECASE,
)


def block_agent(block: str) -> str:
    """Read explicit agent metadata first, then the block header."""
    marker = AGENT_META_RE.search(block)
    if marker is not None:
        return canonical_agent(marker.group(1))
    header = block.splitlines()[0] if block.splitlines() else ""
    return canonical_agent(header)


def _detect_agent(text: str) -> str:
    """Compatibility alias for callers that classify one short label."""
    return canonical_agent(text)


def _tool_fields(
    fields: list[str], fallback_agent: str
) -> tuple[str, str, str, str] | None:
    if len(fields) == 4:
        agent, session, slug, tool = fields
        return canonical_agent(agent), session, slug, tool
    if len(fields) == 3:
        session, slug, tool = fields
        return canonical_agent(fallback_agent), session, slug, tool
    return None


def parse_tool_breadcrumb(
    line: str, *, fallback_agent: str = "unknown"
) -> dict[str, str] | None:
    """Parse current agent-aware and historical tool breadcrumb lines."""
    match = TOOL_LINE_RE.match(line)
    if match is None:
        return None
    parsed = _tool_fields(
        [part.strip() for part in match.group(2).split("|")], fallback_agent
    )
    if parsed is None:
        return None
    agent, session, slug, tool = parsed
    return {
        "time": match.group(1),
        "agent": agent,
        "session": session,
        "slug": slug,
        "tool": tool,
        "target": match.group(3).strip(),
    }


def _read_recent_daily(daily_path: Path, days: int) -> tuple[str, str] | None:
    try:
        file_date = datetime.strptime(daily_path.stem, "%Y-%m-%d")
        content = daily_path.read_text(encoding="utf-8", errors="ignore")
    except (OSError, ValueError):
        return None
    if file_date < datetime.now() - timedelta(days=days):
        return None
    return daily_path.stem, content


def _block_matches_project(block: str, project_slug: str | None) -> bool:
    if not project_slug or project_slug.lower() in block.lower():
        return True
    slug = re.search(r"(?:project\s+)?slug:\s*[`']?([\w-]+)", block, re.IGNORECASE)
    return slug is not None and slug.group(1).lower() == project_slug.lower()


def _memory_section(line: str) -> str | None:
    match = SECTION_RE.match(line)
    if match is None:
        return None
    return next(name for name, value in match.groupdict().items() if value is not None)


def _section_marker(line: str) -> str | None:
    if line.startswith(("## ", "- Trigger:")):
        return ""
    return _memory_section(line)


def _section_item(section: str | None, line: str) -> dict[str, str] | None:
    match = BULLET_RE.match(line)
    if not section or match is None:
        return None
    return {"type": section, "text": match.group(1)[:120]}


def _extract_block_items(block: str) -> list[dict[str, str]]:
    section = None
    items: list[dict[str, str]] = []
    for raw_line in block.splitlines():
        line = raw_line.strip()
        marker = _section_marker(line)
        if marker is not None:
            section = marker or None
            continue
        item = _section_item(section, line)
        if item is not None:
            items.append(item)
    return items


def _tool_activity(
    line: str, date_str: str, project_slug: str | None, fallback_agent: str
) -> dict[str, str] | None:
    parsed = parse_tool_breadcrumb(line, fallback_agent=fallback_agent)
    if parsed is None:
        return None
    if project_slug and parsed["slug"].lower() != project_slug.lower():
        return None
    return {
        "date": date_str,
        "time": parsed["time"],
        "agent": parsed["agent"],
        "type": "tool",
        "tool": parsed["tool"],
        "target": parsed["target"][:80],
        "project": parsed["slug"],
    }


def _tool_activities(
    block: str, date_str: str, project_slug: str | None, agent: str
) -> list[dict[str, str]]:
    activities: list[dict[str, str]] = []
    for line in block.splitlines():
        item = _tool_activity(line, date_str, project_slug, agent)
        if item is not None:
            activities.append(item)
    return activities


def _block_activities(
    block: str, date_str: str, project_slug: str | None
) -> list[dict[str, str]]:
    header = re.match(r"(\d{2}:\d{2}:\d{2})\]\s*(.+)", block)
    if header is None:
        return []
    if not _block_matches_project(block, project_slug):
        return []
    agent = block_agent(block)
    activities = _tool_activities(block, date_str, project_slug, agent)
    activities.extend(
        {
            "date": date_str,
            "time": header.group(1),
            "agent": agent,
            "type": item["type"],
            "text": item["text"],
        }
        for item in _extract_block_items(block)
    )
    return activities


def _extract_activity(daily_path: Path, project_slug: str | None, days: int) -> list[dict]:
    """Extract agent activity from a daily log file."""
    daily = _read_recent_daily(daily_path, days)
    if daily is None:
        return []
    date_str, content = daily
    blocks = re.split(r"^##\s+\[", content, flags=re.MULTILINE)
    activities: list[dict[str, str]] = []
    for block in blocks[1:]:
        activities.extend(_block_activities(block, date_str, project_slug))
    return activities


def _frontmatter(content: str) -> str | None:
    match = re.match(r"^---\s*\n(.*?)\n---", content, re.DOTALL)
    return match.group(1) if match is not None else None


def _frontmatter_value(frontmatter: str, name: str) -> str | None:
    match = re.search(rf"^{re.escape(name)}:\s*(.+?)\s*$", frontmatter, re.MULTILINE)
    return match.group(1) if match is not None else None


def _recent_page_metadata(content: str, days: int) -> tuple[str, str] | None:
    frontmatter = _frontmatter(content)
    if frontmatter is None:
        return None
    timestamp = _frontmatter_value(frontmatter, "timestamp")
    if timestamp is None:
        return None
    try:
        recent = datetime.fromisoformat(timestamp.split("T")[0]) >= (
            datetime.now() - timedelta(days=days)
        )
    except (ValueError, IndexError):
        return None
    return (frontmatter, timestamp) if recent else None


def _page_matches_project(frontmatter: str, project_slug: str | None) -> bool:
    project = _frontmatter_value(frontmatter, "project")
    return project_slug is None or (project or "").strip().lower() == project_slug.lower()


def _page_text(content: str, fallback: str) -> str:
    summary = re.search(
        r"^One-sentence summary:\s*(.+?)\s*$", content, re.MULTILINE | re.IGNORECASE
    )
    if summary is not None:
        return summary.group(1)[:120]
    h1 = re.search(r"^#\s+(.+?)\s*$", content, re.MULTILINE)
    if h1 is not None:
        return h1.group(1)
    return fallback


def _page_time(timestamp: str) -> str:
    if "T" not in timestamp:
        return ""
    return timestamp[11:19]


def _page_event_type(page_type: str | None) -> str:
    if page_type is None:
        return "knowledge"
    return f"knowledge:{page_type}"


def _source_block(content: bytes, block_id: str) -> bytes:
    pattern = re.compile(
        rb"(?m)^## \["
        + re.escape(block_id.encode("ascii"))
        + rb"\][^\r\n]*\r?$"
    )
    matches = list(pattern.finditer(content))
    if len(matches) != 1:
        return b""
    next_header = content.find(b"\n## [", matches[0].end())
    if next_header < 0:
        return content[matches[0].start():]
    return content[matches[0].start():next_header]


def _evidence_agent(reference: str) -> str:
    try:
        resolved = EvidenceResolver(ROOT).resolve(EvidenceRef.parse(reference))
        source = read_stable_bytes(
            resolved.source_path, MAX_DAILY_BYTES, label="timeline evidence source"
        )
        block = _source_block(source, resolved.reference.block_id)
        return block_agent(block.decode("utf-8", errors="ignore"))
    except (EvidenceResolutionError, OSError, TypeError, ValueError):
        return "unknown"


def _page_agents(content: str) -> list[str]:
    agents = {_evidence_agent(reference) for reference in EVIDENCE_REF_RE.findall(content)}
    known = agents - {"unknown"}
    if known:
        return sorted(known)
    return ["unknown"]


def _knowledge_page_activities(
    md: Path, content: str, project_slug: str | None, days: int
) -> list[dict[str, str]]:
    metadata = _recent_page_metadata(content, days)
    if metadata is None:
        return []
    frontmatter, timestamp = metadata
    if not _page_matches_project(frontmatter, project_slug):
        return []
    page_type = _frontmatter_value(frontmatter, "type")
    text = _page_text(content, md.stem)
    return [
        {
            "date": timestamp[:10],
            "time": _page_time(timestamp),
            "agent": agent,
            "type": _page_event_type(page_type),
            "text": text,
            "path": md.relative_to(ROOT).as_posix(),
        }
        for agent in _page_agents(content)
    ]


def _extract_knowledge_timeline(project_slug: str | None, days: int) -> list[dict]:
    """Extract knowledge page creation timeline from frontmatter timestamps."""
    results: list[dict[str, str]] = []
    if not KNOWLEDGE.exists():
        return results

    for md in sorted(KNOWLEDGE.rglob("*.md")):
        try:
            content = md.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        results.extend(_knowledge_page_activities(md, content, project_slug, days))
    return results


def _daily_timeline(project_slug: str | None, days: int) -> list[dict[str, str]]:
    activities: list[dict[str, str]] = []
    if not DAILY_DIR.exists():
        return activities
    for daily in sorted(DAILY_DIR.glob("*.md"), reverse=True):
        activities.extend(_extract_activity(daily, project_slug, days))
    return activities


def _heartbeat_activity(
    slug: str, heartbeat: dict, project_slug: str | None
) -> dict[str, str] | None:
    if project_slug and slug.lower() != project_slug.lower():
        return None
    occurred_at = heartbeat.get("at", "")
    return {
        "date": occurred_at[:10],
        "time": _page_time(occurred_at),
        "agent": canonical_agent(heartbeat.get("reason", "")),
        "type": "heartbeat",
        "text": f"active in {slug}",
    }


def _heartbeat_timeline(project_slug: str | None) -> list[dict[str, str]]:
    try:
        heartbeats = load_state().get("codex_heartbeats", {})
    except Exception:
        return []
    activities: list[dict[str, str]] = []
    for slug, heartbeat in heartbeats.items():
        item = _heartbeat_activity(slug, heartbeat, project_slug)
        if item is not None:
            activities.append(item)
    return activities


def build_timeline(project_slug: str | None = None, days: int = 30) -> list[dict]:
    """Build a unified agent activity timeline."""
    activities = _daily_timeline(project_slug, days)
    activities.extend(_extract_knowledge_timeline(project_slug, days))
    activities.extend(_heartbeat_timeline(project_slug))
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
                 "tool": "[TOOL]", "heartbeat": "[ACTIVE]"}.get(atype, "")
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
