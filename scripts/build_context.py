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
import json
import re
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from context_budget import (  # noqa: E402
    DEFAULT_CONTEXT_BUDGET,
    BudgetExceededError,
    ContextItem,
)
from context_compiler import compile_context_items  # noqa: E402
from markdown_transaction import mutate_knowledge, stable_operation_id  # noqa: E402
from memory_state import ROOT, load_state  # noqa: E402
from secret_redact import redact_secrets  # noqa: E402

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


def _read_text_or_none(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None


def _frontmatter_value(content: str, field: str) -> str | None:
    """A frontmatter field read the way the agent-strength and retirement checks always read it."""
    fm_match = re.match(r"^---\s*\n(.*?)\n---", content, re.DOTALL)
    if not fm_match:
        return None
    match = re.search(rf"^{field}:\s*(.+?)\s*$", fm_match.group(1), re.MULTILINE)
    if not match:
        return None
    return match.group(1).strip()


def _find_project_pages(slug: str) -> list[dict]:
    """Find all knowledge pages tagged with `project: <slug>`."""
    if not KNOWLEDGE.exists():
        return []
    pages = (_project_page(md, slug) for md in sorted(KNOWLEDGE.rglob("*.md")))
    return [page for page in pages if page is not None]


def _project_page(md: Path, slug: str) -> dict | None:
    content = _read_text_or_none(md)
    if content is None or _frontmatter_value(content, "status") in ("archived", "superseded"):
        return None
    project = _extract_frontmatter_field(content, PROJECT_FIELD_RE)
    if not project or project.lower().strip() != slug.lower().strip():
        return None
    return _page_record(md, content)


def _page_record(md: Path, content: str) -> dict:
    title_match = H1_RE.search(content)
    summary_match = SUMMARY_RE.search(content)
    return {
        "path": md.relative_to(ROOT).as_posix(),
        "type": _extract_frontmatter_field(content, TYPE_FIELD_RE) or "unknown",
        "status": _extract_frontmatter_field(content, STATUS_FIELD_RE) or "active",
        "title": title_match.group(1).strip() if title_match else md.stem,
        "summary": summary_match.group(1).strip() if summary_match else "",
    }


def _find_recent_daily_activity(slug: str, days: int = 7) -> list[str]:
    """Find recent daily-log breadcrumbs mentioning this slug."""
    if not DAILY_DIR.exists():
        return []
    cutoff = datetime.now().timestamp() - (days * 86400)
    results: list[str] = []
    for md in sorted(DAILY_DIR.glob("*.md"), reverse=True):
        results.extend(_slug_lines(md, slug, cutoff))
        if len(results) >= 10:
            return results[:10]
    return results


def _slug_lines(md: Path, slug: str, cutoff: float) -> list[str]:
    """The non-blank lines of one recent daily log that mention the slug."""
    content = _recent_text(md, cutoff)
    if content is None or slug.lower() not in content.lower():
        return []
    return [f"{md.stem}: {line.strip()}" for line in content.splitlines() if _mentions(line, slug)]


def _mentions(line: str, slug: str) -> bool:
    return slug.lower() in line.lower() and bool(line.strip())


def _recent_text(md: Path, cutoff: float) -> str | None:
    try:
        if md.stat().st_mtime < cutoff:
            return None
        return md.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None


def _read_state_handoff(slug: str) -> str:
    """Read the 'Where we left off' section from project state.md."""
    state_path = PROJECTS_DIR / slug / "state.md"
    # Containment guard: slug must not escape PROJECTS_DIR (no .., no abs).
    if not state_path.resolve().is_relative_to(PROJECTS_DIR.resolve()):
        print(f"build_context: slug escapes PROJECTS_DIR: {slug!r}", file=sys.stderr)
        return ""
    content = _existing_text(state_path)
    if content is None:
        return ""
    return _where_we_left_off(content)


def _existing_text(path: Path) -> str | None:
    if not path.exists():
        return None
    return _read_text_or_none(path)


def _where_we_left_off(content: str) -> str:
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
    type_counts: dict[str, int] = {}
    _count_authored_types(agent, type_counts)
    _count_feedback_types(agent, type_counts)
    if not type_counts:
        return None  # no data → balanced view
    # Rank types by frequency (most contributions = strongest area)
    ranked = sorted(type_counts.items(), key=lambda x: x[1], reverse=True)
    return [t for t, _ in ranked[:5]]


def _count_authored_types(agent: str, type_counts: dict[str, int]) -> None:
    """Count knowledge pages whose source_authority names the agent, by page type."""
    if not KNOWLEDGE.exists():
        return
    for md in KNOWLEDGE.rglob("*.md"):
        page_type = _authored_page_type(md, agent)
        if page_type is not None:
            type_counts[page_type] = type_counts.get(page_type, 0) + 1


def _authored_page_type(md: Path, agent: str) -> str | None:
    content = _read_text_or_none(md)
    if content is None:
        return None
    # Only the frontmatter source_authority counts; never a substring scan of the body.
    if agent.lower() not in (_frontmatter_value(content, "source_authority") or "").lower():
        return None
    return _frontmatter_value(content, "type")


def _count_feedback_types(agent: str, type_counts: dict[str, int]) -> None:
    """Feedback naming the agent: corrections still mark an area it is active in."""
    feedback_dir = ROOT / "knowledge" / "feedback"
    if not feedback_dir.exists():
        return
    for path in feedback_dir.glob("*.json"):
        feedback_type = _agent_feedback_type(path, agent)
        if feedback_type is not None:
            key = f"feedback_{feedback_type}"
            type_counts[key] = type_counts.get(key, 0) + 1


def _agent_feedback_type(path: Path, agent: str) -> str | None:
    try:
        fb = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if agent.lower() in fb.get("text", "").lower() or agent.lower() in fb.get("project", "").lower():
        return fb.get("type", "")
    return None


def _project_context_item(
    kind: str,
    index: int,
    text: str,
    *,
    total: int,
) -> ContextItem | None:
    """Build a ContextItem from one project-context section."""
    stripped = (text or "").strip()
    if not stripped:
        return None
    priority = {"orientation": 3, "handoff": 3, "evidence": 5, "history": 7}.get(
        kind, 5
    )
    priority_class = {
        "orientation": "evidence",
        "handoff": "handoff",
        "evidence": "evidence",
        "history": "history",
    }.get(kind, "evidence")
    mandatory = kind == "handoff"
    return ContextItem(
        item_id=f"project:{kind}:{index:03d}",
        text=stripped,
        source=f"build_context:{kind}",
        priority=priority,
        relevance=1.0 if index == 0 else max(0.1, 1.0 - (index / max(total, 1))),
        confidence="medium",
        freshness="fresh",
        token_cost=len(stripped.encode("utf-8")),
        mandatory=mandatory,
        representation="l1",
        parent_id="project-handoff" if mandatory else "project-context",
        priority_class=priority_class,
    )


def _pack_project_context(parts: list[tuple[str, str]], max_chars: int) -> str:
    """Pack project-context sections under one shared token budget.

    Replaces the independent character cap with a shared budget; the legacy
    character limit survives as the emergency_byte_cap failure guard so
    Markdown is never sliced mid-item.
    """
    items = _project_context_items(parts)
    if not items:
        return ""
    try:
        packed = compile_context_items(
            items,
            budget=DEFAULT_CONTEXT_BUDGET,
            emergency_byte_cap=max_chars,
            per_source_cap=5,
            per_parent_cap=12,
        )
        return packed.text
    except BudgetExceededError as error:
        return error.failure.render(max_bytes=max_chars)


def _project_context_items(parts: list[tuple[str, str]]) -> list[ContextItem]:
    items = (
        _project_context_item(kind, index, text, total=len(parts))
        for index, (kind, text) in enumerate(parts)
    )
    return [item for item in items if item is not None]


def build_context(slug: str, max_chars: int = 2000, agent: str | None = None) -> str:
    """Build the project-context injection block.

    Args:
        slug: Project slug to scope the context.
        max_chars: Emergency byte cap; packed output never exceeds this.
        agent: Agent name. Context is auto-tailored based on the agent's
               demonstrated strengths (derived from feedback history),
               NOT hardcoded assumptions about which tool is "better at X".
    """
    parts = [
        ("orientation", f"## Project context: {slug}\n"),
        *_handoff_part(slug),  # 1. Handoff note from state.md
        *_knowledge_part(slug, agent),  # 2. Knowledge pages tagged for this project
        *_activity_part(slug),  # 3. Recent activity
        *_heartbeat_part(slug),  # 4. Heartbeat from state.json
    ]
    return _pack_project_context(parts, max_chars)


def _handoff_part(slug: str) -> list[tuple[str, str]]:
    handoff = _read_state_handoff(slug)
    if not handoff:
        return []
    return [("handoff", f"### Where you left off\n{handoff}\n")]


def _knowledge_part(slug: str, agent: str | None) -> list[tuple[str, str]]:
    active_pages = [p for p in _find_project_pages(slug) if p["status"] != "superseded"]
    if agent:
        _order_by_strengths(active_pages, agent.lower())
    if not active_pages:
        return []
    return [("evidence", _knowledge_section(active_pages))]


def _order_by_strengths(pages: list[dict], agent: str) -> None:
    """Per-agent ordering (Dorabotka C v2: auto-detected strengths, not hardcoded).

    The knowledge types the agent has contributed come first; with no
    history the order stays balanced.
    """
    agent_priority = _detect_agent_strengths(agent)
    if not agent_priority:
        return
    pages.sort(key=lambda p: _strength_rank(agent_priority, p["type"]))


def _strength_rank(agent_priority: list[str], page_type: str) -> int:
    if page_type in agent_priority:
        return agent_priority.index(page_type)
    return 99


def _knowledge_section(active_pages: list[dict]) -> str:
    knowledge = [f"### Known knowledge ({len(active_pages)} pages)"]
    by_type: dict[str, list[dict]] = {}
    for p in active_pages:
        by_type.setdefault(p["type"], []).append(p)
    for ptype in sorted(by_type.keys()):
        knowledge.extend([f"**{ptype}s:**", *(f"- {p['summary'] or p['title']}" for p in by_type[ptype][:5]), ""])
    return "\n".join(knowledge)


def _activity_part(slug: str) -> list[tuple[str, str]]:
    activity = _find_recent_daily_activity(slug)
    if not activity:
        return []
    recent = ["### Recent activity (last 7 days)", *(f"- {line}" for line in activity[:5])]
    return [("history", "\n".join(recent))]


def _heartbeat_part(slug: str) -> list[tuple[str, str]]:
    try:
        return _heartbeat_lines(load_state(), slug)
    except Exception:
        return []


def _heartbeat_lines(state: dict, slug: str) -> list[tuple[str, str]]:
    hb = state.get("codex_heartbeats", {}).get(slug, {})
    if not hb:
        return []
    return [("history", "### Last seen\n" f"- {hb.get('reason', 'unknown')} at {hb.get('at', '?')}")]


def main() -> int:
    p = argparse.ArgumentParser(description="Build per-project context for SessionStart.")
    p.add_argument("slug", help="Project slug (e.g. 'your-project')")
    p.add_argument("--max-chars", type=int, default=2000)
    p.add_argument("--write", action="store_true", help="Write to knowledge/projects/<slug>/context.md")
    args = p.parse_args()

    context = build_context(args.slug, args.max_chars)
    if args.write:
        if not re.match(r"^[a-zA-Z0-9_-]+$", args.slug):
            print("build_context: slug must be alphanumeric+hyphens only", file=sys.stderr)
            return 1
        out = PROJECTS_DIR / args.slug / "context.md"
        # Containment guard: slug must not escape PROJECTS_DIR (no .., no abs).
        if not out.resolve().is_relative_to(PROJECTS_DIR.resolve()):
            print(f"build_context: slug escapes PROJECTS_DIR: {args.slug!r}", file=sys.stderr)
            return 1
        rendered = (
            "---\n"
            f"type: project-context\ntitle: \"{args.slug} context\"\n"
            f"description: \"Auto-generated project context for {args.slug}\"\n"
            f"timestamp: {datetime.now().isoformat(timespec='seconds')}\n"
            "---\n\n"
            f"# {args.slug} — Auto-Context\n\n"
            f"Generated by `scripts/build_context.py`. Do not edit manually — "
            f"this file is regenerated on each compile pass.\n\n"
            f"{context}\n"
        )
        encoded = redact_secrets(rendered).encode("utf-8")
        mutate_knowledge(
            stable_operation_id("project-context", args.slug, encoded), {out: encoded}
        )
        print(f"Written: {out.relative_to(ROOT)}")
    else:
        print(context)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
