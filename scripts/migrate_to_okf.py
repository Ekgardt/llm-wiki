"""Migrate existing markdown pages to OKF (Open Knowledge Format v0.1).

OKF requires:
1. Every non-reserved .md file has YAML frontmatter.
2. Frontmatter contains a non-empty `type:` field.

Recommended fields: title, description, tags, timestamp.

This script is idempotent: re-running on already-conformant pages is a
no-op. It infers `type` from the directory, extracts `title` from the
first H1, and `description` from the "One-sentence summary:" line where
present. Existing frontmatter is preserved — only missing fields are
added.

Usage:
    uv run python scripts/migrate_to_okf.py            # dry-run (plan only)
    uv run python scripts/migrate_to_okf.py --apply    # write changes
    uv run python scripts/migrate_to_okf.py --apply --scope wiki
    uv run python scripts/migrate_to_okf.py --report   # write report to state root

Scope filters (default = all):
    wiki, memory, skills, rules, projects, all

Skip rules (these files are NEVER migrated):
    - index.md, log.md (OKF reserved filenames)
    - files that already have a non-empty `type:` in frontmatter
    - vault README.md, CLAUDE.md, AGENTS.md (root-level contracts,
      not knowledge pages — kept in their original format)
"""
from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from memory_state import ROOT, atomic_write  # noqa: E402

# Reserved OKF filenames — no frontmatter allowed at bundle level.
RESERVED_NAMES = frozenset({"index.md", "log.md"})

# Editorial / contract files at the vault root — left alone.
ROOT_LEVEL_SKIP = frozenset(
    {
        "CLAUDE.md",
        "README.md",
        "AGENTS.md",
        "LLM.md",
    }
)

# Type inference rules. Order matters: more specific paths first.
# Each entry: (path-template-substring, inferred type).
TYPE_INFERENCE = [
    # Most-specific path prefixes first.
    ("knowledge/notes/decisions/", "decision"),
    ("knowledge/notes/patterns/", "pattern"),
    ("knowledge/notes/debugging/", "debugging"),
    ("knowledge/notes/concepts/", "concept"),
    ("knowledge/notes/qa/", "qa"),
    ("knowledge/notes/workflows/", "workflow"),
    ("knowledge/notes/facts/", "concept"),           # alias: fact → concept
    ("knowledge/notes/entities/", "entity"),
    ("knowledge/notes/syntheses/", "synthesis"),
    ("knowledge/notes/comparisons/", "synthesis"),    # alias: comparison → synthesis
    ("knowledge/notes/connections/", "synthesis"),    # alias: connection → synthesis
    ("knowledge/projects/", "project-state"),
    ("skills/", "skill"),
    ("rules/", "rule"),
    # Broad fallback last (flat notes without category subdir).
    ("knowledge/notes/", "concept"),
]

# Per-scope glob roots (used by --scope filter).
SCOPE_ROOTS = {
    "wiki": [ROOT / "knowledge" / "notes"],
    "memory": [ROOT / "knowledge" / "notes"],
    "notes": [ROOT / "knowledge" / "notes"],
    "skills": [ROOT / "skills"],
    "rules": [ROOT / "rules"],
    "projects": [ROOT / "knowledge" / "projects"],
    "all": [
        ROOT / "knowledge" / "notes",
        ROOT / "knowledge" / "projects",
        ROOT / "skills",
        ROOT / "rules",
    ],
}


H1_RE = re.compile(r"^#\s+(.+?)\s*$", re.MULTILINE)
SUMMARY_RE = re.compile(
    r"^One-sentence summary:\s*(.+?)\s*$", re.MULTILINE | re.IGNORECASE
)
FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?", re.DOTALL)
TYPE_FIELD_RE = re.compile(r"^type:\s*(.+?)\s*$", re.MULTILINE)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument(
        "--apply",
        action="store_true",
        help="Write changes. Without this flag, the script is a dry-run.",
    )
    p.add_argument(
        "--scope",
        choices=list(SCOPE_ROOTS.keys()),
        default="all",
        help="Limit migration to a subtree (default: all).",
    )
    p.add_argument(
        "--report",
        action="store_true",
        help="Write a markdown report to $LLM_WIKI_STATE_ROOT/logs/.",
    )
    return p.parse_args()


