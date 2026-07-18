from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest
from reliable_memory import SchemaValidationError, canonical_json_bytes, validate_schema

ROOT = Path(__file__).resolve().parent.parent
BENCHMARK = ROOT / "benchmark"
CONTRACT = BENCHMARK / "comparative-v1.json"
SCHEMA = BENCHMARK / "comparative-v1.schema.json"
RUNNER = BENCHMARK / "run_comparative.py"
LEDGER_SCHEMA = BENCHMARK / "comparative-task-ledger-v1.schema.json"
REPORT_SCHEMA = BENCHMARK / "comparative-smoke-report-v1.schema.json"

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
    assert LEDGER_SCHEMA.is_file()
    assert REPORT_SCHEMA.is_file()


def test_schema_is_closed_at_every_object_level():
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

    for path in (SCHEMA, LEDGER_SCHEMA, REPORT_SCHEMA):
        visit(json.loads(path.read_text(encoding="utf-8")))


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


def test_contract_defines_all_adapters_equal_inputs_and_metrics():
    contract = json.loads(CONTRACT.read_text(encoding="utf-8"))

    assert {adapter["id"] for adapter in contract["adapters"]} == ADAPTER_IDS
    assert all(adapter["implementation_status"] == "contract-only" for adapter in contract["adapters"])
    assert set(contract["fairness"]["identical_inputs"]) == FAIRNESS_KEYS
    assert set(contract["metrics"]["per_task_fields"]) == METRIC_FIELDS
    assert contract["metrics"]["latency_summary"] == ["p50", "p95"]


def test_contract_freezes_complete_paired_claim_gating_statistics():
    contract = json.loads(CONTRACT.read_text(encoding="utf-8"))
    statistics = contract["statistics"]

    assert statistics["claim_gating_method"] == "paired-hierarchical-percentile-bootstrap"
    assert statistics["pairing"] == {
        "key": ["repository", "commit", "task"],
        "seed_role": "matched-repeated-run",
        "require_identical_seed_sets": True,
    }
    assert len(statistics["agent_seeds"]) >= 3
    assert len(set(statistics["agent_seeds"])) == len(statistics["agent_seeds"])
    assert statistics["resampling"] == {
        "cluster_level": "resample-pairing-keys-with-replacement",
        "within_cluster_level": "resample-matched-seed-indices-with-replacement",
        "candidate_baseline_pairing": "same-cluster-occurrences-and-seed-indices",
        "cluster_aggregation": "equal-weight-mean",
        "resamples": 10000,
    }
    assert statistics["interval"] == {
        "construction": "percentile",
        "familywise_confidence_level_basis_points": 9500,
        "per_estimand_one_sided_confidence_level_basis_points": 9750,
        "quality_tail": "lower",
        "quality_quantile_basis_points": 250,
        "token_ratio_tail": "upper",
        "token_ratio_quantile_basis_points": 9750,
    }
    assert statistics["rng"] == {
        "algorithm": "sha256-counter-v1",
        "bootstrap_seed_hex": "8f507f48dc6f8f5a73ec78f7d58f3cf4",
        "randomization_seed_hex": "299f31ed0b43d65e46945f85e51b9def",
    }
    assert statistics["estimands"] == {
        "quality_difference": {
            "candidate": "terminal executable success when graded, otherwise blinded factual correctness",
            "baseline": "same score for pinned Graphify",
            "formula": "equal-weight mean over pairing keys of matched-seed mean(candidate-baseline)",
            "scale": "proportion-points",
        },
        "token_ratio": {
            "per_attempt_total": "uncached_input_tokens+uncached_output_tokens+cache_tokens",
            "formula": "sum(candidate attempt totals)/sum(pinned Graphify attempt totals) over the resample",
            "retry_accounting": "include every attempted retry",
            "scale": "ratio",
        },
    }
    assert statistics["missing_and_failures"] == {
        "failed_terminal_quality": "zero",
        "failed_attempt_tokens": "include observed consumption",
        "missing_expected_ledger": "invalidate evidence and close public gate",
        "missing_quality_metric": "invalidate quality bound and close public gate",
        "missing_token_metric": "invalidate token bound and close public gate",
        "zero_graphify_token_denominator": "invalidate token bound and close public gate",
    }
    assert statistics["randomization_diagnostic"] == {
        "gating": False,
        "method": "paired-sign-flip-on-cluster-quality-differences",
        "tail": "two-sided",
        "confidence_set_inversion": False,
        "interpretation": "diagnostic-p-value-only",
    }
    assert contract["public_claim_gate"]["consumed_report_fields"] == {
        "quality": "quality_difference_lower_confidence_bound",
        "token_ratio": "token_ratio_upper_confidence_bound",
    }
    assert contract["public_claim_gate"]["comparisons"] == {
        "quality": "10000*quality_difference_lower_confidence_bound>-200",
        "token_ratio": "10000*token_ratio_upper_confidence_bound<9000",
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
    assert runner.validate_report(first, REPORT_SCHEMA, LEDGER_SCHEMA) == first
    for ledger in first["raw_task_ledgers"]:
        assert set(ledger["adapter_provenance"]) == {
            "configuration_sha256",
            "implementation_status",
            "source_revision",
        }
        if ledger["outcome"] == "failure":
            assert set(ledger["failure"]) == {
                "category",
                "code",
                "message",
                "phase",
                "retryable",
            }


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
        "gate_f_passed": False,
        "hard_gates": {"crash": None, "evidence": None, "freshness": None},
        "interpretation": "orchestration-only-no-quality-claim",
        "quality_difference_lower_confidence_bound": None,
        "real_evidence_complete": False,
        "token_ratio_upper_confidence_bound": None,
    }
    assert report["statistics"]["computed"] is False
    assert report["statistics"]["reason"] == "real paired observations unavailable"


