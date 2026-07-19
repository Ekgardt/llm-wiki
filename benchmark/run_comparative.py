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

    _require_unique((adapter["id"] for adapter in contract["adapters"]), "adapter ids")
    _require_unique(contract["fairness"]["identical_inputs"], "identical inputs")
    _require_unique(contract["metrics"]["latency_summary"], "latency summary")
    _require_unique(contract["metrics"]["per_task_fields"], "metric fields")
    _require_unique(contract["public_claim_gate"]["hard_gates"], "hard gates")
    _require_unique(contract["statistics"]["agent_seeds"], "agent seeds")
    _require_unique(contract["statistics"]["pairing"]["key"], "pairing keys")
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


def _require_unique(values, label: str) -> None:
    fingerprints = []
    for value in values:
        fingerprint = canonical_evidence_json_bytes(value)
        if fingerprint in fingerprints:
            raise ValueError(f"duplicate {label}")
        fingerprints.append(fingerprint)


def _canonical_evidence_value(value: object) -> object:
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("canonical evidence numbers must be finite")
        if value.is_integer() and abs(value) <= 9_007_199_254_740_991:
            return int(value)
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        return unicodedata.normalize("NFC", value)
    if isinstance(value, list):
        return [_canonical_evidence_value(item) for item in value]
    if isinstance(value, dict):
        normalized = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError("canonical evidence object keys must be strings")
            normalized_key = unicodedata.normalize("NFC", key)
            if normalized_key in normalized:
                raise ValueError(f"normalized evidence-key collision: {normalized_key!r}")
            normalized[normalized_key] = _canonical_evidence_value(item)
        return normalized
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
    for ledger in report["raw_task_ledgers"]:
        _validate_ledger_object(ledger, ledger_schema)
    _require_unique(report["statistics"]["agent_seeds"], "report agent seeds")
    _require_unique(
        (ledger["adapter_id"] for ledger in report["raw_task_ledgers"]),
        "report adapter ids",
    )
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
    runtime_provenance = verify_runtime_provenance(contract, real_mode=False)
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
                        {"kind": "git-commit", "value": GRAPHIFY_COMMIT}
                        if adapter["id"] == "graphify-pinned"
                        else {"kind": "unavailable", "value": None}
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


def _gate_f_evidence_complete(path: Path) -> bool:
    try:
        _raw, evidence = _read_bounded_json(path, MAX_MANIFEST_BYTES, "Gate F evidence")
        if set(evidence) != {"artifacts", "checks", "passed", "schema_version"}:
            return False
        if evidence["schema_version"] != "gate-f-evidence/v1" or evidence["passed"] is not True:
            return False
        if evidence["checks"] != {name: True for name in sorted(GATE_F_CHECKS)}:
            return False
        artifacts = evidence["artifacts"]
        if not isinstance(artifacts, list) or not artifacts:
            return False
        for artifact in artifacts:
            if not isinstance(artifact, dict) or set(artifact) != {"path", "sha256"}:
                return False
            artifact_path = Path(artifact["path"])
            if not artifact_path.is_absolute():
                artifact_path = path.parent / artifact_path
            if (
                not artifact_path.is_file()
                or hashlib.sha256(artifact_path.read_bytes()).hexdigest() != artifact["sha256"]
            ):
                return False
        return True
    except (OSError, TypeError, UnicodeError, ValueError, RecursionError):
        return False


