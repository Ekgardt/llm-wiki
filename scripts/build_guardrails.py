"""Guard rails — auto-inject learned corrections to prevent repeating mistakes.

When feedback_capture saves a correction ("no, use JWT instead of
sessions"), it becomes a knowledge page after promotion. But the
agent doesn't SEE that page when working unless it searches for it.

This module compiles all promoted corrections + preferences into a
compact "rules" block that gets injected at SessionStart. The agent
sees them BEFORE acting — preventing the same mistake.

Think of it as "instincts" (nvk/ECC terminology): rules the agent
has internalized from past corrections.

Flow:
  User corrects agent → feedback_capture → promote → knowledge page
                                                  ↓
                                          build_guardrails reads it
                                                  ↓
                                          SessionStart injection
                                                  ↓
                                    Agent sees rule BEFORE acting
                                                  ↓
                                    Same mistake NOT repeated

Usage:
    uv run python scripts/build_guardrails.py                    # print rules
    uv run python scripts/build_guardrails.py --project your-project  # project-scoped
    uv run python scripts/build_guardrails.py --apply              # write to vault
"""
from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from memory_state import (  # noqa: E402
    ROOT,
    BoundedPathInventory,
    bounded_path_inventory,
    parse_frontmatter_scalar,
    parse_project_scope,
    read_json_object_file_bounded,
)
from session_start_project_state import (  # noqa: E402
    _same_native_project_root,
    _slug_identity_key,
)

KNOWLEDGE = ROOT / "knowledge" / "notes"
FEEDBACK_DIR = ROOT / "knowledge" / "feedback"
GUARDRAILS_FILE = ROOT / "knowledge" / "guardrails.md"

FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?", re.DOTALL)
TIMESTAMP_RE = re.compile(r"^timestamp:\s*(.+?)\s*$", re.MULTILINE)
H1_RE = re.compile(r"^#\s+(.+?)\s*$", re.MULTILINE)
SUMMARY_RE = re.compile(
    r"^One-sentence summary:\s*(.+?)\s*$", re.MULTILINE | re.IGNORECASE
)
MAX_NOTE_BYTES = 64 * 1024
MAX_FEEDBACK_BYTES = 64 * 1024
MAX_NOTE_FILES_SCANNED = 1_000
MAX_FEEDBACK_FILES_SCANNED = 1_000
MAX_FEEDBACK_JSON_DEPTH = 64


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


def _matches_project_identity(
    content: str,
    project: str | None,
    project_root: str | Path | None,
) -> bool:
    scope = parse_project_scope(content)
    root_scope = parse_frontmatter_scalar(content, "project_root")
    if project is None and project_root is None:
        return not scope.present and not root_scope.present
    expected_slug = _slug_identity_key(project)
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


def _feedback_matches_project_identity(
    project: str,
    project_root: str,
    expected_project: str | None,
    expected_root: str | Path | None,
) -> bool:
    if expected_project is None and expected_root is None:
        return not project and not project_root
    expected_slug = _slug_identity_key(expected_project)
    return bool(
        expected_slug is not None
        and expected_root is not None
        and _slug_identity_key(project) == expected_slug
        and _same_native_project_root(project_root, str(expected_root))
    )


