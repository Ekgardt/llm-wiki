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

A page's age is the age of its content, not of its file: see
`committed_content_times` below and
`docs/research/2026-08-28-what-a-pages-age-is.md`.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from collections.abc import Mapping
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
from page_status import is_retired, normalized_status  # noqa: E402
from reliable_memory import sha256_bytes  # noqa: E402

KNOWLEDGE = ROOT / "knowledge" / "notes"
# Stay inside knowledge zone (three-zone layout forbids root archive/).
ARCHIVE_ROOT = ROOT / "knowledge" / "notes" / "archive"

FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?", re.DOTALL)
STATUS_RE = re.compile(r"^status:\s*(.+?)\s*$", re.MULTILINE)
TIMESTAMP_RE = re.compile(r"^timestamp:\s*(.+?)\s*$", re.MULTILINE)
CONFIDENCE_RE = re.compile(r"^confidence:\s*(.+?)\s*$", re.MULTILINE)

TYPE_RE = re.compile(r"^type:\s*(.+?)\s*$", re.MULTILINE)

# The status word archiving displaced, so restore can hand it back rather than
# leaving the page with none.
STATUS_BEFORE_ARCHIVE = "status_before_archive"
BEFORE_ARCHIVE_RE = re.compile(rf"^{STATUS_BEFORE_ARCHIVE}:\s*(.+?)\s*$", re.MULTILINE)

_STATUS_LINE_RE = re.compile(r"^status:\s*.+$", re.MULTILINE)

# What archiving writes over a page that had no frontmatter at all, and the one
# shape restore may therefore remove whole.
_INSERTED_FIELD = "status: archived"
_INSERTED_BLOCK = f"---\n{_INSERTED_FIELD}\n---"

MAX_ARCHIVE_PAGE_BYTES = 16 * 1024 * 1024

# One read-only git question may not hold the weekly pass up: the whole set of
# them costs 0.14 s on this vault, and a hang here would stall the archiver.
GIT_TIMEOUT_SECONDS = 20.0

# The pathspec every git question is bounded by, matching KNOWLEDGE's place in
# the three-zone layout.
NOTES_PATHSPEC = "knowledge/notes"

# `git log --format=%x00%ct --name-only` writes one NUL-prefixed record per
# commit: the commit time, then the paths that commit touched.
_COMMIT_SEPARATOR = "\x00"


