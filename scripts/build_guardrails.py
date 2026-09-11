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
import json
import re
import sys
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from bounded_io import read_stable_bytes  # noqa: E402
from claim_tree_manifest import snapshot_guardrail_sources_with_content  # noqa: E402
from markdown_transaction import (  # noqa: E402
    ABSENT,
    MAX_KNOWLEDGE_TARGET_BYTES,
    mutate_knowledge,
    stable_operation_id,
)
from memory_state import ROOT  # noqa: E402
from reliable_memory import sha256_bytes  # noqa: E402

KNOWLEDGE = ROOT / "knowledge" / "notes"
FEEDBACK_DIR = ROOT / "knowledge" / "feedback"
GUARDRAILS_FILE = ROOT / "knowledge" / "guardrails.md"
MAX_GUARDRAILS_BYTES = MAX_KNOWLEDGE_TARGET_BYTES

FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?", re.DOTALL)
TYPE_RE = re.compile(r"^type:\s*(.+?)\s*$", re.MULTILINE)
STATUS_RE = re.compile(r"^status:\s*(.+?)\s*$", re.MULTILINE)
PROJECT_RE = re.compile(r"^project:\s*[\"']?([^\"'\n]+)[\"']?\s*$", re.MULTILINE)
TIMESTAMP_RE = re.compile(r"^timestamp:\s*(.+?)\s*$", re.MULTILINE)
H1_RE = re.compile(r"^#\s+(.+?)\s*$", re.MULTILINE)
SUMMARY_RE = re.compile(
    r"^One-sentence summary:\s*(.+?)\s*$", re.MULTILINE | re.IGNORECASE
)


_RULE_TYPES = ("pattern", "decision", "qa", "debugging")
_RULE_WORDS = re.compile(r"\b(do not|don'?t|always|never|must|should)\b", re.IGNORECASE)


def _collect_corrections(
    project: str | None = None,
    *,
    source_contents: Mapping[str, bytes] | None = None,
) -> list[dict]:
    """Collect all knowledge pages that are corrections/preferences/rules.

    Sources:
    1. Knowledge pages with type: correction/preference/requirement
    2. Promoted feedback candidates (from knowledge/feedback/)
    3. Patterns with 'do not' / 'always' / 'never' in summary
    """
    if source_contents is None:
        _, source_contents = snapshot_guardrail_sources_with_content(ROOT)
    notes = _entries(_selected(source_contents, "knowledge/notes/", ".md"), _knowledge_correction, project)
    feedback = _entries(_selected(source_contents, "knowledge/feedback/", ".json"), _feedback_correction, project)
    return notes + feedback


def _selected(source_contents: Mapping[str, bytes], prefix: str, suffix: str) -> list[tuple[str, bytes]]:
    return [
        (relative, source_bytes)
        for relative, source_bytes in source_contents.items()
        if relative.startswith(prefix) and relative.endswith(suffix)
    ]


def _entries(selected: list[tuple[str, bytes]], reader, project: str | None) -> list[dict]:
    entries = (reader(relative, source_bytes, project) for relative, source_bytes in selected)
    return [entry for entry in entries if entry is not None]


def _knowledge_correction(relative: str, source_bytes: bytes, project: str | None) -> dict | None:
    """Source 1: an active knowledge page of a rule-like type whose summary states a rule."""
    content = source_bytes.decode("utf-8", errors="ignore")
    fm = FRONTMATTER_RE.match(content)
    if not fm:
        return None
    page_type = _rule_page_type(fm.group(1), content, project)
    if page_type is None:
        return None
    return _knowledge_rule(relative, content, page_type)


def _rule_page_type(fm_text: str, content: str, project: str | None) -> str | None:
    page_type = _extract(fm_text, TYPE_RE)
    if _retired(fm_text) or page_type not in _RULE_TYPES:
        return None
    if not _states_a_rule(content) or not _in_scope(_extract(fm_text, PROJECT_RE), project):
        return None
    return page_type


def _retired(fm_text: str) -> bool:
    status = _extract(fm_text, STATUS_RE)
    return bool(status) and status.strip() in ("archived", "superseded")


def _states_a_rule(content: str) -> bool:
    summary = _extract(content, SUMMARY_RE) or ""
    return _RULE_WORDS.search(summary) is not None


def _in_scope(page_project: str | None, project: str | None) -> bool:
    if not project or not page_project:
        return True
    return page_project.lower() == project.lower()