def _collect_corrections(
    project: str | None = None,
    project_root: str | Path | None = None,
) -> list[dict] | None:
    """Collect all knowledge pages that are corrections/preferences/rules.

    Sources:
    1. Knowledge pages with type: correction/preference/requirement
    2. Promoted feedback candidates (from knowledge/feedback/)
    3. Patterns with 'do not' / 'always' / 'never' in summary
    """
    corrections = []
    note_inventory = _bounded_files(
        KNOWLEDGE,
        "*.md",
        MAX_NOTE_FILES_SCANNED,
        recursive=True,
    )
    feedback_inventory = _bounded_files(
        FEEDBACK_DIR,
        "*.json",
        MAX_FEEDBACK_FILES_SCANNED,
        recursive=False,
    )
    if note_inventory.incomplete or feedback_inventory.incomplete:
        return None

    # Source 1: knowledge pages with correction-like types
    if note_inventory.paths:
        for md in note_inventory.paths:
            content = _read_text_bounded(md, MAX_NOTE_BYTES)
            if content is None:
                continue
            fm = FRONTMATTER_RE.match(content)
            if not fm:
                continue
            status = parse_frontmatter_scalar(content, "status")
            if status.present and (
                status.value is None
                or status.value.casefold() in {"archived", "superseded"}
            ):
                continue
            page_type_field = parse_frontmatter_scalar(content, "type")
            page_type = page_type_field.value
            if page_type not in ("pattern", "decision", "qa", "debugging"):
                continue
            summary = _extract(content, SUMMARY_RE) or ""
            if not re.search(r"\b(do not|don'?t|always|never|must|should)\b", summary, re.IGNORECASE):
                continue

            if not _matches_project_identity(content, project, project_root):
                continue

            title_m = H1_RE.search(content)
            summary_m = SUMMARY_RE.search(content)
            corrections.append({
                "type": page_type,
                "title": title_m.group(1).strip() if title_m else md.stem,
                "summary": (summary_m.group(1).strip()[:150] if summary_m else ""),
                "source": "knowledge",
                "path": md.relative_to(ROOT).as_posix(),
            })

    # Source 2: promoted feedback candidates
    if feedback_inventory.paths:
        for f in feedback_inventory.paths:
            candidate = read_json_object_file_bounded(
                f,
                max_bytes=MAX_FEEDBACK_BYTES,
                max_depth=MAX_FEEDBACK_JSON_DEPTH,
            )
            if candidate is None:
                continue
            status = candidate.get("status")
            if not isinstance(status, str) or status != "promoted":
                continue
            fields = {
                "project": candidate.get("project", ""),
                "project_root": candidate.get("project_root", ""),
                "type": candidate.get("type", "feedback"),
                "text": candidate.get("text", ""),
                "promoted_to": candidate.get("promoted_to", ""),
            }
            if any(not isinstance(value, str) for value in fields.values()):
                continue
            if not _feedback_matches_project_identity(
                fields["project"],
                fields["project_root"],
                project,
                project_root,
            ):
                continue
            corrections.append({
                "type": fields["type"],
                "title": fields["text"][:80],
                "summary": fields["text"][:150],
                "source": "feedback",
                "path": fields["promoted_to"],
            })

    return corrections


def _extract(text: str, pattern: re.Pattern) -> str | None:
    m = pattern.search(text)
    return m.group(1).strip() if m else None


def build_guardrails(
    project: str | None = None,
    max_rules: int = 15,
    *,
    project_root: str | Path | None = None,
) -> str | None:
    """Build the guard rails block for SessionStart injection.

    This is the "learned instincts" — rules the agent must follow
    because they were learned from past corrections.
    """
    corrections = _collect_corrections(project, project_root)

    if corrections is None:
        return None
    if not corrections:
        return ""

    # Deduplicate by summary similarity (simple)
    seen: set[str] = set()
    unique = []
    for c in corrections:
        key = c["summary"].lower()[:60]
        if key not in seen:
            seen.add(key)
            unique.append(c)

    unique = unique[:max_rules]

    lines = ["## Guard rails (learned rules — do NOT repeat these mistakes)\n"]

    by_type: dict[str, list[dict]] = {}
    for c in unique:
        by_type.setdefault(c["type"], []).append(c)

    for rtype in sorted(by_type.keys()):
        rules = by_type[rtype]
        label = {
            "correction": "CORRECTION",
            "preference": "PREFERENCE",
            "requirement": "REQUIREMENT",
            "instruction": "INSTRUCTION",
            "pattern_rule": "RULE",
        }.get(rtype, rtype.upper())

        lines.append(f"**{label}** ({len(rules)}):")
        for r in rules[:5]:
            lines.append(f"- {r['summary']}")
        lines.append("")

    return "\n".join(lines).strip()


def main() -> int:
    p = argparse.ArgumentParser(description="Build guard rails from learned corrections.")
    p.add_argument("--project", default=None, help="Filter by project")
    p.add_argument("--project-root", default=None, help="Canonical project root")
    p.add_argument("--max-rules", type=int, default=15)
    p.add_argument("--apply", action="store_true", help="Write to knowledge/guardrails.md")
    args = p.parse_args()

    guardrails = build_guardrails(
        args.project,
        args.max_rules,
        project_root=args.project_root,
    )

    if guardrails is None:
        print("(guard rail inventory unavailable)")
        return 0
    if not guardrails:
        print("(no guard rails — no corrections learned yet)")
        return 0

    if args.apply:
        GUARDRAILS_FILE.write_text(
            f"---\n"
            f"type: guardrails\n"
            f'title: "Learned Guard Rails"\n'
            f'description: "Auto-generated rules from past corrections"\n'
            f"timestamp: {datetime.now().isoformat(timespec='seconds')}\n"
            f"---\n\n"
            f"{guardrails}\n",
            encoding="utf-8",
        )
        print(f"Written: {GUARDRAILS_FILE.relative_to(ROOT)}")
    else:
        print(guardrails)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
