"""Set the refusal gates' thresholds on a recorded run, by Neyman–Pearson.

Offline only: reads a run's results and its staging files, never a provider or
a vault. For every answerable question it builds two readings of the same
question — the original, whose labelled evidence turns are in the window, and
its twin, whose evidence sessions are removed — and measures
`evidence_sufficiency` on both. Then, on the tune half only, it takes the
threshold with the most coverage of originals while firing on at most `alpha`
of the twins, reports d′ (Z(hit) − Z(false alarm), and √2·Z(AUC)), and reports
the decide half without touching it.

The stand-in, stated plainly: the recorded rows of 2026-09-18 do not carry the
twelve candidates' text, so the distractors beside the evidence are the
haystack turns that share the most question terms — a lexical retrieval in
place of the product's. A run with `--twins` replaces the stand-in with the
reader's real window.

    uv run python benchmark/calibrate_refusal_gates.py \\
        --results cache/benchmarks/full-2026-09-18/lme500.judged.jsonl \\
        --staging cache/benchmarks/full-2026-09-18/staging-full

See `docs/research/2026-09-22-quote-then-answer-and-a-refusal-calibrated-on-twins.md`.
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import namedtuple
from datetime import date
from pathlib import Path

BENCHMARK_DIR = Path(__file__).resolve().parent
SCRIPTS_DIR = BENCHMARK_DIR.parent / "scripts"
for folder in (BENCHMARK_DIR, SCRIPTS_DIR):
    if str(folder) not in sys.path:
        sys.path.insert(0, str(folder))

import evidence_sufficiency  # noqa: E402
import longmemeval_data  # noqa: E402
import longmemeval_score  # noqa: E402

Span = namedtuple("Span", "text relative_path")
Turn = namedtuple("Turn", "session day evidence text")
# What one reading measured: the module's own sufficiency of the window, and
# the nearest shown day's distance from the question's window (None without one).
Measure = namedtuple("Measure", "score nearest_day windowed")
DEFAULT_DEPTH = 12
DATE_CANDIDATES = range(0, 15)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results", required=True, help="the run's results or judged JSONL")
    parser.add_argument("--staging", required=True, help="the run's staging directory")
    parser.add_argument("--alpha", type=float, default=evidence_sufficiency.FALSE_FIRE_RATE)
    parser.add_argument("--depth", type=int, default=DEFAULT_DEPTH, help="spans in the window")
    parser.add_argument("--json", default=None, help="write the table here as JSON")
    return parser.parse_args()


def _rows(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _question(staging: Path, question_id: str) -> dict:
    """The staged question, its LoCoMo references written as labels where it has them."""
    staged = json.loads((staging / f"{question_id}.question.json").read_text(encoding="utf-8"))
    return longmemeval_data.labelled_by_evidence(staged)


def _day(date_text: str) -> date:
    return date.fromisoformat(str(date_text).split()[0].replace("/", "-"))


def _turns(question: dict) -> list[Turn]:
    zipped = zip(question["haystack_session_ids"], question["haystack_dates"], question["haystack_sessions"])
    return [
        Turn(str(session_id), _day(date_text), bool(turn.get("has_answer")), str(turn.get("content", "")))
        for session_id, date_text, turns in zipped
        for turn in turns
    ]


def _overlap(question_terms: frozenset[str], turn: Turn) -> int:
    return len(question_terms & evidence_sufficiency.terms_of(turn.text))


def _proxy_top(question_terms: frozenset[str], turns: list[Turn], depth: int) -> list[Turn]:
    """The stand-in for retrieval: the turns sharing the most question terms."""
    ranked = sorted(turns, key=lambda turn: -_overlap(question_terms, turn))
    return ranked[:depth]


def _spans(turns: list[Turn]) -> list[Span]:
    return [Span(turn.text, f"knowledge/daily/{turn.day.isoformat()}.md") for turn in turns]


def _nearest_day(window: tuple[date, date] | None, turns: list[Turn]) -> int | None:
    if window is None:
        return None
    return evidence_sufficiency.days_from_window(window, [turn.day for turn in turns])


def _measure(question: dict, turns: list[Turn]) -> Measure:
    asked_on = _day(question["question_date"])
    measured = evidence_sufficiency.sufficiency(question["question"], _spans(turns), asked_on)
    window = evidence_sufficiency.question_window(question["question"], asked_on)
    return Measure(measured.score, _nearest_day(window, turns), window is not None)


Reading = namedtuple("Reading", "row original twin")


def _readings_of(row: dict, question: dict, depth: int) -> Reading:
    """The original's window (evidence plus stand-in distractors) and the twin's."""
    turns = _turns(question)
    removed = longmemeval_data.evidence_sessions_of(question)
    question_terms = evidence_sufficiency.terms_of(question["question"])
    evidence = [turn for turn in turns if turn.evidence]
    rest = [turn for turn in turns if turn.session not in removed]
    original = evidence + _proxy_top(question_terms, rest, max(0, depth - len(evidence)))
    twin = _proxy_top(question_terms, rest, depth)
    return Reading(row, _measure(question, original), _measure(question, twin))


def _silent_reading(row: dict, question: dict, depth: int) -> Reading:
    """An abstention row: the window is the stand-in over the whole haystack."""
    turns = _turns(question)
    shown = _proxy_top(evidence_sufficiency.terms_of(question["question"]), turns, depth)
    return Reading(row, None, _measure(question, shown))


def readings(rows: list[dict], staging: Path, depth: int) -> list[Reading]:
    out = []
    for row in rows:
        question = _question(staging, str(row["question_id"]))
        if longmemeval_data.expects_silence(question):
            out.append(_silent_reading(row, question, depth))
            continue
        out.append(_readings_of(row, question, depth))
    return out


def auc(positives: list[float], negatives: list[float]) -> float | None:
    """The share of original/twin pairs the signal orders the right way; ties count half."""
    if not positives or not negatives:
        return None
    wins = sum((p > n) + 0.5 * (p == n) for p in positives for n in negatives)
    return wins / (len(positives) * len(negatives))


def _z(share: float) -> float:
    clipped = min(max(share, 0.005), 0.995)
    return statistics.NormalDist().inv_cdf(clipped)


def dprime_from_auc(area: float | None) -> float | None:
    """d′_a = √2 · Z(AUC), the sensitivity index of the whole curve."""
    if area is None:
        return None
    return round(2 ** 0.5 * _z(area), 3)


def dprime_at(hit: float, false_alarm: float) -> float:
    """d′ = Z(hit rate) − Z(false-alarm rate) at one threshold."""
    return round(_z(hit) - _z(false_alarm), 3)


def _share_at_least(values: list[float], threshold: float) -> float:
    if not values:
        return 0.0
    return sum(1 for value in values if value >= threshold) / len(values)


def threshold_at(positives: list[float], negatives: list[float], alpha: float) -> float | None:
    """The smallest threshold that fires on at most `alpha` of the negatives.

    Coverage of the positives falls as the threshold rises, so the smallest one
    that holds the false-fire rate is the one with the most coverage — the
    Neyman–Pearson choice for a one-dimensional signal.
    """
    for candidate in sorted(set(positives + negatives)):
        if _share_at_least(negatives, candidate) <= alpha:
            return candidate
    return None


def _split(items: list[Reading], half: str) -> list[Reading]:
    return [item for item in items if longmemeval_data.split_of(str(item.row["question_id"])) == half]


def _answerable(items: list[Reading]) -> list[Reading]:
    return [item for item in items if item.original is not None]


def _scores(measures: list[Measure]) -> list[float]:
    return [measure.score for measure in measures if measure.score is not None]


def _distances(measures: list[Measure]) -> list[float]:
    return [float(measure.nearest_day) for measure in measures if measure.nearest_day is not None]


def _share_within(distances: list[float], days: int) -> float:
    if not distances:
        return 0.0
    return sum(1 for value in distances if value <= days) / len(distances)


def _largest_tolerance(twins: list[float], alpha: float) -> int:
    """The widest tolerance that keeps at most `alpha` of the twins' dated spans inside."""
    chosen = [days for days in DATE_CANDIDATES if _share_within(twins, days) <= alpha]
    if not chosen:
        return 0
    return max(chosen)