def infer_type(rel_path: str) -> str | None:
    """Infer OKF `type` from the file's path. Returns None if no rule matches."""
    forward = rel_path.replace("\\", "/")
    for needle, type_name in TYPE_INFERENCE:
        if needle in forward:
            # Special case: state.md under projects gets its own type.
            if needle == "knowledge/projects/" and not forward.endswith("state.md"):
                # Other files under projects/ — leave them as concept by default.
                return "concept"
            return type_name
    return None


def extract_title(content: str, fallback: str) -> str:
    """First H1, or filename stem if no H1."""
    m = H1_RE.search(content)
    if m:
        return m.group(1).strip().replace('"', "'")
    return fallback


def extract_description(content: str) -> str:
    """Pull the 'One-sentence summary:' line if present."""
    m = SUMMARY_RE.search(content)
    if m:
        return m.group(1).strip().replace('"', "'")
    return ""


def has_okf_type(content: str) -> bool:
    """True if the file already has a non-empty `type:` in frontmatter."""
    fm = FRONTMATTER_RE.match(content)
    if not fm:
        return False
    type_match = TYPE_FIELD_RE.search(fm.group(1))
    return bool(type_match and type_match.group(1).strip())


def build_frontmatter(
    type_name: str,
    title: str,
    description: str,
    timestamp: str,
) -> str:
    """Build a minimal OKF frontmatter block."""
    lines = ["---", f"type: {type_name}"]
    # Title only if non-trivial (not just the filename stem)
    if title:
        safe_title = title.replace("\\", "\\\\").replace('"', '\\"')
        lines.append(f'title: "{safe_title}"')
    if description:
        # Truncate overly long descriptions; lint will warn if too short.
        # Escape backslashes first, then double quotes for YAML double-quoted scalars.
        desc = description[:200].replace("\\", "\\\\").replace('"', '\\"')
        lines.append(f'description: "{desc}"')
    lines.append(f"timestamp: {timestamp}")
    lines.append("---")
    return "\n".join(lines) + "\n"


def migrate_file(path: Path) -> tuple[str, str | None]:
    """Decide what to do with one file.

    Returns (status, new_content_or_None):
        ("skip_already_okf", None)   — already conformant
        ("skip_reserved", None)      — index.md / log.md
        ("skip_root_contract", None) — CLAUDE.md / README.md / AGENTS.md
        ("skip_no_type_rule", None)  — path doesn't match any TYPE_INFERENCE entry
        ("migrate", new_content)     — frontmatter to prepend
    """
    if path.name in RESERVED_NAMES:
        return ("skip_reserved", None)
    if path.name in ROOT_LEVEL_SKIP and path.parent == ROOT:
        return ("skip_root_contract", None)

    try:
        content = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        return (f"error_read:{type(e).__name__}", None)

    if has_okf_type(content):
        return ("skip_already_okf", None)

    rel = path.relative_to(ROOT).as_posix()
    type_name = infer_type(rel)
    if not type_name:
        return ("skip_no_type_rule", None)

    title = extract_title(content, path.stem)
    description = extract_description(content)
    timestamp = datetime.now().isoformat(timespec="seconds")
    fm = build_frontmatter(type_name, title, description, timestamp)

    # If the file already has frontmatter WITHOUT type (rare but possible
    # — e.g. only `author:` was set), merge instead of double-prepending.
    existing_fm = FRONTMATTER_RE.match(content)
    if existing_fm:
        existing_body = existing_fm.group(1)
        # Inject type: at the top of the existing block.
        new_fm_body = f"type: {type_name}\n{existing_body}"
        if title and "title:" not in existing_body:
            new_fm_body += f'\ntitle: "{title}"'
        if description and "description:" not in existing_body:
            new_fm_body += f'\ndescription: "{description[:200]}"'
        if "timestamp:" not in existing_body:
            new_fm_body += f"\ntimestamp: {timestamp}"
        new_fm = "---\n" + new_fm_body + "\n---\n"
        new_content = new_fm + content[existing_fm.end() :]
    else:
        new_content = fm + content

    return ("migrate", new_content)


