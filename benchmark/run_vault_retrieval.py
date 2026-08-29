"""Does this vault's retrieval beat plain search over its own files?

The critics of the agent-memory leaderboard say the same thing to every
practitioner: baseline `grep` over your own data, require a candidate to exceed
it by a clear margin, and treat a private benchmark over real use as the real
gate. This is that stand for this vault.

The questions are Russian, the pages are English, and the gold page is fixed by
construction — so scoring needs no judge, and the cross-language case the owner
insisted on is the default rather than a special mode.

What it measures is retrieval: whether the page that answers the question is in
the top-k. What it does not measure is *use* — whether an agent then acts on it —
which is the gap the MemoryArena work names and which this stand does not close.

The question it asks the product is asked through a real entry point — by
default the MCP tool's own wrapper, budget and all — because a stand that calls
`search()` directly measures a shape no caller uses, and for four separately
confirmed defects that is exactly why it saw nothing. See
`benchmark/retrieval_paths.py`.

    uv run python benchmark/run_vault_retrieval.py
    uv run python benchmark/run_vault_retrieval.py --json
    uv run python benchmark/run_vault_retrieval.py --path cli
    uv run python benchmark/run_vault_retrieval.py --repeat 3   # show the spread
"""
from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
if str(ROOT / "benchmark") not in sys.path:
    sys.path.insert(0, str(ROOT / "benchmark"))

import answer_key  # noqa: E402
from reliable_memory import validate_schema  # noqa: E402
from retrieval_paths import (  # noqa: E402
    DEFAULT_PATH,
    PATHS,
    Observation,
    observe,
    warm,
)

CORPUS = ROOT / "benchmark/vault-retrieval-v1.json"
SCHEMA = ROOT / "benchmark/vault-retrieval-v1.schema.json"
GREP_ROOTS = ("knowledge/notes", "knowledge/daily", "docs")
MAX_GREP_FILE_BYTES = 512 * 1024
MIN_TERM_LENGTH = 4
TOP_K = 5
_WORD = re.compile(r"[\w-]+", re.UNICODE)


@dataclass(frozen=True)
class CaseResult:
    case_id: str
    gold_path: str
    product_rank: int | None
    grep_rank: int | None
    # Why the answer came out that way. A rank that moved between runs is a
    # number; `signals_used` without `dense`, or a `fallback_reason`, is the
    # reason for it, and the reason is what a reader can act on.
    effective_mode: str | None = None
    signals_used: list[str] = field(default_factory=list)
    fallback_reason: str | None = None
    seconds: float = 0.0


def load_corpus(path: Path) -> dict:
    corpus = json.loads(path.read_text(encoding="utf-8"))
    validate_schema(corpus, SCHEMA)
    return corpus


def _terms(question: str) -> list[str]:
    words = [word.casefold() for word in _WORD.findall(question)]
    return [word for word in words if len(word) >= MIN_TERM_LENGTH]


def _readable(path: Path) -> str:
    try:
        if path.stat().st_size > MAX_GREP_FILE_BYTES:
            return ""
        return path.read_text(encoding="utf-8", errors="ignore").casefold()
    except OSError:
        return ""


def _grep_files(vault: Path) -> list[Path]:
    files: list[Path] = []
    for relative in GREP_ROOTS:
        root = vault / relative
        if root.is_dir():
            files.extend(sorted(path for path in root.rglob("*.md") if path.is_file()))
    return files


def _score(text: str, terms: list[str]) -> tuple[int, int]:
    """(distinct terms present, total occurrences) — the usual grep ranking."""
    present = [term for term in terms if term in text]
    return len(present), sum(text.count(term) for term in present)


def grep_ranking(
    vault: Path,
    question: str,
    limit: int = TOP_K,
    dropped: frozenset[str] | None = None,
) -> list[str]:
    """What a person gets from searching their own files for the question's words.

    The baseline drops the same sheets the product does. Measured on this vault
    it changes nothing — `GREP_ROOTS` holds no sheet, because they are JSON
    under `benchmark/` and Python under `tests/` while this reads `.md` under
    three other roots. It is here so the two sides stay comparable if a sheet
    ever moves, rather than because it is doing work today.
    """
    drop = dropped if dropped is not None else dropped_paths(vault)
    terms = _terms(question)
    scored = _grep_scores(vault, terms)
    return [item[2] for item in sorted(scored) if item[2] not in drop][:limit]


def _grep_scores(vault: Path, terms: list[str]) -> list[tuple[int, int, str]]:
    scored = []
    for path in _grep_files(vault):
        distinct, total = _score(_readable(path), terms)
        scored.append((-distinct, -total, path.relative_to(vault).as_posix()))
    return [item for item in scored if item[0]]


