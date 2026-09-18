#!/usr/bin/env python3
"""Refit the `lane_score` constants from a stand run, and report what changes, per type.

`scripts/lane_score.py` ships seven constants from a logistic fit made on 2026-09-16 over
189 LongMemEval questions. They were fitted on runs where both lanes spoke; the audit of
2026-09-17 showed the order they produce when the dense lane is silent and the reranker did
not run. Correcting them needs a measurement run, and this is the command that turns such a
run into new constants — with the per-type table that says whether they may be pasted.

It reads files. It calls no provider, loads no model and starts no run.

    uv run python benchmark/fit_lane_score.py results.jsonl
    uv run python benchmark/fit_lane_score.py run-a.jsonl run-b.jsonl --depth 12

Input: the per-question records a run writes (JSONL, one object per line, as
`benchmark/run_longmemeval.py` writes them, or a JSON array). Each record needs the
`lane_matrix` field the stand records: one row per candidate with `lexical_rank`,
`dense_rank`, `rerank_score`, `user_turn` and `evidence`.

Research: `docs/research/2026-09-17-the-lane-score-refit-is-one-command.md`.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

# The order coefficients are reported in, and the constant each one ships as.
FEATURE_NAMES = (
    "LEXICAL_RANK",
    "DENSE_RANK",
    "LEXICAL_SILENT",
    "DENSE_SILENT",
    "USER_TURN",
    "RERANK_SCORE",
    "RERANK_APPLIED",
)
# How many candidates the reader is handed; the measurement is "all evidence inside this".
READER_DEPTH = 12
# Gradient descent over a convex objective, on a fixed schedule with no seed and no solver
# options, so two runs of this command over one file print the same constants.
STEPS = 4000
LEARNING_RATE = 0.2
L2 = 1e-3
FOLDS = 5


@dataclass(frozen=True)
class Question:
    """One question's candidates, as features, labels, and the type it belongs to."""

    question_id: str
    question_type: str
    features: list[list[float]]
    labels: list[int]

    @property
    def has_evidence(self) -> bool:
        return any(self.labels)


def _rank_of(value: object) -> int:
    """A lane's rank as a positive integer; 0 when the lane never returned the row."""
    if not isinstance(value, (int, float)):
        return 0
    if value <= 0:
        return 0
    return int(value)


def _log_rank(value: object) -> float:
    from lane_score import FURTHEST_RANK

    rank = _rank_of(value)
    if rank <= 0:
        return math.log(FURTHEST_RANK)
    return math.log(min(rank, FURTHEST_RANK))


def _silent(value: object) -> float:
    if _rank_of(value) > 0:
        return 0.0
    return 1.0


def _rerank_terms(value: object) -> tuple[float, float]:
    """(the cross-encoder's score, whether it spoke at all)."""
    if not isinstance(value, (int, float)):
        return 0.0, 0.0
    return float(value), 1.0


def _flag(value: object) -> float:
    if value:
        return 1.0
    return 0.0


def _features_of(row: Mapping[str, object]) -> list[float]:
    """The seven features `lane_score` scores a candidate with."""
    score, applied = _rerank_terms(row.get("rerank_score"))
    return [
        _log_rank(row.get("lexical_rank")),
        _log_rank(row.get("dense_rank")),
        _silent(row.get("lexical_rank")),
        _silent(row.get("dense_rank")),
        _flag(row.get("user_turn")),
        score,
        applied,
    ]


def _matrix_rows(matrix: object) -> list[Mapping[str, object]]:
    if not isinstance(matrix, list):
        return []
    rows: list[Mapping[str, object]] = []
    for row in matrix:
        _keep_mapping(rows, row)
    return rows


def _keep_mapping(rows: list, row: object) -> None:
    if isinstance(row, Mapping):
        rows.append(row)


def _named(record: Mapping[str, object], key: str, fallback: str) -> str:
    value = str(record.get(key) or "")
    if not value:
        return fallback
    return value