def collect_files(scope: str) -> list[Path]:
    """All .md files in scope, deduplicated."""
    seen: set[Path] = set()
    out: list[Path] = []
    for root in SCOPE_ROOTS[scope]:
        if not root.exists():
            continue
        for p in sorted(root.rglob("*.md")):
            if p.is_file() and p not in seen:
                seen.add(p)
                out.append(p)
    return out


def main() -> int:
    args = parse_args()
    files = collect_files(args.scope)
    print(f"migrate_to_okf: scanned {len(files)} file(s) under scope={args.scope}")

    counts: dict[str, int] = {}
    plan: list[tuple[Path, str]] = []
    skipped_detail: list[tuple[str, Path]] = []
    for path in files:
        rel = path.relative_to(ROOT).as_posix()
        status, new_content = migrate_file(path)
        counts[status] = counts.get(status, 0) + 1
        if status == "migrate":
            plan.append((path, new_content))
            print(f"  MIGRATE: {rel}")
        elif status.startswith("error"):
            print(f"  ERROR: {rel} — {status}")
            skipped_detail.append((status, path))
        elif status in ("skip_no_type_rule",):
            # Track these for the summary so the operator can investigate.
            skipped_detail.append((status, path))

    print("\n=== summary ===")
    for status, count in sorted(counts.items()):
        print(f"  {status}: {count}")
    if skipped_detail:
        print("\nDetail of skipped/error files:")
        for status, path in skipped_detail:
            print(f"  [{status}] {path.relative_to(ROOT).as_posix()}")

    if not args.apply:
        print(f"\nDry-run: {len(plan)} file(s) would be migrated. Re-run with --apply to write.")
        if args.report:
            _write_report(plan, counts, applied=False)
        return 0

    written = 0
    write_errors = 0
    for path, new_content in plan:
        try:
            atomic_write(path, new_content)
            written += 1
        except OSError as e:
            print(f"  WRITE ERROR: {path} — {type(e).__name__}: {e}")
            write_errors += 1
    print(f"\nApplied: {written}/{len(plan)} file(s) migrated.")

    if args.report:
        _write_report(plan, counts, applied=True)
    return 1 if write_errors else 0


def _write_report(
    plan: list[tuple[Path, str]],
    counts: dict[str, int],
    applied: bool,
) -> None:
    """Write a migration report under the state root."""
    import os

    state_root = Path(
        os.environ.get("LLM_WIKI_STATE_ROOT", str(ROOT))
    )
    reports = state_root / "logs"
    reports.mkdir(parents=True, exist_ok=True)
    today = datetime.now().strftime("%Y-%m-%d")
    report = reports / f"okf-migration-{today}.md"
    mode = "applied" if applied else "planned"
    lines = [
        f"# OKF migration report — {today} ({mode})",
        "",
        "## Summary",
        "",
        f"- Total scanned: {sum(counts.values())}",
        f"- Migrated: {counts.get('migrate', 0)}",
        f"- Already conformant: {counts.get('skip_already_okf', 0)}",
        f"- Reserved (skipped): {counts.get('skip_reserved', 0)}",
        f"- Root contracts (skipped): {counts.get('skip_root_contract', 0)}",
        f"- No type rule (skipped): {counts.get('skip_no_type_rule', 0)}",
        "",
        "## Files migrated",
        "",
    ]
    for path, _ in plan:
        rel = path.relative_to(ROOT).as_posix()
        lines.append(f"- `{rel}`")
    report.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Report written to: {report}")


if __name__ == "__main__":
    raise SystemExit(main())
