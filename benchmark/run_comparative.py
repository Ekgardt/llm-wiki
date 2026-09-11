"""Validate and execute the Task 27 comparative benchmark contract.

Ordinary CI uses ``--smoke`` or ``--fixture``. A real run requires an explicit
manifest, a successful ``--preflight``, the pinned Graphify checkout, the exact
model identity, and complete Gate F evidence. No dependency is downloaded.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import platform
import re
import shutil
import subprocess
import sys
import unicodedata
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from bounded_io import read_stable_bytes  # noqa: E402
from reliable_memory import canonical_json_bytes, validate_schema_object  # noqa: E402

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
MAX_MANIFEST_BYTES = 256 * 1024
MAX_REAL_TASKS = 1000
GATE_F_CHECKS = {
    "deletes_and_renames_correct",
    "graph_tools_use_active_generation_or_explicit_fallback",
    "impact_preserves_uncertainty",
    "incremental_equals_clean_rebuild",
}


@dataclass(frozen=True)
class AdapterSpec:
    backend: str
    profile: str | None
    max_results: int = 20


class PreflightError(ValueError):
    """Real execution prerequisites are incomplete or do not match the contract."""

    def __init__(self, findings: list[dict[str, str]]) -> None:
        self.findings = findings
        super().__init__("comparative preflight failed: " + ", ".join(f["code"] for f in findings))


def real_adapter_specs() -> dict[str, AdapterSpec]:
    """Return the fixed six-system comparison surface."""
    return {
        "grep-read": AdapterSpec("bounded-grep-read", None),
        "graphify-pinned": AdapterSpec("pinned-graphify", None),
        "llm-wiki-current": AdapterSpec("current-search", "BASE"),
        "evidence-graph-only": AdapterSpec("evidence-graph", "GRAPH"),
        "hybrid-retrieval": AdapterSpec("hybrid-retrieval", "HYBRID"),
        "adaptive-context-compiler": AdapterSpec("context-compiler", "GLOBAL"),
    }


def _read_bounded_json(path: Path, maximum: int, label: str) -> tuple[bytes, dict]:
    raw = read_stable_bytes(Path(path), maximum, label=label)

    def reject_constant(value: str) -> None:
        raise ValueError(f"non-finite JSON constant {value}")

    def reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict:
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON object key {key!r}")
            result[key] = value
        return result

    try:
        value = json.loads(
            raw.decode("utf-8", errors="strict"),
            object_pairs_hook=reject_duplicate_keys,
            parse_constant=reject_constant,
        )
    except (UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError(f"cannot load {label}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object")
    return raw, value


_CANONICAL_CLAIM_GATE = {
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
}


def _require_unique_contract_lists(contract: dict) -> None:
    _require_unique((adapter["id"] for adapter in contract["adapters"]), "adapter ids")
    _require_unique(contract["fairness"]["identical_inputs"], "identical inputs")
    _require_unique(contract["metrics"]["latency_summary"], "latency summary")
    _require_unique(contract["metrics"]["per_task_fields"], "metric fields")
    _require_unique(contract["public_claim_gate"]["hard_gates"], "hard gates")
    _require_unique(contract["statistics"]["agent_seeds"], "agent seeds")
    _require_unique(contract["statistics"]["pairing"]["key"], "pairing keys")


def _raise_on_first(checks: tuple) -> None:
    """Raise ValueError with the message of the first check that fails, in order."""
    for failed, message in checks:
        if failed():
            raise ValueError(message)


def _require_contract_sets(contract: dict) -> None:
    _raise_on_first(
        (
            (
                lambda: {adapter["id"] for adapter in contract["adapters"]} != ADAPTER_IDS,
                "comparative adapter set is incomplete",
            ),
            (
                lambda: set(contract["fairness"]["identical_inputs"]) != FAIRNESS_KEYS,
                "comparative contract does not require all identical inputs",
            ),
            (
                lambda: set(contract["metrics"]["per_task_fields"]) != METRIC_FIELDS,
                "comparative per-task metric ledger is incomplete",
            ),
        )
    )


def _require_pinned_graphify(contract: dict) -> None:
    graphify = contract["provenance"]["graphify"]
    if (
        graphify["commit"] != GRAPHIFY_COMMIT
        or graphify["dependency_lock"]["git_blob_sha1"] != GRAPHIFY_LOCK_BLOB
    ):
        raise ValueError("Graphify source or dependency lock is not pinned")


def _real_execution_available(contract: dict) -> bool:
    availability = contract["availability"]
    return bool(availability["gate_f_passed"] or availability["heavy_comparison_available"])


def _require_frozen_gate_and_availability(contract: dict) -> None:
    _raise_on_first(
        (
            (
                lambda: contract["public_claim_gate"] != _CANONICAL_CLAIM_GATE,
                "public claim gate differs from canonical Task 27 claim gate",
            ),
            (
                lambda: _real_execution_available(contract),
                "early comparative contract must keep real execution unavailable",
            ),
            (
                lambda: contract["provenance"]["configuration"]["sha256"] != configuration_fingerprint(contract),
                "comparative configuration fingerprint mismatch",
            ),
        )
    )


def load_contract(contract_path: Path | str, schema_path: Path | str) -> dict:
    """Load a canonical contract and reject weakened or incomplete semantics."""
    contract_path = Path(contract_path)
    schema_path = Path(schema_path)
    raw, contract = _read_bounded_json(contract_path, MAX_CONTRACT_BYTES, "comparative contract")
    _schema_raw, schema = _read_bounded_json(
        schema_path, MAX_SCHEMA_BYTES, "comparative schema"
    )
    validate_schema_object(contract, schema)
    if raw != canonical_json_bytes(contract) + b"\n":
        raise ValueError("comparative contract bytes are not canonical and frozen")
    _require_unique_contract_lists(contract)
    _require_contract_sets(contract)
    _require_pinned_graphify(contract)
    _require_frozen_gate_and_availability(contract)
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


def _require_unique(values, label: str) -> None:
    fingerprints = []
    for value in values:
        fingerprint = canonical_evidence_json_bytes(value)
        if fingerprint in fingerprints:
            raise ValueError(f"duplicate {label}")
        fingerprints.append(fingerprint)


def _canonical_number(value: float) -> object:
    if not math.isfinite(value):
        raise ValueError("canonical evidence numbers must be finite")
    if value.is_integer() and abs(value) <= 9_007_199_254_740_991:
        return int(value)
    return value


def _canonical_key(key: object) -> str:
    if not isinstance(key, str):
        raise TypeError("canonical evidence object keys must be strings")
    return unicodedata.normalize("NFC", key)


def _canonical_object(value: dict) -> dict:
    normalized: dict = {}
    for key, item in value.items():
        normalized_key = _canonical_key(key)
        if normalized_key in normalized:
            raise ValueError(f"normalized evidence-key collision: {normalized_key!r}")
        normalized[normalized_key] = _canonical_evidence_value(item)
    return normalized


def _canonical_scalar(value: object) -> tuple[bool, object]:
    """(handled, canonical form) for None, bool, int, float and str."""
    if value is None or isinstance(value, (bool, int)):
        return True, value
    return _canonical_float_or_text(value)


def _canonical_float_or_text(value: object) -> tuple[bool, object]:
    if isinstance(value, float):
        return True, _canonical_number(value)
    if isinstance(value, str):
        return True, unicodedata.normalize("NFC", value)
    return False, None


def _canonical_evidence_value(value: object) -> object:
    handled, canonical = _canonical_scalar(value)
    if handled:
        return canonical
    return _canonical_container(value)


def _canonical_container(value: object) -> object:
    if isinstance(value, list):
        return [_canonical_evidence_value(item) for item in value]
    if isinstance(value, dict):
        return _canonical_object(value)
    raise TypeError(f"canonical evidence does not permit {type(value).__name__} values")


def canonical_evidence_json_bytes(value: object) -> bytes:
    """Encode finite JSON numbers deterministically under the pinned CPython runtime."""
    normalized = _canonical_evidence_value(value)
    return json.dumps(
        normalized,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def evidence_sha256(value: object) -> str:
    return hashlib.sha256(canonical_evidence_json_bytes(value)).hexdigest()


def verify_runtime_provenance(
    contract: dict, *, real_mode: bool, observed: dict | None = None
) -> dict:
    expected = dict(contract["provenance"]["python"])
    observed = observed or {
        "implementation": platform.python_implementation(),
        "version": platform.python_version(),
    }
    matches = observed == expected
    if real_mode and not matches:
        raise ValueError(
            "runtime provenance does not match declared real-comparison Python"
        )
    return {
        "expected": expected,
        "matches_expected": matches,
        "observed": observed,
        "verification": "verified-real" if real_mode else "observed-not-enforced-smoke",
    }


def _validate_ledger_object(ledger: dict, schema: dict) -> dict:
    validate_schema_object(ledger, schema)
    canonical_evidence_json_bytes(ledger)
    failed = ledger["outcome"] == "failure"
    if failed != (ledger["failure"] is not None):
        raise ValueError("ledger failure object must exactly match failure outcome")
    if ledger["input_fingerprint"] != _fingerprint(ledger["inputs"]):
        raise ValueError("ledger input fingerprint mismatch")
    return ledger


def validate_ledger(ledger: dict, schema_path: Path | str = DEFAULT_LEDGER_SCHEMA) -> dict:
    """Validate one closed task ledger and its cross-field invariants."""
    _schema_raw, schema = _read_bounded_json(
        Path(schema_path), MAX_SCHEMA_BYTES, "ledger schema"
    )
    return _validate_ledger_object(ledger, schema)


def _require_ledger_set(ledgers: list[dict]) -> None:
    if {ledger["adapter_id"] for ledger in ledgers} != ADAPTER_IDS:
        raise ValueError("smoke report adapter ledger set is incomplete")
    if len({ledger["input_fingerprint"] for ledger in ledgers}) != 1:
        raise ValueError("smoke report adapters did not receive identical inputs")


def validate_report(
    report: dict,
    report_schema_path: Path | str = DEFAULT_REPORT_SCHEMA,
    ledger_schema_path: Path | str = DEFAULT_LEDGER_SCHEMA,
) -> dict:
    """Validate the closed smoke report and every embedded raw ledger."""
    report_schema_path = Path(report_schema_path)
    _schema_raw, report_schema = _read_bounded_json(
        report_schema_path, MAX_SCHEMA_BYTES, "smoke report schema"
    )
    _ledger_schema_raw, ledger_schema = _read_bounded_json(
        Path(ledger_schema_path), MAX_SCHEMA_BYTES, "ledger schema"
    )
    validate_schema_object(report, report_schema)
    canonical_evidence_json_bytes(report)
    ledgers = report["raw_task_ledgers"]
    for ledger in ledgers:
        _validate_ledger_object(ledger, ledger_schema)
    _require_unique(report["statistics"]["agent_seeds"], "report agent seeds")
    _require_unique((ledger["adapter_id"] for ledger in ledgers), "report adapter ids")
    _require_ledger_set(ledgers)
    return report


def _smoke_source_revision(adapter_id: str) -> dict:
    if adapter_id == "graphify-pinned":
        return {"kind": "git-commit", "value": GRAPHIFY_COMMIT}
    return {"kind": "unavailable", "value": None}


def _smoke_failure() -> dict:
    return {
        "category": "orchestration",
        "code": "real-adapter-disabled",
        "message": "Pinned Graphify is not executed by deterministic smoke.",
        "phase": "smoke",
        "retryable": False,
    }


def _smoke_ledger(
    contract: dict, adapter: dict, task: dict, fingerprint: str, unavailable_metrics: dict
) -> dict:
    failed = adapter["id"] == contract["smoke"]["intentional_failure_adapter"]
    return {
        "adapter_id": adapter["id"],
        "adapter_provenance": {
            "configuration_sha256": contract["provenance"]["configuration"]["sha256"],
            "implementation_status": adapter["implementation_status"],
            "source_revision": _smoke_source_revision(adapter["id"]),
        },
        "attempt": 1,
        "failure": _smoke_failure() if failed else None,
        "input_fingerprint": fingerprint,
        "inputs": task["inputs"],
        "metrics": dict(unavailable_metrics),
        "outcome": "failure" if failed else "orchestration-pass",
        "seed": contract["statistics"]["agent_seeds"][0],
        "task_id": task["id"],
    }


def run_smoke(contract: dict) -> dict:
    """Exercise every adapter ledger shape without running any real backend."""
    task = contract["tasks"][0]
    fingerprint = _fingerprint(task["inputs"])
    bound_fields = contract["public_claim_gate"]["consumed_report_fields"]
    runtime_provenance = verify_runtime_provenance(contract, real_mode=False)
    unavailable_metrics = {field: None for field in sorted(METRIC_FIELDS)}
    ledgers = [
        _smoke_ledger(contract, adapter, task, fingerprint, unavailable_metrics)
        for adapter in contract["adapters"]
    ]
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
        "runtime_provenance": runtime_provenance,
        "schema_version": "comparative-smoke-report/v1",
        "statistics": {
            "agent_seeds": contract["statistics"]["agent_seeds"],
            "claim_gating_method": contract["statistics"]["claim_gating_method"],
            "computed": False,
            "reason": "real paired observations unavailable",
        },
    }


def _command_available(command: list[str]) -> bool:
    executable = command[0] if command else ""
    if not executable:
        return False
    candidate = Path(executable)
    return candidate.is_file() if candidate.is_absolute() else shutil.which(executable) is not None


def _run_probe(command_runner, command: list[str], *, cwd: Path | None = None):
    kwargs = {"capture_output": True, "text": True, "check": False, "timeout": 30}
    if cwd is not None:
        kwargs["cwd"] = cwd
    return command_runner(command, **kwargs)


def _finding(code: str, message: str) -> dict[str, str]:
    return {"code": code, "message": message}


def _artifact_path(raw: object, base: Path) -> Path:
    artifact_path = Path(raw)
    if not artifact_path.is_absolute():
        return base / artifact_path
    return artifact_path


def _artifact_verified(artifact: object, base: Path) -> bool:
    if not isinstance(artifact, dict) or set(artifact) != {"path", "sha256"}:
        return False
    artifact_path = _artifact_path(artifact["path"], base)
    if not artifact_path.is_file():
        return False
    return hashlib.sha256(artifact_path.read_bytes()).hexdigest() == artifact["sha256"]


def _evidence_shape_complete(evidence: dict) -> bool:
    if set(evidence) != {"artifacts", "checks", "passed", "schema_version"}:
        return False
    if evidence["schema_version"] != "gate-f-evidence/v1" or evidence["passed"] is not True:
        return False
    return evidence["checks"] == {name: True for name in sorted(GATE_F_CHECKS)}


def _artifacts_verified(evidence: dict, base: Path) -> bool:
    artifacts = evidence["artifacts"]
    if not isinstance(artifacts, list) or not artifacts:
        return False
    return all(_artifact_verified(artifact, base) for artifact in artifacts)


def _gate_f_evidence_complete(path: Path) -> bool:
    try:
        _raw, evidence = _read_bounded_json(path, MAX_MANIFEST_BYTES, "Gate F evidence")
        if not _evidence_shape_complete(evidence):
            return False
        return _artifacts_verified(evidence, path.parent)
    except (OSError, TypeError, UnicodeError, ValueError, RecursionError):
        return False


_REAL_MANIFEST_REQUIRED = frozenset(
    {
        "adapters",
        "context_budget",
        "gate_f",
        "graphify",
        "hardware",
        "limits",
        "model",
        "repository",
        "retry_policy",
        "schema_version",
        "seeds",
        "tasks",
    }
)


def _manifest_fields_known(manifest: object) -> bool:
    if not isinstance(manifest, dict):
        return False
    return not (set(manifest) - (_REAL_MANIFEST_REQUIRED | {"hard_gates"}))


def _manifest_incomplete(manifest: dict) -> bool:
    return not _REAL_MANIFEST_REQUIRED <= set(manifest) or manifest.get("schema_version") != "comparative-run/v1"


def _require_manifest_shape(manifest: object) -> None:
    _raise_on_first(
        (
            (lambda: not _manifest_fields_known(manifest), "real manifest has unknown fields"),
            (
                lambda: _manifest_incomplete(manifest),
                "real manifest is incomplete or has the wrong schema version",
            ),
            (lambda: set(manifest["adapters"]) != ADAPTER_IDS, "real manifest adapter set is incomplete"),
        )
    )


def _non_empty_str(value: object) -> bool:
    return isinstance(value, str) and bool(value)


def _bounded_str(value: object, low: int, high: int) -> bool:
    return isinstance(value, str) and low <= len(value) <= high


def _valid_commit(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{40}", value) is not None


def _repository_fields_valid(repository: dict) -> bool:
    if not _non_empty_str(repository["path"]) or not _bounded_str(repository["url"], 3, 500):
        return False
    return _valid_commit(repository["commit"])


def _require_manifest_repository(repository: object) -> None:
    if not isinstance(repository, dict) or set(repository) != {"commit", "path", "url"}:
        raise ValueError("real manifest repository is invalid")
    if not _repository_fields_valid(repository):
        raise ValueError("real manifest repository is invalid")


def _require_manifest_graphify(graphify: object) -> None:
    if not isinstance(graphify, dict) or set(graphify) != {"path"} or not _non_empty_str(graphify["path"]):
        raise ValueError("real manifest Graphify path is invalid")


def _all_non_empty_strings(values) -> bool:
    return all(isinstance(part, str) and part for part in values)


def _non_empty_string_list(values: object) -> bool:
    return isinstance(values, list) and bool(values) and _all_non_empty_strings(values)


def _model_valid(model: object) -> bool:
    if not isinstance(model, dict) or set(model) != {"id", "probe_command"}:
        return False
    if not _bounded_str(model["id"], 3, 200):
        return False
    return _non_empty_string_list(model["probe_command"])


def _positive_int_within(value: object, low: int, high: int) -> bool:
    if isinstance(value, bool) or not isinstance(value, int):
        return False
    return low <= value <= high


def _positive_int(value: object) -> bool:
    if isinstance(value, bool) or not isinstance(value, int):
        return False
    return value > 0


def _require_hardware_and_budget(manifest: dict) -> None:
    if not _bounded_str(manifest["hardware"], 3, 200) or not _positive_int_within(
        manifest["context_budget"], 1, 1_000_000
    ):
        raise ValueError("real manifest hardware or context budget is invalid")


def _task_valid(task: object) -> bool:
    if not isinstance(task, dict) or set(task) != {"id", "task"}:
        return False
    if not isinstance(task["id"], str) or re.fullmatch(r"[a-z0-9][a-z0-9-]{2,127}", task["id"]) is None:
        return False
    return _bounded_str(task["task"], 1, 2000)


def _tasks_bounded(tasks: object) -> bool:
    return isinstance(tasks, list) and bool(tasks) and len(tasks) <= MAX_REAL_TASKS


def _require_manifest_tasks(tasks: object) -> None:
    if not _tasks_bounded(tasks):
        raise ValueError("real manifest requires at least one task")
    for task in tasks:
        if not _task_valid(task):
            raise ValueError("real manifest task is invalid")
    _require_unique([task["id"] for task in tasks], "real task ids")


def _require_manifest_seeds(seeds: object) -> None:
    if not isinstance(seeds, list) or len(seeds) < 3:
        raise ValueError("real manifest requires multiple seeds")
    _require_unique(seeds, "real seeds")
    if any(not _positive_int_within(seed, 0, 2_147_483_647) for seed in seeds):
        raise ValueError("real manifest seeds are invalid")


def _retry_policy_valid(retry: dict) -> bool:
    return (
        set(retry) == {"backoff", "max_attempts"}
        and retry["backoff"] in {"none", "fixed", "exponential"}
        and 1 <= retry["max_attempts"] <= 10
    )


def _require_retry_policy(retry: dict) -> None:
    if not _retry_policy_valid(retry):
        raise ValueError("real manifest retry policy is invalid")


def _limits_within_hard_bounds(limits: dict) -> bool:
    if limits["max_stdout_bytes"] > 16 * 1024 * 1024 or limits["max_stderr_bytes"] > 16 * 1024 * 1024:
        return False
    return limits["timeout_seconds"] <= 3600


def _require_limits(limits: dict) -> None:
    _raise_on_first(
        (
            (
                lambda: set(limits) != {"max_stderr_bytes", "max_stdout_bytes", "timeout_seconds"},
                "real manifest limits are incomplete",
            ),
            (
                lambda: any(not _positive_int(value) for value in limits.values()),
                "real manifest limits must be positive integers",
            ),
            (lambda: not _limits_within_hard_bounds(limits), "real manifest limits exceed hard bounds"),
        )
    )


def _gate_f_fields_valid(gate_f: dict) -> bool:
    return (
        _non_empty_str(gate_f["evidence_path"])
        and re.fullmatch(r"[0-9a-f]{64}", gate_f["evidence_sha256"]) is not None
        and isinstance(gate_f["passed"], bool)
    )


def _gate_f_valid(gate_f: object) -> bool:
    if not isinstance(gate_f, dict) or set(gate_f) != {"evidence_path", "evidence_sha256", "passed"}:
        return False
    return _gate_f_fields_valid(gate_f)


def _hard_gates_valid(hard_gates: object) -> bool:
    if hard_gates is None:
        return True
    if not isinstance(hard_gates, dict) or set(hard_gates) != {"crash", "evidence", "freshness"}:
        return False
    return all(isinstance(value, bool) for value in hard_gates.values())


def _command_list_valid(command: object, max_length: int) -> bool:
    return _non_empty_string_list(command) and len(command) <= max_length


def _env_list_valid(names: object) -> bool:
    if not isinstance(names, list) or len(names) > 64:
        return False
    return _all_non_empty_strings(names)


def _require_adapter_config(adapter_id: str, config: dict) -> None:
    _raise_on_first(
        (
            (lambda: set(config) != {"command", "required_env"}, f"adapter {adapter_id} configuration is not closed"),
            (lambda: not _command_list_valid(config["command"], 32), f"adapter {adapter_id} command is invalid"),
            (
                lambda: not _env_list_valid(config["required_env"]),
                f"adapter {adapter_id} environment list is invalid",
            ),
        )
    )


def _require_model(model: object) -> None:
    if not _model_valid(model):
        raise ValueError("real manifest model is invalid")


def _require_declared_gates(manifest: dict) -> None:
    if not _gate_f_valid(manifest["gate_f"]):
        raise ValueError("real manifest Gate F declaration is invalid")
    if not _hard_gates_valid(manifest.get("hard_gates")):
        raise ValueError("real manifest hard gates are invalid")


def _validate_real_manifest(manifest: dict) -> None:
    _require_manifest_shape(manifest)
    _require_manifest_repository(manifest["repository"])
    _require_manifest_graphify(manifest["graphify"])
    _require_model(manifest["model"])
    _require_hardware_and_budget(manifest)
    _require_manifest_tasks(manifest["tasks"])
    _require_manifest_seeds(manifest["seeds"])
    _require_retry_policy(manifest["retry_policy"])
    _require_limits(manifest["limits"])
    _require_declared_gates(manifest)
    for adapter_id, config in manifest["adapters"].items():
        _require_adapter_config(adapter_id, config)


def load_real_manifest(path: Path | str) -> dict:
    """Load one bounded real-run manifest from stable captured bytes."""
    _raw, manifest = _read_bounded_json(Path(path), MAX_MANIFEST_BYTES, "real run manifest")
    _validate_real_manifest(manifest)
    return manifest


def _python_findings(contract: dict) -> list[dict[str, str]]:
    expected_python = contract["provenance"]["python"]
    observed_python = {
        "implementation": platform.python_implementation(),
        "version": platform.python_version(),
    }
    if observed_python != expected_python:
        return [_finding("python-runtime-mismatch", "Python runtime is not the pinned version")]
    return []


def _repository_findings(command_runner, repository: dict) -> list[dict[str, str]]:
    try:
        result = _run_probe(command_runner, ["git", "rev-parse", "HEAD"], cwd=Path(repository["path"]))
        if result.returncode != 0 or result.stdout.strip() != repository["commit"]:
            raise ValueError
    except (OSError, subprocess.SubprocessError, ValueError):
        return [_finding("repository-commit-unverified", "Repository HEAD does not match")]
    return []


def _append_graphify_probe_findings(command_runner, graphify_path: Path, findings: list) -> None:
    head = _run_probe(command_runner, ["git", "rev-parse", "HEAD"], cwd=graphify_path)
    if head.returncode != 0 or head.stdout.strip() != GRAPHIFY_COMMIT:
        findings.append(_finding("graphify-commit-mismatch", "Graphify HEAD is not pinned"))
    lock = _run_probe(command_runner, ["git", "hash-object", "uv.lock"], cwd=graphify_path)
    if lock.returncode != 0 or lock.stdout.strip() != GRAPHIFY_LOCK_BLOB:
        findings.append(_finding("graphify-lock-mismatch", "Graphify uv.lock blob is not pinned"))


def _append_graphify_findings(command_runner, graphify_path: Path, findings: list) -> None:
    if not graphify_path.is_dir() or not (graphify_path / "uv.lock").is_file():
        findings.append(
            _finding("graphify-checkout-unavailable", "Pinned Graphify checkout and uv.lock are required")
        )
        return
    try:
        _append_graphify_probe_findings(command_runner, graphify_path, findings)
    except (OSError, subprocess.SubprocessError):
        findings.append(_finding("graphify-probe-failed", "Graphify provenance could not be read"))


def _model_probe_findings(probe, model: dict) -> list[dict[str, str]]:
    if probe.returncode != 0:
        return [_finding("model-unavailable", "Model identity probe failed")]
    if probe.stdout.strip() != model.get("id"):
        return [_finding("model-identity-mismatch", "Model identity is not exact")]
    return []


def _append_model_findings(command_runner, model: dict, findings: list) -> None:
    probe_command = model.get("probe_command")
    if not isinstance(probe_command, list) or not probe_command or not _command_available(probe_command):
        findings.append(_finding("model-unavailable", "Model identity probe is unavailable"))
        return
    try:
        probe = _run_probe(command_runner, probe_command)
    except (OSError, subprocess.SubprocessError):
        findings.append(_finding("model-unavailable", "Model identity probe failed"))
        return
    findings.extend(_model_probe_findings(probe, model))


def _adapter_config_findings(adapter_id: str, config: dict, environ: dict[str, str]) -> list:
    findings = []
    if not _command_available(config["command"]):
        findings.append(_finding("adapter-command-unavailable", f"{adapter_id} command is unavailable"))
    missing = [name for name in config["required_env"] if not environ.get(name)]
    if missing:
        findings.append(
            _finding(
                "required-environment-unavailable",
                f"{adapter_id} requires {','.join(sorted(missing))}",
            )
        )
    return findings


def _append_adapter_findings(manifest: dict, environ: dict[str, str], findings: list) -> None:
    for adapter_id, config in manifest["adapters"].items():
        findings.extend(_adapter_config_findings(adapter_id, config, environ))


def _gate_f_evidence_findings(evidence_path: Path, gate_f: dict) -> list[dict[str, str]]:
    digest = hashlib.sha256(evidence_path.read_bytes()).hexdigest()
    if digest != gate_f.get("evidence_sha256"):
        return [_finding("gate-f-evidence-mismatch", "Gate F evidence hash differs")]
    if not _gate_f_evidence_complete(evidence_path):
        return [_finding("gate-f-evidence-invalid", "Gate F evidence is incomplete or unverifiable")]
    return []


def _append_gate_f_findings(gate_f: dict, findings: list) -> None:
    evidence_path = Path(gate_f.get("evidence_path", ""))
    if not gate_f.get("passed"):
        findings.append(_finding("gate-f-unavailable", "Gate F is not recorded as passed"))
    if not evidence_path.is_file():
        findings.append(_finding("gate-f-evidence-unavailable", "Gate F evidence file is unavailable"))
        return
    findings.extend(_gate_f_evidence_findings(evidence_path, gate_f))


def _hard_gate_findings(manifest: dict) -> list[dict[str, str]]:
    if manifest.get("hard_gates") != {"crash": True, "evidence": True, "freshness": True}:
        return [_finding("hard-gates-unavailable", "All hard gates require explicit evidence")]
    return []


def preflight_real_run(
    contract: dict,
    manifest: dict,
    *,
    environ: dict[str, str] | None = None,
    command_runner=subprocess.run,
) -> dict:
    """Verify every external prerequisite; return nothing runnable on mismatch."""
    _validate_real_manifest(manifest)
    if manifest["seeds"] != contract["statistics"]["agent_seeds"]:
        raise ValueError("real manifest differs from frozen agent seeds")
    environ = dict(os.environ if environ is None else environ)
    findings = _python_findings(contract)
    findings.extend(_repository_findings(command_runner, manifest["repository"]))
    _append_graphify_findings(command_runner, Path(manifest["graphify"]["path"]), findings)
    _append_model_findings(command_runner, manifest["model"], findings)
    _append_adapter_findings(manifest, environ, findings)
    _append_gate_f_findings(manifest["gate_f"], findings)
    findings.extend(_hard_gate_findings(manifest))
    if findings:
        raise PreflightError(findings)
    return {
        "gate_f_evidence_sha256": manifest["gate_f"]["evidence_sha256"],
        "graphify_commit": GRAPHIFY_COMMIT,
        "model": manifest["model"]["id"],
        "repository_commit": manifest["repository"]["commit"],
        "status": "ready",
    }


class _Sha256CounterRng:
    def __init__(self, seed_hex: str) -> None:
        self.seed = bytes.fromhex(seed_hex)
        self.counter = 0

    def index(self, stop: int) -> int:
        if stop <= 0:
            raise ValueError("RNG stop must be positive")
        block = hashlib.sha256(self.seed + self.counter.to_bytes(16, "big")).digest()
        self.counter += 1
        return int.from_bytes(block, "big") % stop


def _quality(ledger: dict) -> float:
    if ledger["outcome"] == "failure":
        return 0.0
    return _metric_quality(ledger["metrics"])


def _metric_quality(metrics: dict) -> float:
    executable = metrics["executable_task_success"]
    if executable is not None:
        return 1.0 if executable else 0.0
    factual = metrics["blinded_factual_correctness"]
    if factual is None:
        raise ValueError("missing quality metric")
    return float(factual)


def _tokens(ledger: dict) -> int:
    fields = ("uncached_input_tokens", "uncached_output_tokens", "cache_tokens")
    values = [ledger["metrics"][field] for field in fields]
    if any(value is None for value in values):
        raise ValueError("missing token metric")
    return sum(int(value) for value in values)


def _percentile(values: list[float], basis_points: int) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * basis_points / 10000
    low = math.floor(position)
    high = math.ceil(position)
    if low == high:
        return ordered[low]
    fraction = position - low
    return ordered[low] * (1 - fraction) + ordered[high] * fraction


def _attempts_by_key(ledgers: list[dict], candidate_id: str) -> dict[tuple[str, str, int], list[dict]]:
    attempts: dict[tuple[str, str, int], list[dict]] = {}
    for ledger in ledgers:
        if ledger["adapter_id"] not in {candidate_id, "graphify-pinned"}:
            continue
        key = (ledger["adapter_id"], ledger["task_id"], ledger["seed"])
        attempts.setdefault(key, []).append(ledger)
    return attempts


def _seeds_for(attempts: dict, adapter_id: str, task_id: str) -> set[int]:
    return {key[2] for key in attempts if key[:2] == (adapter_id, task_id)}


def _pair_row(attempts: dict, candidate_id: str, task_id: str, seed: int) -> tuple[float, int, int]:
    """(quality difference, candidate tokens, baseline tokens) for one task and seed."""
    candidate_attempts = sorted(attempts[(candidate_id, task_id, seed)], key=lambda x: x["attempt"])
    baseline_attempts = sorted(attempts[("graphify-pinned", task_id, seed)], key=lambda x: x["attempt"])
    return (
        _quality(candidate_attempts[-1]) - _quality(baseline_attempts[-1]),
        sum(_tokens(item) for item in candidate_attempts),
        sum(_tokens(item) for item in baseline_attempts),
    )


def _task_cluster(attempts: dict, candidate_id: str, task_id: str) -> list[tuple[float, int, int]]:
    candidate_seeds = _seeds_for(attempts, candidate_id, task_id)
    baseline_seeds = _seeds_for(attempts, "graphify-pinned", task_id)
    if candidate_seeds != baseline_seeds or not candidate_seeds:
        raise ValueError("candidate and baseline must have identical seed sets")
    return [_pair_row(attempts, candidate_id, task_id, seed) for seed in sorted(candidate_seeds)]


def _paired_clusters(ledgers: list[dict], candidate_id: str) -> list[list[tuple[float, int, int]]]:
    attempts = _attempts_by_key(ledgers, candidate_id)
    tasks = sorted({key[1] for key in attempts})
    clusters = [_task_cluster(attempts, candidate_id, task_id) for task_id in tasks]
    if not clusters:
        raise ValueError("paired observations are unavailable")
    return clusters


def _cluster_mean(cluster: list[tuple[float, int, int]]) -> float:
    return sum(row[0] for row in cluster) / len(cluster)


def _token_totals(rows: list[tuple[float, int, int]]) -> tuple[int, int]:
    return sum(row[1] for row in rows), sum(row[2] for row in rows)


def _observed(clusters: list[list[tuple[float, int, int]]]) -> tuple[float, float]:
    """(observed quality difference, observed token ratio) over every cluster."""
    observed_quality = sum(_cluster_mean(cluster) for cluster in clusters) / len(clusters)
    candidate_tokens, baseline_tokens = _token_totals([row for cluster in clusters for row in cluster])
    if baseline_tokens == 0:
        raise ValueError("zero Graphify token denominator")
    return observed_quality, candidate_tokens / baseline_tokens


def _bootstrap_sample(rng: _Sha256CounterRng, clusters: list) -> tuple[float, float]:
    """(mean quality difference, token ratio) of one cluster-then-row resample."""
    selected_clusters = [clusters[rng.index(len(clusters))] for _ in clusters]
    quality_total = 0.0
    candidate_tokens = 0
    baseline_tokens = 0
    for cluster in selected_clusters:
        selected_rows = [cluster[rng.index(len(cluster))] for _ in cluster]
        quality_total += _cluster_mean(selected_rows)
        sampled_candidate, sampled_baseline = _token_totals(selected_rows)
        candidate_tokens += sampled_candidate
        baseline_tokens += sampled_baseline
    if baseline_tokens == 0:
        raise ValueError("zero Graphify token denominator")
    return quality_total / len(selected_clusters), candidate_tokens / baseline_tokens


def _bootstrap_distributions(
    rng: _Sha256CounterRng, clusters: list, resamples: int
) -> tuple[list[float], list[float]]:
    quality_distribution: list[float] = []
    ratio_distribution: list[float] = []
    for _ in range(resamples):
        quality, ratio = _bootstrap_sample(rng, clusters)
        quality_distribution.append(quality)
        ratio_distribution.append(ratio)
    return quality_distribution, ratio_distribution


def _sign(rng: _Sha256CounterRng) -> int:
    return -1 if rng.index(2) else 1


def _sign_flip_extremes(
    rng: _Sha256CounterRng, cluster_differences: list[float], observed_quality: float, permutations: int
) -> int:
    extreme = 0
    for _ in range(permutations):
        value = sum(difference * _sign(rng) for difference in cluster_differences) / len(cluster_differences)
        if abs(value) >= abs(observed_quality) - 1e-15:
            extreme += 1
    return extreme


def compute_paired_statistics(contract: dict, ledgers: list[dict], *, candidate_id: str) -> dict:
    """Compute the frozen cluster/seed paired bootstrap and sign-flip diagnostic."""
    if candidate_id == "graphify-pinned" or candidate_id not in ADAPTER_IDS:
        raise ValueError("candidate must be a non-Graphify canonical adapter")
    clusters = _paired_clusters(ledgers, candidate_id)
    observed_quality, observed_ratio = _observed(clusters)
    settings = contract["statistics"]
    resamples = settings["resampling"]["resamples"]
    quality_distribution, ratio_distribution = _bootstrap_distributions(
        _Sha256CounterRng(settings["rng"]["bootstrap_seed_hex"]), clusters, resamples
    )
    cluster_differences = [_cluster_mean(cluster) for cluster in clusters]
    extreme = _sign_flip_extremes(
        _Sha256CounterRng(settings["rng"]["randomization_seed_hex"]),
        cluster_differences,
        observed_quality,
        resamples,
    )
    return {
        "candidate_id": candidate_id,
        "quality_difference": observed_quality,
        "quality_difference_lower_confidence_bound": _percentile(
            quality_distribution, settings["interval"]["quality_quantile_basis_points"]
        ),
        "randomization_p_value": (extreme + 1) / (resamples + 1),
        "resamples": resamples,
        "token_ratio": observed_ratio,
        "token_ratio_upper_confidence_bound": _percentile(
            ratio_distribution, settings["interval"]["token_ratio_quantile_basis_points"]
        ),
    }


def _quality_condition(quality: float | None, gate: dict) -> str | None:
    if quality is None:
        return "quality-confidence-interval-unavailable"
    if 10000 * quality <= gate["quality_lower_bound_basis_points_strictly_greater_than"]:
        return "quality-bound-not-met"
    return None


def _ratio_condition(ratio: float | None, gate: dict) -> str | None:
    if ratio is None:
        return "token-ratio-confidence-interval-unavailable"
    if 10000 * ratio >= gate["token_ratio_upper_bound_basis_points_strictly_less_than"]:
        return "token-ratio-bound-not-met"
    return None


def _hard_gates_condition(hard_gates: dict[str, bool], gate: dict) -> str | None:
    if set(hard_gates) != set(gate["hard_gates"]) or not all(hard_gates.values()):
        return "hard-gates-not-passed"
    return None


def evaluate_public_claim_gate(
    contract: dict,
    statistics: dict,
    *,
    gate_f_passed: bool,
    real_evidence_complete: bool,
    hard_gates: dict[str, bool],
) -> dict:
    """Apply every frozen claim condition; missing evidence always closes the gate."""
    failed = []
    if not gate_f_passed:
        failed.append("gate-f-not-passed")
    if not real_evidence_complete:
        failed.append("real-comparative-evidence-unavailable")
    quality = statistics.get("quality_difference_lower_confidence_bound")
    ratio = statistics.get("token_ratio_upper_confidence_bound")
    gate = contract["public_claim_gate"]
    conditions = (
        _quality_condition(quality, gate),
        _ratio_condition(ratio, gate),
        _hard_gates_condition(hard_gates, gate),
    )
    failed.extend(condition for condition in conditions if condition is not None)
    return {
        "eligible": not failed,
        "failed_conditions": failed,
        "gate_f_passed": gate_f_passed,
        "hard_gates": hard_gates,
        "quality_difference_lower_confidence_bound": quality,
        "real_evidence_complete": real_evidence_complete,
        "token_ratio_upper_confidence_bound": ratio,
    }


def _failure_result(code: str, message: str, *, phase: str, retryable: bool) -> dict:
    return {
        "failure": {
            "category": "orchestration",
            "code": code,
            "message": " ".join(message.split())[:500],
            "phase": phase,
            "retryable": retryable,
        },
        "metrics": {field: None for field in sorted(METRIC_FIELDS)},
        "outcome": "failure",
    }


def _run_adapter_command(command: list[str], request: dict, limits: dict):
    """The completed process, or the failure result when it could not run."""
    try:
        return subprocess.run(
            command,
            input=canonical_evidence_json_bytes(request),
            capture_output=True,
            check=False,
            timeout=limits["timeout_seconds"],
        )
    except subprocess.TimeoutExpired:
        return _failure_result("adapter-timeout", "adapter deadline exceeded", phase="query", retryable=True)
    except OSError as exc:
        return _failure_result("adapter-crash", str(exc), phase="setup", retryable=False)


def _adapter_output_failure(completed, limits: dict) -> dict | None:
    if len(completed.stdout) > limits["max_stdout_bytes"] or len(completed.stderr) > limits["max_stderr_bytes"]:
        return _failure_result("adapter-output-limit", "adapter output exceeded limit", phase="query", retryable=False)
    if completed.returncode != 0:
        return _failure_result(
            "adapter-nonzero-exit",
            completed.stderr.decode("utf-8", errors="replace") or f"exit {completed.returncode}",
            phase="query",
            retryable=True,
        )
    return None


def _parsed_adapter_result(stdout: bytes) -> dict:
    result = json.loads(stdout.decode("utf-8", errors="strict"))
    if set(result) != {"failure", "metrics", "outcome"} or set(result["metrics"]) != METRIC_FIELDS:
        raise ValueError("adapter result shape is not closed")
    canonical_evidence_json_bytes(result)
    if (result["outcome"] == "failure") != (result["failure"] is not None):
        raise ValueError("adapter failure does not match outcome")
    return result


def _invoke_adapter(command: list[str], request: dict, limits: dict) -> dict:
    completed = _run_adapter_command(command, request, limits)
    if isinstance(completed, dict):
        return completed
    failure = _adapter_output_failure(completed, limits)
    if failure is not None:
        return failure
    try:
        return _parsed_adapter_result(completed.stdout)
    except (UnicodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        return _failure_result("adapter-invalid-output", str(exc), phase="evaluation", retryable=False)


class _RunContext:
    """One `execute_comparison`: its contract, manifest, adapter specs and the ledgers so far."""

    def __init__(self, contract: dict, manifest: dict, specs: dict, ledger_schema: dict) -> None:
        self.contract = contract
        self.manifest = manifest
        self.specs = specs
        self.ledger_schema = ledger_schema
        self.repository = manifest["repository"]
        self.raw_ledgers: list[dict] = []


def _task_inputs(manifest: dict, task: dict) -> dict:
    repository = manifest["repository"]
    return {
        "commit": repository["commit"],
        "context_budget": manifest["context_budget"],
        "hardware": manifest["hardware"],
        "model": manifest["model"]["id"],
        "repository": repository["url"],
        "retry_policy": manifest["retry_policy"],
        "task": task["task"],
    }


def _adapter_request(specs: dict, adapter_id: str, attempt: int, inputs: dict, seed: int, task_id: str) -> dict:
    return {
        "adapter": {
            "backend": specs[adapter_id].backend,
            "id": adapter_id,
            "max_results": specs[adapter_id].max_results,
            "profile": specs[adapter_id].profile,
        },
        "attempt": attempt,
        "inputs": inputs,
        "schema_version": "comparative-adapter-input/v1",
        "seed": seed,
        "task_id": task_id,
    }


def _source_revision_value(adapter_id: str, repository_commit: str) -> str:
    if adapter_id == "graphify-pinned":
        return GRAPHIFY_COMMIT
    return repository_commit


def _ledger_base(
    ctx: _RunContext, adapter_id: str, attempt: int, fingerprint: str, inputs: dict, seed: int, task_id: str
) -> dict:
    return {
        "adapter_id": adapter_id,
        "adapter_provenance": {
            "configuration_sha256": ctx.contract["provenance"]["configuration"]["sha256"],
            "implementation_status": "implemented-pinned",
            "source_revision": {
                "kind": "git-commit",
                "value": _source_revision_value(adapter_id, ctx.repository["commit"]),
            },
        },
        "attempt": attempt,
        "input_fingerprint": fingerprint,
        "inputs": inputs,
        "seed": seed,
        "task_id": task_id,
    }


def _ledger_with_result(ledger_base: dict, result: dict) -> dict:
    return {
        **ledger_base,
        "failure": result["failure"],
        "metrics": result["metrics"],
        "outcome": result["outcome"],
    }


def _validated_ledger(ledger_base: dict, result: dict, ledger_schema: dict) -> tuple[dict, dict]:
    """(ledger, result) after validation; an invalid ledger becomes a failure ledger."""
    ledger = _ledger_with_result(ledger_base, result)
    try:
        _validate_ledger_object(ledger, ledger_schema)
    except (TypeError, ValueError) as exc:
        result = _failure_result("adapter-invalid-metrics", str(exc), phase="evaluation", retryable=False)
        ledger = _ledger_with_result(ledger_base, result)
    return ledger, result


def _retry_done(result: dict) -> bool:
    return result["outcome"] != "failure" or not result["failure"]["retryable"]


def _run_attempts(ctx: _RunContext, task: dict, inputs: dict, fingerprint: str, seed: int, adapter_id: str) -> None:
    config = ctx.manifest["adapters"][adapter_id]
    for attempt in range(1, ctx.manifest["retry_policy"]["max_attempts"] + 1):
        request = _adapter_request(ctx.specs, adapter_id, attempt, inputs, seed, task["id"])
        result = _invoke_adapter(config["command"], request, ctx.manifest["limits"])
        base = _ledger_base(ctx, adapter_id, attempt, fingerprint, inputs, seed, task["id"])
        ledger, result = _validated_ledger(base, result, ctx.ledger_schema)
        ctx.raw_ledgers.append(ledger)
        if _retry_done(result):
            return


def _run_task(ctx: _RunContext, task: dict) -> None:
    inputs = _task_inputs(ctx.manifest, task)
    fingerprint = _fingerprint(inputs)
    for seed in ctx.manifest["seeds"]:
        for adapter_id in sorted(ADAPTER_IDS):
            _run_attempts(ctx, task, inputs, fingerprint, seed, adapter_id)


def _paired_statistics_or_reason(contract: dict, raw_ledgers: list[dict]) -> tuple[dict, str | None]:
    try:
        statistics = compute_paired_statistics(contract, raw_ledgers, candidate_id="adaptive-context-compiler")
    except (TypeError, ValueError) as exc:
        reason = " ".join(str(exc).split())[:500]
        return {
            "candidate_id": "adaptive-context-compiler",
            "computed": False,
            "quality_difference_lower_confidence_bound": None,
            "reason": reason,
            "token_ratio_upper_confidence_bound": None,
        }, reason
    statistics["computed"] = True
    return statistics, None


def _evidence_complete(raw_ledgers: list[dict], expected_terminal: int) -> bool:
    terminal: dict[tuple[str, str, int], dict] = {}
    for ledger in raw_ledgers:
        terminal[(ledger["adapter_id"], ledger["task_id"], ledger["seed"])] = ledger
    if len(terminal) != expected_terminal:
        return False
    return all(ledger["outcome"] != "failure" for ledger in terminal.values())


def _latency_summary(raw_ledgers: list[dict], adapter_id: str) -> dict:
    latencies = [
        float(ledger["metrics"]["query_latency_ms"])
        for ledger in raw_ledgers
        if ledger["adapter_id"] == adapter_id and ledger["metrics"]["query_latency_ms"] is not None
    ]
    if not latencies:
        return {"query_latency_ms": {"p50": None, "p95": None}}
    return {"query_latency_ms": {"p50": _percentile(latencies, 5000), "p95": _percentile(latencies, 9500)}}


def _claim_gate(
    contract: dict, manifest: dict, statistics: dict, statistics_error: str | None, complete: bool, fixture_mode: bool
) -> dict:
    hard_gates = manifest.get("hard_gates", {"crash": False, "evidence": False, "freshness": False})
    claim_gate = evaluate_public_claim_gate(
        contract,
        statistics,
        gate_f_passed=bool(manifest["gate_f"]["passed"]) and not fixture_mode,
        real_evidence_complete=complete and not fixture_mode,
        hard_gates=hard_gates,
    )
    if statistics_error is not None:
        claim_gate["failed_conditions"].append("statistics-unavailable")
        claim_gate["eligible"] = False
    return claim_gate


def _comparison_mode(fixture_mode: bool) -> str:
    if fixture_mode:
        return "deterministic-adapter-integration-fixture"
    return "real-bounded-comparison"


def execute_comparison(contract: dict, manifest: dict, *, fixture_mode: bool = False) -> dict:
    """Execute bounded adapters and retain every attempt, including retries."""
    _validate_real_manifest(manifest)
    if manifest["seeds"] != contract["statistics"]["agent_seeds"]:
        raise ValueError("real manifest differs from frozen agent seeds")
    if not fixture_mode:
        preflight_real_run(contract, manifest)
    _ledger_schema_raw, ledger_schema = _read_bounded_json(
        DEFAULT_LEDGER_SCHEMA, MAX_SCHEMA_BYTES, "ledger schema"
    )
    ctx = _RunContext(contract, manifest, real_adapter_specs(), ledger_schema)
    for task in manifest["tasks"]:
        _run_task(ctx, task)
    statistics, statistics_error = _paired_statistics_or_reason(contract, ctx.raw_ledgers)
    expected_terminal = len(ADAPTER_IDS) * len(manifest["tasks"]) * len(manifest["seeds"])
    claim_gate = _claim_gate(
        contract,
        manifest,
        statistics,
        statistics_error,
        _evidence_complete(ctx.raw_ledgers, expected_terminal),
        fixture_mode,
    )
    metric_summaries = {
        adapter_id: _latency_summary(ctx.raw_ledgers, adapter_id) for adapter_id in sorted(ADAPTER_IDS)
    }
    return {
        "metric_summaries": metric_summaries,
        "mode": _comparison_mode(fixture_mode),
        "public_claim_gate": claim_gate,
        "quality_claim": claim_gate["eligible"],
        "raw_task_ledgers": ctx.raw_ledgers,
        "schema_version": "comparative-run-report/v1",
        "statistics": statistics,
    }


def _write_canonical(path: Path, value: object) -> str:
    raw = canonical_evidence_json_bytes(value) + b"\n"
    path.write_bytes(raw)
    return hashlib.sha256(raw).hexdigest()


def write_run_artifacts(report: dict, output_dir: Path | str) -> dict:
    """Persist the report and one immutable file per raw adapter attempt."""
    output_dir = Path(output_dir)
    if output_dir.exists() and any(output_dir.iterdir()):
        raise ValueError("comparative output directory must be absent or empty")
    output_dir.mkdir(parents=True, exist_ok=True)
    ledger_dir = output_dir / "raw-task-ledgers"
    ledger_dir.mkdir()
    ledger_hashes = {}
    for sequence, ledger in enumerate(report["raw_task_ledgers"], start=1):
        validate_ledger(ledger)
        filename = (
            f"{sequence:06d}-{ledger['task_id']}-{ledger['adapter_id']}-"
            f"seed-{ledger['seed']}-attempt-{ledger['attempt']}.json"
        )
        ledger_hashes[filename] = _write_canonical(ledger_dir / filename, ledger)
    report_sha256 = _write_canonical(output_dir / "report.json", report)
    index = {
        "ledger_sha256": ledger_hashes,
        "report_sha256": report_sha256,
        "schema_version": "comparative-artifact-index/v1",
    }
    _write_canonical(output_dir / "artifact-index.json", index)
    return index


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--contract", type=Path, default=DEFAULT_CONTRACT)
    parser.add_argument("--schema", type=Path, default=DEFAULT_SCHEMA)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--smoke", action="store_true", help="run bounded fake/offline smoke")
    modes.add_argument("--preflight", action="store_true", help="verify an exact real-run manifest")
    modes.add_argument("--run", action="store_true", help="execute a preflighted real comparison")
    modes.add_argument(
        "--fixture", action="store_true", help="run checked-in deterministic adapter fixtures"
    )
    parser.add_argument("--manifest", type=Path, help="comparative-run/v1 manifest")
    parser.add_argument("--output", type=Path, help="new or empty evidence output directory")
    parser.add_argument("--json", action="store_true", help="emit canonical JSON")
    return parser


def _needs_manifest(args: argparse.Namespace) -> bool:
    return args.preflight or args.run or args.fixture


def _missing_manifest(args: argparse.Namespace) -> bool:
    return _needs_manifest(args) and args.manifest is None


def _missing_output(args: argparse.Namespace) -> bool:
    return (args.run or args.fixture) and args.output is None


def _argument_error(args: argparse.Namespace) -> str | None:
    if not any((args.smoke, args.preflight, args.run, args.fixture)):
        return "real comparative execution is unavailable until Gate F and complete evidence"
    return _missing_argument_error(args)


def _missing_argument_error(args: argparse.Namespace) -> str | None:
    if _missing_manifest(args):
        return "comparative execution failed: --manifest is required"
    if _missing_output(args):
        return "comparative execution failed: --output is required"
    return None


def _execute(args: argparse.Namespace) -> dict:
    contract = load_contract(args.contract, args.schema)
    if args.smoke:
        result = run_smoke(contract)
        validate_report(result)
        return result
    assert args.manifest is not None
    manifest = load_real_manifest(args.manifest)
    if args.preflight:
        return preflight_real_run(contract, manifest)
    result = execute_comparison(contract, manifest, fixture_mode=args.fixture)
    assert args.output is not None
    write_run_artifacts(result, args.output)
    return result


def _rendered(result: dict, as_json: bool) -> str:
    output = canonical_evidence_json_bytes(result).decode("utf-8")
    if as_json:
        return output
    return json.dumps(result, indent=2, sort_keys=True, allow_nan=False)


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    error = _argument_error(args)
    if error is not None:
        print(error, file=sys.stderr)
        return 2
    try:
        rendered = _rendered(_execute(args), args.json)
    except (OSError, subprocess.SubprocessError, TypeError, UnicodeError, ValueError, RecursionError) as exc:
        message = " ".join(str(exc).split())[:500]
        print(f"comparative execution failed: {message}", file=sys.stderr)
        return 2
    print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