# The question sheet lives in the vault like everything else and `benchmark` is
# an approved corpus root, so a question that appears in it verbatim retrieves
# it. That is the measurement looking at itself, not the product working.
#
# This used to be one hardcoded path, and it was wrong twice over: it missed
# `tests/test_intent_conditional_trust.py`, which pins one case's question next
# to that case's gold page, and `run_vault_application` inherited it and so
# dropped this stand's sheet while leaving its own in. The set is now derived
# from what the files on disk actually say — see `benchmark/answer_key.py` —
# and both stands drop the same one.
def dropped_paths(vault: Path = ROOT) -> frozenset[str]:
    return answer_key.sheets(vault)


def product_observation(
    question: str,
    limit: int = TOP_K,
    path: str = DEFAULT_PATH,
    dropped: frozenset[str] | None = None,
) -> Observation:
    """One retrieval through a real entry point, kept whole — ranks and reasons.

    The request is deepened by exactly the number of sheets that could be
    dropped, so removing them leaves `limit` real rows rather than a short
    list. That deepening is not free — `_candidate_pool` grows with the
    requested limit — and `run_stand_contamination.py` reports the
    uncompensated number beside this one so the compensation cannot quietly
    become the score.
    """
    drop = dropped if dropped is not None else dropped_paths()
    seen = observe(path, question, limit + len(drop))
    kept = [found for found in seen.result_paths if found not in drop][:limit]
    return Observation(
        path=seen.path,
        result_paths=kept,
        trace=seen.trace,
        seconds=seen.seconds,
        error=seen.error,
    )


def product_ranking(
    question: str,
    limit: int = TOP_K,
    path: str = DEFAULT_PATH,
    dropped: frozenset[str] | None = None,
) -> list[str]:
    return product_observation(question, limit, path, dropped).result_paths


def _rank_of(gold: str, paths: list[str]) -> int | None:
    for index, path in enumerate(paths, start=1):
        if path == gold:
            return index
    return None


def score_case(
    case: dict, vault: Path, limit: int = TOP_K, path: str = DEFAULT_PATH
) -> CaseResult:
    gold = str(case["gold_path"])
    question = str(case["question"])
    # One set for both sides, taken from the vault under measurement, so a
    # `--vault` elsewhere cannot leave the product and the baseline disagreeing.
    dropped = dropped_paths(vault)
    seen = product_observation(question, limit, path, dropped)
    return CaseResult(
        case_id=str(case["case_id"]),
        gold_path=gold,
        product_rank=_rank_of(gold, seen.result_paths),
        grep_rank=_rank_of(gold, grep_ranking(vault, question, limit, dropped)),
        effective_mode=str(seen.trace.get("effective_mode") or "") or None,
        signals_used=seen.signals,
        fallback_reason=seen.fallback_reason,
        seconds=seen.seconds,
    )


def _hit_rate(ranks: list[int | None], depth: int) -> float:
    if not ranks:
        return 0.0
    hits = sum(1 for rank in ranks if rank is not None and rank <= depth)
    return round(hits / len(ranks), 4)


def measure(results: list[CaseResult]) -> dict[str, float]:
    product = [item.product_rank for item in results]
    grep = [item.grep_rank for item in results]
    return {
        "case_count": len(results),
        "product_hit_at_1": _hit_rate(product, 1),
        "product_hit_at_5": _hit_rate(product, 5),
        "grep_hit_at_1": _hit_rate(grep, 1),
        "grep_hit_at_5": _hit_rate(grep, 5),
        "gain_over_grep_at_5": round(_hit_rate(product, 5) - _hit_rate(grep, 5), 4),
    }


def evaluate(metrics: dict[str, float], thresholds: dict[str, float]) -> dict[str, object]:
    checks = {
        "hit_at_5": metrics["product_hit_at_5"] >= thresholds["min_hit_at_5"],
        "gain_over_grep_at_5": (
            metrics["gain_over_grep_at_5"] >= thresholds["min_gain_over_grep_at_5"]
        ),
    }
    return {"metric_results": checks, "passed": all(checks.values())}


def _misses(results: list[CaseResult]) -> list[dict[str, object]]:
    return [
        {
            "case_id": item.case_id,
            "gold_path": item.gold_path,
            "product_rank": item.product_rank,
            "grep_rank": item.grep_rank,
            "effective_mode": item.effective_mode,
            "signals_used": item.signals_used,
            "fallback_reason": item.fallback_reason,
        }
        for item in results
        if item.product_rank is None or item.product_rank > 1
    ]


# What the product says when the clock, rather than the ranking, decided.
_BUDGET_REASONS = (
    "optional_stage_timeout",
    "retrieval_deadline_exceeded",
    "TimeoutError",
)


def _lost_its_budget(result: CaseResult) -> bool:
    reason = result.fallback_reason or ""
    return any(mark in reason for mark in _BUDGET_REASONS)


