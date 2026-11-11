"""Aggregate candidate and lexical reports into one selection comparison and publish it."""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path, PurePosixPath

import retrieval_v2_corpus
import retrieval_v2_matrix
import retrieval_v2_outputs
import retrieval_v2_recompute
import retrieval_v2_selection_gates
from retrieval_v2_contract import (
    DEFAULT_SCHEMA,
    FROZEN_CORPUS_IDS,
    MAX_CORPUS_BYTES,
    REPORT_FIELDS,
    ModelSelection,
    WorkerPayload,
)
from retrieval_v2_environment import (
    environment_provenance,
    matrix_policy_fingerprint,
    observed_runtime_environment,
    runner_sha256,
    sha256_json,
)
from retrieval_v2_matrix import require_exact_keys
from retrieval_v2_outputs import canonical_report_bytes, is_reparse_point, write_new_bytes
from retrieval_v2_recompute import (
    candidate_policy,
    recompute_report_metrics,
    verified_baseline_metrics,
)
from retrieval_v2_selection_gates import (
    CANDIDATE_REPORT_EQUALITIES,
    CANDIDATE_REPORT_IDENTITIES,
    RERANKER_DEPTH_KEYS,
    fields_equal,
    fields_identical,
    first_candidate_selection,
    objective_dominates,
    recompute_reranker_depth_metrics,
)

# isort: split
from bounded_io import read_stable_bytes
from reliable_memory import canonical_json_bytes


def _require_utc_timestamp(measured_at: str) -> None:
    try:
        parsed_time = datetime.fromisoformat(measured_at)
    except ValueError as exc:
        raise ValueError("aggregation measured_at must be an ISO UTC timestamp") from exc
    if not measured_at.endswith("Z") or parsed_time.utcoffset() is None:
        raise ValueError("aggregation measured_at must be an ISO UTC timestamp")


def _absolute_output_request(repo: Path, output_path: Path | str) -> Path:
    requested = Path(output_path)
    if requested.is_symlink():
        raise ValueError("selection output must not be a symlink")
    if not requested.is_absolute():
        return repo / requested
    return requested


def _output_relative_to_repo(repo: Path, requested: Path) -> PurePosixPath:
    try:
        relative = requested.relative_to(repo)
    except ValueError as exc:
        raise ValueError("selection output must be under benchmark/results") from exc
    return PurePosixPath(relative.as_posix())


def _path_names_private(normalized: PurePosixPath) -> bool:
    return any(marker in normalized.as_posix().casefold() for marker in ("private", "personal"))


def _normalized_results_path(normalized: PurePosixPath) -> bool:
    if normalized.parts[:2] != ("benchmark", "results") or normalized.suffix != ".json":
        return False
    if any(part in {"", ".", ".."} for part in normalized.parts):
        return False
    return not _path_names_private(normalized)


def _selection_output_path(repo: Path, output_path: Path | str) -> Path:
    normalized = _output_relative_to_repo(repo, _absolute_output_request(repo, output_path))
    if not _normalized_results_path(normalized):
        raise ValueError("selection output must be a normalized JSON path under benchmark/results")
    output = repo / Path(*normalized.parts)
    if output.exists() or output.is_symlink():
        raise ValueError("selection output already exists")
    return output


def _bound_baseline_report(repo: Path, baseline: dict) -> dict:
    baseline_path = repo / Path(*PurePosixPath(baseline["raw_report_path"]).parts)
    baseline_raw = read_stable_bytes(baseline_path, MAX_CORPUS_BYTES, label="baseline raw report")
    if hashlib.sha256(baseline_raw).hexdigest() != baseline["raw_report_sha256"]:
        raise ValueError("bound baseline raw report SHA256 mismatch")
    try:
        baseline_report = json.loads(baseline_raw)
    except json.JSONDecodeError as exc:
        raise ValueError("bound baseline raw report is invalid") from exc
    if baseline_raw != canonical_report_bytes(baseline_report):
        raise ValueError("bound baseline raw report must use canonical JSON bytes")
    return baseline_report


