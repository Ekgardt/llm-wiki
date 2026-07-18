"""Validate and smoke-test the Task 27 comparative benchmark contract.

This early harness is deterministic, fake, offline, and orchestration-only.
It cannot execute a real comparison or support a quality claim before Gate F.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from reliable_memory import canonical_json_bytes, validate_schema  # noqa: E402

DEFAULT_CONTRACT = Path(__file__).with_name("comparative-v1.json")
DEFAULT_SCHEMA = Path(__file__).with_name("comparative-v1.schema.json")
DEFAULT_LEDGER_SCHEMA = Path(__file__).with_name("comparative-task-ledger-v1.schema.json")
DEFAULT_REPORT_SCHEMA = Path(__file__).with_name("comparative-smoke-report-v1.schema.json")
MAX_CONTRACT_BYTES = 128 * 1024
MAX_SCHEMA_BYTES = 128 * 1024

ADAPTER_IDS = {
    "adaptive-context-compiler",
    "evidence-graph-only",
    "graphify-pinned",
    "grep-read",
    "hybrid-retrieval",
    "llm-wiki-current",
}
FAIRNESS_KEYS = {
    "commit",
    "context_budget",
    "hardware",
    "model",
    "repository",
    "retry_policy",
    "task",
}
METRIC_FIELDS = {
    "blinded_factual_correctness",
    "cache_tokens",
    "edge_precision",
    "edge_recall",
    "executable_task_success",
    "freshness",
    "incremental_time_ms",
    "index_size_bytes",
    "indexing_time_ms",
    "peak_ram_bytes",
    "query_latency_ms",
    "retrieval_quality",
    "uncached_input_tokens",
    "uncached_output_tokens",
}
GRAPHIFY_COMMIT = "cb96bdaa0c367bec8d5c5aee5d7c9ebb727e9780"
GRAPHIFY_LOCK_BLOB = "088ebbbdcb17eacec5b60541f290381f6adf33e7"


def _read_bounded_json(path: Path, maximum: int, label: str) -> tuple[bytes, dict]:
    try:
        if path.stat().st_size > maximum:
            raise ValueError(f"{label} exceeds {maximum} bytes")
        raw = path.read_bytes()
        value = json.loads(raw)
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot load {label}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object")
    return raw, value


def load_contract(contract_path: Path | str, schema_path: Path | str) -> dict:
    """Load a canonical contract and reject weakened or incomplete semantics."""
    contract_path = Path(contract_path)
    schema_path = Path(schema_path)
    raw, contract = _read_bounded_json(contract_path, MAX_CONTRACT_BYTES, "comparative contract")
    _schema_raw, _schema = _read_bounded_json(schema_path, MAX_SCHEMA_BYTES, "comparative schema")
    validate_schema(contract, schema_path)
    if raw != canonical_json_bytes(contract) + b"\n":
        raise ValueError("comparative contract bytes are not canonical and frozen")

    if {adapter["id"] for adapter in contract["adapters"]} != ADAPTER_IDS:
        raise ValueError("comparative adapter set is incomplete")
    if set(contract["fairness"]["identical_inputs"]) != FAIRNESS_KEYS:
        raise ValueError("comparative contract does not require all identical inputs")
    if set(contract["metrics"]["per_task_fields"]) != METRIC_FIELDS:
        raise ValueError("comparative per-task metric ledger is incomplete")
    graphify = contract["provenance"]["graphify"]
    if (
        graphify["commit"] != GRAPHIFY_COMMIT
        or graphify["dependency_lock"]["git_blob_sha1"] != GRAPHIFY_LOCK_BLOB
    ):
        raise ValueError("Graphify source or dependency lock is not pinned")
    gate = contract["public_claim_gate"]
    if gate != {
        "comparisons": {
            "quality": "10000*quality_difference_lower_confidence_bound>-200",
            "token_ratio": "10000*token_ratio_upper_confidence_bound<9000",
        },
        "consumed_report_fields": {
            "quality": "quality_difference_lower_confidence_bound",
            "token_ratio": "token_ratio_upper_confidence_bound",
        },
        "hard_gates": ["crash", "evidence", "freshness"],
        "quality_lower_bound_basis_points_strictly_greater_than": -200,
        "requires_gate_f": True,
        "requires_real_evidence": True,
        "token_ratio_upper_bound_basis_points_strictly_less_than": 9000,
    }:
        raise ValueError("public claim gate differs from canonical Task 27 claim gate")
    if contract["availability"]["gate_f_passed"] or contract["availability"]["heavy_comparison_available"]:
        raise ValueError("early comparative contract must keep real execution unavailable")
    if contract["provenance"]["configuration"]["sha256"] != configuration_fingerprint(contract):
        raise ValueError("comparative configuration fingerprint mismatch")
    return contract


def configuration_fingerprint(contract: dict) -> str:
    """Hash every operational choice while excluding the hash field itself."""
    provenance = dict(contract["provenance"])
    provenance["configuration"] = {"id": provenance["configuration"]["id"]}
    projection = {
        key: contract[key]
        for key in (
            "adapters",
            "fairness",
            "metrics",
            "public_claim_gate",
            "smoke",
            "statistics",
            "tasks",
        )
    }
    projection["provenance"] = provenance
    return hashlib.sha256(canonical_json_bytes(projection)).hexdigest()


def _fingerprint(inputs: dict) -> str:
    return hashlib.sha256(canonical_json_bytes(inputs)).hexdigest()


def validate_ledger(ledger: dict, schema_path: Path | str = DEFAULT_LEDGER_SCHEMA) -> dict:
    """Validate one closed task ledger and its cross-field invariants."""
    schema_path = Path(schema_path)
    _schema_raw, _schema = _read_bounded_json(schema_path, MAX_SCHEMA_BYTES, "ledger schema")
    validate_schema(ledger, schema_path)
    failed = ledger["outcome"] == "failure"
    if failed != (ledger["failure"] is not None):
        raise ValueError("ledger failure object must exactly match failure outcome")
    if ledger["input_fingerprint"] != _fingerprint(ledger["inputs"]):
        raise ValueError("ledger input fingerprint mismatch")
    return ledger


def validate_report(
    report: dict,
    report_schema_path: Path | str = DEFAULT_REPORT_SCHEMA,
    ledger_schema_path: Path | str = DEFAULT_LEDGER_SCHEMA,
) -> dict:
    """Validate the closed smoke report and every embedded raw ledger."""
    report_schema_path = Path(report_schema_path)
    _schema_raw, _schema = _read_bounded_json(
        report_schema_path, MAX_SCHEMA_BYTES, "smoke report schema"
    )
    validate_schema(report, report_schema_path)
    for ledger in report["raw_task_ledgers"]:
        validate_ledger(ledger, ledger_schema_path)
    if {ledger["adapter_id"] for ledger in report["raw_task_ledgers"]} != ADAPTER_IDS:
        raise ValueError("smoke report adapter ledger set is incomplete")
    if len({ledger["input_fingerprint"] for ledger in report["raw_task_ledgers"]}) != 1:
        raise ValueError("smoke report adapters did not receive identical inputs")
    return report


def run_smoke(contract: dict) -> dict:
    """Exercise every adapter ledger shape without running any real backend."""
    task = contract["tasks"][0]
    inputs = task["inputs"]
    fingerprint = _fingerprint(inputs)
    bound_fields = contract["public_claim_gate"]["consumed_report_fields"]
    unavailable_metrics = {field: None for field in sorted(METRIC_FIELDS)}
    ledgers = []
    for adapter in contract["adapters"]:
        failed = adapter["id"] == contract["smoke"]["intentional_failure_adapter"]
        ledgers.append(
            {
                "adapter_id": adapter["id"],
                "adapter_provenance": {
                    "configuration_sha256": contract["provenance"]["configuration"][
                        "sha256"
                    ],
                    "implementation_status": adapter["implementation_status"],
                    "source_revision": (
                        GRAPHIFY_COMMIT
                        if adapter["id"] == "graphify-pinned"
                        else contract["provenance"]["configuration"]["sha256"]
                    ),
                },
                "attempt": 1,
                "failure": (
                    {
                        "category": "orchestration",
                        "code": "real-adapter-disabled",
                        "message": "Pinned Graphify is not executed by deterministic smoke.",
                        "phase": "smoke",
                        "retryable": False,
                    }
                    if failed
                    else None
                ),
                "input_fingerprint": fingerprint,
                "inputs": inputs,
                "metrics": dict(unavailable_metrics),
                "outcome": "failure" if failed else "orchestration-pass",
                "seed": contract["statistics"]["agent_seeds"][0],
                "task_id": task["id"],
            }
        )

    return {
        "bounded": {
            "adapter_count": len(contract["adapters"]),
            "attempts_per_adapter": contract["smoke"]["attempts_per_adapter"],
            "task_count": len(contract["tasks"]),
        },
        "heavy_comparison_available": False,
        "mode": "deterministic-fake-offline-smoke",
        "network_access": False,
        "public_claim_gate": {
            "eligible": False,
            "failed_conditions": [
                "gate-f-not-passed",
                "real-comparative-evidence-unavailable",
                "quality-confidence-interval-unavailable",
                "token-ratio-confidence-interval-unavailable",
                "hard-gates-unmeasured",
            ],
            "gate_f_passed": False,
            "hard_gates": {"crash": None, "evidence": None, "freshness": None},
            "interpretation": "orchestration-only-no-quality-claim",
            bound_fields["quality"]: None,
            "real_evidence_complete": False,
            bound_fields["token_ratio"]: None,
        },
        "quality_claim": False,
        "raw_task_ledgers": ledgers,
        "schema_version": "comparative-smoke-report/v1",
        "statistics": {
            "agent_seeds": contract["statistics"]["agent_seeds"],
            "claim_gating_method": contract["statistics"]["claim_gating_method"],
            "computed": False,
            "reason": "real paired observations unavailable",
        },
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--contract", type=Path, default=DEFAULT_CONTRACT)
    parser.add_argument("--schema", type=Path, default=DEFAULT_SCHEMA)
    parser.add_argument("--smoke", action="store_true", help="run bounded fake/offline smoke")
    parser.add_argument("--json", action="store_true", help="emit canonical JSON")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if not args.smoke:
        print(
            "real comparative execution is unavailable until Gate F and complete evidence",
            file=sys.stderr,
        )
        return 2
    try:
        contract = load_contract(args.contract, args.schema)
        report = run_smoke(contract)
        validate_report(report)
    except (OSError, ValueError) as exc:
        print(f"comparative smoke failed: {exc}", file=sys.stderr)
        return 2
    output = canonical_json_bytes(report).decode("utf-8")
    print(output if args.json else json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
