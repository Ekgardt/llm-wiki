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

    uv run python benchmark/run_vault_retrieval.py
    uv run python benchmark/run_vault_retrieval.py --json
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from reliable_memory import validate_schema  # noqa: E402

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


def grep_ranking(vault: Path, question: str, limit: int = TOP_K) -> list[str]:
    """What a person gets from searching their own files for the question's words."""
    terms = _terms(question)
    scored = []
    for path in _grep_files(vault):
        distinct, total = _score(_readable(path), terms)
        if distinct:
            scored.append((-distinct, -total, path.relative_to(vault).as_posix()))
    return [item[2] for item in sorted(scored)[:limit]]


def product_ranking(question: str, limit: int = TOP_K) -> list[str]:
    from search_memory import search

    results = search(question, limit=limit)
    return [str(item.get("path") or item.get("relative_path") or "") for item in results]


def _rank_of(gold: str, paths: list[str]) -> int | None:
    for index, path in enumerate(paths, start=1):
        if path == gold:
            return index
    return None


def score_case(case: dict, vault: Path, limit: int = TOP_K) -> CaseResult:
    gold = str(case["gold_path"])
    question = str(case["question"])
    return CaseResult(
        case_id=str(case["case_id"]),
        gold_path=gold,
        product_rank=_rank_of(gold, product_ranking(question, limit)),
        grep_rank=_rank_of(gold, grep_ranking(vault, question, limit)),
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


def run(corpus: dict, vault: Path) -> dict[str, object]:
    results = [score_case(case, vault) for case in corpus["cases"]]
    metrics = measure(results)
    return {
        "corpus_id": corpus["corpus_id"],
        "metrics": metrics,
        "gates": evaluate(metrics, corpus["thresholds"]),
        "misses": [
            {
                "case_id": item.case_id,
                "gold_path": item.gold_path,
                "product_rank": item.product_rank,
                "grep_rank": item.grep_rank,
            }
            for item in results
            if item.product_rank is None or item.product_rank > 1
        ],
        "thresholds": corpus["thresholds"],
    }


def _print_report(report: dict[str, object]) -> None:
    metrics = report["metrics"]
    for name in (
        "case_count",
        "product_hit_at_1",
        "product_hit_at_5",
        "grep_hit_at_1",
        "grep_hit_at_5",
        "gain_over_grep_at_5",
    ):
        print(f"{name}: {metrics[name]}")
    print(f"gates passed: {report['gates']['passed']}")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", type=Path, default=CORPUS)
    parser.add_argument("--vault", type=Path, default=ROOT)
    parser.add_argument("--json", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    report = run(load_corpus(args.corpus), args.vault)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        _print_report(report)
    return 0 if report["gates"]["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
