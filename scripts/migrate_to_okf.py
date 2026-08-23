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
from bounded_io import read_stable_bytes  # noqa: E402
from markdown_transaction import mutate_knowledge, stable_operation_id  # noqa: E402
from memory_state import ROOT  # noqa: E402
from reliable_memory import sha256_bytes  # noqa: E402
from vault_editorial import EDITORIAL_NAMES  # noqa: E402

# Reserved OKF filenames — no frontmatter allowed at bundle level.
RESERVED_NAMES = frozenset({"index.md", "log.md"})
MAX_MIGRATION_PAGE_BYTES = 16 * 1024 * 1024

# Editorial / contract files at the vault root — left alone.
#
# `EDITORIAL_NAMES` (imported above) covers the ones that are editorial wherever
# they sit: directory READMEs, indexes, logs, project state pages. The linter has
# always exempted those from the frontmatter checks, so writing frontmatter into
# them satisfies nobody and edits tracked files nobody asked to change.
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


def _matched_type(forward: str, needle: str, type_name: str) -> str:
    """Under `projects/`, only `state.md` is a project-state page."""
    if needle != "knowledge/projects/":
        return type_name
    if forward.endswith("state.md"):
        return type_name
    return "concept"


def infer_type(rel_path: str) -> str | None:
    """Infer OKF `type` from the file's path. Returns None if no rule matches."""
    forward = rel_path.replace("\\", "/")
    for needle, type_name in TYPE_INFERENCE:
        if needle in forward:
            return _matched_type(forward, needle, type_name)
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


def _root_contract_status(path: Path) -> str | None:
    if path.name in ROOT_LEVEL_SKIP and path.parent == ROOT:
        return "skip_root_contract"
    return None


def _skip_status(path: Path) -> str | None:
    """The reason this file must not be touched, or None to go on.

    Editorial names are skipped wherever they sit, not only at the vault root.
    The linter has always exempted them from the frontmatter checks, so stamping
    frontmatter into a directory README satisfied no check and changed a tracked
    public file for nobody.
    """
    if path.name in RESERVED_NAMES:
        return "skip_reserved"
    if path.name in EDITORIAL_NAMES:
        return "skip_editorial"
    return _root_contract_status(path)


def _read_page(path: Path) -> tuple[str, str | None]:
    """(status, text) — the status is empty when the read succeeded."""
    try:
        return "", path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        return (f"error_read:{type(e).__name__}", None)


def _added_field(body: str, field: str, value: str) -> str:
    """The quoted field to append, or nothing when it is empty or already set."""
    if not value:
        return ""
    if f"{field}:" in body:
        return ""
    return f'\n{field}: "{value}"'


def _added_timestamp(body: str, timestamp: str) -> str:
    """The timestamp is a bare scalar, not a quoted string."""
    if "timestamp:" in body:
        return ""
    return f"\ntimestamp: {timestamp}"


def _merged_frontmatter(
    existing_body: str,
    type_name: str,
    title: str,
    description: str,
    timestamp: str,
) -> str:
    """Inject `type:` into a frontmatter block that has one without a type."""
    body = f"type: {type_name}\n{existing_body}"
    body += _added_field(existing_body, "title", title)
    body += _added_field(existing_body, "description", description[:200])
    body += _added_timestamp(existing_body, timestamp)
    return "---\n" + body + "\n---\n"


def _migrated_content(
    content: str,
    type_name: str,
    title: str,
    description: str,
    timestamp: str,
) -> str:
    existing = FRONTMATTER_RE.match(content)
    if existing is None:
        return build_frontmatter(type_name, title, description, timestamp) + content
    merged = _merged_frontmatter(
        existing.group(1), type_name, title, description, timestamp
    )
    return merged + content[existing.end() :]


def _page_migration(path: Path, content: str, type_name: str) -> str:
    return _migrated_content(
        content,
        type_name,
        extract_title(content, path.stem),
        extract_description(content),
        datetime.now().isoformat(timespec="seconds"),
    )


def migrate_file(path: Path) -> tuple[str, str | None]:
    """Decide what to do with one file.

    Returns (status, new_content_or_None):
        ("skip_already_okf", None)   — already conformant
        ("skip_reserved", None)      — index.md / log.md
        ("skip_editorial", None)     — editorial metadata anywhere in the tree
                                       (directory README.md, state.md, ...)
        ("skip_root_contract", None) — CLAUDE.md / README.md / AGENTS.md
        ("skip_no_type_rule", None)  — path doesn't match any TYPE_INFERENCE entry
        ("migrate", new_content)     — frontmatter to prepend
    """
    skipped = _skip_status(path)
    if skipped is not None:
        return (skipped, None)
    status, content = _read_page(path)
    if content is None:
        return (status, None)
    if has_okf_type(content):
        return ("skip_already_okf", None)
    type_name = infer_type(path.relative_to(ROOT).as_posix())
    if not type_name:
        return ("skip_no_type_rule", None)
    return ("migrate", _page_migration(path, content, type_name))


