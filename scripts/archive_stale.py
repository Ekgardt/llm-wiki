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
from bounded_io import read_stable_bytes  # noqa: E402
from markdown_transaction import ABSENT, mutate_knowledge, stable_operation_id  # noqa: E402
from memory_state import ROOT  # noqa: E402
from okf_types import NEVER_ARCHIVE_TYPES  # noqa: E402
from reliable_memory import sha256_bytes  # noqa: E402

KNOWLEDGE = ROOT / "knowledge" / "notes"
# Stay inside knowledge zone (three-zone layout forbids root archive/).
ARCHIVE_ROOT = ROOT / "knowledge" / "notes" / "archive"

FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?", re.DOTALL)
STATUS_RE = re.compile(r"^status:\s*(.+?)\s*$", re.MULTILINE)
TIMESTAMP_RE = re.compile(r"^timestamp:\s*(.+?)\s*$", re.MULTILINE)
CONFIDENCE_RE = re.compile(r"^confidence:\s*(.+?)\s*$", re.MULTILINE)

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
MAX_ARCHIVE_PAGE_BYTES = 16 * 1024 * 1024


def _get_type_threshold(page_type: str) -> int:
    """Get archive age threshold for a page type."""
    return TYPE_AGE_DAYS.get(page_type, DEFAULT_AGE_DAYS)


def _is_stale(md: Path, default_cutoff_ts: float, default_days: int) -> bool:
    """Check if a page is stale using hybrid time + access-aware thresholds.

    v4.0: Combines type-aware mtime thresholds with Ebbinghaus decay score
    from access_tracking. A page that is mtime-stale but frequently accessed
    STAYS ALIVE (access reinforces). A page that is mtime-stale AND never
    accessed gets archived (both signals agree).
    """
    try:
        content = md.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return False
    # Skip if already superseded or archived
    page_type = ""
    confidence = "medium"
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
        conf_m = CONFIDENCE_RE.search(fm.group(1))
        confidence = conf_m.group(1).strip() if conf_m else "medium"
        # Type-specific threshold
        threshold_days = _get_type_threshold(page_type)
        threshold_ts = datetime.now().timestamp() - (threshold_days * 86400)
    else:
        # No frontmatter → use default
        threshold_ts = default_cutoff_ts
    # Check file age against the type-specific threshold
    try:
        mtime_stale = md.stat().st_mtime < threshold_ts
    except OSError:
        return False

    if not mtime_stale:
        return False  # Not old enough by time.

    # v4.0: Hybrid forgetting — check access-based decay score.
    # Only applies when we have actual access data for this page.
    # A page that is mtime-stale but frequently accessed stays alive.
    try:
        from access_tracking import decay_score, get_access_stats
        stats = get_access_stats(md.stem)
        if stats["total_count"] > 0:
            score = decay_score(md.stem, page_type or "concept", confidence)
            if score > 0.3:
                return False  # Access reinforces — keep alive.
    except Exception:
        pass  # access_tracking unavailable — use mtime only.

    return True


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
            source_bytes = read_stable_bytes(
                md, MAX_ARCHIVE_PAGE_BYTES, label="stale archive source"
            )
            content = source_bytes.decode("utf-8")
        except (OSError, UnicodeDecodeError, ValueError):
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

        if archive_path.exists():
            # Collision: same-named page already archived. Append a suffix.
            stem = archive_path.stem
            suffix = archive_path.suffix
            parent = archive_path.parent
            counter = 1
            while archive_path.exists():
                archive_path = parent / f"{stem}-{counter}{suffix}"
                counter += 1
        encoded = content.encode("utf-8")
        try:
            mutate_knowledge(
                stable_operation_id(
                    "archive-stale", md.relative_to(ROOT).as_posix(), encoded
                ),
                {archive_path: encoded, md: None},
                preconditions={
                    md.relative_to(ROOT).as_posix(): sha256_bytes(source_bytes),
                    archive_path.relative_to(ROOT).as_posix(): ABSENT,
                },
            )
        except (OSError, RuntimeError, ValueError):
            return f"WRITE_ERROR: {archive_path}"
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