def _git_output(root: Path, arguments: list[str]) -> str | None:
    """One read-only git question, or None when git cannot answer it.

    Absent git, an unborn or non-repository vault, a timeout and a broken
    invocation are all the same answer here — no history — and the caller falls
    back to the file clock rather than refusing to archive.

    `--no-optional-locks` because this is a read: `git diff` would otherwise
    refresh the index and take `index.lock`, and the nightly self-update
    (2026-08-23) runs git against this same checkout.
    """
    try:
        completed = subprocess.run(
            [
                "git",
                "--no-optional-locks",
                "-C",
                str(root),
                "-c",
                "core.quotePath=false",
                *arguments,
            ],
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout


def _repository_prefix(root: Path) -> str:
    """Where the vault sits inside its repository, as git spells that path.

    Empty when the vault *is* the repository root, which is this installation.
    It matters because `diff` and `log` name paths from the repository root
    while the vault names them from its own, and a nested vault whose two
    halves never met would silently fall back to file clocks everywhere.
    """
    output = _git_output(root, ["rev-parse", "--show-prefix"])
    if output is None:
        return ""
    return output.strip()


def _null_separated(root: Path, arguments: list[str]) -> set[str]:
    output = _git_output(root, [*arguments, "--", NOTES_PATHSPEC])
    if output is None:
        return set()
    return {item for item in output.split("\0") if item}


def _timed_paths(lines: list[str]) -> tuple[float, list[str]] | None:
    if not lines[0].isdigit():
        return None
    return float(lines[0]), lines[1:]


def _commit_record(record: str) -> tuple[float, list[str]] | None:
    """One `<commit time>` and the paths it touched, or None for anything else."""
    lines = [line for line in record.split("\n") if line]
    if len(lines) < 2:
        return None
    return _timed_paths(lines)


def _fold_commit(record: str, times: dict[str, float]) -> None:
    """The first commit to name a path is the last one that touched it."""
    parsed = _commit_record(record)
    if parsed is None:
        return
    stamp, paths = parsed
    for path in paths:
        times.setdefault(path, stamp)


def _commit_times(root: Path) -> dict[str, float]:
    """When each note was last touched by a commit, in one history pass."""
    output = _git_output(
        root,
        ["log", "--no-renames", "--format=%x00%ct", "--name-only", "--", NOTES_PATHSPEC],
    )
    if output is None:
        return {}
    times: dict[str, float] = {}
    for record in output.split(_COMMIT_SEPARATOR):
        _fold_commit(record, times)
    return times


def committed_content_times(root: Path) -> dict[str, float]:
    """Vault-relative note path -> when the bytes now on disk were committed.

    Only pages git can vouch for are listed: tracked *and* unmodified, so the
    last commit that touched the path is the commit that wrote what is there
    now. A locally modified page, an untracked one, a vault that is not a
    repository and a machine without git are all absent from this map, and age
    by their file clock instead.

    This exists because `st_mtime` answers "when was this file last written",
    which on this vault is not "when did this page last change": a checkout, an
    index rebuild or the nightly backlink writer rewrites bytes that are
    already identical, and every such touch used to restart the forgetting
    clock. Measured 2026-08-28 — 53 of 76 tracked notes carried an mtime more
    than a day newer than their last content change while byte-identical to
    HEAD, the largest gap 38 days.
    """
    unmodified = _null_separated(root, ["ls-files", "--full-name", "-z"]) - _null_separated(
        root, ["diff", "--name-only", "-z", "HEAD"]
    )
    times = _commit_times(root)
    prefix = _repository_prefix(root)
    return {
        path[len(prefix):]: times[path] for path in sorted(unmodified) if path in times
    }


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


def _committed_time(md: Path, committed: Mapping[str, float], default: float) -> float:
    try:
        relative = md.relative_to(ROOT).as_posix()
    except ValueError:
        return default
    return committed.get(relative, default)


def _content_change_time(md: Path, committed: Mapping[str, float]) -> float | None:
    """The oldest evidence of this page's last content change.

    The file clock and the commit that wrote these bytes are both upper bounds
    on when the content last changed, so the older of the two is the honest
    answer — and taking the older one means this signal can only ever make a
    page more archivable than the file clock alone did, never less.
    """
    try:
        modified = md.stat().st_mtime
    except OSError:
        return None
    return min(modified, _committed_time(md, committed, modified))


def _stale_by_age(md: Path, threshold_ts: float, committed: Mapping[str, float]) -> bool:
    changed = _content_change_time(md, committed)
    if changed is None:
        return False
    return changed < threshold_ts


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


def _stale_without_frontmatter(
    md: Path, default_cutoff_ts: float, committed: Mapping[str, float]
) -> bool:
    if not _stale_by_age(md, default_cutoff_ts, committed):
        return False
    return not _access_keeps_alive(md, "", "medium")


def _stale_with_frontmatter(
    md: Path, frontmatter: str, committed: Mapping[str, float]
) -> bool:
    if _kept_by_frontmatter(frontmatter):
        return False
    page_type = _field_value(frontmatter, TYPE_RE)
    threshold_ts = datetime.now().timestamp() - (
        _get_type_threshold(page_type) * 86400
    )
    if not _stale_by_age(md, threshold_ts, committed):
        return False
    confidence = _field_value(frontmatter, CONFIDENCE_RE) or "medium"
    return not _access_keeps_alive(md, page_type, confidence)


def _is_stale(
    md: Path,
    default_cutoff_ts: float,
    default_days: int,
    committed: Mapping[str, float] | None = None,
) -> bool:
    """Check if a page is stale using hybrid time + access-aware thresholds.

    v4.0: Combines type-aware age thresholds with the Ebbinghaus decay score
    from access_tracking. A page that is old but frequently accessed STAYS
    ALIVE (access reinforces). A page that is old AND never accessed gets
    archived (both signals agree).

    `committed` carries `committed_content_times`; without it the page ages by
    its file clock alone, which is what a caller holding no history can honestly
    say.
    """
    try:
        content = md.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return False
    known = committed or {}
    frontmatter = FRONTMATTER_RE.match(content)
    if frontmatter is None:
        return _stale_without_frontmatter(md, default_cutoff_ts, known)
    return _stale_with_frontmatter(md, frontmatter.group(1), known)

def _with_archived_status(content: str) -> str:
    """The page as archived: its status field says so, however it was written."""
    frontmatter = FRONTMATTER_RE.match(content)
    if frontmatter is None:
        return _inserted_archived_frontmatter(content)
    declared = _field_value(frontmatter.group(1), STATUS_RE)
    if not declared:
        return re.sub(r"^(---\s*\n)", r"\1status: archived\n", content, count=1)
    return _replaced_status(content, declared)


def _replaced_status(content: str, declared: str) -> str:
    """`status: archived`, keeping the word it displaced for the way back.

    Restore used to delete the status line outright, so a page archived as
    `status: preliminary` came back declaring nothing at all — a page's own
    editorial state lost across a round trip this module calls dormancy rather
    than deletion (`NEW-128`).
    """
    if normalized_status(declared) == "archived":
        return re.sub(_STATUS_LINE_RE, "status: archived", content, count=1)
    replacement = f"status: archived\n{STATUS_BEFORE_ARCHIVE}: {declared}"
    return re.sub(_STATUS_LINE_RE, lambda _: replacement, content, count=1)


def _inserted_archived_frontmatter(content: str) -> str:
    """A page with no frontmatter gets one, so it declares that it is retired.

    This used to hand the page back untouched whenever the literal `status:`
    appeared anywhere in the body — prose, not a declaration. The page was then
    archived while declaring no retired status at all: the corpus collector
    still refused it by directory, but the legacy lexical index, which does not
    skip `archive/`, answered it at rank 1 from inside the archive (`NEW-126`).
    """
    return f"{_INSERTED_BLOCK}\n\n{content}"


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


def _stale_pages(
    cutoff: float, days: int, committed: Mapping[str, float] | None = None
) -> list[Path]:
    known = committed_content_times(ROOT) if committed is None else committed
    return [md for md in _scanned_pages() if _is_stale(md, cutoff, days, known)]


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


def _archived_pages(slug: str) -> list[Path]:
    """Every archived copy of one slug, newest archive year first."""
    if not ARCHIVE_ROOT.is_dir():
        return []
    return sorted(ARCHIVE_ROOT.rglob(f"{slug}.md"), reverse=True)


def _without_archived_status(content: str) -> str:
    """The page as active again: exactly what archiving wrote is taken back.

    Three shapes, because archiving writes three: a whole frontmatter block it
    inserted, a `status:` line it added, or a status word it displaced and
    recorded. Each is undone into the bytes it was made from.
    """
    frontmatter = FRONTMATTER_RE.match(content)
    if frontmatter is None:
        return content
    if not re.search(r"^status:\s*archived\s*$", frontmatter.group(1), re.MULTILINE):
        return content
    return _reactivated(content, frontmatter)


def _reactivated(content: str, frontmatter: re.Match[str]) -> str:
    if frontmatter.group(1).strip() == _INSERTED_FIELD:
        return content[frontmatter.end():]
    previous = _field_value(frontmatter.group(1), BEFORE_ARCHIVE_RE)
    if not previous:
        return re.sub(r"^status:\s*archived\s*\n", "", content, count=1, flags=re.MULTILINE)
    return _previous_status(content, previous)


def _previous_status(content: str, previous: str) -> str:
    """The status word archiving displaced, and no trace of the marker."""
    without_marker = re.sub(
        rf"^{STATUS_BEFORE_ARCHIVE}:\s*.*\n",
        "",
        content,
        count=1,
        flags=re.MULTILINE,
    )
    return re.sub(
        _STATUS_LINE_RE, lambda _: f"status: {previous}", without_marker, count=1
    )


def _restore_destination(archived: Path) -> Path:
    """Where an archived page belongs: `archive/<year>/<subdir>/<name>`."""
    relative = archived.relative_to(ARCHIVE_ROOT).parts
    return KNOWLEDGE.joinpath(*relative[1:])


def _committed_restore(archived: Path, destination: Path, source_bytes: bytes) -> bool:
    content = _without_archived_status(source_bytes.decode("utf-8")).encode("utf-8")
    relative = archived.relative_to(ROOT).as_posix()
    try:
        mutate_knowledge(
            stable_operation_id("restore-page", relative, content),
            {destination: content, archived: None},
            preconditions={
                relative: sha256_bytes(source_bytes),
                destination.relative_to(ROOT).as_posix(): ABSENT,
            },
        )
    except (OSError, RuntimeError, ValueError):
        return False
    return True


def restore_page(slug: str, *, apply: bool) -> str:
    """Bring one archived page back to the active tree.

    Archiving is dormancy, not deletion, so reactivation is part of the
    contract rather than a manual `mv`: the page returns to the directory it
    came from and loses the `status: archived` line that archiving added.
    """
    archived = _archived_pages(slug)
    if not archived:
        return f"NOT ARCHIVED: {slug}"
    return _restore_one(archived[0], apply)


def _restore_one(source: Path, apply: bool) -> str:
    """One archived copy, checked against the active tree before it moves."""
    destination = _restore_destination(source)
    if destination.exists():
        return f"ALREADY ACTIVE: {destination.relative_to(ROOT).as_posix()}"
    if not apply:
        return f"WOULD RESTORE: {source.relative_to(ROOT).as_posix()}"
    return _restored(source, destination)


def _restored(source: Path, destination: Path) -> str:
    try:
        source_bytes = read_stable_bytes(
            source, MAX_ARCHIVE_PAGE_BYTES, label="restore source"
        )
    except (OSError, ValueError):
        return f"READ_ERROR: {source}"
    if not _committed_restore(source, destination, source_bytes):
        return f"WRITE_ERROR: {destination}"
    return f"RESTORED: {destination.relative_to(ROOT).as_posix()}"


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
        "--restore",
        metavar="SLUG",
        default=None,
        help="Bring one archived page back to the active tree",
    )
    parser.add_argument(
        "--explain", action="store_true", help="Show why each page was flagged"
    )
    return parser.parse_args()


def main() -> int:
    args = _parsed_arguments()
    if args.restore:
        outcome = restore_page(args.restore, apply=args.apply)
        print(f"  {outcome}")
        return 1 if "ERROR" in outcome else 0
    cutoff = datetime.now().timestamp() - (args.days * 86400)
    committed = committed_content_times(ROOT)
    print(
        f"Age from committed content for {len(committed)} page(s); "
        "the rest age by their file clock."
    )
    stale = _stale_pages(cutoff, args.days, committed)
    if not stale:
        print(f"No stale pages found (threshold: {args.days} days).")
        return 0
    print(f"Found {len(stale)} stale page(s) older than {args.days} days:\n")
    failures = _archive_all(stale, args.apply)
    _print_outcome(len(stale), failures, args.apply)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
