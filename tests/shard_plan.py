"""Split the test files across CI shards by measured cost.

The full suite takes about an hour on a hosted Windows runner, and a run that
long starves its own concurrency tests. Each shard is a separate runner with its
own cores, so splitting the files shortens the wall clock without adding
contention on one machine.

`shard_weights.json` holds seconds per file from a full local run
(`pytest -q --durations=0`); refresh it from that output or from the junit
artifacts the workflow uploads. Files missing from it fall back to a small
default weight, so adding a test file never breaks the split.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
WEIGHTS_PATH = TESTS_DIR / "shard_weights.json"
DEFAULT_WEIGHT_SECONDS = 5.0


def test_files() -> list[str]:
    """Every collected test module, in a stable order."""
    return sorted(path.name for path in TESTS_DIR.glob("test_*.py"))


def weights() -> dict[str, float]:
    if not WEIGHTS_PATH.is_file():
        return {}
    return json.loads(WEIGHTS_PATH.read_text(encoding="utf-8"))


def _cost(name: str, table: dict[str, float]) -> float:
    return float(table.get(name, DEFAULT_WEIGHT_SECONDS))


def plan(shard_count: int) -> list[list[str]]:
    """Longest-processing-time packing: the costliest file picks the emptiest shard."""
    _require_positive(shard_count)
    table = weights()
    ordered = sorted(test_files(), key=lambda name: (-_cost(name, table), name))
    shards: list[list[str]] = [[] for _ in range(shard_count)]
    totals = [0.0] * shard_count
    for name in ordered:
        index = totals.index(min(totals))
        shards[index].append(name)
        totals[index] += _cost(name, table)
    return [sorted(shard) for shard in shards]


def _require_positive(shard_count: int) -> None:
    if isinstance(shard_count, bool) or not isinstance(shard_count, int) or shard_count < 1:
        raise ValueError("shard count must be a positive integer")


def _require_selected(shard: int, shard_count: int) -> None:
    _require_positive(shard_count)
    if isinstance(shard, bool) or not isinstance(shard, int) or not 1 <= shard <= shard_count:
        raise ValueError("shard must be between 1 and the shard count")


def _pytest_arguments(raw: list[str]) -> list[str]:
    return raw[1:] if raw[:1] == ["--"] else raw


def _parse(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run one CI shard of the test suite.")
    parser.add_argument("--shard", type=int, required=True, help="1-based shard number")
    parser.add_argument("--of", type=int, required=True, help="total shard count")
    parser.add_argument("--list", action="store_true", help="print the files and exit")
    parser.add_argument("pytest_args", nargs=argparse.REMAINDER)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Run the shard in a child `python -m pytest`, not in this process.

    `multiprocessing` with the spawn start method re-imports the main module in
    every child. Tests that spawn workers would therefore re-enter this module
    instead of pytest's, which is not the shape the suite runs under anywhere
    else. Handing the work to `python -m pytest` keeps that shape identical.
    """
    args = _parse(argv)
    _require_selected(args.shard, args.of)
    files = plan(args.of)[args.shard - 1]
    if args.list:
        print("\n".join(files))
        return 0
    paths = [str(TESTS_DIR / name) for name in files]
    command = [sys.executable, "-m", "pytest", *_pytest_arguments(args.pytest_args), *paths]
    return subprocess.call(command)


if __name__ == "__main__":
    raise SystemExit(main())