def _question_of(record: Mapping[str, object]) -> Question | None:
    rows = _matrix_rows(record.get("lane_matrix"))
    if not rows:
        return None
    return Question(
        _named(record, "question_id", ""),
        _named(record, "question_type", "unknown"),
        [_features_of(row) for row in rows],
        [int(_flag(row.get("evidence"))) for row in rows],
    )


def _json_array(text: str) -> list[dict]:
    loaded = json.loads(text)
    return [item for item in loaded if isinstance(item, dict)]


def _one_record(line: str) -> dict | None:
    """One JSONL record, or None for a line that is not one.

    A run's aggregated report is a JSON object written over many lines, and an
    operator will point this at one by mistake. That is a file with no records,
    which the command already says plainly — not a traceback.
    """
    try:
        loaded = json.loads(line)
    except json.JSONDecodeError:
        return None
    if isinstance(loaded, dict):
        return loaded
    return None


def _json_lines(text: str) -> list[dict]:
    found = [_one_record(line) for line in text.splitlines() if line.strip()]
    return [record for record in found if record is not None]


def _records(path: Path) -> list[dict]:
    """The per-question records of one result file, JSONL or a JSON array."""
    text = path.read_text(encoding="utf-8")
    if text.lstrip().startswith("["):
        return _json_array(text)
    return _json_lines(text)


def _fittable(record: Mapping[str, object]) -> Question | None:
    item = _question_of(record)
    if item is None or not item.has_evidence:
        return None
    return item


def questions(paths: Sequence[Path]) -> list[Question]:
    """Every question with a lane matrix and at least one labelled evidence row."""
    found: list[Question] = []
    for path in paths:
        found.extend(_fitted_questions(_records(path)))
    return found


def _fitted_questions(records: Sequence[Mapping[str, object]]) -> list[Question]:
    items = [_fittable(record) for record in records]
    return [item for item in items if item is not None]


def _design(items: Sequence[Question]):
    import numpy as np

    rows = [row for item in items for row in item.features]
    labels = [label for item in items for label in item.labels]
    return np.asarray(rows, dtype=float), np.asarray(labels, dtype=float)


def _descended(design, labels, weights):
    """One gradient step of the L2-regularized logistic objective."""
    import numpy as np

    predicted = 1.0 / (1.0 + np.exp(-design @ weights))
    gradient = design.T @ (predicted - labels) / len(labels) + L2 * weights
    return weights - LEARNING_RATE * gradient


def fit(items: Sequence[Question]) -> list[float]:
    """L2-regularized logistic coefficients over every candidate of these questions."""
    import numpy as np

    design, labels = _design(items)
    weights = np.zeros(design.shape[1], dtype=float)
    for _step in range(STEPS):
        weights = _descended(design, labels, weights)
    return [float(value) for value in weights]


def shipped_weights() -> list[float]:
    import lane_score

    return [float(getattr(lane_score, name)) for name in FEATURE_NAMES]


def _dot(weights: Sequence[float], row: Sequence[float]) -> float:
    return sum(weight * value for weight, value in zip(weights, row))


def _head_indices(item: Question, weights: Sequence[float], depth: int) -> set[int]:
    """The candidate positions the reader would be handed, best score first."""
    scored = [(-_dot(weights, row), index) for index, row in enumerate(item.features)]
    scored.sort()
    return {index for _score, index in scored[:depth]}


def _evidence_indices(item: Question) -> list[int]:
    return [index for index, label in enumerate(item.labels) if label]


def all_evidence_in_depth(item: Question, weights: Sequence[float], depth: int) -> bool:
    """Whether every labelled row of this question is inside the reader's own depth."""
    head = _head_indices(item, weights, depth)
    return set(_evidence_indices(item)).issubset(head)


def _folds(items: Sequence[Question], count: int) -> list[list[Question]]:
    """Questions split by question, never by candidate, in a stable order."""
    ordered = sorted(items, key=lambda item: item.question_id)
    return [ordered[index::count] for index in range(count)]


