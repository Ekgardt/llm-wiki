"""Auto-archive stale knowledge pages.

Moves pages older than `--days` (default 180) from active directories
into `archive/YYYY/`. Archived pages remain available as Markdown and in
Git history, but are excluded from active search, index.md, and SessionStart
context.

This prevents the vault from becoming a graveyard of obsolete decisions
— the #1 failure mode of long-lived knowledge bases.

Usage:
    uv run python scripts/archive_stale.py              # dry-run (plan only)
    uv run python scripts/archive_stale.py --apply      # move files
    uv run python scripts/archive_stale.py --days 90    # custom threshold

Pages are NEVER deleted — only moved. Git tracks the move. The page's
frontmatter gets `status: archived` so active retrieval can exclude it.
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
from okf_types import (  # noqa: E402
    DEFAULT_AGE_DAYS,
    NEVER_ARCHIVE_TYPES,
    TYPE_AGE_DAYS,
)
from page_status import is_retired  # noqa: E402
from reliable_memory import sha256_bytes  # noqa: E402

KNOWLEDGE = ROOT / "knowledge" / "notes"
# Stay inside knowledge zone (three-zone layout forbids root archive/).
ARCHIVE_ROOT = ROOT / "knowledge" / "notes" / "archive"

FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?", re.DOTALL)
STATUS_RE = re.compile(r"^status:\s*(.+?)\s*$", re.MULTILINE)
TIMESTAMP_RE = re.compile(r"^timestamp:\s*(.+?)\s*$", re.MULTILINE)
CONFIDENCE_RE = re.compile(r"^confidence:\s*(.+?)\s*$", re.MULTILINE)

TYPE_RE = re.compile(r"^type:\s*(.+?)\s*$", re.MULTILINE)

MAX_ARCHIVE_PAGE_BYTES = 16 * 1024 * 1024


def _get_type_threshold(page_type: str) -> int:
    """Get archive age threshold for a page type."""
    return TYPE_AGE_DAYS.get(page_type, DEFAULT_AGE_DAYS)


def _field_value(frontmatter: str, pattern: re.Pattern[str]) -> str:
    match = pattern.search(frontmatter)
    if match is None:
        return ""
    return match.group(1).strip()


def _kept_by_frontmatter(frontmatter: str) -> bool:
    """Retired pages are already out of the way; evergreen types never leave."""
    if is_retired(_field_value(frontmatter, STATUS_RE)):
        return True
    return _field_value(frontmatter, TYPE_RE) in NEVER_ARCHIVE_TYPES


def _stale_by_mtime(md: Path, threshold_ts: float) -> bool:
    try:
        return md.stat().st_mtime < threshold_ts
    except OSError:
        return False


def _access_keeps_alive(md: Path, page_type: str, confidence: str) -> bool:
    """A page that is old but still consulted stays: access reinforces.

    Only applies where there is actual access data; without the tracker this
    answers False and age alone decides.
    """
    try:
        from access_tracking import decay_score, get_access_stats

        if get_access_stats(md.stem)["total_count"] <= 0:
            return False
        return decay_score(md.stem, page_type or "concept", confidence) > 0.3
    except Exception:  # noqa: BLE001 - access_tracking is optional
        return False


def _stale_without_frontmatter(md: Path, default_cutoff_ts: float) -> bool:
    if not _stale_by_mtime(md, default_cutoff_ts):
        return False
    return not _access_keeps_alive(md, "", "medium")


def _stale_with_frontmatter(md: Path, frontmatter: str) -> bool:
    if _kept_by_frontmatter(frontmatter):
        return False
    page_type = _field_value(frontmatter, TYPE_RE)
    threshold_ts = datetime.now().timestamp() - (
        _get_type_threshold(page_type) * 86400
    )
    if not _stale_by_mtime(md, threshold_ts):
        return False
    confidence = _field_value(frontmatter, CONFIDENCE_RE) or "medium"
    return not _access_keeps_alive(md, page_type, confidence)


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
    frontmatter = FRONTMATTER_RE.match(content)
    if frontmatter is None:
        return _stale_without_frontmatter(md, default_cutoff_ts)
    return _stale_with_frontmatter(md, frontmatter.group(1))

def _with_archived_status(content: str) -> str:
    """The page as archived: its status field says so, however it was written."""
    frontmatter = FRONTMATTER_RE.match(content)
    if frontmatter is None:
        return _inserted_archived_frontmatter(content)
    if STATUS_RE.search(frontmatter.group(1)):
        return re.sub(
            r"(^status:\s*).+$", r"\1archived", content, count=1, flags=re.MULTILINE
        )
    return re.sub(r"^(---\s*\n)", r"\1status: archived\n", content, count=1)


def _inserted_archived_frontmatter(content: str) -> str:
    if "status:" in content:
        return content
    return f"---\nstatus: archived\n---\n\n{content}"


def _free_archive_path(archive_path: Path) -> Path:
    """A same-named page may already be archived; number this one instead."""
    parent, stem, suffix = archive_path.parent, archive_path.stem, archive_path.suffix
    counter = 1
    while archive_path.exists():
        archive_path = parent / f"{stem}-{counter}{suffix}"
        counter += 1
    return archive_path


def _archive_destination(md: Path) -> tuple[Path, Path]:
    """(destination subdirectory, archive path) for one page."""
    year = datetime.now().strftime("%Y")
    try:
        dest_subdir = md.relative_to(KNOWLEDGE).parent
    except ValueError:
        dest_subdir = md.relative_to(ROOT).parent
    return dest_subdir, ARCHIVE_ROOT / year / dest_subdir / md.name


def _committed_archive(md: Path, archive_path: Path, source_bytes: bytes, content: str) -> bool:
    encoded = content.encode("utf-8")
    relative = md.relative_to(ROOT).as_posix()
    try:
        mutate_knowledge(
            stable_operation_id("archive-stale", relative, encoded),
            {archive_path: encoded, md: None},
            preconditions={
                relative: sha256_bytes(source_bytes),
                archive_path.relative_to(ROOT).as_posix(): ABSENT,
            },
        )
    except (OSError, RuntimeError, ValueError):
        return False
    return True


def _archive_page(md: Path, apply: bool) -> str:
    """Move page to archive/YYYY/ and add status: archived to frontmatter."""
    rel = md.relative_to(ROOT)
    dest_subdir, archive_path = _archive_destination(md)
    if not apply:
        return f"WOULD ARCHIVE: {rel}"
    try:
        source_bytes = read_stable_bytes(
            md, MAX_ARCHIVE_PAGE_BYTES, label="stale archive source"
        )
        content = _with_archived_status(source_bytes.decode("utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        return f"READ_ERROR: {md}"
    archive_path = _free_archive_path(archive_path)
    if not _committed_archive(md, archive_path, source_bytes, content):
        return f"WRITE_ERROR: {archive_path}"
    year = archive_path.parent.parent.name
    return f"ARCHIVED: {rel} → archive/{year}/{dest_subdir.as_posix()}/{md.name}"

def _scanned_pages() -> list[Path]:
    """Every active note, without the archive and the editorial files."""
    if not KNOWLEDGE.exists():
        return []
    skip = {"readme.md", "index.md", "log.md"}
    return [
        md
        for md in KNOWLEDGE.rglob("*.md")
        if "archive" not in md.parts and md.name.lower() not in skip
    ]


def _stale_pages(cutoff: float, days: int) -> list[Path]:
    return [md for md in _scanned_pages() if _is_stale(md, cutoff, days)]


def _archive_all(stale: list[Path], apply: bool) -> int:
    """Archive each page, printing what happened; returns the failure count."""
    failures = 0
    for md in stale:
        result = _archive_page(md, apply)
        print(f"  {result}")
        failures += int(apply and "_ERROR:" in result)
    return failures


def _print_outcome(count: int, failures: int, apply: bool) -> None:
    if not apply:
        print(f"\nDry-run. Re-run with --apply to move {count} page(s) to archive/.")
        return
    if failures:
        print(f"\nArchived {count - failures} page(s); {failures} FAILED.")
        return
    print(f"\nArchived {count} page(s).")


def _parsed_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Archive stale knowledge pages.")
    parser.add_argument(
        "--days",
        type=int,
        default=180,
        help=(
            "Sets the base threshold; type-specific thresholds "
            "(debugging=60d, pattern=180d, etc.) still apply."
        ),
    )
    parser.add_argument(
        "--apply", action="store_true", help="Actually move files (default: dry-run)"
    )
    parser.add_argument(
        "--explain", action="store_true", help="Show why each page was flagged"
    )
    return parser.parse_args()


def main() -> int:
    args = _parsed_arguments()
    cutoff = datetime.now().timestamp() - (args.days * 86400)
    stale = _stale_pages(cutoff, args.days)
    if not stale:
        print(f"No stale pages found (threshold: {args.days} days).")
        return 0
    print(f"Found {len(stale)} stale page(s) older than {args.days} days:\n")
    failures = _archive_all(stale, args.apply)
    _print_outcome(len(stale), failures, args.apply)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