def _scope_pages(root: Path) -> list[Path]:
    return [page for page in sorted(root.rglob("*.md")) if page.is_file()]


def collect_files(scope: str) -> list[Path]:
    """All .md files in scope, in order, without duplicates."""
    pages: list[Path] = []
    for root in SCOPE_ROOTS[scope]:
        if root.exists():
            pages.extend(_scope_pages(root))
    return list(dict.fromkeys(pages))


def _rel(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def _source_hash(path: Path) -> tuple[str, str | None]:
    """(status, digest) — the status is empty when the hash was taken."""
    try:
        data = read_stable_bytes(path, MAX_MIGRATION_PAGE_BYTES, label="migration page")
    except (OSError, ValueError) as exc:
        return (f"error_read:{type(exc).__name__}", None)
    return "", sha256_bytes(data)


class MigrationPlan:
    """What one scan decided, before anything is written."""

    def __init__(self) -> None:
        self.counts: dict[str, int] = {}
        self.pages: list[tuple[Path, str]] = []
        self.source_hashes: dict[Path, str] = {}
        self.skipped: list[tuple[str, Path]] = []

    def record(self, status: str, path: Path, content: str | None) -> None:
        self.counts[status] = self.counts.get(status, 0) + 1
        if status == "migrate" and content is not None:
            self.pages.append((path, content))
            print(f"  MIGRATE: {_rel(path)}")
            return
        self._record_skip(status, path)

    def _record_skip(self, status: str, path: Path) -> None:
        if status.startswith("error"):
            print(f"  ERROR: {_rel(path)} — {status}")
            self.skipped.append((status, path))
            return
        if status == "skip_no_type_rule":
            # Kept in the summary so the operator can investigate.
            self.skipped.append((status, path))


def _plan_migration(files: list[Path]) -> MigrationPlan:
    plan = MigrationPlan()
    for path in files:
        status, digest = _source_hash(path)
        if digest is None:
            plan.record(status, path, None)
            continue
        plan.source_hashes[path] = digest
        status, content = migrate_file(path)
        plan.record(status, path, content)
    return plan


def _print_summary(plan: MigrationPlan) -> None:
    print("\n=== summary ===")
    for status, count in sorted(plan.counts.items()):
        print(f"  {status}: {count}")
    if not plan.skipped:
        return
    print("\nDetail of skipped/error files:")
    for status, path in plan.skipped:
        print(f"  [{status}] {_rel(path)}")


def _write_page(path: Path, content: str, digest: str) -> str | None:
    """None when the page was written, or the message naming the failure."""
    encoded = content.encode("utf-8")
    rel = _rel(path)
    try:
        mutate_knowledge(
            stable_operation_id("okf-migrate", rel, encoded),
            {path: encoded},
            preconditions={rel: digest},
        )
    except (OSError, RuntimeError, ValueError) as e:
        return f"{type(e).__name__}: {e}"
    return None


def _apply_plan(plan: MigrationPlan) -> int:
    """Write every planned page; returns how many writes failed."""
    errors = 0
    for path, content in plan.pages:
        failure = _write_page(path, content, plan.source_hashes[path])
        if failure is not None:
            print(f"  WRITE ERROR: {path} — {failure}")
            errors += 1
    print(f"\nApplied: {len(plan.pages) - errors}/{len(plan.pages)} file(s) migrated.")
    return errors


def _dry_run(plan: MigrationPlan, report: bool) -> int:
    print(
        f"\nDry-run: {len(plan.pages)} file(s) would be migrated. "
        "Re-run with --apply to write."
    )
    if report:
        _write_report(plan.pages, plan.counts, applied=False)
    return 0


def main() -> int:
    args = parse_args()
    files = collect_files(args.scope)
    print(f"migrate_to_okf: scanned {len(files)} file(s) under scope={args.scope}")
    plan = _plan_migration(files)
    _print_summary(plan)
    if not args.apply:
        return _dry_run(plan, args.report)
    errors = _apply_plan(plan)
    if args.report:
        _write_report(plan.pages, plan.counts, applied=True)
    return 1 if errors else 0


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