def _verdicts(items: Sequence[Question], weights: Sequence[float], depth: int) -> dict[str, bool]:
    return {item.question_id: all_evidence_in_depth(item, weights, depth) for item in items}


def _rest_of(folds: Sequence[Sequence[Question]], skipped: int) -> list[Question]:
    return [item for index, fold in enumerate(folds) if index != skipped for item in fold]


def cross_validated(items: Sequence[Question], depth: int) -> dict[str, bool]:
    """Per question: is all its evidence inside the depth, under weights fitted without it."""
    verdicts: dict[str, bool] = {}
    folds = _folds(items, FOLDS)
    for index, held_out in enumerate(folds):
        rest = _rest_of(folds, index)
        verdicts.update(_verdicts(held_out, _weights_for(rest), depth))
    return verdicts


def _weights_for(rest: Sequence[Question]) -> list[float]:
    if not rest:
        return shipped_weights()
    return fit(rest)


def shipped_verdicts(items: Sequence[Question], depth: int) -> dict[str, bool]:
    return _verdicts(items, shipped_weights(), depth)


def _share(values: Sequence[bool]) -> tuple[int, float]:
    if not values:
        return 0, 0.0
    return len(values), round(sum(values) / len(values), 4)


def _grouped(items: Sequence[Question], verdicts: Mapping[str, bool]) -> dict[str, list[bool]]:
    groups: dict[str, list[bool]] = {}
    for item in items:
        groups.setdefault(item.question_type, []).append(verdicts[item.question_id])
    groups["overall"] = [verdicts[item.question_id] for item in items]
    return groups


def by_type(items: Sequence[Question], verdicts: Mapping[str, bool]) -> dict[str, tuple[int, float]]:
    """(questions, share whose evidence is all inside the depth), per type and overall."""
    groups = _grouped(items, verdicts)
    return {name: _share(values) for name, values in sorted(groups.items())}


def _print_table(items: Sequence[Question], shipped: Mapping, refit: Mapping) -> None:
    before, after = by_type(items, shipped), by_type(items, refit)
    print(f"{'question type':<32}{'n':>5}{'shipped':>10}{'refit':>10}")
    for name, (count, value) in after.items():
        print(f"{name:<32}{count:>5}{before[name][1]:>10.4f}{value:>10.4f}")


def _changed(items: Sequence[Question], gained: Mapping, lost: Mapping) -> list[Question]:
    return [item for item in items if gained[item.question_id] and not lost[item.question_id]]


def _print_changes(items: Sequence[Question], shipped: Mapping, refit: Mapping) -> None:
    won = _changed(items, refit, shipped)
    lost = _changed(items, shipped, refit)
    print(f"\nwon {len(won)}, lost {len(lost)}")
    for item in lost:
        print(f"  lost: {item.question_id} ({item.question_type})")


def _print_constants(weights: Sequence[float]) -> None:
    print("\nconstants for scripts/lane_score.py:")
    for name, value in zip(FEATURE_NAMES, weights):
        print(f"{name} = {value:.4f}")


def report(paths: Sequence[Path], depth: int) -> int:
    items = questions(paths)
    if not items:
        print("no question in these files carries a lane matrix with labelled evidence", file=sys.stderr)
        return 1
    shipped = shipped_verdicts(items, depth)
    refit = cross_validated(items, depth)
    print(f"{len(items)} questions with labelled evidence, reader depth {depth}\n")
    _print_table(items, shipped, refit)
    _print_changes(items, shipped, refit)
    _print_constants(fit(items))
    print("\nRead the per-type rows before pasting: an overall gain can hide a type that lost.")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("results", nargs="+", type=Path, help="stand result files (JSONL or JSON array)")
    parser.add_argument("--depth", type=int, default=READER_DEPTH, help="how many candidates the reader gets")
    args = parser.parse_args(argv)
    return report(args.results, args.depth)


if __name__ == "__main__":
    raise SystemExit(main())
