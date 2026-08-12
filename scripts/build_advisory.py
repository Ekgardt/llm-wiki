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
from memory_state import (  # noqa: E402
    REPORTS_DIR,
    ROOT,
    BoundedPathInventory,
    bounded_path_inventory,
    parse_frontmatter_scalar,
    parse_project_scope,
)
from session_start_project_state import (  # noqa: E402
    _read_trusted_state_body,
    _same_native_project_root,
    _slug_identity_key,
    _state_h2_title,
    _state_visible_lines,
    _trusted_state_body_matches_identity,
)

PROJECTS_DIR = ROOT / "knowledge" / "projects"
KNOWLEDGE = ROOT / "knowledge" / "notes"
DAILY_DIR = ROOT / "knowledge" / "daily"

FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?", re.DOTALL)
TIMESTAMP_RE = re.compile(r"^timestamp:\s*(.+?)\s*$", re.MULTILINE)
H1_RE = re.compile(r"^#\s+(.+?)\s*$", re.MULTILINE)
SUMMARY_RE = re.compile(
    r"^One-sentence summary:\s*(.+?)\s*$", re.MULTILINE | re.IGNORECASE
)
MAX_NOTE_BYTES = 64 * 1024
MAX_STATE_BYTES = 64 * 1024
MAX_LINT_REPORT_BYTES = 128 * 1024
MAX_NOTE_FILES_SCANNED = 1_000
MAX_LINT_REPORTS_SCANNED = 100
_TRUSTED_STATE_UNSET = object()


def _bounded_files(
    directory: Path,
    pattern: str,
    limit: int,
    *,
    recursive: bool,
) -> BoundedPathInventory:
    return bounded_path_inventory(
        directory,
        pattern,
        limit,
        recursive=recursive,
        kind="file",
    )


def _note_inventory() -> BoundedPathInventory:
    return _bounded_files(
        KNOWLEDGE,
        "*.md",
        MAX_NOTE_FILES_SCANNED,
        recursive=True,
    )


def _lint_report_inventory() -> BoundedPathInventory:
    return _bounded_files(
        REPORTS_DIR,
        "lint-*.md",
        MAX_LINT_REPORTS_SCANNED,
        recursive=False,
    )


def _read_text_bounded(path: Path, max_bytes: int) -> str | None:
    try:
        with path.open("rb") as handle:
            raw = handle.read(max_bytes + 1)
    except OSError:
        return None
    if len(raw) > max_bytes:
        return None
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _fm_field(content: str, pattern: re.Pattern) -> str | None:
    fm = FRONTMATTER_RE.match(content)
    if not fm:
        return None
    m = pattern.search(fm.group(1))
    return m.group(1).strip() if m else None


def _matches_project_identity(
    content: str,
    slug: str | None,
    project_root: str | Path | None,
) -> bool:
    scope = parse_project_scope(content)
    root_scope = parse_frontmatter_scalar(content, "project_root")
    if slug is None and project_root is None:
        return not scope.present and not root_scope.present
    expected_slug = _slug_identity_key(slug)
    return bool(
        expected_slug is not None
        and project_root is not None
        and scope.present
        and scope.value is not None
        and _slug_identity_key(scope.value) == expected_slug
        and root_scope.present
        and root_scope.value is not None
        and _same_native_project_root(root_scope.value, str(project_root))
    )


def _read_open_threads(
    slug: str,
    state_path: Path | None,
    project_root: str | Path | None = None,
    *,
    trusted_state_body: str | None | object = _TRUSTED_STATE_UNSET,
) -> list[str]:
    """Extract open threads from project state.md."""
    if trusted_state_body is _TRUSTED_STATE_UNSET:
        if state_path is None or project_root is None:
            return []
        try:
            ownership = _read_trusted_state_body(
                state_path,
                slug,
                Path(project_root),
            )
        except (ImportError, OSError, RuntimeError, TypeError, ValueError):
            return []
    else:
        ownership = trusted_state_body
    try:
        cached_identity_matches = bool(
            isinstance(ownership, str)
            and project_root is not None
            and _trusted_state_body_matches_identity(
                ownership,
                slug,
                Path(project_root),
            )
        )
    except (OSError, RuntimeError, TypeError, ValueError):
        cached_identity_matches = False
    if not cached_identity_matches:
        return []

    return _open_threads_from_state(ownership)


def _open_threads_from_state(ownership: str) -> list[str]:
    visible = _state_visible_lines(ownership)
    start = next(
        (
            index + 1
            for index, line in enumerate(visible)
            if _state_h2_title(line) == "open threads"
        ),
        None,
    )
    if start is None:
        return []
    threads: list[str] = []
    for line in visible[start:]:
        if _state_h2_title(line) is not None:
            break
        line = line.strip()
        if line.startswith("- ") and len(line) > 3:
            threads.append(line[2:].strip()[:120])
    return threads[:5]


def _find_last_decision(
    slug: str | None = None,
    inventory: BoundedPathInventory | None = None,
    *,
    project_root: str | Path | None = None,
) -> dict | None:
    """Find the most recent decision page (optionally filtered by project).

    The compiler writes decisions FLAT under knowledge/notes/ (not in a
    decisions/ subdir), so we scan all .md files and filter by
    frontmatter `type: decision`.
    """
    current = _note_inventory() if inventory is None else inventory
    if current.incomplete:
        return None
    candidates = []
    for md in current.paths:
        content = _read_text_bounded(md, MAX_NOTE_BYTES)
        if content is None:
            continue
        page_type = parse_frontmatter_scalar(content, "type")
        if page_type.value is None or page_type.value.casefold() != "decision":
            continue
        status = parse_frontmatter_scalar(content, "status")
        if status.present and (
            status.value is None
            or status.value.casefold() in {"archived", "superseded"}
        ):
            continue
        ts = _fm_field(content, TIMESTAMP_RE)
        if not ts:
            continue
        if not _matches_project_identity(content, slug, project_root):
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