def _validate_real_manifest(manifest: dict) -> None:
    required = {
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
    if not isinstance(manifest, dict) or set(manifest) - (required | {"hard_gates"}):
        raise ValueError("real manifest has unknown fields")
    if not required <= set(manifest) or manifest.get("schema_version") != "comparative-run/v1":
        raise ValueError("real manifest is incomplete or has the wrong schema version")
    if set(manifest["adapters"]) != ADAPTER_IDS:
        raise ValueError("real manifest adapter set is incomplete")
    repository = manifest["repository"]
    if not isinstance(repository, dict) or set(repository) != {"commit", "path", "url"}:
        raise ValueError("real manifest repository is invalid")
    if (
        not isinstance(repository["path"], str)
        or not repository["path"]
        or not isinstance(repository["url"], str)
        or not 3 <= len(repository["url"]) <= 500
        or not isinstance(repository["commit"], str)
        or re.fullmatch(r"[0-9a-f]{40}", repository["commit"]) is None
    ):
        raise ValueError("real manifest repository is invalid")
    graphify = manifest["graphify"]
    if not isinstance(graphify, dict) or set(graphify) != {"path"} or not isinstance(
        graphify["path"], str
    ) or not graphify["path"]:
        raise ValueError("real manifest Graphify path is invalid")
    model = manifest["model"]
    if (
        not isinstance(model, dict)
        or set(model) != {"id", "probe_command"}
        or not isinstance(model["id"], str)
        or not 3 <= len(model["id"]) <= 200
        or not isinstance(model["probe_command"], list)
        or not model["probe_command"]
        or not all(isinstance(part, str) and part for part in model["probe_command"])
    ):
        raise ValueError("real manifest model is invalid")
    if (
        not isinstance(manifest["hardware"], str)
        or not 3 <= len(manifest["hardware"]) <= 200
        or isinstance(manifest["context_budget"], bool)
        or not isinstance(manifest["context_budget"], int)
        or not 1 <= manifest["context_budget"] <= 1_000_000
    ):
        raise ValueError("real manifest hardware or context budget is invalid")
    if (
        not isinstance(manifest["tasks"], list)
        or not manifest["tasks"]
        or len(manifest["tasks"]) > MAX_REAL_TASKS
    ):
        raise ValueError("real manifest requires at least one task")
    for task in manifest["tasks"]:
        if (
            not isinstance(task, dict)
            or set(task) != {"id", "task"}
            or not isinstance(task["id"], str)
            or re.fullmatch(r"[a-z0-9][a-z0-9-]{2,127}", task["id"]) is None
            or not isinstance(task["task"], str)
            or not 1 <= len(task["task"]) <= 2000
        ):
            raise ValueError("real manifest task is invalid")
    task_ids = [task["id"] for task in manifest["tasks"]]
    _require_unique(task_ids, "real task ids")
    seeds = manifest["seeds"]
    if not isinstance(seeds, list) or len(seeds) < 3:
        raise ValueError("real manifest requires multiple seeds")
    _require_unique(seeds, "real seeds")
    if seeds != manifest.get("seeds") or any(
        isinstance(seed, bool) or not isinstance(seed, int) or not 0 <= seed <= 2_147_483_647
        for seed in seeds
    ):
        raise ValueError("real manifest seeds are invalid")
    retry = manifest["retry_policy"]
    if set(retry) != {"backoff", "max_attempts"} or retry["backoff"] not in {
        "none",
        "fixed",
        "exponential",
    } or not 1 <= retry["max_attempts"] <= 10:
        raise ValueError("real manifest retry policy is invalid")
    limits = manifest["limits"]
    if set(limits) != {"max_stderr_bytes", "max_stdout_bytes", "timeout_seconds"}:
        raise ValueError("real manifest limits are incomplete")
    if any(isinstance(value, bool) or not isinstance(value, int) or value <= 0 for value in limits.values()):
        raise ValueError("real manifest limits must be positive integers")
    if (
        limits["max_stdout_bytes"] > 16 * 1024 * 1024
        or limits["max_stderr_bytes"] > 16 * 1024 * 1024
        or limits["timeout_seconds"] > 3600
    ):
        raise ValueError("real manifest limits exceed hard bounds")
    gate_f = manifest["gate_f"]
    if not isinstance(gate_f, dict) or set(gate_f) != {
        "evidence_path",
        "evidence_sha256",
        "passed",
    } or not isinstance(gate_f["evidence_path"], str) or not gate_f["evidence_path"] or re.fullmatch(
        r"[0-9a-f]{64}", gate_f["evidence_sha256"]
    ) is None or not isinstance(gate_f["passed"], bool):
        raise ValueError("real manifest Gate F declaration is invalid")
    hard_gates = manifest.get("hard_gates")
    if hard_gates is not None and (
        not isinstance(hard_gates, dict)
        or set(hard_gates) != {"crash", "evidence", "freshness"}
        or not all(isinstance(value, bool) for value in hard_gates.values())
    ):
        raise ValueError("real manifest hard gates are invalid")
    for adapter_id, config in manifest["adapters"].items():
        if set(config) != {"command", "required_env"}:
            raise ValueError(f"adapter {adapter_id} configuration is not closed")
        if not isinstance(config["command"], list) or not config["command"] or len(
            config["command"]
        ) > 32 or not all(
            isinstance(part, str) and part for part in config["command"]
        ):
            raise ValueError(f"adapter {adapter_id} command is invalid")
        if not isinstance(config["required_env"], list) or len(config["required_env"]) > 64 or not all(
            isinstance(name, str) and name for name in config["required_env"]
        ):
            raise ValueError(f"adapter {adapter_id} environment list is invalid")


def load_real_manifest(path: Path | str) -> dict:
    """Load one bounded real-run manifest from stable captured bytes."""
    _raw, manifest = _read_bounded_json(Path(path), MAX_MANIFEST_BYTES, "real run manifest")
    _validate_real_manifest(manifest)
    return manifest


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
    findings: list[dict[str, str]] = []
    expected_python = contract["provenance"]["python"]
    observed_python = {
        "implementation": platform.python_implementation(),
        "version": platform.python_version(),
    }
    if observed_python != expected_python:
        findings.append(_finding("python-runtime-mismatch", "Python runtime is not the pinned version"))

    repository = manifest["repository"]
    repository_path = Path(repository["path"])
    try:
        result = _run_probe(command_runner, ["git", "rev-parse", "HEAD"], cwd=repository_path)
        if result.returncode != 0 or result.stdout.strip() != repository["commit"]:
            raise ValueError
    except (OSError, subprocess.SubprocessError, ValueError):
        findings.append(_finding("repository-commit-unverified", "Repository HEAD does not match"))

    graphify_path = Path(manifest["graphify"]["path"])
    if not graphify_path.is_dir() or not (graphify_path / "uv.lock").is_file():
        findings.append(
            _finding("graphify-checkout-unavailable", "Pinned Graphify checkout and uv.lock are required")
        )
    else:
        try:
            head = _run_probe(command_runner, ["git", "rev-parse", "HEAD"], cwd=graphify_path)
            if head.returncode != 0 or head.stdout.strip() != GRAPHIFY_COMMIT:
                findings.append(_finding("graphify-commit-mismatch", "Graphify HEAD is not pinned"))
            lock = _run_probe(
                command_runner, ["git", "hash-object", "uv.lock"], cwd=graphify_path
            )
            if lock.returncode != 0 or lock.stdout.strip() != GRAPHIFY_LOCK_BLOB:
                findings.append(_finding("graphify-lock-mismatch", "Graphify uv.lock blob is not pinned"))
        except (OSError, subprocess.SubprocessError):
            findings.append(_finding("graphify-probe-failed", "Graphify provenance could not be read"))

    model = manifest["model"]
    probe_command = model.get("probe_command")
    if not isinstance(probe_command, list) or not probe_command or not _command_available(probe_command):
        findings.append(_finding("model-unavailable", "Model identity probe is unavailable"))
    else:
        try:
            probe = _run_probe(command_runner, probe_command)
            if probe.returncode != 0:
                findings.append(_finding("model-unavailable", "Model identity probe failed"))
            elif probe.stdout.strip() != model.get("id"):
                findings.append(_finding("model-identity-mismatch", "Model identity is not exact"))
        except (OSError, subprocess.SubprocessError):
            findings.append(_finding("model-unavailable", "Model identity probe failed"))

    for adapter_id, config in manifest["adapters"].items():
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

    gate_f = manifest["gate_f"]
    evidence_path = Path(gate_f.get("evidence_path", ""))
    if not gate_f.get("passed"):
        findings.append(_finding("gate-f-unavailable", "Gate F is not recorded as passed"))
    if not evidence_path.is_file():
        findings.append(_finding("gate-f-evidence-unavailable", "Gate F evidence file is unavailable"))
    else:
        digest = hashlib.sha256(evidence_path.read_bytes()).hexdigest()
        if digest != gate_f.get("evidence_sha256"):
            findings.append(_finding("gate-f-evidence-mismatch", "Gate F evidence hash differs"))
        elif not _gate_f_evidence_complete(evidence_path):
            findings.append(
                _finding("gate-f-evidence-invalid", "Gate F evidence is incomplete or unverifiable")
            )
    hard_gates = manifest.get("hard_gates")
    if hard_gates != {"crash": True, "evidence": True, "freshness": True}:
        findings.append(_finding("hard-gates-unavailable", "All hard gates require explicit evidence"))
    if findings:
        raise PreflightError(findings)
    return {
        "gate_f_evidence_sha256": gate_f["evidence_sha256"],
        "graphify_commit": GRAPHIFY_COMMIT,
        "model": model["id"],
        "repository_commit": repository["commit"],
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
    executable = ledger["metrics"]["executable_task_success"]
    if executable is not None:
        return 1.0 if executable else 0.0
    factual = ledger["metrics"]["blinded_factual_correctness"]
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


def compute_paired_statistics(contract: dict, ledgers: list[dict], *, candidate_id: str) -> dict:
    """Compute the frozen cluster/seed paired bootstrap and sign-flip diagnostic."""
    if candidate_id == "graphify-pinned" or candidate_id not in ADAPTER_IDS:
        raise ValueError("candidate must be a non-Graphify canonical adapter")
    relevant = [
        ledger for ledger in ledgers if ledger["adapter_id"] in {candidate_id, "graphify-pinned"}
    ]
    attempts: dict[tuple[str, str, int], list[dict]] = {}
    for ledger in relevant:
        key = (ledger["adapter_id"], ledger["task_id"], ledger["seed"])
        attempts.setdefault(key, []).append(ledger)
    tasks = sorted({key[1] for key in attempts})
    clusters = []
    for task_id in tasks:
        candidate_seeds = {key[2] for key in attempts if key[:2] == (candidate_id, task_id)}
        baseline_seeds = {
            key[2] for key in attempts if key[:2] == ("graphify-pinned", task_id)
        }
        if candidate_seeds != baseline_seeds or not candidate_seeds:
            raise ValueError("candidate and baseline must have identical seed sets")
        rows = []
        for seed in sorted(candidate_seeds):
            candidate_attempts = sorted(attempts[(candidate_id, task_id, seed)], key=lambda x: x["attempt"])
            baseline_attempts = sorted(
                attempts[("graphify-pinned", task_id, seed)], key=lambda x: x["attempt"]
            )
            rows.append(
                (
                    _quality(candidate_attempts[-1]) - _quality(baseline_attempts[-1]),
                    sum(_tokens(item) for item in candidate_attempts),
                    sum(_tokens(item) for item in baseline_attempts),
                )
            )
        clusters.append(rows)
    if not clusters:
        raise ValueError("paired observations are unavailable")

    observed_quality = sum(sum(row[0] for row in cluster) / len(cluster) for cluster in clusters) / len(clusters)
    candidate_tokens = sum(row[1] for cluster in clusters for row in cluster)
    baseline_tokens = sum(row[2] for cluster in clusters for row in cluster)
    if baseline_tokens == 0:
        raise ValueError("zero Graphify token denominator")
    observed_ratio = candidate_tokens / baseline_tokens

    settings = contract["statistics"]
    rng = _Sha256CounterRng(settings["rng"]["bootstrap_seed_hex"])
    quality_distribution: list[float] = []
    ratio_distribution: list[float] = []
    for _ in range(settings["resampling"]["resamples"]):
        selected_clusters = [clusters[rng.index(len(clusters))] for _ in clusters]
        quality_total = 0.0
        sampled_candidate_tokens = 0
        sampled_baseline_tokens = 0
        for cluster in selected_clusters:
            selected_rows = [cluster[rng.index(len(cluster))] for _ in cluster]
            quality_total += sum(row[0] for row in selected_rows) / len(selected_rows)
            sampled_candidate_tokens += sum(row[1] for row in selected_rows)
            sampled_baseline_tokens += sum(row[2] for row in selected_rows)
        if sampled_baseline_tokens == 0:
            raise ValueError("zero Graphify token denominator")
        quality_distribution.append(quality_total / len(selected_clusters))
        ratio_distribution.append(sampled_candidate_tokens / sampled_baseline_tokens)

    cluster_differences = [sum(row[0] for row in cluster) / len(cluster) for cluster in clusters]
    random_rng = _Sha256CounterRng(settings["rng"]["randomization_seed_hex"])
    extreme = 0
    permutations = settings["resampling"]["resamples"]
    for _ in range(permutations):
        value = sum(
            difference * (-1 if random_rng.index(2) else 1)
            for difference in cluster_differences
        ) / len(cluster_differences)
        if abs(value) >= abs(observed_quality) - 1e-15:
            extreme += 1
    return {
        "candidate_id": candidate_id,
        "quality_difference": observed_quality,
        "quality_difference_lower_confidence_bound": _percentile(
            quality_distribution, settings["interval"]["quality_quantile_basis_points"]
        ),
        "randomization_p_value": (extreme + 1) / (permutations + 1),
        "resamples": settings["resampling"]["resamples"],
        "token_ratio": observed_ratio,
        "token_ratio_upper_confidence_bound": _percentile(
            ratio_distribution, settings["interval"]["token_ratio_quantile_basis_points"]
        ),
    }


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
    if quality is None:
        failed.append("quality-confidence-interval-unavailable")
    elif 10000 * quality <= gate["quality_lower_bound_basis_points_strictly_greater_than"]:
        failed.append("quality-bound-not-met")
    if ratio is None:
        failed.append("token-ratio-confidence-interval-unavailable")
    elif 10000 * ratio >= gate["token_ratio_upper_bound_basis_points_strictly_less_than"]:
        failed.append("token-ratio-bound-not-met")
    if set(hard_gates) != set(gate["hard_gates"]) or not all(hard_gates.values()):
        failed.append("hard-gates-not-passed")
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


def _invoke_adapter(command: list[str], request: dict, limits: dict) -> dict:
    try:
        completed = subprocess.run(
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
    if len(completed.stdout) > limits["max_stdout_bytes"] or len(completed.stderr) > limits["max_stderr_bytes"]:
        return _failure_result("adapter-output-limit", "adapter output exceeded limit", phase="query", retryable=False)
    if completed.returncode != 0:
        return _failure_result(
            "adapter-nonzero-exit",
            completed.stderr.decode("utf-8", errors="replace") or f"exit {completed.returncode}",
            phase="query",
            retryable=True,
        )
    try:
        result = json.loads(completed.stdout.decode("utf-8", errors="strict"))
        if set(result) != {"failure", "metrics", "outcome"} or set(result["metrics"]) != METRIC_FIELDS:
            raise ValueError("adapter result shape is not closed")
        canonical_evidence_json_bytes(result)
        if (result["outcome"] == "failure") != (result["failure"] is not None):
            raise ValueError("adapter failure does not match outcome")
        return result
    except (UnicodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        return _failure_result("adapter-invalid-output", str(exc), phase="evaluation", retryable=False)


def execute_comparison(contract: dict, manifest: dict, *, fixture_mode: bool = False) -> dict:
    """Execute bounded adapters and retain every attempt, including retries."""
    _validate_real_manifest(manifest)
    if manifest["seeds"] != contract["statistics"]["agent_seeds"]:
        raise ValueError("real manifest differs from frozen agent seeds")
    if not fixture_mode:
        preflight_real_run(contract, manifest)
    specs = real_adapter_specs()
    raw_ledgers = []
    _ledger_schema_raw, ledger_schema = _read_bounded_json(
        DEFAULT_LEDGER_SCHEMA, MAX_SCHEMA_BYTES, "ledger schema"
    )
    repository = manifest["repository"]
    for task in manifest["tasks"]:
        inputs = {
            "commit": repository["commit"],
            "context_budget": manifest["context_budget"],
            "hardware": manifest["hardware"],
            "model": manifest["model"]["id"],
            "repository": repository["url"],
            "retry_policy": manifest["retry_policy"],
            "task": task["task"],
        }
        fingerprint = _fingerprint(inputs)
        for seed in manifest["seeds"]:
            for adapter_id in sorted(ADAPTER_IDS):
                config = manifest["adapters"][adapter_id]
                for attempt in range(1, manifest["retry_policy"]["max_attempts"] + 1):
                    request = {
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
                        "task_id": task["id"],
                    }
                    result = _invoke_adapter(config["command"], request, manifest["limits"])
                    ledger_base = {
                        "adapter_id": adapter_id,
                        "adapter_provenance": {
                            "configuration_sha256": contract["provenance"]["configuration"]["sha256"],
                            "implementation_status": "implemented-pinned",
                            "source_revision": {
                                "kind": "git-commit",
                                "value": GRAPHIFY_COMMIT
                                if adapter_id == "graphify-pinned"
                                else repository["commit"],
                            },
                        },
                        "attempt": attempt,
                        "input_fingerprint": fingerprint,
                        "inputs": inputs,
                        "seed": seed,
                        "task_id": task["id"],
                    }
                    ledger = {
                        **ledger_base,
                        "failure": result["failure"],
                        "metrics": result["metrics"],
                        "outcome": result["outcome"],
                    }
                    try:
                        _validate_ledger_object(ledger, ledger_schema)
                    except (TypeError, ValueError) as exc:
                        result = _failure_result(
                            "adapter-invalid-metrics",
                            str(exc),
                            phase="evaluation",
                            retryable=False,
                        )
                        ledger = {
                            **ledger_base,
                            "failure": result["failure"],
                            "metrics": result["metrics"],
                            "outcome": result["outcome"],
                        }
                    raw_ledgers.append(ledger)
                    if result["outcome"] != "failure" or not result["failure"]["retryable"]:
                        break
    statistics_error = None
    try:
        statistics = compute_paired_statistics(
            contract, raw_ledgers, candidate_id="adaptive-context-compiler"
        )
        statistics["computed"] = True
    except (TypeError, ValueError) as exc:
        statistics_error = " ".join(str(exc).split())[:500]
        statistics = {
            "candidate_id": "adaptive-context-compiler",
            "computed": False,
            "quality_difference_lower_confidence_bound": None,
            "reason": statistics_error,
            "token_ratio_upper_confidence_bound": None,
        }
    expected_terminal = len(ADAPTER_IDS) * len(manifest["tasks"]) * len(manifest["seeds"])
    terminal = {}
    for ledger in raw_ledgers:
        key = (ledger["adapter_id"], ledger["task_id"], ledger["seed"])
        terminal[key] = ledger
    complete = len(terminal) == expected_terminal and all(
        ledger["outcome"] != "failure" for ledger in terminal.values()
    )
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
    metric_summaries = {}
    for adapter_id in sorted(ADAPTER_IDS):
        latencies = [
            float(ledger["metrics"]["query_latency_ms"])
            for ledger in raw_ledgers
            if ledger["adapter_id"] == adapter_id
            and ledger["metrics"]["query_latency_ms"] is not None
        ]
        metric_summaries[adapter_id] = {
            "query_latency_ms": {
                "p50": _percentile(latencies, 5000) if latencies else None,
                "p95": _percentile(latencies, 9500) if latencies else None,
            }
        }
    return {
        "metric_summaries": metric_summaries,
        "mode": "deterministic-adapter-integration-fixture" if fixture_mode else "real-bounded-comparison",
        "public_claim_gate": claim_gate,
        "quality_claim": claim_gate["eligible"],
        "raw_task_ledgers": raw_ledgers,
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


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if not any((args.smoke, args.preflight, args.run, args.fixture)):
        print(
            "real comparative execution is unavailable until Gate F and complete evidence",
            file=sys.stderr,
        )
        return 2
    if (args.preflight or args.run or args.fixture) and args.manifest is None:
        print("comparative execution failed: --manifest is required", file=sys.stderr)
        return 2
    if (args.run or args.fixture) and args.output is None:
        print("comparative execution failed: --output is required", file=sys.stderr)
        return 2
    try:
        contract = load_contract(args.contract, args.schema)
        if args.smoke:
            result = run_smoke(contract)
            validate_report(result)
        else:
            assert args.manifest is not None
            manifest = load_real_manifest(args.manifest)
            if args.preflight:
                result = preflight_real_run(contract, manifest)
            else:
                result = execute_comparison(contract, manifest, fixture_mode=args.fixture)
                assert args.output is not None
                write_run_artifacts(result, args.output)
        output = canonical_evidence_json_bytes(result).decode("utf-8")
        rendered = (
            output
            if args.json
            else json.dumps(result, indent=2, sort_keys=True, allow_nan=False)
        )
    except (OSError, subprocess.SubprocessError, TypeError, UnicodeError, ValueError, RecursionError) as exc:
        message = " ".join(str(exc).split())[:500]
        print(f"comparative execution failed: {message}", file=sys.stderr)
        return 2
    print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
