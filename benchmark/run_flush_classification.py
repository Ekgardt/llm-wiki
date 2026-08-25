"""Measure what session classification keeps and what it drops.

The audit asked how often a session that held a decision, a fix, or a gotcha
ends up without it in durable memory. That number was unknown: the only tests
covered response parsing. This runner scores the whole classification step —
the product's own prompt, one provider, the product's parser — against a
labelled corpus, and gates on three numbers.

The corpus shipped here is a small public one. A real answer needs real
sessions, which live only in an installed vault; point `--corpus` at one.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from flush_memory import (  # noqa: E402
    CLASSIFICATION_SYSTEM_PROMPT,
    _classify_response,
    build_classification_prompt,
)
from reliable_memory import validate_schema  # noqa: E402

CORPUS = ROOT / "benchmark/flush-classification-v1.json"
SCHEMAS = {
    "flush-classification/v1": ROOT / "benchmark/flush-classification-v1.schema.json",
    "flush-classification/v2": ROOT / "benchmark/flush-classification-v2.schema.json",
}
MAX_TOKENS = 1500


@dataclass(frozen=True)
class CaseOutcome:
    case_id: str
    expected_tier: str
    observed_tier: str
    tier_matched: bool
    missing_markers: tuple[str, ...]
    falsely_promoted: bool


def _canned_adapter(case: dict) -> str:
    return str(case["canned_response"])


def _provider_adapter(case: dict) -> str:
    from llm_client import call_llm

    prompt = build_classification_prompt(str(case["transcript"]), str(case["event"]))
    return call_llm(prompt, CLASSIFICATION_SYSTEM_PROMPT, max_tokens=MAX_TOKENS) or ""


ADAPTERS: dict[str, Callable[[dict], str]] = {
    "canned": _canned_adapter,
    "provider": _provider_adapter,
}


def load_corpus(path: Path) -> dict:
    corpus = json.loads(path.read_text(encoding="utf-8"))
    schema = SCHEMAS.get(str(corpus.get("schema_version")))
    if schema is None:
        raise ValueError("unknown corpus schema version")
    validate_schema(corpus, schema)
    return corpus


def label_state(corpus: dict) -> dict[str, object]:
    """How much of this corpus a human has actually confirmed.

    A v1 corpus is hand-written and counts as reviewed. A corpus built from real
    sessions carries model-produced labels until somebody says otherwise, and a
    number measured against those is provisional — current practice calibrates a
    judge against human labels rather than substituting one for the other.
    """
    cases = corpus["cases"]
    reviewed = sum(1 for case in cases if case.get("label_reviewed", True))
    return {
        "case_count": len(cases),
        "reviewed_count": reviewed,
        "provisional": reviewed < len(cases),
    }


def score_case(case: dict, response: str) -> CaseOutcome:
    """Compare one classification against its label."""
    tier, body = _classify_response(response)
    expected = str(case["expected_tier"])
    missing = tuple(
        marker for marker in case["required_markers"] if marker not in body
    )
    return CaseOutcome(
        case_id=str(case["case_id"]),
        expected_tier=expected,
        observed_tier=tier,
        tier_matched=tier == expected,
        missing_markers=missing,
        falsely_promoted=expected == "ok" and tier != "ok",
    )


def _rate(numerator: int, denominator: int) -> float:
    if denominator == 0:
        return 1.0
    return round(numerator / denominator, 6)


def _durable_cases(outcomes: list[CaseOutcome]) -> list[CaseOutcome]:
    return [item for item in outcomes if item.expected_tier != "ok"]


def _abstaining_cases(outcomes: list[CaseOutcome]) -> list[CaseOutcome]:
    return [item for item in outcomes if item.expected_tier == "ok"]


def _false_promotion_rate(outcomes: list[CaseOutcome]) -> float:
    abstaining = _abstaining_cases(outcomes)
    if not abstaining:
        return 0.0
    promoted = [item for item in abstaining if item.falsely_promoted]
    return _rate(len(promoted), len(abstaining))


def measure(outcomes: list[CaseOutcome]) -> dict[str, float]:
    durable = _durable_cases(outcomes)
    kept = [item for item in durable if not item.missing_markers]
    matched = [item for item in outcomes if item.tier_matched]
    return {
        "case_count": len(outcomes),
        "durable_case_count": len(durable),
        "tier_accuracy": _rate(len(matched), len(outcomes)),
        "durable_content_recall": _rate(len(kept), len(durable)),
        "false_promotion_rate": _false_promotion_rate(outcomes),
    }


def evaluate(metrics: dict[str, float], thresholds: dict[str, float]) -> dict[str, object]:
    results = {
        "tier_accuracy": metrics["tier_accuracy"] >= thresholds["tier_accuracy"],
        "durable_content_recall": (
            metrics["durable_content_recall"] >= thresholds["durable_content_recall"]
        ),
        "false_promotion_rate": (
            metrics["false_promotion_rate"] <= thresholds["false_promotion_rate"]
        ),
    }
    return {"metric_results": results, "passed": all(results.values())}


def run(corpus: dict, adapter: Callable[[dict], str]) -> dict[str, object]:
    outcomes = [score_case(case, adapter(case)) for case in corpus["cases"]]
    metrics = measure(outcomes)
    return {
        "corpus_id": corpus["corpus_id"],
        "labels": label_state(corpus),
        "gates": evaluate(metrics, corpus["thresholds"]),
        "metrics": metrics,
        "misses": [
            {
                "case_id": item.case_id,
                "expected_tier": item.expected_tier,
                "observed_tier": item.observed_tier,
                "missing_markers": list(item.missing_markers),
            }
            for item in outcomes
            if not item.tier_matched or item.missing_markers
        ],
        "thresholds": corpus["thresholds"],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", type=Path, default=CORPUS)
    parser.add_argument("--adapter", choices=sorted(ADAPTERS), default="canned")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    report = run(load_corpus(args.corpus), ADAPTERS[args.adapter])
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        metrics = report["metrics"]
        print(f"cases: {metrics['case_count']}")
        for name in ("tier_accuracy", "durable_content_recall", "false_promotion_rate"):
            print(f"{name}: {metrics[name]}")
        print(f"gates passed: {report['gates']['passed']}")
        labels = report["labels"]
        if labels["provisional"]:
            print(
                f"labels: {labels['reviewed_count']}/{labels['case_count']} reviewed "
                "— these numbers are provisional"
            )
    return 0 if report["gates"]["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
