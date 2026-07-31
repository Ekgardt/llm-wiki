"""Build bounded project context from one confirmed project registry entry."""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from memory_state import (  # noqa: E402
    ROOT,
    BoundedPathInventory,
    atomic_write,
    bounded_path_inventory,
    load_state,
    parse_frontmatter_scalar,
    parse_project_scope,
)
from session_start_context import (  # noqa: E402
    _latest_useful_daily,
    _recent_daily_paths,
)
from session_start_project_state import (  # noqa: E402
    ProjectAliasResolution,
    _read_state_ownership_body,
    _slug_identity_key,
    resolve_project_alias,
)

KNOWLEDGE = ROOT / "knowledge" / "notes"
DAILY_DIR = ROOT / "knowledge" / "daily"
PROJECTS_DIR = ROOT / "knowledge" / "projects"

MAX_KNOWLEDGE_ENTRIES = 2_000
MAX_FEEDBACK_ENTRIES = 1_000
MAX_NOTE_BYTES = 64 * 1024
MAX_FEEDBACK_BYTES = 64 * 1024

FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?", re.DOTALL)
H1_RE = re.compile(r"^#\s+(.+?)\s*$", re.MULTILINE)
SUMMARY_RE = re.compile(
    r"^One-sentence summary:\s*(.+?)\s*$",
    re.MULTILINE | re.IGNORECASE,
)
SOURCE_AUTHORITY_FIELD_RE = re.compile(
    r"^source_authority:\s*(.+?)\s*$",
    re.MULTILINE,
)


def _read_text_bounded(path: Path, max_bytes: int) -> str | None:
    try:
        with path.open("rb") as handle:
            raw = handle.read(max_bytes + 1)
        if len(raw) > max_bytes:
            return None
        return raw.decode("utf-8", errors="strict")
    except (OSError, UnicodeDecodeError):
        return None


def _extract_frontmatter_field(content: str, pattern: re.Pattern) -> str | None:
    frontmatter = FRONTMATTER_RE.match(content)
    if not frontmatter:
        return None
    match = pattern.search(frontmatter.group(1))
    return match.group(1).strip() if match else None


def _note_inventory() -> BoundedPathInventory:
    return bounded_path_inventory(
        KNOWLEDGE,
        "*.md",
        MAX_KNOWLEDGE_ENTRIES,
        recursive=True,
        kind="file",
    )


def _feedback_inventory() -> BoundedPathInventory:
    return bounded_path_inventory(
        ROOT / "knowledge" / "feedback",
        "*.json",
        MAX_FEEDBACK_ENTRIES,
        recursive=False,
        kind="file",
    )


def _find_project_pages(
    slug: str,
    inventory: BoundedPathInventory | None = None,
) -> list[dict]:
    """Find bounded active knowledge pages assigned to the exact project alias."""
    current = _note_inventory() if inventory is None else inventory
    if current.incomplete:
        return []
    slug_key = _slug_identity_key(slug)
    results = []
    for markdown in current.paths:
        content = _read_text_bounded(markdown, MAX_NOTE_BYTES)
        if content is None:
            continue
        scope = parse_project_scope(content)
        if (
            not scope.present
            or scope.value is None
            or _slug_identity_key(scope.value) != slug_key
        ):
            continue
        status_field = parse_frontmatter_scalar(content, "status")
        if status_field.present and status_field.value is None:
            continue
        status = status_field.value or "active"
        if status.casefold() in {"archived", "superseded"}:
            continue
        page_type_field = parse_frontmatter_scalar(content, "type")
        if page_type_field.present and page_type_field.value is None:
            continue
        page_type = page_type_field.value or "unknown"
        title_match = H1_RE.search(content)
        summary_match = SUMMARY_RE.search(content)
        results.append(
            {
                "path": markdown.relative_to(ROOT).as_posix(),
                "type": page_type,
                "status": status,
                "title": title_match.group(1).strip() if title_match else markdown.stem,
                "summary": summary_match.group(1).strip() if summary_match else "",
            }
        )
    return results


def _find_recent_daily_activity(
    slug: str,
    project_root: Path,
    days: int = 7,
) -> list[str]:
    """Render one bounded non-tool record matching both alias and owned root."""
    paths = _recent_daily_paths(daily_dir=DAILY_DIR)[: max(0, days)]
    selected = _latest_useful_daily(slug, project_root, paths)
    if selected is None:
        return []
    daily_path, record = selected
    return [
        f"{daily_path.stem}: {line.strip()[:120]}"
        for line in record.lines[:5]
        if line.strip()
    ]


def _read_state_handoff(state_path: Path) -> str:
    """Read the handoff from the exact bounded state selected by the registry."""
    content = _read_state_ownership_body(state_path)
    if content is None:
        return ""
    match = re.search(
        r"^##\s*Where we left off\s*$\n(.*?)(?=\n##\s|\Z)",
        content,
        re.MULTILINE | re.DOTALL,
    )
    return match.group(1).strip() if match else ""