def _reject_nonfinite_constant(value: str):
    raise ValueError(f"nonfinite JSON constant: {value}")


def _parsed_candidate_report(raw: bytes) -> dict:
    try:
        report = json.loads(raw, parse_constant=_reject_nonfinite_constant)
    except (json.JSONDecodeError, ValueError) as exc:
        raise ValueError(f"invalid candidate raw report: {exc}") from exc
    require_exact_keys(report, REPORT_FIELDS, "candidate raw report")
    if raw != canonical_report_bytes(report):
        raise ValueError("candidate raw report must use canonical JSON bytes")
    return report


def _candidate_provenance_complete(report: dict, bound: tuple) -> bool:
    if report["corpus_id"] not in FROZEN_CORPUS_IDS or report["vector_backend"] == "usearch-hnsw":
        return False
    return (
        fields_equal(report, CANDIDATE_REPORT_EQUALITIES)
        and fields_identical(report, CANDIDATE_REPORT_IDENTITIES)
        and fields_equal(report, bound)
    )


def _comparable_environment(environment: object) -> object:
    if not isinstance(environment, dict):
        return environment
    comparable = dict(environment)
    comparable.pop("verified_lock", None)
    return comparable


class _CandidateReports:
    """The candidate raw reports of one aggregation, each checked and recomputed as it is added."""

    def __init__(self, corpus: dict, matrix: dict, validated: ModelSelection) -> None:
        self.corpus = corpus
        self.expected = {retrieval_v2_recompute.candidate_key(spec): spec for spec in retrieval_v2_matrix.required_candidate_specs(matrix)}
        self.contract_hash = sha256_json(matrix["benchmark_contract"])
        self.runner_hash = runner_sha256()
        self.bound = (
            ("matrix_sha256", validated.matrix_sha256),
            ("corpus_sha256", validated.corpus_sha256),
            ("benchmark_contract_sha256", self.contract_hash),
            ("benchmark_runner_sha256", self.runner_hash),
        )
        self.reports: dict[str, dict] = {}
        self.report_hashes: dict[str, str] = {}
        self.report_bytes: dict[str, bytes] = {}
        self.recomputed_metrics: dict[str, tuple] = {}
        self.recomputed_depth_metrics: dict[str, dict] = {}
        self.common_environment = None

    def _require_environment(self, report: dict) -> None:
        environment = report["methodology"].get("environment_provenance")
        expected_environment = environment_provenance(report["vector_backend"])
        if _comparable_environment(environment) != expected_environment:
            raise ValueError("candidate environment provenance or lock hash mismatch")
        self._require_common_environment(environment)

    def _require_common_environment(self, environment: object) -> None:
        if self.common_environment is None:
            self.common_environment = environment
            return
        if environment != self.common_environment:
            raise ValueError("candidate environment provenance is inconsistent")

    def _new_key(self, report: dict) -> str:
        key = retrieval_v2_recompute.candidate_key(report["candidate"])
        if key not in self.expected or key in self.reports:
            raise ValueError("candidate raw report is unknown or duplicated")
        return key

    def add(self, raw_path: Path | str) -> None:
        raw = read_stable_bytes(Path(raw_path), MAX_CORPUS_BYTES, label="candidate raw report")
        report = _parsed_candidate_report(raw)
        if not _candidate_provenance_complete(report, self.bound):
            raise ValueError("candidate raw report provenance is incomplete")
        self._require_environment(report)
        key = self._new_key(report)
        self.recomputed_metrics[key] = recompute_report_metrics(self.corpus, report)
        self.recomputed_depth_metrics[key] = recompute_reranker_depth_metrics(self.corpus, report)
        self.reports[key] = report
        self.report_hashes[key] = hashlib.sha256(raw).hexdigest()
        self.report_bytes[key] = raw

    def require_complete(self) -> None:
        if set(self.reports) != set(self.expected):
            raise ValueError("aggregation requires the complete candidate report set")


