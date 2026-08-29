"""Aggregate several ``run_code_parity`` reports into one spread table.

CODE-07 re-run (2026-08-29). One run of the parity stand is a point;
codebase-memory-mcp was measured non-deterministic on an unchanged
repository, so a single point cannot separate a product difference from
that product's own variance. This reads N report files and prints, per
side, the min-max spread of every column the stand reports, plus the
paired token comparison over the tasks a chosen pair both answered
correctly in that same run.

Usage:
    uv run python benchmark/aggregate_code_parity.py report1.json ...
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

SIDES = ("llm_wiki", "llm_wiki_best", "cbm")
PAIR = ("llm_wiki_best", "cbm")
COLUMNS = (
    "correct",
    "wrong",
    "total_tokens",
    "total_seconds",
    "wrong_but_confident",
    "operator_attention_events",
)


def load_reports(paths: list[Path]) -> list[dict]:
    return [json.loads(path.read_text(encoding="utf-8")) for path in paths]


def _present_sides(reports: list[dict]) -> list[str]:
    return [side for side in SIDES if all(side in r["summary"] for r in reports)]


def _column_values(reports: list[dict], side: str, column: str) -> list:
    return [r["summary"][side][column] for r in reports]


def _spread(values: list) -> str:
    low, high = min(values), max(values)
    if low == high:
        return f"{low}"
    return f"{low}-{high}"


def _side_row(reports: list[dict], side: str) -> dict:
    row = {"side": side, "runs": len(reports)}
    for column in COLUMNS:
        row[column] = _spread(_column_values(reports, side, column))
    return row


def _both_correct(task: dict, pair: tuple[str, str]) -> bool:
    return all(task[side]["grade"] == "correct" for side in pair)


def _paired_tokens_one(report: dict, pair: tuple[str, str]) -> dict:
    shared = [t for t in report["tasks"] if _both_correct(t, pair)]
    totals = {side: sum(t[side]["tokens"] for t in shared) for side in pair}
    return {"tasks": len(shared), **totals}


def paired_tokens(reports: list[dict], pair: tuple[str, str]) -> list[dict]:
    usable = [r for r in reports if all(side in r["summary"] for side in pair)]
    return [_paired_tokens_one(report, pair) for report in usable]


def _ratio(entry: dict, pair: tuple[str, str]) -> float:
    denominator = entry[pair[1]] or 1
    return round(entry[pair[0]] / denominator, 2)


def _print_sides(reports: list[dict]) -> None:
    header = ("side", "runs", *COLUMNS)
    print(" | ".join(header))
    for side in _present_sides(reports):
        row = _side_row(reports, side)
        print(" | ".join(str(row[key]) for key in header))


def _print_pair(reports: list[dict], pair: tuple[str, str]) -> None:
    entries = paired_tokens(reports, pair)
    if not entries:
        return
    print()
    print(f"paired tokens on tasks both {pair[0]} and {pair[1]} answered correctly")
    for entry in entries:
        ratio = _ratio(entry, pair)
        print(
            f"  n={entry['tasks']} {pair[0]}={entry[pair[0]]} "
            f"{pair[1]}={entry[pair[1]]} ratio={ratio}x"
        )


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("reports", nargs="+", type=Path)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    reports = load_reports(args.reports)
    _print_sides(reports)
    _print_pair(reports, PAIR)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
