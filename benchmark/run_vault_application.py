"""Does what memory returns let you act — or only recall the page?

The retrieval stand asks whether the right page is in the top five. This one
asks the harder question the 2026 surveys keep repeating: `remembering ≠
retrieving text ≠ using experience`. Each case names something an operator has
to *do*, and the answer counts only if the exact token needed to do it — a flag,
an interval, a bound — actually reached the reader inside the retrieved text.

Baseline is the same as the other stand: `grep` over the vault's own files,
because a memory system that cannot beat that is not earning its cost. Tokens
are verified to appear verbatim in the gold page, so a correct retrieval can
always pass.

What this does not measure: whether an agent then acts on what it read. That is
a further step and this stand does not claim it.

Like the retrieval stand, it asks the product through a real entry point —
by default the MCP tool's own wrapper, budget and all. See
`benchmark/retrieval_paths.py` for why that matters.

    uv run python benchmark/run_vault_application.py
    uv run python benchmark/run_vault_application.py --json
    uv run python benchmark/run_vault_application.py --path cli
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
if str(ROOT / "benchmark") not in sys.path:
    sys.path.insert(0, str(ROOT / "benchmark"))

from reliable_memory import validate_schema  # noqa: E402
from retrieval_paths import DEFAULT_PATH, PATHS, warm  # noqa: E402
from run_vault_retrieval import (  # noqa: E402
    TOP_K,
    _readable,
    grep_ranking,
    product_ranking,
)

CORPUS = ROOT / "benchmark/vault-application-v1.json"
SCHEMA = ROOT / "benchmark/vault-application-v1.schema.json"


@dataclass(frozen=True)
class CaseResult:
    case_id: str
    product_applied: bool
    grep_applied: bool


def load_corpus(path: Path = CORPUS) -> dict:
    corpus = json.loads(path.read_text(encoding="utf-8"))
    validate_schema(corpus, SCHEMA)
    return corpus


# The task sheet lives in the vault and carries every expected token, so a
# retrieval that finds the sheet would pass every case by reading the answers.
# That is the measurement looking at itself, so this one path never counts.
_SELF = "benchmark/vault-application-v1.json"


def _text_of(vault: Path, paths: list[str]) -> str:
    return "\n".join(_readable(vault / path) for path in paths if path != _SELF)


def applied(text: str, tokens: list[str]) -> bool:
    """True when every token an operator needs is present, verbatim."""
    return all(token.casefold() in text for token in tokens)


def score_case(
    case: dict, vault: Path, limit: int = TOP_K, path: str = DEFAULT_PATH
) -> CaseResult:
    tokens = [str(token) for token in case["expected_tokens"]]
    task = str(case["task"])
    found = product_ranking(task, limit, path)
    return CaseResult(
        case_id=str(case["case_id"]),
        product_applied=applied(_text_of(vault, found), tokens),
        grep_applied=applied(_text_of(vault, grep_ranking(vault, task, limit)), tokens),
    )


def _rate(flags: list[bool]) -> float:
    if not flags:
        return 0.0
    return round(sum(1 for flag in flags if flag) / len(flags), 4)


def measure(results: list[CaseResult]) -> dict[str, float]:
    product = _rate([item.product_applied for item in results])
    grep = _rate([item.grep_applied for item in results])
    return {
        "case_count": len(results),
        "product_applied_at_5": product,
        "grep_applied_at_5": grep,
        "gain_over_grep_at_5": round(product - grep, 4),
    }


def evaluate(metrics: dict[str, float], thresholds: dict[str, float]) -> dict[str, object]:
    checks = {
        "applied_at_5": metrics["product_applied_at_5"] >= thresholds["min_applied_at_5"],
        "gain_over_grep_at_5": (
            metrics["gain_over_grep_at_5"] >= thresholds["min_gain_over_grep_at_5"]
        ),
    }
    return {"metric_results": checks, "passed": all(checks.values())}


def _misses(results: list[CaseResult]) -> list[dict[str, object]]:
    return [
        {
            "case_id": item.case_id,
            "product_applied": item.product_applied,
            "grep_applied": item.grep_applied,
        }
        for item in results
        if not item.product_applied
    ]


def run(
    corpus: dict, vault: Path, *, path: str = DEFAULT_PATH, warmup: bool = True
) -> dict[str, object]:
    warmup_seconds = _warmed(path, warmup)
    results = [score_case(case, vault, TOP_K, path) for case in corpus["cases"]]
    metrics = measure(results)
    return {
        "corpus_id": corpus["corpus_id"],
        "retrieval_path": path,
        "warmup_seconds": warmup_seconds,
        "metrics": metrics,
        "gates": evaluate(metrics, corpus["thresholds"]),
        "misses": _misses(results),
    }


def _warmed(path: str, warmup: bool) -> float:
    if not warmup:
        return 0.0
    return warm(path)


def _print_report(report: dict[str, object]) -> None:
    metrics = report["metrics"]
    assert isinstance(metrics, dict)
    print(f"retrieval_path: {report['retrieval_path']}")
    for name, value in metrics.items():
        print(f"{name}: {value}")
    gates = report["gates"]
    assert isinstance(gates, dict)
    print(f"gates passed: {gates['passed']}")
    for miss in report["misses"]:  # type: ignore[union-attr]
        print(f"  missed: {miss['case_id']} (grep_applied={miss['grep_applied']})")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="Print the raw report")
    parser.add_argument(
        "--path",
        choices=PATHS,
        default=DEFAULT_PATH,
        help="Which real entry point to measure (default: the MCP tool's)",
    )
    parser.add_argument(
        "--no-warm",
        dest="warmup",
        action="store_false",
        help="Skip the warmup call that makes the process resemble a resident server",
    )
    arguments = parser.parse_args(argv)
    report = run(load_corpus(), ROOT, path=arguments.path, warmup=arguments.warmup)
    if arguments.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        _print_report(report)
    gates = report["gates"]
    assert isinstance(gates, dict)
    return 0 if gates["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