def _reranker_identity_matches(reranker_report: dict, target: dict) -> bool:
    return (
        reranker_report.get("model_id") == target["id"]
        and reranker_report.get("revision") == target["revision"]
        and reranker_report.get("variant_id") == target["variant_id"]
    )


def _reranker_depths_complete(reranker_report: dict, matrix: dict) -> bool:
    return (
        reranker_report.get("depths") == matrix["benchmark_contract"]["reranker_depths"]
        and set(reranker_report.get("depth_metrics", {})) == RERANKER_DEPTH_KEYS
    )


def _require_reranker_depth_evidence(reranker_report: object, target: dict, matrix: dict) -> None:
    if (
        not isinstance(reranker_report, dict)
        or not _reranker_identity_matches(reranker_report, target)
        or not _reranker_depths_complete(reranker_report, matrix)
    ):
        raise ValueError("reranker raw report lacks complete comparable depth evidence")


def _depth_variant(report: dict, depth: int, depth_metrics: dict, recomputed: tuple) -> tuple[dict, dict]:
    derived = dict(report)
    derived["overall"], derived["slices"] = recomputed
    derived["measurements"] = dict(report["measurements"])
    if "warm_latency_p95_ms" in depth_metrics:
        derived["measurements"]["warm_latency_p95_ms"] = depth_metrics["warm_latency_p95_ms"]
    candidate = dict(report["candidate"])
    candidate["rerank_depth"] = depth
    return candidate, derived


def _report_variants(matrix: dict, report: dict, reranker: dict | None, recomputed_depths: dict) -> list:
    if reranker is None:
        if report["reranker"] is not None:
            raise ValueError("embedding-only report contains unexpected reranker evidence")
        return [(report["candidate"], report)]
    reranker_report = report["reranker"]
    _require_reranker_depth_evidence(reranker_report, report["candidate"]["reranker"], matrix)
    return [
        _depth_variant(
            report, depth, reranker_report["depth_metrics"][str(depth)], recomputed_depths[str(depth)]
        )
        for depth in matrix["benchmark_contract"]["reranker_depths"]
    ]


def _evaluated_variant(matrix, candidate, evaluated_report, embedding, reranker, raw_hash) -> dict:
    try:
        gates, objective = retrieval_v2_selection_gates.selection_report_gates(matrix, evaluated_report, embedding, reranker)
    except (KeyError, TypeError, OverflowError, ValueError) as exc:
        raise ValueError(f"candidate raw report metrics are invalid: {exc}") from exc
    return {
        "candidate": candidate,
        "gates": gates,
        "objective_values": objective,
        "raw_report_sha256": raw_hash,
    }


def _evaluated_report_variants(matrix: dict, collected: _CandidateReports, key: str) -> list[dict]:
    report = dict(collected.reports[key])
    report["overall"], report["slices"] = collected.recomputed_metrics[key]
    embedding, reranker = candidate_policy(matrix, report["candidate"])
    variants = _report_variants(matrix, report, reranker, collected.recomputed_depth_metrics[key])
    return [
        _evaluated_variant(matrix, candidate, evaluated_report, embedding, reranker, collected.report_hashes[key])
        for candidate, evaluated_report in variants
    ]


def _evaluated_candidates(matrix: dict, collected: _CandidateReports) -> list[dict]:
    evaluated: list[dict] = []
    for key in sorted(collected.reports):
        evaluated.extend(_evaluated_report_variants(matrix, collected, key))
    return evaluated


def _dominated(item: dict, passing: list[dict], objectives: dict) -> bool:
    return any(
        other is not item
        and objective_dominates(other["objective_values"], item["objective_values"], objectives)
        for other in passing
    )


def _pareto_frontier(evaluated: list[dict], objectives: dict) -> list[dict]:
    passing = [item for item in evaluated if item["gates"]["overall"]]
    return [item for item in passing if not _dominated(item, passing, objectives)]


