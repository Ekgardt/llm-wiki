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
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "benchmark"))

from flush_classification_metrics import CaseOutcome as CaseOutcome  # noqa: E402
from flush_classification_metrics import _abstaining_cases as _abstaining_cases  # noqa: E402
from flush_classification_metrics import _durable_cases as _durable_cases  # noqa: E402
from flush_classification_metrics import _false_promotion_rate as _false_promotion_rate  # noqa: E402
from flush_classification_metrics import _rate as _rate  # noqa: E402
from flush_classification_metrics import confirmed_cases as confirmed_cases  # noqa: E402
from flush_classification_metrics import evaluate as evaluate  # noqa: E402
from flush_classification_metrics import is_confirmed as is_confirmed  # noqa: E402
from flush_classification_metrics import label_state as label_state  # noqa: E402
from flush_classification_metrics import measure as measure  # noqa: E402
from flush_classification_metrics import score_case as score_case  # noqa: E402
from llm_wiki.classification_protocol import (  # noqa: E402
    CLASSIFICATION_SYSTEM_PROMPT,
    build_classification_prompt,
)
from llm_wiki.classification_protocol import (  # noqa: E402
    classify_response as classify_response,
)
from reliable_memory import validate_schema  # noqa: E402

CORPUS = ROOT / "benchmark/flush-classification-v1.json"
SCHEMAS = {
    "flush-classification/v1": ROOT / "benchmark/flush-classification-v1.schema.json",
    "flush-classification/v3": ROOT / "benchmark/flush-classification-v3.schema.json",
}
MAX_TOKENS = 1500


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


def run(corpus: dict, adapter: Callable[[dict], str]) -> dict[str, object]:
    outcomes = [score_case(case, adapter(case)) for case in confirmed_cases(corpus)]
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
        print(
            f"labels: {labels['confirmed_count']} confirmed, "
            f"{labels['contested_count']} contested and left out"
        )
    return 0 if report["gates"]["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
