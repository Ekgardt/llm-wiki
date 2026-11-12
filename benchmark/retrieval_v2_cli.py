"""The command-line interface of the retrieval-v2 benchmark."""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path

import retrieval_v2_corpus
import retrieval_v2_orchestration
import retrieval_v2_outputs
import retrieval_v2_run
from retrieval_v2_aggregation import aggregate_selection
from retrieval_v2_contract import (
    AUTHORITATIVE_SELECTION_ADAPTER_KIND,
    MODEL_MATRIX_ADAPTER_KIND,
    SELECTION_AGGREGATION_ADAPTER_KIND,
    WorkerPayload,
)
from retrieval_v2_matrix import load_model_selection
from retrieval_v2_orchestration import (
    orchestrate_selection,
    parser,
    validate_cli_args,
    validate_worker_payload,
)
from retrieval_v2_outputs import canonical_report_bytes, require_outside_source_and_vault
from retrieval_v2_workspace import prefetch_models

# isort: split
from reliable_memory import SchemaValidationError


def _pretty_json(value: dict) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n"


def _print_json_or(args: argparse.Namespace, serialized: str, message: str) -> None:
    if args.json:
        print(serialized, end="")
        return
    print(message)


def _source_argv(argv: Sequence[str] | None) -> list[str]:
    if argv is not None:
        return list(argv)
    return sys.argv[1:]


def _reject_legacy_flags(args: argparse.Namespace) -> None:
    if args.semantic or args.report:
        raise ValueError("use --adapter model-matrix instead of legacy --semantic/--report")
    if args.model:
        raise ValueError("use exact --model-id and --variant-id selectors")


def _run_authoritative_cli(args: argparse.Namespace, argv: Sequence[str] | None) -> int:
    del argv
    artifact = orchestrate_selection(
        matrix_path=args.matrix,
        corpus_path=args.corpus,
        repo_root=args.repo_root,
        output_path=args.selection_output,
        measured_at=args.measured_at,
        cache_root=args.cache_root,
        deadline_seconds=args.deadline_seconds,
    )
    message = (
        "retrieval-v2 authoritative selection complete; "
        f"release_evidence={str(artifact['release_evidence']).lower()}"
    )
    _print_json_or(args, _pretty_json(artifact), message)
    return 0


def _run_aggregation_cli(args: argparse.Namespace, argv: Sequence[str] | None) -> int:
    del argv
    artifact = aggregate_selection(
        args.raw_report,
        matrix_path=args.matrix,
        corpus_path=args.corpus,
        repo_root=args.repo_root,
        output_path=args.selection_output,
        measured_at=args.measured_at,
    )
    _print_json_or(args, _pretty_json(artifact), "retrieval-v2 report comparison complete; release_evidence=false")
    return 0


def _acquisition_receipt_for(args: argparse.Namespace, argv: Sequence[str] | None) -> dict:
    if not args.internal_worker:
        return retrieval_v2_orchestration.run_bounded_model_worker(_source_argv(argv), deadline_seconds=args.deadline_seconds)
    selection = load_model_selection(
        args.matrix,
        args.corpus,
        model_id=args.model_id,
        variant_id=args.variant_id,
        reranker_id=args.reranker_id,
    )
    return prefetch_models(selection, cache_root=args.cache_root)


def _write_acquisition_output(requested: Path, serialized: str) -> None:
    output = requested.expanduser()
    if not output.is_absolute():
        output = Path.cwd() / output
    if output.exists() or output.is_symlink():
        raise ValueError("--output must not already exist")
    output.parent.mkdir(parents=True, exist_ok=True)
    retrieval_v2_outputs.write_new_output(output.parent.resolve(strict=True) / output.name, serialized)


def _run_acquisition_cli(args: argparse.Namespace, argv: Sequence[str] | None) -> int:
    receipt = _acquisition_receipt_for(args, argv)
    serialized = canonical_report_bytes(receipt).decode("utf-8")
    if args.output is not None:
        _write_acquisition_output(args.output, serialized)
    _print_json_or(args, serialized, "model acquisition complete; quality_claim=false")
    return 0


def _worker_report_or_none(args: argparse.Namespace, argv: Sequence[str] | None) -> dict | None:
    if args.adapter != MODEL_MATRIX_ADAPTER_KIND or args.internal_worker:
        return None
    payload = retrieval_v2_orchestration.run_bounded_model_worker(_source_argv(argv), deadline_seconds=args.deadline_seconds)
    if not isinstance(payload, WorkerPayload):
        raise ValueError("benchmark worker did not return a worker payload")
    validate_worker_payload(payload.report, payload.canonical_bytes, args.matrix, args.corpus, args.schema)
    return _interpreted_worker_report(payload.report, args.output)


