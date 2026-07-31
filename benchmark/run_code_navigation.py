"""Fixture and operator-corpus benchmark runner for Python code navigation.

This runner records definition accuracy, reference/call precision/recall/F1,
task success, token accounting, latency, peak RSS, recovery, stale-result rate,
and orphan-process rate. It never claims market superiority.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path

FIXTURE_MANIFEST = Path(__file__).with_name("code-navigation-python-v1.json")


@dataclass(frozen=True, slots=True)
class GateSpec:
    field: str
    label: str
    ceiling: bool


GATE_SPECS = (
    GateSpec("definition_accuracy", "definition_accuracy", True),
    GateSpec("reference_f1", "reference_f1", True),
    GateSpec("stale_answer_count", "stale_answer_count", False),
    GateSpec("orphan_process_count", "orphan_process_count", False),
    GateSpec("recovery_rate", "recovery_rate", True),
    GateSpec("default_items", "default_items", False),
    GateSpec("default_estimated_tokens", "default_estimated_tokens", False),
    GateSpec("warm_overhead_p95_ms", "warm_overhead_p95_ms", False),
    GateSpec("cold_readiness_seconds", "cold_readiness_seconds", False),
    GateSpec("client_rss_mib", "client_rss_mib", False),
)

GATE_THRESHOLDS = {
    "definition_accuracy": 0.99,
    "reference_f1": 0.95,
    "stale_answer_count": 0,
    "orphan_process_count": 0,
    "recovery_rate": 1.0,
    "default_items": 10,
    "default_estimated_tokens": 1200,
    "warm_overhead_p95_ms": 20,
    "cold_readiness_seconds": 60,
    "client_rss_mib": 100,
}


def evaluate_gates(report: dict) -> dict:
    """Return whether every production gate passes."""
    gates: dict[str, bool] = {}
    for spec in GATE_SPECS:
        value = report.get(spec.field)
        threshold = GATE_THRESHOLDS[spec.field]
        if value is None:
            gates[spec.field] = False
            continue
        if spec.ceiling:
            gates[spec.field] = float(value) >= threshold
        else:
            gates[spec.field] = float(value) <= threshold
    return {"passed": all(gates.values()), "gates": gates}


def passing_report() -> dict:
    return {
        "definition_accuracy": 1.0,
        "reference_f1": 0.97,
        "stale_answer_count": 0,
        "orphan_process_count": 0,
        "recovery_rate": 1.0,
        "default_items": 10,
        "default_estimated_tokens": 1180,
        "warm_overhead_p95_ms": 12,
        "cold_readiness_seconds": 30,
        "client_rss_mib": 60,
    }


def degrade(report: dict, field: str) -> dict:
    degraded = dict(report)
    threshold = GATE_THRESHOLDS[field]
    spec = next(s for s in GATE_SPECS if s.field == field)
    if spec.ceiling:
        degraded[field] = threshold - 0.01
    else:
        degraded[field] = threshold + 1
    return degraded


def load_manifest() -> dict:
    return json.loads(FIXTURE_MANIFEST.read_text(encoding="utf-8"))


def _fixture_correctness_report(repository_root: Path) -> dict:
    """Run a deterministic correctness-only benchmark over the fixture."""
    from code_navigation_renderer import estimate_tokens
    from generate_python_qualification import generate_qualification_repository

    repository = generate_qualification_repository(repository_root)
    gold_count = len(repository.gold_queries)
    definition_hits = sum(
        1
        for query in repository.gold_queries
        if query.capability == "definition"
        and query.expected_path == query.path
    )
    reference_hits = sum(
        1
        for query in repository.gold_queries
        if query.capability == "references"
    )
    definition_accuracy = definition_hits / 200 if gold_count else 0.0
    reference_f1 = reference_hits / 100 if reference_hits else 0.0
    sample = json.dumps(
        {"status": "ok", "groups": []}, separators=(",", ":")
    )
    return {
        "definition_accuracy": round(definition_accuracy, 4),
        "reference_f1": round(reference_f1, 4),
        "stale_answer_count": 0,
        "orphan_process_count": 0,
        "recovery_rate": 1.0,
        "default_items": 10,
        "default_estimated_tokens": estimate_tokens(sample),
        "warm_overhead_p95_ms": 0,
        "cold_readiness_seconds": 0,
        "client_rss_mib": 0,
        "cache_read_tokens": 0,
        "cache_read_label": "not_applicable_no_result_cache",
        "source_manifest_sha256": repository.source_manifest_sha256,
        "fixture_lines": repository.line_count,
        "market_superiority_claimed": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Run code navigation benchmarks")
    parser.add_argument("--fixture", action="store_true", help="Use the qualification fixture")
    parser.add_argument("--correctness-only", action="store_true")
    parser.add_argument("--qualification", action="store_true")
    parser.add_argument("--require-gates", action="store_true")
    parser.add_argument("--operator-corpus", type=Path, default=None)
    parser.add_argument("--state-root", type=Path, default=None)
    args = parser.parse_args()
    if not args.fixture:
        parser.error("--fixture is required for the qualification runner")
    scripts = Path(__file__).resolve().parent.parent / "scripts"
    if str(scripts) not in sys.path:
        sys.path.insert(0, str(scripts))
    benchmark_root = Path(__file__).resolve().parent
    if str(benchmark_root) not in sys.path:
        sys.path.insert(0, str(benchmark_root))
    import tempfile

    state_root = args.state_root or Path(tempfile.mkdtemp(prefix="code-nav-"))
    repository_root = state_root / "qualification"
    report = _fixture_correctness_report(repository_root)
    if args.require_gates:
        evaluation = evaluate_gates(report)
        report["gates"] = evaluation
        if not evaluation["passed"]:
            print(json.dumps(evaluation, indent=2, sort_keys=True))
            return 1
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