def test_smoke_gate_consumes_contract_selected_bound_field_names():
    runner = _runner_module()
    contract = runner.load_contract(CONTRACT, SCHEMA)
    contract["public_claim_gate"]["consumed_report_fields"] = {
        "quality": "selected_quality_bound",
        "token_ratio": "selected_token_bound",
    }

    gate = runner.run_smoke(contract)["public_claim_gate"]

    assert gate["selected_quality_bound"] is None
    assert gate["selected_token_bound"] is None
    assert "quality_difference_lower_confidence_bound" not in gate
    assert "token_ratio_upper_confidence_bound" not in gate


def test_report_and_ledger_schemas_reject_missing_extra_and_wrong_types():
    runner = _runner_module()
    report = runner.run_smoke(runner.load_contract(CONTRACT, SCHEMA))

    bad_reports = []
    missing = json.loads(json.dumps(report))
    missing.pop("quality_claim")
    bad_reports.append(missing)
    extra = json.loads(json.dumps(report))
    extra["unexpected"] = True
    bad_reports.append(extra)
    wrong_type = json.loads(json.dumps(report))
    wrong_type["bounded"]["adapter_count"] = "six"
    bad_reports.append(wrong_type)
    for bad in bad_reports:
        with pytest.raises(SchemaValidationError):
            runner.validate_report(bad, REPORT_SCHEMA, LEDGER_SCHEMA)

    ledger = report["raw_task_ledgers"][0]
    bad_ledgers = []
    missing = json.loads(json.dumps(ledger))
    missing.pop("seed")
    bad_ledgers.append(missing)
    extra = json.loads(json.dumps(ledger))
    extra["unexpected"] = True
    bad_ledgers.append(extra)
    wrong_type = json.loads(json.dumps(ledger))
    wrong_type["metrics"]["uncached_input_tokens"] = "unknown"
    bad_ledgers.append(wrong_type)
    for bad in bad_ledgers:
        with pytest.raises(SchemaValidationError):
            runner.validate_ledger(bad, LEDGER_SCHEMA)


def test_ledger_schema_accepts_typed_task27_measurements():
    runner = _runner_module()
    report = runner.run_smoke(runner.load_contract(CONTRACT, SCHEMA))
    ledger = json.loads(json.dumps(report["raw_task_ledgers"][0]))
    ledger["failure"] = None
    ledger["outcome"] = "success"
    ledger["metrics"] = {
        "blinded_factual_correctness": 0.75,
        "cache_tokens": 20,
        "edge_precision": 0.8,
        "edge_recall": 0.7,
        "executable_task_success": True,
        "freshness": 1.0,
        "incremental_time_ms": 2.5,
        "index_size_bytes": 4096,
        "indexing_time_ms": 10.5,
        "peak_ram_bytes": 8192,
        "query_latency_ms": 3.25,
        "retrieval_quality": 0.9,
        "uncached_input_tokens": 100,
        "uncached_output_tokens": 25,
    }

    assert runner.validate_ledger(ledger, LEDGER_SCHEMA) == ledger


def test_main_validates_generated_report_before_output(monkeypatch, capsys):
    runner = _runner_module()
    monkeypatch.setattr(runner, "run_smoke", lambda contract: {"schema_version": "wrong"})

    assert runner.main(["--smoke", "--json"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "failed" in captured.err


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
