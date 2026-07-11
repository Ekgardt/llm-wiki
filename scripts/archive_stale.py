"""Auto-archive stale knowledge pages.

Moves pages older than `--days` (default 180) from active directories
into `archive/YYYY/`. Archived pages remain searchable via FTS5 but
are excluded from index.md and SessionStart context.

This prevents the vault from becoming a graveyard of obsolete decisions
— the #1 failure mode of long-lived knowledge bases.

Usage:
    uv run python scripts/archive_stale.py              # dry-run (plan only)
    uv run python scripts/archive_stale.py --apply      # move files
    uv run python scripts/archive_stale.py --days 90    # custom threshold

Pages are NEVER deleted — only moved. Git tracks the move. The page's
frontmatter gets `status: archived` added so lint and search know to
de-prioritize it.
"""
from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from memory_state import ROOT  # noqa: E402
from okf_types import NEVER_ARCHIVE_TYPES  # noqa: E402

KNOWLEDGE = ROOT / "knowledge" / "notes"
# Stay inside knowledge zone (three-zone layout forbids root archive/).
ARCHIVE_ROOT = ROOT / "knowledge" / "notes" / "archive"

FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?", re.DOTALL)
STATUS_RE = re.compile(r"^status:\s*(.+?)\s*$", re.MULTILINE)
TIMESTAMP_RE = re.compile(r"^timestamp:\s*(.+?)\s*$", re.MULTILINE)

TYPE_RE = re.compile(r"^type:\s*(.+?)\s*$", re.MULTILINE)

# Type-specific age thresholds (Dorabotka D: smart archive by type)
TYPE_AGE_DAYS = {
    "debugging": 60,       # old debugging notes go stale fast
    "gap": 90,             # gaps close when a real page is created (AGENTS.md §5)
    "pattern": 180,        # patterns live longer
    "workflow": 365,       # workflows are durable
    "qa": 365,            # Q&A stays relevant
}

# Default for untyped pages
DEFAULT_AGE_DAYS = 180


def _get_type_threshold(page_type: str) -> int:
    """Get archive age threshold for a page type."""
    return TYPE_AGE_DAYS.get(page_type, DEFAULT_AGE_DAYS)


def _is_stale(md: Path, default_cutoff_ts: float, default_days: int) -> bool:
    """Check if a page is stale using smart type-aware thresholds.

    Instead of a flat 180-day cutoff for everything, uses per-type
    thresholds: debugging logs archive at 60 days, decisions NEVER
    archive, concepts NEVER archive, patterns at 180 days.
    """
    try:
        content = md.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return False
    # Skip if already superseded or archived
    fm = FRONTMATTER_RE.match(content)
    if fm:
        status_m = STATUS_RE.search(fm.group(1))
        if status_m and status_m.group(1).strip() in ("superseded", "archived"):
            return False
        type_m = TYPE_RE.search(fm.group(1))
        page_type = type_m.group(1).strip() if type_m else ""
        # Evergreen types: never archive
        if page_type in NEVER_ARCHIVE_TYPES:
            return False
        # Type-specific threshold
        threshold_days = _get_type_threshold(page_type)
        threshold_ts = datetime.now().timestamp() - (threshold_days * 86400)
    else:
        # No frontmatter → use default
        threshold_ts = default_cutoff_ts
    # Check file age against the type-specific threshold
    try:
        return md.stat().st_mtime < threshold_ts
    except OSError:
        return False


def _archive_page(md: Path, apply: bool) -> str:
    """Move page to archive/YYYY/ and add status: archived to frontmatter."""
    year = datetime.now().strftime("%Y")
    rel = md.relative_to(ROOT)
    # Destination is relative to the KNOWLEDGE tree (drop the redundant
    # knowledge/notes/ prefix so archived pages don't land at a doubled path).
    try:
        rel_under = md.relative_to(KNOWLEDGE)
        dest_subdir = rel_under.parent
    except ValueError:
        dest_subdir = rel.parent
    archive_path = ARCHIVE_ROOT / year / dest_subdir / md.name

    if apply:
        try:
            content = md.read_text(encoding="utf-8")
        except OSError:
            return f"READ_ERROR: {md}"
        # Set status: archived — replace existing status value or insert new.
        if FRONTMATTER_RE.match(content):
            fm_text = FRONTMATTER_RE.match(content).group(1)
            if STATUS_RE.search(fm_text):
                # Replace existing status value with "archived".
                content = re.sub(
                    r"(^status:\s*).+$", r"\1archived", content,
                    count=1, flags=re.MULTILINE,
                )
            else:
                # No status field yet — insert after opening ---.
                content = re.sub(
                    r"^(---\s*\n)", r"\1status: archived\n", content, count=1,
                )
        elif "status:" not in content:
            content = f"---\nstatus: archived\n---\n\n{content}"

        archive_path.parent.mkdir(parents=True, exist_ok=True)
        if archive_path.exists():
            # Collision: same-named page already archived. Append a suffix.
            stem = archive_path.stem
            suffix = archive_path.suffix
            parent = archive_path.parent
            counter = 1
            while archive_path.exists():
                archive_path = parent / f"{stem}-{counter}{suffix}"
                counter += 1
        # Write-to-temp → atomic rename → unlink source. This order
        # ensures the archive copy is fully written BEFORE the original
        # is removed — no data-loss window if the write fails mid-stream.
        tmp_path = archive_path.with_suffix(".md.tmp")
        try:
            tmp_path.write_text(content, encoding="utf-8")
            tmp_path.replace(archive_path)
        except OSError:
            try:
                tmp_path.unlink()
            except OSError:
                pass
            return f"WRITE_ERROR: {archive_path}"
        try:
            md.unlink()
        except OSError:
            return f"UNLINK_ERROR: {md} (archived copy at {archive_path})"
        return f"ARCHIVED: {rel} → archive/{year}/{dest_subdir.as_posix()}/{md.name}"
    else:
        return f"WOULD ARCHIVE: {rel}"


def main() -> int:
    p = argparse.ArgumentParser(description="Archive stale knowledge pages.")
    p.add_argument("--days", type=int, default=180, help="Sets the base threshold; type-specific thresholds (debugging=60d, pattern=180d, etc.) still apply.")
    p.add_argument("--apply", action="store_true", help="Actually move files (default: dry-run)")
    p.add_argument("--explain", action="store_true", help="Show why each page was flagged")
    args = p.parse_args()

    cutoff = datetime.now().timestamp() - (args.days * 86400)
    stale: list[Path] = []

    # Scan knowledge notes once (flat + optional typed subdirs).
    if KNOWLEDGE.exists():
        for md in KNOWLEDGE.rglob("*.md"):
            if "archive" in md.parts:
                continue
            if md.name.lower() in {"readme.md", "index.md", "log.md"}:
                continue
            if _is_stale(md, cutoff, args.days):
                stale.append(md)

    if not stale:
        print(f"No stale pages found (threshold: {args.days} days).")
        return 0

    print(f"Found {len(stale)} stale page(s) older than {args.days} days:\n")
    failures = 0
    for md in stale:
        result = _archive_page(md, args.apply)
        print(f"  {result}")
        if args.apply and "_ERROR:" in result:
            failures += 1

    if not args.apply:
        print(f"\nDry-run. Re-run with --apply to move {len(stale)} page(s) to archive/.")
    elif failures:
        print(f"\nArchived {len(stale) - failures} page(s); {failures} FAILED.")
    else:
        print(f"\nArchived {len(stale)} page(s).")

    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
