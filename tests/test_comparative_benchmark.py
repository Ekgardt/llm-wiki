from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest
from reliable_memory import canonical_json_bytes, validate_schema

ROOT = Path(__file__).resolve().parent.parent
BENCHMARK = ROOT / "benchmark"
CONTRACT = BENCHMARK / "comparative-v1.json"
SCHEMA = BENCHMARK / "comparative-v1.schema.json"
RUNNER = BENCHMARK / "run_comparative.py"

ADAPTER_IDS = {
    "grep-read",
    "graphify-pinned",
    "llm-wiki-current",
    "evidence-graph-only",
    "hybrid-retrieval",
    "adaptive-context-compiler",
}
FAIRNESS_KEYS = {
    "repository",
    "commit",
    "hardware",
    "task",
    "model",
    "context_budget",
    "retry_policy",
}
METRIC_FIELDS = {
    "executable_task_success",
    "blinded_factual_correctness",
    "retrieval_quality",
    "uncached_input_tokens",
    "uncached_output_tokens",
    "cache_tokens",
    "indexing_time_ms",
    "incremental_time_ms",
    "query_latency_ms",
    "peak_ram_bytes",
    "index_size_bytes",
    "edge_precision",
    "edge_recall",
    "freshness",
}


def _runner_module():
    spec = importlib.util.spec_from_file_location("run_comparative", RUNNER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_required_comparative_artifacts_exist():
    assert SCHEMA.is_file()
    assert CONTRACT.is_file()
    assert RUNNER.is_file()


def test_schema_is_closed_at_every_object_level():
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))

    def visit(rule: object) -> None:
        if isinstance(rule, dict):
            if rule.get("type") == "object":
                assert rule.get("additionalProperties") is False
                assert set(rule.get("required", ())) == set(rule.get("properties", ()))
            for value in rule.values():
                visit(value)
        elif isinstance(rule, list):
            for value in rule:
                visit(value)

    visit(schema)


def test_contract_is_canonical_schema_valid_and_semantically_closed():
    runner = _runner_module()
    raw = CONTRACT.read_bytes()
    contract = json.loads(raw)

    validate_schema(contract, SCHEMA)
    assert raw == canonical_json_bytes(contract) + b"\n"
    assert runner.load_contract(CONTRACT, SCHEMA) == contract


def test_graphify_and_execution_provenance_are_fully_pinned():
    runner = _runner_module()
    contract = json.loads(CONTRACT.read_text(encoding="utf-8"))
    provenance = contract["provenance"]

    assert provenance["graphify"] == {
        "repository": "https://github.com/Graphify-Labs/graphify",
        "commit": "cb96bdaa0c367bec8d5c5aee5d7c9ebb727e9780",
        "dependency_lock": {
            "path": "uv.lock",
            "git_blob_sha1": "088ebbbdcb17eacec5b60541f290381f6adf33e7",
        },
    }
    assert provenance["python"]["version"] == "3.12.10"
    assert provenance["python"]["implementation"] == "CPython"
    assert provenance["model"]["id"] == "deterministic-fake/comparative-v1"
    assert provenance["model"]["revision"] == "comparative-smoke-v1"
    assert provenance["tokenizer"]["id"] == "utf8-bytes/v1"
    assert provenance["tokenizer"]["revision"] == "comparative-smoke-v1"
    assert provenance["configuration"]["id"] == "comparative-smoke-v1"
    assert len(provenance["configuration"]["sha256"]) == 64
    assert provenance["configuration"]["sha256"] != "0" * 64
    assert provenance["configuration"]["sha256"] == runner.configuration_fingerprint(contract)
    assert provenance["dependencies"] == []
    assert all(value not in {None, "", "latest", "unversioned"} for value in (
        provenance["python"]["version"],
        provenance["model"]["revision"],
        provenance["tokenizer"]["revision"],
        provenance["configuration"]["sha256"],
    ))


