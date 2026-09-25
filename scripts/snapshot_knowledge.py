#!/usr/bin/env python3
"""Keep a second copy of the memory, outside the vault, with its history.

Measured 2026-09-02: `knowledge/` is 20 MB and about 2.8 MB of it changes in a
day. Of 116 pages only 83 are in git, and of 15 daily logs only 3 — the rest is
private by design and therefore exists in exactly one place, on one disk. The
undo trail was not a second copy either: it held before-and-after images of
individual writes, and it has just been cut from 5.0 GB to 307 MB because a
month of them was an ad-hoc backup nobody could restore a day from.

This is the real second copy. It lives at `~/llm-wiki-snapshots/`, outside the
vault, so that losing or wiping the vault directory does not take the copy with
it. The owner asked for the same disk; that trades protection against a disk
failure for having no cloud and no password to lose, and it is stated here
rather than implied.

**Git rather than Restic**, which is a deviation from
`docs/research/2026-09-02-where-undo-belongs-and-for-how-long.md` and is
deliberate. Restic buys client-side encryption, which matters when the copy
leaves the machine — and the owner ruled that out. What is left is history,
deduplication and compression of a small text tree, which is exactly what git
already does, with no new dependency, no daemon, and no passphrase that would
lose the backup if forgotten. `git log` and `git restore` are the recovery path.

The repository has **no remote and never gets one**. It cannot push anywhere.
That is the whole answer to "will it leak": there is nowhere to leak to.

See `knowledge/notes/memory-keeps-a-second-copy-decision.md`.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from memory_state import ROOT  # noqa: E402

DEFAULT_SNAPSHOT_ROOT = Path.home() / "llm-wiki-snapshots"

# What is worth a second copy: the memory itself. Not `cache/`, `logs/` or
# `run/`, which are regenerable or operational by contract.
SNAPSHOT_SUBTREE = "knowledge"

# When the last snapshot succeeded, committed or not. Inside `.git`, so it is never
# part of the copy: an unchanged memory makes no commit, and the commit time alone
# made doctor call a current copy stale (audit B-32,
# docs/research/2026-09-25-a-snapshot-says-when-it-last-looked.md).
CHECKED_MARKER = Path(".git") / "llm-wiki-last-snapshot"


class SnapshotFailed(RuntimeError):
    """The copy could not be committed; the reason is git's own last line."""


def snapshot_root(home: Path | None = None) -> Path:
    """`LLM_WIKI_SNAPSHOT_ROOT`, else `llm-wiki-snapshots` in the given home or the user's."""
    raw = os.environ.get("LLM_WIKI_SNAPSHOT_ROOT", "").strip()
    if raw:
        return Path(raw)
    if home is None:
        return DEFAULT_SNAPSHOT_ROOT
    return Path(home) / DEFAULT_SNAPSHOT_ROOT.name


def _git(root: Path, *arguments: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *arguments],
        cwd=str(root),
        capture_output=True,
        text=True,
        check=False,
    )


def _initialised(root: Path) -> None:
    """A repository with no remote, created once, owner-only."""
    root.mkdir(parents=True, exist_ok=True)
    root.chmod(0o700)
    if (root / ".git").is_dir():
        return
    _git(root, "init", "--quiet")
    _git(root, "config", "user.name", "llm-wiki snapshot")
    _git(root, "config", "user.email", "snapshot@localhost")


def _mirrored(source: Path, destination: Path) -> None:
    """The subtree as it is now, with anything deleted since gone from the copy."""
    if destination.exists():
        shutil.rmtree(destination)
    shutil.copytree(source, destination, symlinks=True)


def _committed(root: Path, message: str) -> str:
    """The new commit, or "no change" when nothing differs; a failed commit raises."""
    _require_success(_git(root, "add", "-A"), "git add")
    if _git(root, "diff", "--cached", "--quiet").returncode == 0:
        return "no change"
    _require_success(_git(root, "commit", "--quiet", "-m", message), "git commit")
    return _git(root, "rev-parse", "--short", "HEAD").stdout.strip() or "committed"


def _require_success(result: subprocess.CompletedProcess, step: str) -> None:
    if result.returncode == 0:
        return
    lines = (result.stderr or result.stdout or "").strip().splitlines()
    raise SnapshotFailed(f"{step} failed: {lines[-1] if lines else result.returncode}")


def _mark_checked(root: Path, stamp: str) -> None:
    (root / CHECKED_MARKER).write_text(stamp + "\n", encoding="utf-8")


def last_checked_at(root: Path) -> datetime | None:
    """When the last snapshot succeeded, from its marker; None when there is none."""
    try:
        return datetime.fromisoformat((root / CHECKED_MARKER).read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def take_snapshot(vault: Path | None = None, root: Path | None = None) -> dict:
    """One snapshot of the memory. Returns what it did, never raises for 'nothing'."""
    source = (vault or ROOT) / SNAPSHOT_SUBTREE
    if not source.is_dir():
        return {"status": "no memory to snapshot", "commit": None}
    destination_root = root or snapshot_root()
    _initialised(destination_root)
    _mirrored(source, destination_root / SNAPSHOT_SUBTREE)
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    commit = _committed(destination_root, f"snapshot {stamp}")
    _mark_checked(destination_root, stamp)
    return {"status": "ok", "commit": commit}


def _report(result: dict, root: Path) -> str:
    return f"snapshot at {root}: {result['status']}, {result['commit']}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vault", default=None)
    parser.add_argument("--snapshot-root", default=None)
    arguments = parser.parse_args()
    vault = Path(arguments.vault) if arguments.vault else None
    root = Path(arguments.snapshot_root) if arguments.snapshot_root else snapshot_root()
    print(_report(take_snapshot(vault, root), root))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
