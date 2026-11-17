"""Qualification gates and the closed report, manifest and gold schemas of the Python navigation benchmark."""

from __future__ import annotations

import builtins
import copy
import hashlib
import json
import sys
from pathlib import Path

import pytest

BENCHMARK_ROOT = Path(__file__).resolve().parent.parent / "benchmark"
SCRIPTS_ROOT = Path(__file__).resolve().parent.parent / "scripts"
for path in (BENCHMARK_ROOT, SCRIPTS_ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import navigation_benchmark_schema as schema  # noqa: E402
from code_intelligence import PositionRange  # noqa: E402
from code_navigation import (  # noqa: E402
    NavigationLocation,
    Provenance,
    ResolutionLabel,
)
from generate_python_qualification import (  # noqa: E402
    PYRIGHT_VERSION,
    generate_qualification_repository,
    gold_payload,
)
from navigation_benchmark_gates import GATE_THRESHOLDS, evaluate_gates  # noqa: E402
from navigation_benchmark_queries import _current_citation  # noqa: E402
from navigation_benchmark_schema import (  # noqa: E402
    load_manifest,
    validate_gold,
    validate_manifest,
    validate_report,
)

from tests.code_navigation_benchmark_helpers import _measured_report, _metric  # noqa: E402


def _set_gate(report: dict[str, object], field: str, value: object) -> None:
    paths = {
        "definition_accuracy": ("correctness", "definitions", "accuracy"),
        "reference_f1": ("correctness", "references", "f1"),
        "stale_answer_count": ("reliability", "stale_answer_count"),
        "stale_result_rate": ("reliability", "stale_result_rate"),
        "orphan_process_count": ("reliability", "orphan_process_count"),
        "orphan_process_rate": ("reliability", "orphan_process_rate"),
        "recovery_rate": ("reliability", "recovery_rate"),
        "default_items": ("tokens", "default_items"),
        "default_estimated_tokens": ("tokens", "max_default_estimated_tokens"),
        "warm_overhead_p95_ms": ("performance", "warm_overhead_p95_ms"),
        "cold_readiness_seconds": ("performance", "cold_readiness_seconds"),
        "client_rss_mib": ("resources", "client_peak_rss_mib"),
    }
    current: dict[str, object] = report
    for key in paths[field][:-1]:
        current = current[key]  # type: ignore[assignment]
    current[paths[field][-1]] = value


def test_gates_require_schema_valid_measured_complete_evidence() -> None:
    report = _measured_report()
    validate_report(report)
    assert evaluate_gates(report)["passed"] is True

    hand_built = {field: threshold for field, threshold in GATE_THRESHOLDS.items()}
    assert evaluate_gates(hand_built)["passed"] is False
    assert evaluate_gates(hand_built)["schema_valid"] is False

    incomplete = copy.deepcopy(report)
    incomplete["evidence"]["query_attempts"] = 399  # type: ignore[index]
    assert evaluate_gates(incomplete)["passed"] is False
    assert evaluate_gates(incomplete)["evidence_complete"] is False

    fake = copy.deepcopy(report)
    fake["evidence"]["measured"] = False  # type: ignore[index]
    assert evaluate_gates(fake)["passed"] is False

    inconsistent_recovery = copy.deepcopy(report)
    inconsistent_recovery["reliability"]["crash_recoveries"] = 19  # type: ignore[index]
    assert evaluate_gates(inconsistent_recovery)["passed"] is False
    assert evaluate_gates(inconsistent_recovery)["evidence_complete"] is False

    inconsistent_metric = copy.deepcopy(report)
    inconsistent_metric["correctness"]["references"]["true_positive"] = 94  # type: ignore[index]
    assert evaluate_gates(inconsistent_metric)["passed"] is False
    assert evaluate_gates(inconsistent_metric)["evidence_complete"] is False

    incomplete_pairing = copy.deepcopy(report)
    incomplete_pairing["performance"]["direct_pyright_p95_ms"] = None  # type: ignore[index]
    assert evaluate_gates(incomplete_pairing)["passed"] is False
    assert evaluate_gates(incomplete_pairing)["evidence_complete"] is False


@pytest.mark.parametrize(
    ("os_name", "python_version"),
    [
        ("Windows", "3.14.14"),
        ("Darwin", "3.14.14"),
        ("Linux", "3.9.19"),
        ("Linux", "3.11.9"),
        ("Linux", "3.10.14"),
        ("Linux", "3.13.9"),
        ("Linux", "3.15.0"),
        ("Linux", "3.14"),
        ("Linux", "3.14.14 (qualification build)"),
    ],
)
def test_qualification_gates_require_linux_and_machine_readable_python_3_14(
    os_name: str,
    python_version: str,
) -> None:
    report = _measured_report()
    report["environment"]["os"] = os_name  # type: ignore[index]
    report["identity"]["python_version"] = python_version  # type: ignore[index]
    validate_report(report)

    evaluation = evaluate_gates(report)

    assert evaluation["schema_valid"] is True
    assert evaluation["evidence_complete"] is False
    assert evaluation["passed"] is False


def test_schema_valid_metric_edge_cases_fail_closed_without_raising() -> None:
    zero_reference_domain = _measured_report()
    zero_reference_domain["correctness"]["references"] = {  # type: ignore[index]
        "attempted": 100,
        **_metric(0, 0, 0),
    }
    validate_report(zero_reference_domain)
    evaluation = evaluate_gates(zero_reference_domain)
    assert evaluation["schema_valid"] is True
    assert evaluation["evidence_complete"] is False
    assert evaluation["passed"] is False

    zero_definition_denominator = _measured_report()
    zero_definition_denominator["correctness"]["definitions"] = {  # type: ignore[index]
        "attempted": 0,
        "exact": 0,
        "accuracy": 0.0,
    }
    evaluation = evaluate_gates(zero_definition_denominator)
    assert evaluation["passed"] is False

    wrong_reference_domain = _measured_report()
    wrong_reference_domain["correctness"]["references"] = {  # type: ignore[index]
        "attempted": 100,
        **_metric(474, 26, 25),
    }
    validate_report(wrong_reference_domain)
    evaluation = evaluate_gates(wrong_reference_domain)
    assert evaluation["schema_valid"] is True
    assert evaluation["evidence_complete"] is False

    wrong_call_domain = _measured_report()
    wrong_call_domain["correctness"]["calls"] = {  # type: ignore[index]
        "attempted": 100,
        **_metric(99, 1, 0),
    }
    assert evaluate_gates(wrong_call_domain)["evidence_complete"] is False


@pytest.mark.parametrize(
    ("section", "field", "value"),
    [
        ("correctness", "citation_correctness_rate", float("nan")),
        ("correctness", "task_success_rate", float("nan")),
        ("reliability", "edit_to_fresh_p50_ms", float("nan")),
        ("reliability", "stale_result_rate", float("nan")),
        ("reliability", "edit_to_fresh_p95_ms", float("inf")),
        ("performance", "warm_overhead_p95_ms", float("inf")),
    ],
)
def test_report_rejects_nonfinite_numbers_before_schema_acceptance(
    section: str,
    field: str,
    value: float,
) -> None:
    report = _measured_report()
    report[section][field] = value  # type: ignore[index]
    with pytest.raises(ValueError, match="finite"):
        validate_report(report)
    evaluation = evaluate_gates(report)
    assert evaluation["schema_valid"] is False
    assert evaluation["passed"] is False


def test_token_ids_are_unique_known_gold_queries_and_rates_are_consistent() -> None:
    duplicate = _measured_report()
    duplicate["tokens"]["tasks"][1]["query_id"] = (  # type: ignore[index]
        duplicate["tokens"]["tasks"][0]["query_id"]  # type: ignore[index]
    )
    evaluation = evaluate_gates(duplicate)
    assert evaluation["passed"] is False
    assert evaluation["evidence_complete"] is False

    unknown = _measured_report()
    unknown["tokens"]["tasks"][0]["query_id"] = "unknown-999"  # type: ignore[index]
    evaluation = evaluate_gates(unknown)
    assert evaluation["passed"] is False
    assert evaluation["evidence_complete"] is False

    wrong_stale_rate = _measured_report()
    wrong_stale_rate["reliability"]["stale_result_rate"] = 0.1  # type: ignore[index]
    assert evaluate_gates(wrong_stale_rate)["evidence_complete"] is False

    wrong_orphan_rate = _measured_report()
    wrong_orphan_rate["reliability"]["orphan_process_rate"] = 0.25  # type: ignore[index]
    assert evaluate_gates(wrong_orphan_rate)["evidence_complete"] is False

    wrong_citation_rate = _measured_report()
    wrong_citation_rate["correctness"]["citation_locations_correct"] = 799  # type: ignore[index]
    validate_report(wrong_citation_rate)
    assert evaluate_gates(wrong_citation_rate)["evidence_complete"] is False


@pytest.mark.parametrize(
    ("attempted", "correct"),
    [(795, 795), (800, 795)],
)
def test_solved_tasks_require_their_gold_location_citations(
    attempted: int,
    correct: int,
) -> None:
    report = _measured_report()
    report["correctness"]["citation_locations_attempted"] = attempted  # type: ignore[index]
    report["correctness"]["citation_locations_correct"] = correct  # type: ignore[index]
    report["correctness"]["citation_correctness_rate"] = correct / attempted  # type: ignore[index]
    validate_report(report)

    evaluation = evaluate_gates(report)
    assert evaluation["schema_valid"] is True
    assert evaluation["evidence_complete"] is False
    assert evaluation["passed"] is False


@pytest.mark.parametrize(
    ("mode", "section", "p50_field", "p95_field"),
    [
        (
            "correctness-only",
            "reliability",
            "edit_to_fresh_p50_ms",
            "edit_to_fresh_p95_ms",
        ),
        (
            "qualification",
            "performance",
            "warm_facade_p50_ms",
            "warm_facade_p95_ms",
        ),
    ],
)
def test_percentile_ordering_fails_closed(
    mode: str,
    section: str,
    p50_field: str,
    p95_field: str,
) -> None:
    report = _measured_report(mode)
    report[section][p50_field] = 9.0  # type: ignore[index]
    report[section][p95_field] = 8.0  # type: ignore[index]
    validate_report(report)

    evaluation = evaluate_gates(report)
    assert evaluation["schema_valid"] is True
    assert evaluation["evidence_complete"] is False
    assert evaluation["passed"] is False


def test_present_operator_corpus_must_be_available_and_error_free() -> None:
    report = _measured_report("correctness-only")
    report["operator_corpus"] = {
        "available": False,
        "python_files": 1,
        "queries_attempted": 1,
        "queries_succeeded": 1,
        "errors": 1,
    }
    validate_report(report)

    evaluation = evaluate_gates(report)
    assert evaluation["schema_valid"] is True
    assert evaluation["evidence_complete"] is False
    assert evaluation["passed"] is False


@pytest.mark.parametrize(
    ("field", "failure"),
    [
        ("definition_accuracy", 0.989999),
        ("reference_f1", 0.949999),
        ("stale_answer_count", 1),
        ("stale_result_rate", 0.000001),
        ("orphan_process_count", 1),
        ("orphan_process_rate", 0.000001),
        ("recovery_rate", 0.999999),
        ("default_items", 11),
        ("default_estimated_tokens", 1201),
        ("warm_overhead_p95_ms", 30.000001),
        ("cold_readiness_seconds", 60.000001),
        ("client_rss_mib", 100.0),
    ],
)
def test_every_production_threshold_fails_closed(field: str, failure: object) -> None:
    report = _measured_report()
    _set_gate(report, field, failure)
    evaluation = evaluate_gates(report)
    assert evaluation["gates"][field]["passed"] is False
    assert evaluation["passed"] is False


def test_correctness_mode_has_schema_locked_reduced_gate_scope() -> None:
    report = _measured_report("correctness-only")
    report["environment"]["os"] = "Windows"  # type: ignore[index]
    report["identity"]["python_version"] = "3.12.10 (diagnostic build)"  # type: ignore[index]
    validate_report(report)
    evaluation = evaluate_gates(report)
    assert evaluation["scope"] == "correctness_reliability"
    assert evaluation["passed"] is True
    assert "warm_overhead_p95_ms" not in evaluation["gates"]

    masquerade = copy.deepcopy(report)
    masquerade["performance"]["available"] = True  # type: ignore[index]
    with pytest.raises(ValueError, match="schema"):
        validate_report(masquerade)


def test_report_schema_rejects_unknown_fields_and_unmeasured_qualification() -> None:
    report = _measured_report()
    with pytest.raises(ValueError, match="schema"):
        validate_report({**report, "unknown": True})
    nested_unknown = copy.deepcopy(report)
    nested_unknown["identity"]["unknown"] = True  # type: ignore[index]
    with pytest.raises(ValueError, match="schema"):
        validate_report(nested_unknown)

    unmeasured = copy.deepcopy(report)
    unmeasured["performance"]["available"] = False  # type: ignore[index]
    for field in (
        "cold_readiness_seconds",
        "warm_facade_p50_ms",
        "warm_facade_p95_ms",
        "direct_pyright_p95_ms",
        "warm_overhead_p95_ms",
    ):
        unmeasured["performance"][field] = None  # type: ignore[index]
    unmeasured["performance"]["sample_count"] = 0  # type: ignore[index]
    unmeasured["resources"] = {
        "available": False,
        "client_peak_rss_mib": None,
        "method": "unavailable",
    }
    validate_report(unmeasured)
    evaluation = evaluate_gates(unmeasured)
    assert evaluation["schema_valid"] is True
    assert evaluation["evidence_complete"] is False
    assert evaluation["passed"] is False

    negative_overhead = copy.deepcopy(report)
    negative_overhead["performance"]["warm_overhead_p95_ms"] = -0.25  # type: ignore[index]
    validate_report(negative_overhead)


def test_runtime_schema_validation_has_no_jsonschema_import(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_import = builtins.__import__

    def blocked_import(name: str, *args, **kwargs):
        if name == "jsonschema" or name.startswith("jsonschema."):
            raise ImportError("jsonschema intentionally unavailable")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked_import)
    validate_manifest(load_manifest())
    validate_report(_measured_report())
    repository = generate_qualification_repository(tmp_path / "schema-gold")
    validate_gold(gold_payload(repository))


@pytest.mark.parametrize(
    ("document", "mutate"),
    [
        ("manifest", lambda value: value.__setitem__("fixture_seed", True)),
        ("gold", lambda value: value["queries"][0].__setitem__("line", True)),
        (
            "gold",
            lambda value: value["queries"][0].__setitem__("query_id", "definition-x"),
        ),
        (
            "report",
            lambda value: value["environment"].__setitem__("cpu_core_count", True),
        ),
        (
            "report",
            lambda value: value["reliability"].__setitem__("orphan_process_rate", 1.01),
        ),
        (
            "report",
            lambda value: value["tokens"]["tasks"].append(value["tokens"]["tasks"][0]),
        ),
    ],
)
def test_stdlib_schema_validator_rejects_closed_contract_violations(
    tmp_path: Path,
    document: str,
    mutate,
) -> None:
    repository = generate_qualification_repository(tmp_path / f"strict-{document}")
    values = {
        "manifest": load_manifest(),
        "gold": gold_payload(repository),
        "report": _measured_report(),
    }
    validators = {
        "manifest": validate_manifest,
        "gold": validate_gold,
        "report": validate_report,
    }
    value = copy.deepcopy(values[document])
    mutate(value)
    with pytest.raises(ValueError, match="schema"):
        validators[document](value)


def test_public_schemas_are_valid_draft7_when_optional_validator_is_available() -> None:
    jsonschema = pytest.importorskip("jsonschema")
    for path in (
        schema.MANIFEST_SCHEMA,
        schema.GOLD_SCHEMA,
        schema.REPORT_SCHEMA,
    ):
        jsonschema.Draft7Validator.check_schema(json.loads(path.read_text(encoding="utf-8")))


def test_citation_validation_requires_the_current_source_hash(tmp_path: Path) -> None:
    content = b"def target() -> int:\n    return 1\n"
    source = tmp_path / "module.py"
    source.write_bytes(content)
    start = content.index(b"target")
    location = NavigationLocation(
        "module.py",
        PositionRange(start, start + len(b"target")),
        1,
        len(b"def "),
        None,
        None,
        ResolutionLabel.LSP_CONFIRMED,
        (Provenance("lsp", "pyright", PYRIGHT_VERSION, "provider_reported"),),
    )
    digest = hashlib.sha256(content).hexdigest()
    assert _current_citation(tmp_path, location, expected_sha256=digest)
    assert not _current_citation(tmp_path, location, expected_sha256="0" * 64)