def _budget_degraded(results: list[CaseResult]) -> list[str]:
    """Cases the budget answered instead of the ranking.

    A run where this list is long is a statement about the machine, not about
    retrieval quality, and a reader comparing two such runs is comparing load.
    """
    return [item.case_id for item in results if _lost_its_budget(item)]


def _one_round(corpus: dict, vault: Path, path: str) -> list[CaseResult]:
    return [score_case(case, vault, TOP_K, path) for case in corpus["cases"]]


def _median_metrics(per_run: list[dict[str, float]]) -> dict[str, float]:
    """The middle run, key by key. With a single run that is the run itself."""
    return {
        key: round(statistics.median([run[key] for run in per_run]), 4)
        for key in per_run[0]
    }


def _spread(per_run: list[dict[str, float]]) -> dict[str, dict[str, float]]:
    """How far each number moved across runs of identical code.

    A paired difference smaller than this spread means nothing, and the point
    of printing it is that a reader does not have to take that on trust.
    """
    return {
        key: {
            "min": min(run[key] for run in per_run),
            "max": max(run[key] for run in per_run),
        }
        for key in per_run[0]
        if key != "case_count"
    }


def _hit(result: CaseResult, depth: int) -> bool:
    return result.product_rank is not None and result.product_rank <= depth


def _unstable_cases(
    rounds: list[list[CaseResult]], depth: int = TOP_K
) -> list[dict[str, object]]:
    """The cases that did not answer the same way twice, and what they blamed."""
    unstable: list[dict[str, object]] = []
    for attempts in zip(*rounds):
        outcomes = {_hit(item, depth) for item in attempts}
        if len(outcomes) > 1:
            unstable.append(_wobble(attempts))
    return unstable


def _wobble(attempts: tuple[CaseResult, ...]) -> dict[str, object]:
    return {
        "case_id": attempts[0].case_id,
        "ranks": [item.product_rank for item in attempts],
        "signals_used": [item.signals_used for item in attempts],
        "fallback_reasons": [item.fallback_reason for item in attempts],
    }


def _warmed(path: str, warmup: bool) -> float:
    if not warmup:
        return 0.0
    return warm(path)


def run(
    corpus: dict,
    vault: Path,
    *,
    path: str = DEFAULT_PATH,
    repeat: int = 1,
    warmup: bool = True,
) -> dict[str, object]:
    warmup_seconds = _warmed(path, warmup)
    rounds = [_one_round(corpus, vault, path) for _ in range(repeat)]
    per_run = [measure(results) for results in rounds]
    metrics = _median_metrics(per_run)
    return {
        "corpus_id": corpus["corpus_id"],
        "retrieval_path": path,
        "runs": repeat,
        "warmup_seconds": warmup_seconds,
        "metrics": metrics,
        "per_run_metrics": per_run,
        "spread": _spread(per_run),
        "unstable_cases": _unstable_cases(rounds),
        "gates": evaluate(metrics, corpus["thresholds"]),
        "misses": _misses(rounds[0]),
        "budget_degraded_cases": _budget_degraded(rounds[0]),
        "thresholds": corpus["thresholds"],
    }


def _print_report(report: dict[str, object]) -> None:
    metrics = report["metrics"]
    print(f"retrieval_path: {report['retrieval_path']}")
    print(f"runs: {report['runs']}")
    for name in (
        "case_count",
        "product_hit_at_1",
        "product_hit_at_5",
        "grep_hit_at_1",
        "grep_hit_at_5",
        "gain_over_grep_at_5",
    ):
        print(f"{name}: {metrics[name]}")
    _print_spread(report)
    print(f"budget_degraded_cases: {report['budget_degraded_cases']}")
    print(f"gates passed: {report['gates']['passed']}")


def _print_spread(report: dict[str, object]) -> None:
    """With one run there is no spread to print, and no claim of stability."""
    if int(report["runs"]) < 2:  # type: ignore[call-overload]
        return
    spread = report["spread"]
    print(f"product_hit_at_5 across runs: {spread['product_hit_at_5']}")  # type: ignore[index]
    print(f"unstable cases: {[item['case_id'] for item in report['unstable_cases']]}")  # type: ignore[union-attr]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", type=Path, default=CORPUS)
    parser.add_argument("--vault", type=Path, default=ROOT)
    parser.add_argument("--json", action="store_true")
    parser.add_argument(
        "--path",
        choices=PATHS,
        default=DEFAULT_PATH,
        help="Which real entry point to measure (default: the MCP tool's)",
    )
    parser.add_argument(
        "--repeat",
        type=int,
        default=1,
        help="Run the corpus this many times and report the spread",
    )
    parser.add_argument(
        "--no-warm",
        dest="warmup",
        action="store_false",
        help="Skip the warmup call that makes the process resemble a resident server",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    report = run(
        load_corpus(args.corpus),
        args.vault,
        path=args.path,
        repeat=args.repeat,
        warmup=args.warmup,
    )
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        _print_report(report)
    return 0 if report["gates"]["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
