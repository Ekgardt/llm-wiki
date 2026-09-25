#!/usr/bin/env python3
"""Retire benchmark run directories nobody has touched for a month.

`cache/benchmarks/` holds two kinds of directory: dataset caches, named by the
`DATASET_DIR` of each `benchmark/*_data.py`, which are expensive to fetch again
and are kept; and run outputs an operator command wrote (`full-2026-09-17`,
`locomo-2026-09-19`, with staged vaults inside), which nothing removed: 1.5 GB on
2026-09-24. A run directory not modified for `RUN_RETENTION_DAYS` is removed;
`cache/` is disposable by contract, and the results worth keeping are published
under `benchmark/*.json`. Run by the nightly pass. See
`docs/research/2026-09-24-every-store-has-a-bound.md`.
"""

from __future__ import annotations

import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from memory_state import ROOT  # noqa: E402

# The `DATASET_DIR` names of `benchmark/*_data.py`; a test keeps them in step.
DATASET_CACHES = frozenset({"beam", "litragbench", "locomo", "longmemeval", "refusalbench"})
RUN_RETENTION_DAYS = 30


def benchmarks_directory(root: Path = ROOT) -> Path:
    return Path(root) / "cache" / "benchmarks"


def _newest_mtime(directory: Path) -> float:
    """The newest modification under a run, so a run still being written is kept."""
    times = [directory.stat().st_mtime]
    times.extend(path.stat().st_mtime for path in directory.rglob("*") if not path.is_symlink())
    return max(times)


def stale_runs(directory: Path, now: float, retention_days: int = RUN_RETENTION_DAYS) -> list[Path]:
    """Run directories untouched past the window; dataset caches are never runs."""
    horizon = now - retention_days * 86400
    return sorted(path for path in _runs(directory) if _newest_mtime(path) < horizon)


def _runs(directory: Path) -> list[Path]:
    if not directory.is_dir():
        return []
    return [path for path in directory.iterdir() if _is_run(path)]


def _is_run(path: Path) -> bool:
    return path.is_dir() and not path.is_symlink() and path.name not in DATASET_CACHES


def retire(directory: Path, now: float | None = None) -> list[str]:
    removed = []
    for run in stale_runs(directory, time.time() if now is None else now):
        shutil.rmtree(run)
        removed.append(run.name)
    return removed


def main() -> int:
    removed = retire(benchmarks_directory())
    print(f"retire_benchmark_runs: removed {len(removed)} run(s): {', '.join(removed) or 'none'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