def test_contract_defines_all_adapters_equal_inputs_metrics_and_statistics():
    contract = json.loads(CONTRACT.read_text(encoding="utf-8"))

    assert {adapter["id"] for adapter in contract["adapters"]} == ADAPTER_IDS
    assert all(adapter["implementation_status"] == "contract-only" for adapter in contract["adapters"])
    assert set(contract["fairness"]["identical_inputs"]) == FAIRNESS_KEYS
    assert set(contract["metrics"]["per_task_fields"]) == METRIC_FIELDS
    assert contract["metrics"]["latency_summary"] == ["p50", "p95"]
    assert len(contract["statistics"]["seeds"]) >= 3
    assert len(set(contract["statistics"]["seeds"])) == len(contract["statistics"]["seeds"])
    assert contract["statistics"]["pairing_unit"] == "repository-commit-task-seed"
    assert contract["statistics"]["confidence_level_basis_points"] == 9500
    assert set(contract["statistics"]["interval_methods"]) == {
        "paired-bootstrap",
        "paired-randomization",
    }


def test_loader_rejects_input_inequality_and_claim_gate_relaxation(tmp_path):
    runner = _runner_module()
    contract = json.loads(CONTRACT.read_text(encoding="utf-8"))

    unequal = json.loads(json.dumps(contract))
    unequal["fairness"]["identical_inputs"].remove("hardware")
    unequal_path = tmp_path / "unequal.json"
    unequal_path.write_bytes(canonical_json_bytes(unequal) + b"\n")
    with pytest.raises(ValueError, match="identical.inputs"):
        runner.load_contract(unequal_path, SCHEMA)

    relaxed = json.loads(json.dumps(contract))
    relaxed["public_claim_gate"]["quality_lower_bound_basis_points_strictly_greater_than"] = -300
    relaxed_path = tmp_path / "relaxed.json"
    relaxed_path.write_bytes(canonical_json_bytes(relaxed) + b"\n")
    with pytest.raises(ValueError, match="public.claim.gate"):
        runner.load_contract(relaxed_path, SCHEMA)


def test_smoke_is_deterministic_bounded_and_keeps_raw_failures():
    runner = _runner_module()
    contract = runner.load_contract(CONTRACT, SCHEMA)

    first = runner.run_smoke(contract)
    second = runner.run_smoke(contract)

    assert first == second
    assert first["mode"] == "deterministic-fake-offline-smoke"
    assert first["network_access"] is False
    assert first["quality_claim"] is False
    assert first["heavy_comparison_available"] is False
    assert first["bounded"]["adapter_count"] == 6
    assert first["bounded"]["task_count"] == 1
    assert first["bounded"]["attempts_per_adapter"] == 1
    assert {ledger["adapter_id"] for ledger in first["raw_task_ledgers"]} == ADAPTER_IDS
    assert len(first["raw_task_ledgers"]) == len(ADAPTER_IDS)
    assert any(ledger["outcome"] == "failure" for ledger in first["raw_task_ledgers"])
    assert all(set(ledger["metrics"]) == METRIC_FIELDS for ledger in first["raw_task_ledgers"])
    assert all(all(value is None for value in ledger["metrics"].values()) for ledger in first["raw_task_ledgers"])


def test_smoke_uses_identical_inputs_and_fail_closed_public_gate():
    runner = _runner_module()
    report = runner.run_smoke(runner.load_contract(CONTRACT, SCHEMA))
    fingerprints = {ledger["input_fingerprint"] for ledger in report["raw_task_ledgers"]}

    assert len(fingerprints) == 1
    assert report["public_claim_gate"] == {
        "eligible": False,
        "failed_conditions": [
            "gate-f-not-passed",
            "real-comparative-evidence-unavailable",
            "quality-confidence-interval-unavailable",
            "token-ratio-confidence-interval-unavailable",
            "hard-gates-unmeasured",
        ],
        "interpretation": "orchestration-only-no-quality-claim",
    }
    assert report["statistics"]["computed"] is False
    assert report["statistics"]["reason"] == "real paired observations unavailable"


def test_cli_smoke_is_json_offline_and_real_mode_is_unavailable():
    smoke = subprocess.run(
        [sys.executable, str(RUNNER), "--smoke", "--json"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert smoke.returncode == 0, smoke.stderr
    report = json.loads(smoke.stdout)
    assert report["quality_claim"] is False
    assert report["network_access"] is False

    real = subprocess.run(
        [sys.executable, str(RUNNER), "--json"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert real.returncode == 2
    assert not real.stdout
    assert "unavailable until Gate F" in real.stderr
