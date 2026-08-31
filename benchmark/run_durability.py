"""Measure zero silent loss as a number: N killed captures, every one accounted for.

Usage:
    uv run python benchmark/run_durability.py --trials 108 --seed 0
    uv run python benchmark/run_durability.py --trials 20 --report out.json

Each trial drives one session_end capture through the real adapter -> queue ->
worker chain in subprocesses inside a freshly adopted temp vault, SIGKILLs the
owning process at one of the injected boundaries (both producer and worker
stages, before and after each boundary), runs the documented recovery, and
audits every durable surface. A trial counts against the property only when
its content is gone with no durable trace — see `durability_stand.OUTCOMES`.

The stand kills processes; it does not cut power. Nothing here proves fsync
behaviour under power loss, and the report says so.
"""
from __future__ import annotations

import argparse
import json
import random
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from durability_stand import (  # noqa: E402
    OUTCOMES,
    TrialResult,
    TrialSpec,
    discard_trial,
    kill_points,
    run_trial,
)

CLEAN_SENTINELS = 2


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--trials", type=int, default=108, help="killed trials (>= 1)")
    parser.add_argument("--seed", type=int, default=0, help="shuffle seed for kill order")
    parser.add_argument("--base", type=Path, default=None, help="trial scratch directory")
    parser.add_argument("--keep", action="store_true", help="keep every trial vault on disk")
    parser.add_argument("--report", type=Path, default=None, help="write the JSON report here")
    return parser.parse_args(argv)


def assigned_specs(trials: int, seed: int) -> list[TrialSpec]:
    """Clean sentinels first, then killed trials stratified over every point."""
    points = kill_points()
    killed = [TrialSpec(*points[index % len(points)]) for index in range(trials)]
    random.Random(seed).shuffle(killed)
    clean = [TrialSpec(None, "before") for _ in range(CLEAN_SENTINELS)]
    return clean + killed


def _spec_label(spec: TrialSpec) -> str:
    if spec.stage is None:
        return "clean"
    return f"{spec.stage}:{spec.point}"


def _keep_for_forensics(result: TrialResult) -> bool:
    kill_missed = result.stage is not None and not result.kill_observed
    return result.outcome == "silent-loss" or kill_missed


def _run_one(spec: TrialSpec, index: int, base: Path, keep: bool) -> TrialResult:
    trial_base = base / f"trial-{index:04d}-{_spec_label(spec).replace(':', '-')}"
    result = run_trial(spec, trial_base, marker=f"stand-{index:04d}")
    if not (keep or _keep_for_forensics(result)):
        discard_trial(trial_base)
    return result


def run_all(specs: list[TrialSpec], base: Path, keep: bool) -> list[TrialResult]:
    results = []
    for index, spec in enumerate(specs):
        started = time.perf_counter()
        result = _run_one(spec, index, base, keep)
        elapsed = time.perf_counter() - started
        print(
            f"trial {index:04d} {_spec_label(spec):>24} -> {result.outcome:>15} "
            f"(recovery runs {result.recovery_runs}, {elapsed:.1f}s)",
            flush=True,
        )
        results.append(result)
    return results


def _point_summary(results: list[TrialResult]) -> dict[str, dict]:
    summary: dict[str, dict] = {}
    for result in results:
        label = _spec_label(TrialSpec(result.stage, result.point))
        entry = summary.setdefault(label, {"trials": 0, "outcomes": Counter(), "recovery": []})
        entry["trials"] += 1
        entry["outcomes"][result.outcome] += 1
        entry["recovery"].append(result.recovery_runs)
    return summary


def _killed(results: list[TrialResult]) -> list[TrialResult]:
    return [result for result in results if result.stage is not None]


def _named_reasons(results: list[TrialResult]) -> list[str]:
    reasons: set[str] = set()
    for result in results:
        reasons.update(result.worker_reasons)
    return sorted(reasons)


def _landed_recovery_runs(killed: list[TrialResult]) -> list[int]:
    landed = [result for result in killed if result.outcome in ("landed", "duplicated")]
    return [result.recovery_runs for result in landed]


def _outcome_totals(results: list[TrialResult]) -> dict[str, int]:
    counts = Counter(result.outcome for result in results)
    return {outcome: counts.get(outcome, 0) for outcome in OUTCOMES}


def _per_point_report(results: list[TrialResult]) -> dict[str, dict]:
    report = {}
    for label, entry in sorted(_point_summary(results).items()):
        report[label] = {
            "trials": entry["trials"],
            "outcomes": dict(entry["outcomes"]),
            "mean_recovery_runs": _mean(entry["recovery"]),
        }
    return report


def aggregate(results: list[TrialResult]) -> dict:
    killed = _killed(results)
    outcomes = _outcome_totals(results)
    return {
        "trials": len(results),
        "killed_trials": len(killed),
        "kills_observed": sum(1 for result in killed if result.kill_observed),
        "outcomes": outcomes,
        "silent_losses": outcomes["silent-loss"],
        "mean_recovery_runs_when_landed": _mean(_landed_recovery_runs(killed)),
        "named_failure_reasons": _named_reasons(results),
        "per_point": _per_point_report(results),
    }


def _mean(values: list[int]) -> float | None:
    if not values:
        return None
    return round(sum(values) / len(values), 2)


def _print_totals(report: dict) -> None:
    print("\n=== zero-silent-loss durability stand ===")
    print(f"trials: {report['trials']} ({report['killed_trials']} killed)")
    print(f"kills observed: {report['kills_observed']}/{report['killed_trials']}")
    for outcome in OUTCOMES:
        print(f"  {outcome:>16}: {report['outcomes'][outcome]}")
    print(f"silent losses: {report['silent_losses']} (target 0)")
    print(f"mean recovery runs when landed: {report['mean_recovery_runs_when_landed']}")


def _print_points(report: dict) -> None:
    print("\nper kill point (outcome: count):")
    for label, entry in report["per_point"].items():
        outcomes = ", ".join(f"{key}={value}" for key, value in sorted(entry["outcomes"].items()))
        print(f"  {label:>24} x{entry['trials']}: {outcomes}")
    print("\ndistinct named failure reasons observed:")
    for reason in report["named_failure_reasons"]:
        print(f"  - {reason}")


def print_report(report: dict) -> None:
    _print_totals(report)
    _print_points(report)


def _verdict(report: dict) -> int:
    missed = report["killed_trials"] - report["kills_observed"]
    failed = report["silent_losses"] > 0 or missed > 0
    print(f"\nverdict: {'FAIL' if failed else 'PASS'} (silent losses "
          f"{report['silent_losses']}, kills missed {missed})")
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    base = args.base or Path(tempfile.mkdtemp(prefix="llmwiki-durability-"))
    base.mkdir(parents=True, exist_ok=True)
    print(f"trial scratch: {base}")
    results = run_all(assigned_specs(args.trials, args.seed), base, args.keep)
    report = aggregate(results)
    print_report(report)
    if args.report is not None:
        args.report.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"report written: {args.report}")
    return _verdict(report)


if __name__ == "__main__":
    raise SystemExit(main())
