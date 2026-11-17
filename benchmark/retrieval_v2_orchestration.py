"""Authoritative selection orchestration, its trusted bindings and the bounded worker protocol."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import secrets
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path

import retrieval_v2_aggregation
import retrieval_v2_corpus
import retrieval_v2_environment
import retrieval_v2_matrix
import retrieval_v2_measurements
import retrieval_v2_recompute
import retrieval_v2_selection_gates
from retrieval_v2_aggregation import (
    checked_output_parent,
    payload_digest,
    publish_retained_and_output,
    retained_reports_root,
)
from retrieval_v2_contract import (
    ADAPTER_KIND,
    AUTHORITATIVE_SELECTION_ADAPTER_KIND,
    DEFAULT_CORPUS,
    DEFAULT_MATRIX,
    DEFAULT_SCHEMA,
    LEXICAL_CONFIGURATIONS,
    MAX_CORPUS_BYTES,
    MODEL_MATRIX_ADAPTER_KIND,
    REPORT_FIELDS,
    ROOT,
    RUNNER_PATH,
    SELECTION_AGGREGATION_ADAPTER_KIND,
    THRESHOLDS,
    ModelSelection,
    WorkerPayload,
)
from retrieval_v2_environment import environment_provenance, runner_sha256, sha256_json
from retrieval_v2_matrix import load_model_selection, matrix_target, require_exact_keys
from retrieval_v2_outputs import canonical_report_bytes, prepared_model_cache_root, write_new_bytes
from retrieval_v2_recompute import recompute_report_metrics
from retrieval_v2_selection_gates import fields_identical, recompute_reranker_depth_metrics

# isort: split
from bounded_io import read_stable_bytes
from reliable_memory import canonical_json_bytes


class _TrustedBindings:
    """The transport and completeness contract an authoritative selection was bound to at import."""

    def __init__(self, **bindings) -> None:
        self.worker = bindings["worker"]
        self.process_runner = bindings["process_runner"]
        self.candidate_enumerator = bindings["candidate_enumerator"]
        self.lexical_arguments = bindings["lexical_arguments"]
        self.lexical_selector = bindings["lexical_selector"]
        self.candidate_arguments = bindings["candidate_arguments"]
        self.candidate_key = bindings["candidate_key"]
        self.aggregator = bindings["aggregator"]
        self.lexical_contract_sha256 = bindings["lexical_contract_sha256"]
        self.lexical_levels = bindings["lexical_levels"]
        self.candidate_count = bindings["candidate_count"]

    def _values(self) -> tuple:
        return (
            self.worker,
            self.process_runner,
            self.candidate_enumerator,
            self.lexical_arguments,
            self.lexical_selector,
            self.candidate_arguments,
            self.candidate_key,
            self.aggregator,
            self.lexical_contract_sha256,
            self.lexical_levels,
            self.candidate_count,
        )

    def _current_identities(self) -> tuple:
        return (
            (retrieval_v2_matrix.required_candidate_specs, self.candidate_enumerator),
            (lexical_ablation_worker_arguments, self.lexical_arguments),
            (retrieval_v2_selection_gates.select_lexical_winner, self.lexical_selector),
            (candidate_worker_arguments, self.candidate_arguments),
            (retrieval_v2_recompute.candidate_key, self.candidate_key),
            (retrieval_v2_aggregation.aggregate_reports, self.aggregator),
        )

    def _contract_intact(self) -> bool:
        if any(current is not trusted for current, trusted in self._current_identities()):
            return False
        return sha256_json(LEXICAL_CONFIGURATIONS) == self.lexical_contract_sha256

    def require_intact(self) -> None:
        if any(item is None for item in self._values()):
            raise TypeError("authoritative orchestration requires its bound subprocess transport")
        if not self._contract_intact():
            raise ValueError("authoritative completeness contract was modified")


def _require_candidate_specs(specs: list, trusted: _TrustedBindings) -> None:
    unique = {trusted.candidate_key(spec) for spec in specs}
    if len(specs) != trusted.candidate_count or len(unique) != trusted.candidate_count:
        raise ValueError("authoritative selection requires exactly 27 unique matrix candidates")


def _authoritative_inputs(trusted: _TrustedBindings, matrix_path: Path | str) -> tuple[dict, dict, list]:
    matrix = json.loads(read_stable_bytes(Path(matrix_path), MAX_CORPUS_BYTES, label="model matrix"))
    lexical_configurations = json.loads(json.dumps(LEXICAL_CONFIGURATIONS))
    if tuple(lexical_configurations) != trusted.lexical_levels:
        raise ValueError("authoritative selection requires exact L0 through L4 configurations")
    specs = json.loads(json.dumps(trusted.candidate_enumerator(matrix)))
    _require_candidate_specs(specs, trusted)
    return matrix, lexical_configurations, specs


def _require_cache_and_deadline(cache_root: object, deadline_seconds: object) -> None:
    if cache_root is None or deadline_seconds is None:
        raise ValueError("authoritative orchestration requires cache root and deadline")


def _invocation_matches(registered: tuple, argv, expected: dict, payload: WorkerPayload) -> bool:
    return (
        registered[2] == tuple(argv)
        and registered[3] == sha256_json(expected)
        and registered[4] == hashlib.sha256(payload.canonical_bytes).digest()
    )


class _WorkerLedger:
    """Binds each worker payload to the one invocation that produced it."""

    def __init__(self, worker, deadline_seconds: float, trusted_transport: bool) -> None:
        self.worker = worker
        self.deadline_seconds = deadline_seconds
        self.trusted_transport = trusted_transport
        self.nonce = secrets.token_bytes(32)
        self.records: dict[int, tuple] = {}

    def run(self, argv, expected: dict):
        payload = self.worker(argv, deadline_seconds=self.deadline_seconds)
        if self.trusted_transport and isinstance(payload, WorkerPayload):
            self.records[id(payload)] = (
                self.nonce,
                payload,
                tuple(argv),
                sha256_json(expected),
                hashlib.sha256(payload.canonical_bytes).digest(),
            )
        return payload

    def consume(self, payload, argv, expected: dict) -> bool:
        registered = self.records.pop(id(payload), None)
        if registered is None:
            return False
        if registered[0] is not self.nonce or registered[1] is not payload:
            return False
        return _invocation_matches(registered, argv, expected, payload)


def _worker_ledger(trusted: _TrustedBindings, deadline_seconds: float) -> _WorkerLedger:
    worker = run_bounded_model_worker
    trusted_transport = worker is trusted.worker and retrieval_v2_measurements.run_process_tree is trusted.process_runner
    return _WorkerLedger(worker, deadline_seconds, trusted_transport)


def _run_lexical_workers(ledger, trusted, cache_root, deadline_seconds, matrix, corpus_path) -> tuple[list, bool]:
    lexical_results = []
    execution_bound = True
    for argv in trusted.lexical_arguments(cache_root, deadline_seconds, levels=trusted.lexical_levels):
        expected = {"candidate": None, "lexical_configuration": argv[argv.index("--lexical-config") + 1]}
        payload = ledger.run(argv, expected)
        if not isinstance(payload, WorkerPayload):
            raise TypeError("lexical worker transport returned an invalid payload")
        validate_lexical_worker_payload(payload, matrix=matrix, corpus_path=corpus_path)
        execution_bound &= ledger.consume(payload, argv, expected)
        lexical_results.append(payload)
    return lexical_results, execution_bound


def _lexical_winner(trusted: _TrustedBindings, lexical_results: list, lexical_configurations: dict) -> str:
    reports = [item.report for item in lexical_results]
    return trusted.lexical_selector(reports, configurations=lexical_configurations)["id"]


def _require_candidate_payload(payload: object, spec: dict, lexical_config: str) -> None:
    if not isinstance(payload, WorkerPayload):
        raise TypeError("authoritative worker transport returned an invalid payload")
    _require_undegraded_candidate(payload.report, spec, lexical_config)
    validate_worker_payload(payload.report, payload.canonical_bytes)
    if payload.report.get("candidate") != spec:
        raise ValueError("worker result does not match requested matrix candidate")


def _require_undegraded_candidate(report: dict, spec: dict, lexical_config: str) -> None:
    if report.get("effective_mode") != MODEL_MATRIX_ADAPTER_KIND:
        raise ValueError(
            f"authoritative candidate {retrieval_v2_recompute.candidate_key(spec)} degraded: "
            f"fallback_reason={report.get('fallback_reason')!s}"
        )
    if report.get("methodology", {}).get("lexical_configuration", {}).get("id") != lexical_config:
        raise ValueError("candidate report does not use the frozen lexical winner")


def _run_candidate_workers(ledger, trusted, specs, cache_root, deadline_seconds, lexical_config) -> tuple[list, bool]:
    candidate_payloads = []
    execution_bound = True
    for spec in specs:
        argv = trusted.candidate_arguments(spec, cache_root, deadline_seconds, lexical_config=lexical_config)
        expected = {"candidate": spec, "lexical_configuration": lexical_config}
        payload = ledger.run(argv, expected)
        _require_candidate_payload(payload, spec, lexical_config)
        execution_bound &= ledger.consume(payload, argv, expected)
        candidate_payloads.append(payload)
    return candidate_payloads, execution_bound


def _written_candidate_paths(temporary: Path, candidate_payloads: list) -> list[Path]:
    paths = []
    for index, result in enumerate(candidate_payloads):
        path = temporary / f"candidate-{index}.json"
        write_new_bytes(path, result.canonical_bytes)
        paths.append(path)
    return paths


def _lexical_levels_of(lexical_results: list) -> set[str]:
    return {item.report["methodology"]["lexical_configuration"]["id"] for item in lexical_results}


def _require_complete_selection_evidence(lexical_results: list, candidate_payloads: list, trusted) -> None:
    if (
        len(lexical_results) != len(trusted.lexical_levels)
        or _lexical_levels_of(lexical_results) != set(trusted.lexical_levels)
        or len(candidate_payloads) != trusted.candidate_count
    ):
        raise ValueError("authoritative selection evidence is incomplete")


def _aggregate_in_temporary(trusted, candidate_payloads, lexical_results, execution_bound, **locations) -> dict:
    with tempfile.TemporaryDirectory(prefix="llm-wiki-authoritative-selection-") as temporary:
        paths = _written_candidate_paths(Path(temporary), candidate_payloads)
        _require_complete_selection_evidence(lexical_results, candidate_payloads, trusted)
        return trusted.aggregator(
            paths,
            **locations,
            _lexical_results=lexical_results,
            _write_analysis=not execution_bound,
        )


def _authoritative_output(repo: Path, output_path: Path | str) -> Path:
    requested_output = Path(output_path)
    if not requested_output.is_absolute():
        requested_output = repo / requested_output
    output = requested_output.parent.resolve() / requested_output.name
    if output.exists() or output.is_symlink():
        raise ValueError("selection output already exists")
    return output


def _publish_authoritative_selection(artifact, repo_root, output_path, candidate_payloads, lexical_results) -> None:
    artifact["schema_version"] = "retrieval-selection/v1"
    artifact["quality_claim"] = artifact["selected"] is not None
    artifact["release_evidence"] = True
    serialized = canonical_json_bytes(artifact) + b"\n"
    repo = Path(repo_root).resolve(strict=True)
    output = _authoritative_output(repo, output_path)
    resolved_parent = checked_output_parent(repo, output)
    retained_root = retained_reports_root(repo)
    retained_reports = [
        (payload_digest(payload), payload.canonical_bytes) for payload in [*candidate_payloads, *lexical_results]
    ]
    publish_retained_and_output(retained_root, retained_reports, resolved_parent / output.name, serialized.decode("utf-8"))


def _orchestrate_selection_impl(
    *,
    matrix_path: Path | str,
    corpus_path: Path | str,
    repo_root: Path | str,
    output_path: Path | str,
    measured_at: str,
    cache_root: Path | str | None = None,
    deadline_seconds: float | None = None,
    _trusted_worker=None,
    _trusted_process_runner=None,
    _trusted_candidate_enumerator=None,
    _trusted_lexical_arguments=None,
    _trusted_lexical_selector=None,
    _trusted_candidate_arguments=None,
    _trusted_candidate_key=None,
    _trusted_aggregator=None,
    _trusted_lexical_contract_sha256=None,
    _trusted_lexical_levels=None,
    _trusted_candidate_count=None,
) -> dict:
    trusted = _TrustedBindings(
        worker=_trusted_worker,
        process_runner=_trusted_process_runner,
        candidate_enumerator=_trusted_candidate_enumerator,
        lexical_arguments=_trusted_lexical_arguments,
        lexical_selector=_trusted_lexical_selector,
        candidate_arguments=_trusted_candidate_arguments,
        candidate_key=_trusted_candidate_key,
        aggregator=_trusted_aggregator,
        lexical_contract_sha256=_trusted_lexical_contract_sha256,
        lexical_levels=_trusted_lexical_levels,
        candidate_count=_trusted_candidate_count,
    )
    trusted.require_intact()
    matrix, lexical_configurations, specs = _authoritative_inputs(trusted, matrix_path)
    _require_cache_and_deadline(cache_root, deadline_seconds)
    cache_root = prepared_model_cache_root(cache_root)
    ledger = _worker_ledger(trusted, deadline_seconds)
    lexical_results, lexical_bound = _run_lexical_workers(
        ledger, trusted, cache_root, deadline_seconds, matrix, corpus_path
    )
    lexical_config = _lexical_winner(trusted, lexical_results, lexical_configurations)
    candidate_payloads, candidate_bound = _run_candidate_workers(
        ledger, trusted, specs, cache_root, deadline_seconds, lexical_config
    )
    execution_bound = lexical_bound & candidate_bound
    artifact = _aggregate_in_temporary(
        trusted,
        candidate_payloads,
        lexical_results,
        execution_bound,
        matrix_path=matrix_path,
        corpus_path=corpus_path,
        repo_root=repo_root,
        output_path=output_path,
        measured_at=measured_at,
    )
    if not execution_bound:
        return artifact
    _publish_authoritative_selection(artifact, repo_root, output_path, candidate_payloads, lexical_results)
    return artifact


def lexical_ablation_worker_arguments(
    cache_root: Path,
    deadline_seconds: float,
    *,
    levels: Sequence[str] | None = None,
) -> list[list[str]]:
    levels = tuple(LEXICAL_CONFIGURATIONS) if levels is None else levels
    return [
        [
            "--adapter",
            ADAPTER_KIND,
            "--cache-root",
            str(cache_root),
            "--lexical-config",
            level,
            "--deadline-seconds",
            str(deadline_seconds),
        ]
        for level in levels
    ]


_LEXICAL_WORKER_IDENTITIES = (
    ("quality_claim", False),
    ("release_evidence", False),
    ("candidate", None),
    ("model_id", None),
    ("fallback_reason", None),
)


def _lexical_worker_configuration_valid(report: dict, lexical: object) -> bool:
    return lexical in LEXICAL_CONFIGURATIONS and (
        report["methodology"]["lexical_configuration"] == LEXICAL_CONFIGURATIONS[lexical]
    )


def _lexical_worker_bound_to_matrix(report: dict, matrix: dict) -> bool:
    if report["corpus_sha256"] != matrix["benchmark_contract"]["corpus"]["sha256"]:
        return False
    if report["matrix_sha256"] != hashlib.sha256(canonical_json_bytes(matrix) + b"\n").hexdigest():
        return False
    return _lexical_worker_bound_to_runner(report, matrix)


def _lexical_worker_bound_to_runner(report: dict, matrix: dict) -> bool:
    if report["benchmark_contract_sha256"] != sha256_json(matrix["benchmark_contract"]):
        return False
    if report["benchmark_runner_sha256"] != runner_sha256():
        return False
    return report["methodology"].get("environment_provenance") == environment_provenance(None)


def _lexical_worker_provenance_valid(report: dict, lexical: object, matrix: dict) -> bool:
    if not _lexical_worker_configuration_valid(report, lexical):
        return False
    if not fields_identical(report, _LEXICAL_WORKER_IDENTITIES):
        return False
    return _lexical_worker_bound_to_matrix(report, matrix)


def validate_lexical_worker_payload(
    payload: WorkerPayload, *, matrix: dict, corpus_path: Path | str
) -> None:
    report = payload.report
    if payload.canonical_bytes != canonical_report_bytes(report):
        raise ValueError("lexical worker report is not canonical")
    require_exact_keys(report, REPORT_FIELDS, "lexical worker report")
    lexical = report.get("methodology", {}).get("lexical_configuration", {}).get("id")
    if not _lexical_worker_provenance_valid(report, lexical, matrix):
        raise ValueError("lexical worker provenance is incomplete or incomparable")
    corpus = retrieval_v2_corpus.load_corpus(corpus_path, Path(corpus_path).with_name(DEFAULT_SCHEMA.name))
    recompute_report_metrics(
        corpus,
        report,
        require_complete_rankings=False,
        require_normalized_confidence=False,
    )


def candidate_worker_arguments(
    spec: dict,
    cache_root: Path,
    deadline_seconds: float,
    *,
    lexical_config: str,
) -> list[str]:
    argv = [
        "--adapter",
        MODEL_MATRIX_ADAPTER_KIND,
        "--model-id",
        spec["embedding"]["id"],
        "--variant-id",
        spec["embedding"]["variant_id"],
        "--cache-root",
        str(cache_root),
        "--lexical-config",
        lexical_config,
        "--vector-backend",
        "numpy-exact",
        "--deadline-seconds",
        str(deadline_seconds),
    ]
    if spec["reranker"] is not None:
        argv.extend(("--reranker-id", spec["reranker"]["id"]))
    return argv


def parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run the frozen retrieval-v2 orchestration benchmark (the run_benchmark.py default); "
            "use run_benchmark.py --legacy-only for the legacy slice"
        )
    )
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    parser.add_argument("--schema", type=Path, default=DEFAULT_SCHEMA)
    parser.add_argument("--json", action="store_true", help="Print the complete JSON report")
    parser.add_argument("--output", type=Path, help="Write the complete JSON report")
    parser.add_argument("--cache-root", type=Path, help="Explicit isolated benchmark cache directory")
    parser.add_argument(
        "--adapter",
        choices=(
            ADAPTER_KIND,
            MODEL_MATRIX_ADAPTER_KIND,
            SELECTION_AGGREGATION_ADAPTER_KIND,
            AUTHORITATIVE_SELECTION_ADAPTER_KIND,
        ),
        default=ADAPTER_KIND,
    )
    parser.add_argument(
        "--lexical-config",
        choices=tuple(LEXICAL_CONFIGURATIONS),
        help="Run one explicit BM25-only lexical ablation (L0 through L4)",
    )
    parser.add_argument("--matrix", type=Path, default=DEFAULT_MATRIX)
    parser.add_argument("--model-id")
    parser.add_argument("--variant-id")
    parser.add_argument("--reranker-id")
    parser.add_argument(
        "--rerank-depth", type=int, choices=(10, 20, 50), help=argparse.SUPPRESS
    )
    parser.add_argument(
        "--vector-backend", choices=("numpy-exact", "usearch-exact", "usearch-hnsw")
    )
    parser.add_argument("--allow-download", action="store_true")
    parser.add_argument("--deadline-seconds", type=float)
    parser.add_argument("--internal-worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--raw-report", action="append", type=Path)
    parser.add_argument("--selection-output", type=Path)
    parser.add_argument("--repo-root", type=Path, default=ROOT)
    parser.add_argument("--measured-at")
    parser.add_argument("--model", help=argparse.SUPPRESS)
    parser.add_argument("--semantic", action="store_true", help="Reserved for Task 10 model runs")
    parser.add_argument("--report", action="store_true", help="Reserved for Task 10 report publication")
    return parser


_MODEL_MATRIX_REQUIRED_OPTIONS = (
    ("--model-id", "model_id"),
    ("--variant-id", "variant_id"),
    ("--cache-root", "cache_root"),
    ("--lexical-config", "lexical_config"),
    ("--vector-backend", "vector_backend"),
)


def _positive_finite_deadline(value: float | None) -> bool:
    return value is not None and math.isfinite(value) and value > 0


def _any_set(values: Sequence[object]) -> bool:
    return any(value is not None for value in values)


def _authoritative_cli_complete(args: argparse.Namespace) -> bool:
    return (
        args.cache_root is not None
        and args.selection_output is not None
        and args.measured_at is not None
        and _positive_finite_deadline(args.deadline_seconds)
    )


def _authoritative_cli_overreach(args: argparse.Namespace):
    return (
        args.raw_report
        or _any_set((args.model_id, args.variant_id, args.reranker_id, args.output))
        or args.allow_download
    )


def _validate_authoritative_args(args: argparse.Namespace) -> None:
    if not _authoritative_cli_complete(args):
        raise ValueError(
            "authoritative-selection requires cache root, selection output, measured-at, and deadline"
        )
    if _authoritative_cli_overreach(args):
        raise ValueError("authoritative-selection derives candidates from the closed matrix")


def _aggregation_cli_complete(args: argparse.Namespace) -> bool:
    return bool(args.raw_report) and args.selection_output is not None and args.measured_at is not None


def _aggregation_run_options(args: argparse.Namespace) -> tuple:
    return (
        args.cache_root,
        args.model_id,
        args.variant_id,
        args.reranker_id,
        args.rerank_depth,
        args.vector_backend,
        args.lexical_config,
        args.output,
    )


def _validate_aggregation_args(args: argparse.Namespace) -> None:
    if not _aggregation_cli_complete(args):
        raise ValueError(
            "selection-aggregation requires explicit --raw-report, --selection-output, and --measured-at"
        )
    if _any_set(_aggregation_run_options(args)) or args.allow_download:
        raise ValueError("selection-aggregation does not accept model-run options")


def _missing_model_matrix_options(args: argparse.Namespace) -> list[str]:
    return [name for name, attribute in _MODEL_MATRIX_REQUIRED_OPTIONS if getattr(args, attribute) is None]


def _validate_model_matrix_args(args: argparse.Namespace) -> None:
    missing = _missing_model_matrix_options(args)
    if missing:
        raise ValueError("model-matrix adapter requires explicit " + ", ".join(missing))
    _require_matrix_deadline_and_depth(args)


def _require_matrix_deadline_and_depth(args: argparse.Namespace) -> None:
    if not _positive_finite_deadline(args.deadline_seconds):
        raise ValueError("model-matrix adapter requires positive --deadline-seconds")
    if args.rerank_depth is not None:
        raise ValueError("reranker evidence automatically runs depths 10, 20, and 50 together")


def _validate_lexical_args(args: argparse.Namespace) -> None:
    real_model_options = (args.model_id, args.variant_id, args.reranker_id, args.rerank_depth, args.vector_backend)
    if _any_set(real_model_options) or args.allow_download:
        raise ValueError("real-model options require --adapter model-matrix")


def _cli_validator(adapter: str):
    validators = {
        AUTHORITATIVE_SELECTION_ADAPTER_KIND: _validate_authoritative_args,
        SELECTION_AGGREGATION_ADAPTER_KIND: _validate_aggregation_args,
        MODEL_MATRIX_ADAPTER_KIND: _validate_model_matrix_args,
    }
    return validators.get(adapter, _validate_lexical_args)


def validate_cli_args(args: argparse.Namespace) -> None:
    _cli_validator(args.adapter)(args)


_DROPPED_WORKER_FLAGS = frozenset({"--internal-worker", "--json"})


def _worker_arguments(argv: Sequence[str], output: Path) -> list[str]:
    cleaned = []
    skip = False
    for value in argv:
        if skip:
            skip = False
            continue
        skip = value == "--output"
        if skip or value in _DROPPED_WORKER_FLAGS:
            continue
        cleaned.append(value)
    return [*cleaned, "--output", str(output), "--internal-worker"]


def validate_worker_payload(
    report: dict,
    raw: bytes,
    matrix_path: Path | str = DEFAULT_MATRIX,
    corpus_path: Path | str = DEFAULT_CORPUS,
    schema_path: Path | str = DEFAULT_SCHEMA,
) -> None:
    """The parent re-derives the candidate and the metrics from the same matrix,
    corpus and schema the worker was given; validating against the defaults
    refused every candidate only a non-default matrix names and every trace
    set of a non-default corpus (2026-09-10, the cross-lingual run)."""
    _require_worker_report_shape(report, raw)
    selection = _worker_selection(report, matrix_path, corpus_path)
    if _worker_provenance(report) != _expected_worker_provenance(selection):
        raise ValueError("worker payload provenance is incomplete")
    environment = report["methodology"].get("environment_provenance")
    if environment != environment_provenance(report["vector_backend"]):
        raise ValueError("worker payload environment provenance mismatch")
    corpus = retrieval_v2_corpus.load_corpus(corpus_path, schema_path)
    recompute_report_metrics(corpus, report)
    recompute_reranker_depth_metrics(corpus, report)
    lexical = report["methodology"].get("lexical_configuration", {}).get("id")
    retrieval_v2_environment.verify_locked_environment(report["vector_backend"], lexical_config=lexical)


def _require_worker_report_shape(report: dict, raw: bytes) -> None:
    if raw != canonical_report_bytes(report):
        raise ValueError("worker report is not canonical")
    require_exact_keys(report, REPORT_FIELDS, "worker report")
    _require_unattested_quality_mode(report)


def _require_unattested_quality_mode(report: dict) -> None:
    if report["quality_claim"] is not False or report["release_evidence"] is not False:
        raise ValueError("worker payload attempted to self-attest")
    if report["effective_mode"] != MODEL_MATRIX_ADAPTER_KIND:
        raise ValueError("degraded worker payload cannot become quality evidence")


def _worker_selection(report: dict, matrix_path: Path | str, corpus_path: Path | str):
    reranker_target = report.get("candidate", {}).get("reranker")
    reranker_id = None
    if isinstance(reranker_target, dict):
        reranker_id = reranker_target.get("id")
    return load_model_selection(
        matrix_path,
        corpus_path,
        model_id=report.get("model_id"),
        variant_id=report.get("variant_id"),
        reranker_id=reranker_id,
    )


def _expected_worker_provenance(selection: ModelSelection) -> dict:
    reranker = None
    if selection.reranker is not None:
        reranker = matrix_target(selection.reranker, selection.reranker_variant)
    return {
        "candidate": {
            "embedding": matrix_target(selection.embedding, selection.variant),
            "reranker": reranker,
        },
        "matrix_sha256": selection.matrix_sha256,
        "corpus_sha256": selection.corpus_sha256,
        "benchmark_contract_sha256": sha256_json(selection.matrix["benchmark_contract"]),
        "benchmark_runner_sha256": runner_sha256(),
        "acquisition_mode": "offline-local-files-only",
        "fallback_reason": None,
        "thresholds": THRESHOLDS,
    }


def _worker_provenance(report: dict) -> dict:
    return {
        key: report[key]
        for key in (
            "candidate",
            "matrix_sha256",
            "corpus_sha256",
            "benchmark_contract_sha256",
            "benchmark_runner_sha256",
            "acquisition_mode",
            "fallback_reason",
            "thresholds",
        )
    }


def _termination(returncode: int) -> str:
    if returncode < 0:
        return f"signal-{abs(returncode)}"
    return "exit-code"


def _worker_failure_message(completed: subprocess.CompletedProcess, deadline_seconds: float) -> str:
    error = completed.stderr.decode("utf-8", errors="replace").strip()
    return (
        "real benchmark worker failed: "
        f"returncode={completed.returncode}; timeout_seconds={deadline_seconds}; "
        f"termination={_termination(completed.returncode)}; stderr={error or 'empty'}"
    )


def _completed_worker(command: list[str], deadline_seconds: float) -> subprocess.CompletedProcess:
    try:
        return retrieval_v2_measurements.run_process_tree(command, timeout=deadline_seconds)
    except TimeoutError as exc:
        raise TimeoutError(
            "real benchmark worker failed: "
            f"timeout_seconds={deadline_seconds}; termination=deadline-exceeded; {exc}"
        ) from exc


def _require_worker_report(completed: subprocess.CompletedProcess, report_path: Path, deadline_seconds: float) -> None:
    if completed.returncode not in {0, 2} or not report_path.is_file():
        raise ValueError(_worker_failure_message(completed, deadline_seconds))


def _acquisition_receipt(raw: bytes, report: dict) -> dict:
    if raw != canonical_report_bytes(report) or report.get("quality_claim") is not False:
        raise ValueError("model acquisition receipt is invalid")
    return report


def _transported_payload(raw: bytes, report: dict) -> WorkerPayload:
    if raw != canonical_report_bytes(report):
        raise ValueError("worker report is not canonical")
    if report.get("quality_claim") is not False or report.get("release_evidence") is not False:
        raise ValueError("worker transport received a self-attested payload")
    return WorkerPayload(report, raw)


def run_bounded_model_worker(
    argv: Sequence[str], *, deadline_seconds: float
) -> WorkerPayload | dict:
    with tempfile.TemporaryDirectory(prefix="llm-wiki-retrieval-worker-") as temporary:
        report_path = Path(temporary) / "report.json"
        command = [
            sys.executable,
            str(RUNNER_PATH),
            *_worker_arguments(argv, report_path),
        ]
        completed = _completed_worker(command, deadline_seconds)
        _require_worker_report(completed, report_path, deadline_seconds)
        raw = read_stable_bytes(report_path, MAX_CORPUS_BYTES, label="worker report")
        report = json.loads(raw)
        if report.get("artifact_kind") == "model-acquisition-receipt":
            return _acquisition_receipt(raw, report)
        return _transported_payload(raw, report)


def _bind_orchestrator(
    implementation,
    trusted_worker,
    trusted_process_runner,
    trusted_candidate_enumerator,
    trusted_lexical_arguments,
    trusted_lexical_selector,
    trusted_candidate_arguments,
    trusted_candidate_key,
    trusted_aggregator,
    trusted_lexical_contract_sha256,
    trusted_lexical_levels,
    trusted_candidate_count,
):
    def orchestrate_selection(
        *,
        matrix_path: Path | str,
        corpus_path: Path | str,
        repo_root: Path | str,
        output_path: Path | str,
        measured_at: str,
        cache_root: Path | str | None = None,
        deadline_seconds: float | None = None,
    ) -> dict:
        return implementation(
            matrix_path=matrix_path,
            corpus_path=corpus_path,
            repo_root=repo_root,
            output_path=output_path,
            measured_at=measured_at,
            cache_root=cache_root,
            deadline_seconds=deadline_seconds,
            _trusted_worker=trusted_worker,
            _trusted_process_runner=trusted_process_runner,
            _trusted_candidate_enumerator=trusted_candidate_enumerator,
            _trusted_lexical_arguments=trusted_lexical_arguments,
            _trusted_lexical_selector=trusted_lexical_selector,
            _trusted_candidate_arguments=trusted_candidate_arguments,
            _trusted_candidate_key=trusted_candidate_key,
            _trusted_aggregator=trusted_aggregator,
            _trusted_lexical_contract_sha256=trusted_lexical_contract_sha256,
            _trusted_lexical_levels=trusted_lexical_levels,
            _trusted_candidate_count=trusted_candidate_count,
        )

    return orchestrate_selection


orchestrate_selection = _bind_orchestrator(
    _orchestrate_selection_impl,
    run_bounded_model_worker,
    retrieval_v2_measurements.run_process_tree,
    retrieval_v2_matrix.required_candidate_specs,
    lexical_ablation_worker_arguments,
    retrieval_v2_selection_gates.select_lexical_winner,
    candidate_worker_arguments,
    retrieval_v2_recompute.candidate_key,
    retrieval_v2_aggregation.aggregate_reports,
    sha256_json(LEXICAL_CONFIGURATIONS),
    ("L0", "L1", "L2", "L3", "L4"),
    27,
)
del _bind_orchestrator
del _orchestrate_selection_impl