def _selection_order(item: dict) -> tuple:
    values = item["objective_values"]
    return (
        -values["overall"],
        values["warm_p95_ms"],
        values["peak_rss_bytes"],
        values["index_bytes"],
        retrieval_v2_recompute.candidate_key(item["candidate"]),
    )


def _selected_candidate(frontier: list[dict]) -> dict | None:
    if not frontier:
        return None
    return min(frontier, key=_selection_order)


def _retained_report_path(digest: str) -> str:
    return f"benchmark/results/reports/{digest}.json"


def _candidate_report_entries(collected: _CandidateReports) -> list[dict]:
    return [
        {
            "candidate": collected.reports[key]["candidate"],
            "raw_report_sha256": collected.report_hashes[key],
            "retained_path": _retained_report_path(collected.report_hashes[key]),
        }
        for key in sorted(collected.reports)
    ]


def payload_digest(payload: WorkerPayload) -> str:
    return hashlib.sha256(payload.canonical_bytes).hexdigest()


def _lexical_report_entries(lexical_results: Sequence[WorkerPayload] | None) -> list[dict]:
    return [
        {
            "lexical_configuration": item.report["methodology"]["lexical_configuration"]["id"],
            "raw_report_sha256": payload_digest(item),
            "retained_path": _retained_report_path(payload_digest(item)),
        }
        for item in (lexical_results or ())
    ]


def _selection_outcome(selected: dict | None) -> dict:
    if selected is None:
        return {
            "selected": None,
            "gates": {"outcome": "no-winner", "fallback": "current-bm25"},
            "measurements": None,
        }
    return {
        "selected": selected["candidate"],
        "gates": selected["gates"],
        "measurements": selected["objective_values"],
    }


def _comparison_artifact(
    matrix: dict,
    validated: ModelSelection,
    collected: _CandidateReports,
    measured_at: str,
    baseline_observed_runtime: dict,
    frontier: list[dict],
    lexical_results: Sequence[WorkerPayload] | None,
) -> dict:
    selected = _selected_candidate(frontier)
    return {
        "schema_version": "retrieval-comparison/v1",
        "quality_claim": False,
        "release_evidence": False,
        "measured_at": measured_at,
        "matrix_sha256": validated.matrix_sha256,
        "matrix_policy_sha256": matrix_policy_fingerprint(matrix),
        "corpus_sha256": validated.corpus_sha256,
        "benchmark_contract_sha256": collected.contract_hash,
        "benchmark_runner_sha256": collected.runner_hash,
        "baseline": {**matrix["selection"]["baseline"], "observed_runtime": baseline_observed_runtime},
        "candidate_reports": _candidate_report_entries(collected) + _lexical_report_entries(lexical_results),
        "pareto": frontier,
        **_selection_outcome(selected),
    }


def _require_no_reparse_ancestors(start: Path, repo: Path) -> None:
    current = start
    while current != repo:
        if is_reparse_point(current):
            raise ValueError("selection output path must not contain symlinks or reparse points")
        current = current.parent


def checked_output_parent(repo: Path, output: Path) -> Path:
    output.parent.mkdir(parents=True, exist_ok=True)
    resolved_parent = output.parent.resolve(strict=True)
    if resolved_parent != repo and repo not in resolved_parent.parents:
        raise ValueError("selection output parent escaped benchmark/results")
    _require_no_reparse_ancestors(output.parent, repo)
    return resolved_parent


def retained_reports_root(repo: Path) -> Path:
    retained_root = repo / "benchmark" / "results" / "reports"
    retained_root.mkdir(parents=True, exist_ok=True)
    if retained_root.resolve(strict=True) != retained_root or is_reparse_point(retained_root):
        raise ValueError("retained report path must not contain symlinks or reparse points")
    return retained_root