def _find_contradictions(
    inventory: BoundedPathInventory | None = None,
) -> list[str]:
    """Check lint report for contradiction findings."""
    # Check if the last lint report exists and has findings
    current = _lint_report_inventory() if inventory is None else inventory
    if current.incomplete or not current.paths:
        return []
    report = _read_text_bounded(current.paths[-1], MAX_LINT_REPORT_BYTES)
    if report is None:
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


def _find_cross_project_insights(
    slug: str,
    inventory: BoundedPathInventory | None = None,
    *,
    project_root: str | Path | None = None,
) -> list[str]:
    """Find knowledge pages in OTHER projects that share concepts with this project."""
    # Get this project's pages' titles
    project_titles: set[str] = set()
    other_pages: list[dict] = []
    current = _note_inventory() if inventory is None else inventory
    if current.incomplete:
        return []
    for md in current.paths:
        content = _read_text_bounded(md, MAX_NOTE_BYTES)
        if content is None:
            continue
        scope = parse_project_scope(content)
        if scope.present and scope.value is None:
            continue
        project = scope.value
        root_scope = parse_frontmatter_scalar(content, "project_root")
        if (
            project is None
            or root_scope.value is None
            or _slug_identity_key(project) is None
        ):
            continue
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
        if _matches_project_identity(content, slug, project_root):
            project_titles.add(title)
        else:
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


def _find_stale_pages(inventory: BoundedPathInventory | None = None) -> int:
    """Count pages older than 90 days without supersede."""
    cutoff = (datetime.now().timestamp()) - (90 * 86400)
    count = 0
    current = _note_inventory() if inventory is None else inventory
    if current.incomplete:
        return 0
    for md in current.paths:
        try:
            content = _read_text_bounded(md, MAX_NOTE_BYTES)
            if content is None:
                continue
            modified = md.stat().st_mtime
        except OSError:
            continue
        status = parse_frontmatter_scalar(content, "status")
        if status.present and (
            status.value is None
            or status.value.casefold() in {"archived", "superseded"}
        ):
            continue
        if modified < cutoff:
            count += 1
    return count


def build_advisory(
    slug: str | None = None,
    max_chars: int = 800,
    use_llm: bool = False,
    *,
    state_path: Path | None = None,
    project_root: str | Path | None = None,
    trusted_state_body: str | None | object = _TRUSTED_STATE_UNSET,
) -> str:
    """Build the proactive advisory block for SessionStart injection.

    This is the "navigator" layer — actionable intelligence, not just inventory.
    Non-LLM, <100ms for rule-based. Optional LLM enhancement adds ~5-10s.

    Args:
        slug: Project slug to scope the advisory.
        max_chars: Maximum output length.
        use_llm: If True and LLM available, generate a richer insight paragraph.
    """
    # Always build the rule-based advisory first (fast, reliable)
    rule_based = _build_rule_based_advisory(
        slug,
        max_chars,
        state_path,
        project_root,
        trusted_state_body,
    )

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


def _build_rule_based_advisory(
    slug: str | None,
    max_chars: int,
    state_path: Path | None = None,
    project_root: str | Path | None = None,
    trusted_state_body: str | None | object = _TRUSTED_STATE_UNSET,
) -> str:
    """Build the fast rule-based advisory (no LLM)."""
    parts: list[str] = []
    note_inventory = _note_inventory()
    lint_inventory = _lint_report_inventory()

    # 1. Open threads (most actionable)
    if slug:
        threads = _read_open_threads(
            slug,
            state_path,
            project_root,
            trusted_state_body=trusted_state_body,
        )
        if threads:
            parts.append(f"**Open threads ({len(threads)}):**")
            for t in threads:
                parts.append(f"- {t}")
            parts.append("")

    # 2. Last decision
    if note_inventory.incomplete:
        parts.append("**Advisory sources:** knowledge inventory unavailable.")
        parts.append("")
    else:
        last = _find_last_decision(
            slug,
            note_inventory,
            project_root=project_root,
        )
        if last:
            parts.append(f"**Last decision** ({last['timestamp']}):")
            parts.append(f"- {last['title']}: {last['summary']}")
            parts.append("")

    # 3. Potential contradictions
    if lint_inventory.incomplete:
        parts.append("**Advisory sources:** lint report inventory unavailable.")
        parts.append("")
    else:
        contradictions = _find_contradictions(lint_inventory)
        if contradictions:
            parts.append(f"**Lint alerts ({len(contradictions)}):**")
            for c in contradictions:
                parts.append(f"- {c}")
            parts.append("")

    # 4. Cross-project insights
    if slug and not note_inventory.incomplete:
        insights = _find_cross_project_insights(
            slug,
            note_inventory,
            project_root=project_root,
        )
        if insights:
            parts.append("**Cross-project insights:**")
            for i in insights:
                parts.append(f"- {i}")
            parts.append("")

    # 5. Stale page count (gentle nudge)
    if not note_inventory.incomplete:
        stale = _find_stale_pages(note_inventory)
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
    p.add_argument("--project-root", default=None, help="Canonical project root")
    p.add_argument("--max-chars", type=int, default=800)
    p.add_argument("--llm", action="store_true", help="Enhance with LLM insight (needs ~5-10s)")
    args = p.parse_args()
    advisory = build_advisory(
        args.slug,
        args.max_chars,
        use_llm=args.llm,
        project_root=args.project_root,
    )
    if advisory:
        print(advisory)
    else:
        print("(no advisory — vault is clean)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
