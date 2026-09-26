"""Split the test files across CI shards by measured cost.

The full suite takes about an hour on a hosted Windows runner, and a run that
long starves its own concurrency tests. Each shard is a separate runner with its
own cores, so splitting the files shortens the wall clock without adding
contention on one machine.

`shard_weights.json` holds seconds per file, the slowest any CI job measured.
Refresh it from the JUnit artifacts the workflow uploads
(`python -m tests.shard_plan --refresh <downloaded artifacts>`), and weigh a new
test file with `python -m tests.shard_plan --weigh tests/test_new.py`; a test
holds every test file to having a weight. A file still without one costs the
table's mean, not a guess: a flat 5 s left 578 of 705 files unweighted and the
Windows shards of one run between 809 s and 1996 s (audit 2026-09-26 C-13,
docs/research/2026-09-26-every-test-file-has-a-weight.md).
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from collections.abc import Iterable
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
WEIGHTS_PATH = TESTS_DIR / "shard_weights.json"
# Only when the table is empty; otherwise an unweighted file costs the table's mean.
DEFAULT_WEIGHT_SECONDS = 5.0


def test_files() -> list[str]:
    """Every collected test module, in a stable order."""
    return sorted(path.name for path in TESTS_DIR.glob("test_*.py"))


def weights() -> dict[str, float]:
    if not WEIGHTS_PATH.is_file():
        return {}
    return json.loads(WEIGHTS_PATH.read_text(encoding="utf-8"))


def _unknown_cost(table: dict[str, float]) -> float:
    if not table:
        return DEFAULT_WEIGHT_SECONDS
    return sum(table.values()) / len(table)


def _cost(name: str, table: dict[str, float]) -> float:
    return float(table.get(name, _unknown_cost(table)))


def _case_module(case: ET.Element) -> str:
    """`tests.test_x…`: the classname, or the name of a module skipped whole at collection."""
    return case.get("classname") or case.get("name") or ""


def _case_file(case: ET.Element) -> str | None:
    """`tests.test_x` as the file name `test_x.py`."""
    parts = _case_module(case).split(".")
    if len(parts) < 2 or parts[0] != "tests":
        return None
    return f"{parts[1]}.py"


def junit_seconds(report: Path) -> dict[str, float]:
    """Seconds per test file in one JUnit report."""
    seconds: dict[str, float] = {}
    for case in ET.parse(report).getroot().iter("testcase"):
        name = _case_file(case)
        if name is not None:
            seconds[name] = seconds.get(name, 0.0) + float(case.get("time") or 0.0)
    return seconds


def slowest(reports: Iterable[Path]) -> dict[str, float]:
    """Each file's cost in the slowest job that ran it."""
    merged: dict[str, float] = {}
    for report in reports:
        for name, seconds in junit_seconds(report).items():
            merged[name] = max(merged.get(name, 0.0), seconds)
    return merged


def refreshed(table: dict[str, float], measured: dict[str, float]) -> dict[str, float]:
    """Measured files take their new cost; files that no longer exist leave the table."""
    present = set(test_files())
    merged = {**table, **measured}
    return {name: round(merged[name], 2) for name in sorted(merged) if name in present}


def _write_weights(table: dict[str, float]) -> None:
    WEIGHTS_PATH.write_text(json.dumps(table, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def weigh(paths: list[str]) -> dict[str, float]:
    """Run the given test files once here and return their measured seconds."""
    with tempfile.TemporaryDirectory() as directory:
        report = Path(directory) / "junit.xml"
        command = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", f"--junitxml={report}", *paths]
        subprocess.call(command)
        return junit_seconds(report) if report.is_file() else {}


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
    parser.add_argument("--shard", type=int, help="1-based shard number")
    parser.add_argument("--of", type=int, help="total shard count")
    parser.add_argument("--refresh", type=Path, help="rewrite the weights from JUnit files under this directory")
    parser.add_argument("--weigh", nargs="+", help="measure these test files here and record them")
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
    if args.refresh is not None:
        _write_weights(refreshed(weights(), slowest(sorted(args.refresh.rglob("*.xml")))))
        return 0
    if args.weigh:
        _write_weights(refreshed(weights(), weigh(args.weigh)))
        return 0
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
