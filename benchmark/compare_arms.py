#!/usr/bin/env python3
"""Judge two arms of a benchmark against a rule fixed before the run.

Six LongMemEval runs on this vault span 0.11 at n=50, and every published
figure the backlog compares against is a three-run mean. A stand that cannot see
that spread cannot see a real improvement either — and, worse, cannot see the
regression that the largest item in the backlog risks on the categories that
currently work.

The rule, stated in
`docs/research/2026-08-31-a-decision-rule-stated-before-the-run.md` and computed
here rather than written by hand:

    An arm wins a category only if its mean exceeds the baseline's mean by more
    than the baseline's own observed spread in that category. A drop of more
    than that spread is a loss, and a loss blocks the change whatever the
    overall mean does. Everything else is "no difference measured" — which is
    not "no difference", and is not a win.

Spread is `max - min` across an arm's runs: the plainest statement of how much
the number moves when nothing changed.

Usage:

    python benchmark/compare_arms.py \\
        --baseline before-1.json before-2.json before-3.json \\
        --candidate after-1.json after-2.json after-3.json
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

WIN = "win"
LOSS = "loss"
NO_DIFFERENCE = "no difference measured"

# The field of interest in each report row. Kept as a name rather than inlined
# so a later comparison over a different measure needs one argument, not a fork.
DEFAULT_METRIC = "accuracy"

# What the rule assumes each arm was run. Fewer is not refused — a first look is
# worth having — but the verdict is then weaker than it reads, and saying so is
# cheaper than remembering it. Measured on this vault 2026-08-31: three baseline
# runs and one candidate run already produce two "win" verdicts, on categories
# whose baseline spread happened to be exactly zero.
MINIMUM_RUNS = 3


def _reports(paths: list[str]) -> list[dict]:
    return [json.loads(Path(path).read_text(encoding="utf-8")) for path in paths]


def _values(reports: list[dict], category: str, metric: str) -> list[float]:
    found = []
    for report in reports:
        row = report.get(category)
        if isinstance(row, dict) and isinstance(row.get(metric), (int, float)):
            found.append(float(row[metric]))
    return found


def summarise(values: list[float]) -> dict[str, float | int | None]:
    """Mean, min, max and spread — or a shape that says there was nothing."""
    if not values:
        return {"runs": 0, "mean": None, "min": None, "max": None, "spread": None}
    return {
        "runs": len(values),
        "mean": round(sum(values) / len(values), 4),
        "min": round(min(values), 4),
        "max": round(max(values), 4),
        "spread": round(max(values) - min(values), 4),
    }


def _both_measured(baseline: dict, candidate: dict) -> bool:
    return baseline["mean"] is not None and candidate["mean"] is not None


def _judged(difference: float, threshold: float) -> str:
    if difference > threshold:
        return WIN
    return LOSS if difference < -threshold else NO_DIFFERENCE


def verdict(baseline: dict, candidate: dict) -> str:
    """Win, loss, or no difference measured — against the baseline's own noise."""
    if not _both_measured(baseline, candidate):
        return NO_DIFFERENCE
    return _judged(
        candidate["mean"] - baseline["mean"], baseline["spread"] or 0.0
    )


def _categories(reports: list[dict]) -> list[str]:
    names: dict[str, None] = {}
    for report in reports:
        for name in report:
            names.setdefault(name, None)
    return sorted(names, key=lambda name: (name == "overall", name))


def compare(
    baseline_reports: list[dict],
    candidate_reports: list[dict],
    *,
    metric: str = DEFAULT_METRIC,
) -> dict[str, dict]:
    rows: dict[str, dict] = {}
    for category in _categories(baseline_reports + candidate_reports):
        before = summarise(_values(baseline_reports, category, metric))
        after = summarise(_values(candidate_reports, category, metric))
        rows[category] = {
            "baseline": before,
            "candidate": after,
            "verdict": verdict(before, after),
        }
    return rows


def _cell(value: float | int | None) -> str:
    return "-" if value is None else f"{value}"


def _line(category: str, row: dict) -> str:
    before, after = row["baseline"], row["candidate"]
    return (
        f"{category:<26} "
        f"{_cell(before['mean']):>7} ±{_cell(before['spread']):<7} "
        f"{_cell(after['mean']):>7} ±{_cell(after['spread']):<7} "
        f"{row['verdict']}"
    )


def render(rows: dict[str, dict]) -> str:
    header = f"{'category':<26} {'baseline':>7} {'spread':<8} {'candidate':>7} {'spread':<8} verdict"
    return "\n".join([header] + [_line(name, row) for name, row in rows.items()])


def _blocking_losses(rows: dict[str, dict]) -> list[str]:
    return [name for name, row in rows.items() if row["verdict"] == LOSS]


def under_run_arms(rows: dict[str, dict]) -> list[str]:
    """Arms whose overall row rests on fewer runs than the rule assumes."""
    overall = rows.get("overall")
    if not isinstance(overall, dict):
        return []
    return [
        arm
        for arm in ("baseline", "candidate")
        if overall[arm]["runs"] < MINIMUM_RUNS
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", nargs="+", required=True)
    parser.add_argument("--candidate", nargs="+", required=True)
    parser.add_argument("--metric", default=DEFAULT_METRIC)
    parser.add_argument("--out", default=None, help="write the comparison as JSON")
    args = parser.parse_args()

    rows = compare(
        _reports(args.baseline), _reports(args.candidate), metric=args.metric
    )
    print(render(rows))
    thin = under_run_arms(rows)
    if thin:
        print(
            f"\nweak: {', '.join(thin)} ran fewer than {MINIMUM_RUNS} times; "
            "every verdict above is a first look, not a measurement"
        )
    losses = _blocking_losses(rows)
    if losses:
        print(f"\nblocked: {', '.join(losses)} lost by more than the baseline's spread")
    if args.out:
        Path(args.out).write_text(
            json.dumps(rows, indent=2, ensure_ascii=False), encoding="utf-8"
        )
    return 1 if losses else 0


if __name__ == "__main__":
    raise SystemExit(main())
