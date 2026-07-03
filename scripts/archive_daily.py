"""Archive old compiled daily logs under `memory/daily/archive/YYYY-MM/`.

Policy (from [[Memory Subsystem Action Plan]]):

- Daily logs are append-only; never delete.
- Keep `memory/daily/` flat while file count is comfortable.
- When the flat directory grows past a threshold (default 30 files),
  move **compiled** daily logs older than `MAX_AGE_DAYS` (default 90)
  into `memory/daily/archive/YYYY-MM/` based on the log's date.
- **Un-compiled** logs stay in the flat directory regardless of age,
  so the next compile pass still finds them.

"Compiled" means the log's file hash is present in
`$LLM_WIKI_STATE_ROOT/memory-state/state.json::compiled_daily_hashes`
(managed by `compile_memory.py`; runtime state lives outside the vault
so git and Obsidian don't track ephemeral churn).

Usage:
    uv run python scripts/archive_daily.py                 # dry-run by default
    uv run python scripts/archive_daily.py --commit        # actually move files
    uv run python scripts/archive_daily.py --threshold 50  # delay trigger
    uv run python scripts/archive_daily.py --max-age 180   # keep 6 months flat

The script exits 0 and prints nothing if the threshold isn't crossed —
safe to run from cron / hooks without generating noise.

Not wired into any hook yet. Will become a scheduled task or a
compile-time side-effect when `memory/daily/` actually approaches the
threshold. Until then, this file is dormant infrastructure (commit
it ready, don't trigger it until needed).
"""
from __future__ import annotations

import argparse
import re
import shutil
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from memory_state import ROOT, load_state  # noqa: E402

DAILY_DIR = ROOT / "memory" / "daily"
ARCHIVE_DIR = DAILY_DIR / "archive"

# Filename pattern: YYYY-MM-DD.md
DATE_NAME_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})\.md$")

DEFAULT_THRESHOLD = 30
DEFAULT_MAX_AGE_DAYS = 90


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument(
        "--commit",
        action="store_true",
        help="Actually move files. Without this flag the script only prints what it would do.",
    )
    p.add_argument(
        "--threshold",
        type=int,
        default=DEFAULT_THRESHOLD,
        help=f"Archive only when `memory/daily/` has >= this many flat files (default {DEFAULT_THRESHOLD}).",
    )
    p.add_argument(
        "--max-age",
        type=int,
        default=DEFAULT_MAX_AGE_DAYS,
        help=f"Archive compiled logs older than this many days (default {DEFAULT_MAX_AGE_DAYS}).",
    )
    p.add_argument(
        "--force",
        action="store_true",
        help="Ignore the flat-dir threshold; run the archive check anyway.",
    )
    return p.parse_args()


def flat_dailies() -> list[Path]:
    """Dailies sitting directly in memory/daily/ (not already in archive/)."""
    if not DAILY_DIR.exists():
        return []
    return sorted(p for p in DAILY_DIR.glob("*.md") if p.is_file())


def parse_date(name: str) -> datetime | None:
    m = DATE_NAME_RE.match(name)
    if not m:
        return None
    try:
        return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def find_candidates(
    dailies: list[Path], max_age_days: int, compiled_hashes: dict
) -> list[tuple[Path, Path]]:
    """Return (source, archive-target) pairs for logs eligible to move.

    Eligibility: name is a valid YYYY-MM-DD.md, log is compiled (hash in
    state.json), and its date is older than cutoff.
    """
    cutoff = datetime.now() - timedelta(days=max_age_days)
    out: list[tuple[Path, Path]] = []
    for p in dailies:
        d = parse_date(p.name)
        if d is None:
            continue
        if d >= cutoff:
            continue
        if p.name not in compiled_hashes:
            # Un-compiled logs stay put regardless of age — next compile
            # pass needs to find them.
            continue
        month_dir = ARCHIVE_DIR / f"{d.year:04d}-{d.month:02d}"
        out.append((p, month_dir / p.name))
    return out


def move_file(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dst))


def main() -> int:
    args = parse_args()
    dailies = flat_dailies()
    if not dailies:
        # Nothing to do, silent success.
        return 0

    if len(dailies) < args.threshold and not args.force:
        # Under threshold — silent no-op. Don't print "nothing to do",
        # this script is meant to run from cron.
        return 0

    state = load_state()
    compiled = state.get("compiled_daily_hashes", {})

    candidates = find_candidates(dailies, args.max_age, compiled)
    if not candidates:
        print(
            f"archive_daily: {len(dailies)} flat logs (threshold {args.threshold}), "
            f"none eligible (need compiled + age > {args.max_age}d)."
        )
        return 0

    verb = "Would move" if not args.commit else "Moving"
    print(
        f"archive_daily: {len(dailies)} flat logs, "
        f"{len(candidates)} eligible for archive (compiled + age > {args.max_age}d)."
    )
    for src, dst in candidates:
        rel_dst = dst.relative_to(ROOT).as_posix()
        print(f"  {verb}: {src.name} → {rel_dst}")
        if args.commit:
            move_file(src, dst)

    if not args.commit:
        print("(dry-run. Rerun with --commit to actually archive.)")
    else:
        print(f"Archived {len(candidates)} log(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