def _negated(values: list[float]) -> list[float]:
    return [-value for value in values]


def date_tolerance(items: list[Reading], alpha: float) -> dict:
    """The largest tolerance in days that keeps twins' dated spans inside on at most `alpha`."""
    originals = _distances([item.original for item in items])
    twins = _distances([item.twin for item in items])
    days = _largest_tolerance(twins, alpha)
    hit, false_alarm = _share_within(originals, days), _share_within(twins, days)
    return {
        "windowed_originals": len(originals),
        "windowed_twins": len(twins),
        "date_days": days,
        "originals_within": round(hit, 4),
        "twins_within": round(false_alarm, 4),
        "d_prime": dprime_at(hit, false_alarm),
        "d_prime_from_auc": dprime_from_auc(auc(_negated(originals), _negated(twins))),
    }


def _half_report(items: list[Reading], threshold: float | None) -> dict:
    originals = _scores([item.original for item in items])
    twins = _scores([item.twin for item in items])
    area = auc(originals, twins)
    hit, false_alarm = _share_at_least(originals, threshold or 0.0), _share_at_least(twins, threshold or 0.0)
    return {
        "n": len(items),
        "mean_original": round(statistics.fmean(originals), 4),
        "mean_twin": round(statistics.fmean(twins), 4),
        "auc": round(area, 4),
        "d_prime_from_auc": dprime_from_auc(area),
        "coverage_of_originals": round(hit, 4),
        "false_fire_on_twins": round(false_alarm, 4),
        "d_prime_at_threshold": dprime_at(hit, false_alarm),
    }