def _detect_agent_strengths(
    agent: str,
    note_inventory: BoundedPathInventory | None = None,
    feedback_inventory: BoundedPathInventory | None = None,
) -> list[str] | None:
    """Derive agent-specific page type ordering from bounded local evidence."""
    type_counts: dict[str, int] = {}
    notes = _note_inventory() if note_inventory is None else note_inventory
    if not notes.incomplete:
        for markdown in notes.paths:
            content = _read_text_bounded(markdown, MAX_NOTE_BYTES)
            if content is None:
                continue
            source_authority = (
                _extract_frontmatter_field(content, SOURCE_AUTHORITY_FIELD_RE) or ""
            )
            if agent.casefold() not in source_authority.casefold():
                continue
            page_type = parse_frontmatter_scalar(content, "type")
            if page_type.value:
                type_counts[page_type.value] = type_counts.get(page_type.value, 0) + 1

    feedback = _feedback_inventory() if feedback_inventory is None else feedback_inventory
    if not feedback.incomplete:
        for path in feedback.paths:
            content = _read_text_bounded(path, MAX_FEEDBACK_BYTES)
            if content is None:
                continue
            try:
                candidate = json.loads(content)
            except json.JSONDecodeError:
                continue
            if not isinstance(candidate, dict):
                continue
            text = str(candidate.get("text", ""))
            project = str(candidate.get("project", ""))
            if agent.casefold() not in text.casefold() and agent.casefold() not in project.casefold():
                continue
            feedback_type = str(candidate.get("type", ""))
            key = f"feedback_{feedback_type}"
            type_counts[key] = type_counts.get(key, 0) + 1

    if not type_counts:
        return None
    ranked = sorted(type_counts.items(), key=lambda item: item[1], reverse=True)
    return [page_type for page_type, _count in ranked[:5]]


def _matching_heartbeat(slug: str) -> dict:
    try:
        heartbeats = load_state().get("codex_heartbeats", {})
    except Exception:
        return {}
    if not isinstance(heartbeats, dict):
        return {}
    slug_key = _slug_identity_key(slug)
    matches = [
        heartbeat
        for candidate, heartbeat in heartbeats.items()
        if _slug_identity_key(candidate) == slug_key and isinstance(heartbeat, dict)
    ]
    return matches[0] if len(matches) == 1 else {}


def _build_resolved_context(
    identity: ProjectAliasResolution,
    max_chars: int,
    agent: str | None,
) -> str:
    slug = identity.slug
    parts = [f"## Project context: {slug}\n"]

    handoff = _read_state_handoff(identity.state_path)
    if handoff:
        parts.append(f"### Where you left off\n{handoff[:500]}\n")

    note_inventory = _note_inventory()
    pages = _find_project_pages(slug, note_inventory)
    agent_priority: list[str] | None = None
    if agent:
        agent_priority = _detect_agent_strengths(agent.casefold(), note_inventory)
        if agent_priority:
            pages.sort(
                key=lambda page: (
                    agent_priority.index(page["type"])
                    if page["type"] in agent_priority
                    else 99
                )
            )

    if pages:
        parts.append(f"### Known knowledge ({len(pages)} pages)")
        by_type: dict[str, list[dict]] = {}
        for page in pages:
            by_type.setdefault(page["type"], []).append(page)
        type_order = sorted(by_type)
        if agent_priority:
            ranked_types = [page_type for page_type in agent_priority if page_type in by_type]
            type_order = [
                *ranked_types,
                *(page_type for page_type in type_order if page_type not in ranked_types),
            ]
        for page_type in type_order:
            parts.append(f"**{page_type}s:**")
            for page in by_type[page_type][:5]:
                summary = page["summary"][:80] if page["summary"] else page["title"]
                parts.append(f"- {summary}")
            parts.append("")

    activity = _find_recent_daily_activity(slug, identity.project_root)
    if activity:
        parts.append("### Recent activity (last 7 days)")
        parts.extend(f"- {line}" for line in activity[:5])
        parts.append("")

    heartbeat = _matching_heartbeat(slug)
    if heartbeat:
        parts.append("### Last seen")
        parts.append(
            f"- {heartbeat.get('reason', 'unknown')} at {heartbeat.get('at', '?')}"
        )

    text = "\n".join(parts).strip()
    if len(text) > max_chars:
        text = text[: max(0, max_chars - 20)].rstrip() + "\n... (truncated)\n"
    return text


def build_context(
    slug: str,
    max_chars: int = 2000,
    agent: str | None = None,
) -> str:
    """Build context only when the alias resolves to one owned registry state."""
    identity = resolve_project_alias(slug, PROJECTS_DIR)
    if identity is None:
        return ""
    return _build_resolved_context(identity, max_chars, agent)


def _context_document(identity: ProjectAliasResolution, context: str) -> str:
    timestamp = datetime.now().isoformat(timespec="seconds")
    return (
        "---\n"
        "type: project-context\n"
        f"title: \"{identity.slug} context\"\n"
        f"description: \"Auto-generated project context for {identity.slug}\"\n"
        f"timestamp: {timestamp}\n"
        "---\n\n"
        f"# {identity.slug} - Auto-Context\n\n"
        "Generated by `scripts/build_context.py`. Do not edit manually; "
        "this file is regenerated on each compile pass.\n\n"
        f"{context}\n"
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build bounded per-project context from the project registry."
    )
    parser.add_argument("slug", help="Persisted project runtime alias")
    parser.add_argument("--max-chars", type=int, default=2000)
    parser.add_argument(
        "--write",
        action="store_true",
        help="Write beside the exact registered state.md",
    )
    args = parser.parse_args()

    identity = resolve_project_alias(args.slug, PROJECTS_DIR)
    if identity is None:
        print("build_context: project alias is missing or ambiguous", file=sys.stderr)
        return 1
    context = _build_resolved_context(identity, args.max_chars, None)
    if not args.write:
        print(context)
        return 0

    output_path = identity.state_path.parent / "context.md"
    atomic_write(output_path, _context_document(identity, context))
    try:
        display_path = output_path.relative_to(ROOT)
    except ValueError:
        display_path = output_path
    print(f"Written: {display_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