def _interpreted_worker_report(worker_report: dict, output: Path | None) -> dict:
    report = json.loads(json.dumps(worker_report))
    if output is None:
        report["gates"]["interpretation"] = "stdout-only-non-quality"
    return report


def _run_options_from_args(args: argparse.Namespace) -> dict:
    run_options = {
        "adapter": args.adapter,
        "corpus_path": args.corpus,
        "matrix_path": args.matrix,
        "model_id": args.model_id,
        "variant_id": args.variant_id,
        "reranker_id": args.reranker_id,
        "rerank_depth": args.rerank_depth,
        "vector_backend": args.vector_backend,
        "allow_download": args.allow_download,
        "lexical_config": args.lexical_config,
        "raw_output_written": args.output is not None,
    }
    if args.adapter == MODEL_MATRIX_ADAPTER_KIND and args.internal_worker:
        run_options["lexical_deadline_seconds"] = args.deadline_seconds
    return run_options


def _run_cli_benchmark(corpus: dict, args: argparse.Namespace, run_options: dict) -> dict:
    if args.cache_root is not None:
        return retrieval_v2_run.run_benchmark(corpus, cache_root=args.cache_root, **run_options)
    with tempfile.TemporaryDirectory(prefix="llm-wiki-retrieval-v2-") as temporary:
        return retrieval_v2_run.run_benchmark(corpus, cache_root=Path(temporary), **run_options)


def _serialized_cli_report(args: argparse.Namespace, report: dict) -> str:
    if args.adapter == MODEL_MATRIX_ADAPTER_KIND or args.internal_worker:
        return canonical_report_bytes(report).decode("utf-8")
    return _pretty_json(report)


def _absolute_output_path(requested_output: Path) -> Path:
    if requested_output.is_absolute():
        return requested_output
    return Path.cwd() / requested_output


def _new_report_output_path(requested: Path) -> Path:
    requested_output = requested.expanduser()
    if requested_output.is_symlink():
        raise ValueError("--output must not be a symlink")
    requested_output = _absolute_output_path(requested_output)
    output = requested_output.parent.resolve() / requested_output.name
    if output.exists() or output.is_symlink():
        raise ValueError("--output must not already exist")
    return output


def _write_report_output(requested: Path, serialized: str) -> None:
    output = _new_report_output_path(requested)
    require_outside_source_and_vault(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    retrieval_v2_outputs.write_new_output(output.parent.resolve(strict=True) / output.name, serialized)


def _cli_exit_code(report: dict) -> int:
    gates = report["gates"]
    if gates["passed_for_orchestration"] and not gates.get("degraded", False):
        return 0
    return 2


def _run_benchmark_cli(args: argparse.Namespace, argv: Sequence[str] | None) -> int:
    report = _worker_report_or_none(args, argv)
    corpus = retrieval_v2_corpus.load_corpus(args.corpus, args.schema)
    run_options = _run_options_from_args(args)
    if report is None:
        report = _run_cli_benchmark(corpus, args, run_options)
    serialized = _serialized_cli_report(args, report)
    if args.output is not None:
        _write_report_output(args.output, serialized)
    message = (
        f"retrieval-v2 {report.get('adapter_kind', args.adapter)} complete; "
        f"quality_claim={str(report['quality_claim']).lower()}; "
        f"release_evidence={str(report.get('release_evidence', False)).lower()}"
    )
    _print_json_or(args, serialized, message)
    return _cli_exit_code(report)


def _cli_mode(args: argparse.Namespace):
    selection_modes = {
        AUTHORITATIVE_SELECTION_ADAPTER_KIND: _run_authoritative_cli,
        SELECTION_AGGREGATION_ADAPTER_KIND: _run_aggregation_cli,
    }
    mode = selection_modes.get(args.adapter)
    if mode is not None:
        return mode
    if args.adapter == MODEL_MATRIX_ADAPTER_KIND and args.allow_download:
        return _run_acquisition_cli
    return _run_benchmark_cli


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        validate_cli_args(args)
        _reject_legacy_flags(args)
        return _cli_mode(args)(args, argv)
    except (OSError, ValueError, SchemaValidationError) as exc:
        print(f"retrieval-v2 error: {exc}", file=sys.stderr)
        return 2