def _remove_created_reports(created_reports: list[tuple[Path, os.stat_result]]) -> None:
    for retained, identity in reversed(created_reports):
        _remove_unchanged_report(retained, identity)


def _remove_unchanged_report(retained: Path, identity: os.stat_result) -> None:
    with contextlib.suppress(FileNotFoundError):
        if os.path.samestat(identity, retained.stat(follow_symlinks=False)):
            retained.unlink()


def publish_retained_and_output(
    retained_root: Path, retained_reports: list[tuple[str, bytes]], output: Path, serialized: str
) -> None:
    created_reports: list[tuple[Path, os.stat_result]] = []
    try:
        for digest, data in retained_reports:
            retained = retained_root / f"{digest}.json"
            identity = write_new_bytes(retained, data)
            created_reports.append((retained, identity))
        retrieval_v2_outputs.write_new_output(output, serialized)
    except BaseException:
        _remove_created_reports(created_reports)
        raise


def _write_comparison(repo: Path, output: Path, artifact: dict, collected: _CandidateReports, lexical_results) -> None:
    serialized = (canonical_json_bytes(artifact) + b"\n").decode("utf-8")
    resolved_parent = checked_output_parent(repo, output)
    output = resolved_parent / output.name
    retained_root = retained_reports_root(repo)
    retained_reports = [(collected.report_hashes[key], collected.report_bytes[key]) for key in sorted(collected.reports)]
    retained_reports += [(payload_digest(item), item.canonical_bytes) for item in lexical_results or ()]
    publish_retained_and_output(retained_root, retained_reports, output, serialized)


def aggregate_reports(
    raw_report_paths: Sequence[Path | str],
    *,
    matrix_path: Path | str,
    corpus_path: Path | str,
    repo_root: Path | str,
    output_path: Path | str,
    measured_at: str,
    _lexical_results: Sequence[WorkerPayload] | None = None,
    _write_analysis: bool = True,
) -> dict:
    matrix = json.loads(read_stable_bytes(Path(matrix_path), MAX_CORPUS_BYTES, label="model matrix"))
    validated = first_candidate_selection(matrix, matrix_path, corpus_path)
    _require_utc_timestamp(measured_at)
    repo = Path(repo_root).resolve(strict=True)
    output = _selection_output_path(repo, output_path)
    baseline_report = _bound_baseline_report(repo, matrix["selection"]["baseline"])
    verified_baseline_metrics(matrix, baseline_report, corpus_path=corpus_path)
    baseline_observed_runtime = observed_runtime_environment(baseline_report["vector_backend"], lexical_config="L4")
    corpus = retrieval_v2_corpus.load_corpus(corpus_path, Path(corpus_path).with_name(DEFAULT_SCHEMA.name))
    collected = _CandidateReports(corpus, matrix, validated)
    for raw_path in raw_report_paths:
        collected.add(raw_path)
    collected.require_complete()
    frontier = _pareto_frontier(_evaluated_candidates(matrix, collected), matrix["selection"]["pareto_objectives"])
    artifact = _comparison_artifact(
        matrix, validated, collected, measured_at, baseline_observed_runtime, frontier, _lexical_results
    )
    if set(artifact) != set(matrix["selection"]["aggregation_evidence_contract"]["required_fields"]):
        raise ValueError("selection artifact does not match matrix evidence contract")
    if not _write_analysis:
        return artifact
    _write_comparison(repo, output, artifact, collected, _lexical_results)
    return artifact


def aggregate_selection(
    raw_report_paths: Sequence[Path | str],
    *,
    matrix_path: Path | str,
    corpus_path: Path | str,
    repo_root: Path | str,
    output_path: Path | str,
    measured_at: str,
) -> dict:
    """Compare report files without producing release or selection evidence."""
    return aggregate_reports(
        raw_report_paths,
        matrix_path=matrix_path,
        corpus_path=corpus_path,
        repo_root=repo_root,
        output_path=output_path,
        measured_at=measured_at,
    )