def _knowledge_rule(relative: str, content: str, page_type: str) -> dict:
    md = ROOT / relative
    title_m = H1_RE.search(content)
    summary_m = SUMMARY_RE.search(content)
    return {
        "type": page_type,
        "title": title_m.group(1).strip() if title_m else md.stem,
        "summary": (summary_m.group(1).strip()[:150] if summary_m else ""),
        "source": "knowledge",
        "path": md.relative_to(ROOT).as_posix(),
    }


def _feedback_correction(relative: str, source_bytes: bytes, project: str | None) -> dict | None:
    """Source 2: a promoted feedback candidate of this project (or of any, when unscoped)."""
    try:
        candidate = json.loads(source_bytes)
    except json.JSONDecodeError:
        return None
    if candidate.get("status") != "promoted" or not _feedback_in_scope(candidate, project):
        return None
    return {
        "type": candidate.get("type", "feedback"),
        "title": candidate.get("text", "")[:80],
        "summary": candidate.get("text", "")[:150],
        "source": "feedback",
        "path": candidate.get("promoted_to", ""),
    }


def _feedback_in_scope(candidate: dict, project: str | None) -> bool:
    if not project:
        return True
    return candidate.get("project", "").lower() == project.lower()


def _extract(text: str, pattern: re.Pattern) -> str | None:
    m = pattern.search(text)
    return m.group(1).strip() if m else None


_RULE_LABELS = {
    "correction": "CORRECTION",
    "preference": "PREFERENCE",
    "requirement": "REQUIREMENT",
    "instruction": "INSTRUCTION",
    "pattern_rule": "RULE",
}


def build_guardrails(
    project: str | None = None,
    max_rules: int = 15,
    *,
    source_contents: Mapping[str, bytes] | None = None,
) -> str:
    """Build the guard rails block for SessionStart injection.

    This is the "learned instincts" — rules the agent must follow
    because they were learned from past corrections.
    """
    corrections = _collect_corrections(project, source_contents=source_contents)
    if not corrections:
        return ""
    lines = ["## Guard rails (learned rules — do NOT repeat these mistakes)\n"]
    by_type: dict[str, list[dict]] = {}
    for c in _deduplicated(corrections)[:max_rules]:
        by_type.setdefault(c["type"], []).append(c)
    for rtype in sorted(by_type.keys()):
        lines.extend(_type_block(rtype, by_type[rtype]))
    return "\n".join(lines).strip()


def _deduplicated(corrections: list[dict]) -> list[dict]:
    """Deduplicate by summary similarity (simple): the first 60 lower-cased characters."""
    seen: set[str] = set()
    unique = []
    for c in corrections:
        key = c["summary"].lower()[:60]
        if key not in seen:
            seen.add(key)
            unique.append(c)
    return unique


def _type_block(rtype: str, rules: list[dict]) -> list[str]:
    label = _RULE_LABELS.get(rtype, rtype.upper())
    return [f"**{label}** ({len(rules)}):", *(f"- {r['summary']}" for r in rules[:5]), ""]


def main() -> int:
    p = argparse.ArgumentParser(description="Build guard rails from learned corrections.")
    p.add_argument("--project", default=None, help="Filter by project")
    p.add_argument("--max-rules", type=int, default=15)
    p.add_argument("--apply", action="store_true", help="Write to knowledge/guardrails.md")
    args = p.parse_args()

    source_manifest, source_contents = snapshot_guardrail_sources_with_content(ROOT)
    guardrails = build_guardrails(
        args.project,
        args.max_rules,
        source_contents=source_contents,
    )

    if not guardrails:
        print("(no guard rails — no corrections learned yet)")
        return 0

    if args.apply:
        content = (
            "---\n"
            "type: guardrails\n"
            f'title: "Learned Guard Rails"\n'
            f'description: "Auto-generated rules from past corrections"\n'
            f"timestamp: {datetime.now().isoformat(timespec='seconds')}\n"
            "---\n\n"
            f"{guardrails}\n"
        )
        encoded = content.encode("utf-8")
        try:
            before_hash = sha256_bytes(
                read_stable_bytes(
                    GUARDRAILS_FILE,
                    MAX_GUARDRAILS_BYTES,
                    label="guardrails target",
                )
            )
        except FileNotFoundError:
            before_hash = ABSENT
        mutate_knowledge(
            stable_operation_id(
                "build-guardrails",
                f"{before_hash}:{source_manifest['source_manifest_sha256']}",
                encoded,
            ),
            {GUARDRAILS_FILE: encoded},
            preconditions={
                "guardrails_source_manifest": source_manifest,
                GUARDRAILS_FILE.relative_to(ROOT).as_posix(): before_hash,
            },
        )
        print(f"Written: {GUARDRAILS_FILE.relative_to(ROOT)}")
    else:
        print(guardrails)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