def sufficiency_threshold(items: list[Reading], alpha: float) -> float | None:
    """The threshold set on the tune half alone."""
    tune = _split(items, longmemeval_data.TUNE)
    return threshold_at(
        _scores([item.original for item in tune]),
        _scores([item.twin for item in tune]),
        alpha,
    )


def _refused(item: Reading) -> bool:
    return longmemeval_score.declined_to_answer(item.row)


def _fires(measure: Measure, threshold: float | None) -> bool:
    return threshold is not None and measure.score is not None and measure.score >= threshold


def recorded_refusals(items: list[Reading], threshold: float | None) -> list[dict]:
    """Each recorded refusal on an answerable question, and what the gate now does with it."""
    return [
        {
            "question_id": str(item.row["question_id"]),
            "score": item.original.score,
            "nearest_day_from_window": item.original.nearest_day,
            "reads_again": _fires(item.original, threshold),
            "evidence_reached_candidates": bool((item.row.get("coverage") or {}).get("all_turns")),
        }
        for item in items
        if _refused(item)
    ]


def silence_rows(items: list[Reading], threshold: float | None) -> dict:
    """The rows whose right answer is silence: how many the gate would leave silent."""
    silent = [item for item in items if item.original is None]
    firing = sum(1 for item in silent if _fires(item.twin, threshold))
    return {"silence_expected": len(silent), "would_read_again": firing, "still_refuse": len(silent) - firing}


def _refusal_counts(refusals: list[dict]) -> dict:
    reading = [entry for entry in refusals if entry["reads_again"]]
    return {
        "recorded_refusals": refusals,
        "refusals_recorded": len(refusals),
        "refusals_read_again": len(reading),
        "refusals_read_again_with_evidence_in_candidates": sum(
            1 for entry in reading if entry["evidence_reached_candidates"]
        ),
    }


def calibrate(items: list[Reading], alpha: float) -> dict:
    """The table: the date tolerance's evidence, the threshold from tune, both halves, the recorded rows."""
    answerable = _answerable(items)
    threshold = sufficiency_threshold(answerable, alpha)
    return {
        "alpha": alpha,
        "date_scope_days_in_code": evidence_sufficiency.DATE_SCOPE_DAYS,
        "date_tolerance_on_tune": date_tolerance(_split(answerable, longmemeval_data.TUNE), alpha),
        "date_tolerance_on_decide": date_tolerance(_split(answerable, longmemeval_data.DECIDE), alpha),
        "sufficient_to_read_again_in_code": evidence_sufficiency.SUFFICIENT_TO_READ_AGAIN,
        "sufficient_to_read_again_from_tune": threshold,
        "tune": _half_report(_split(answerable, longmemeval_data.TUNE), threshold),
        "decide": _half_report(_split(answerable, longmemeval_data.DECIDE), threshold),
        **_refusal_counts(recorded_refusals(answerable, threshold)),
        **silence_rows(items, threshold),
    }


def _print(report: dict) -> None:
    for key, value in report.items():
        if key == "recorded_refusals":
            continue
        print(f"{key}: {json.dumps(value)}")
    for entry in report["recorded_refusals"]:
        print("  refusal", json.dumps(entry))


def main() -> int:
    args = parse_args()
    rows = _rows(Path(args.results))
    report = calibrate(readings(rows, Path(args.staging), args.depth), args.alpha)
    _print(report)
    if args.json:
        Path(args.json).write_text(json.dumps(report, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
