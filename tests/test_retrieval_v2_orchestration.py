"""The retrieval v2 selection orchestration accepts only parent-attested worker evidence."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from tests.retrieval_v2_helpers import CORPUS, ROOT, SCHEMA, PinnedJieba, spec_identity
from tests.retrieval_v2_reports_helpers import candidate_report

BENCHMARK = Path(__file__).resolve().parent.parent / "benchmark"
if str(BENCHMARK) not in sys.path:
    sys.path.insert(0, str(BENCHMARK))

import retrieval_v2_aggregation  # noqa: E402
import retrieval_v2_contract  # noqa: E402
import retrieval_v2_corpus  # noqa: E402
import retrieval_v2_environment  # noqa: E402
import retrieval_v2_matrix  # noqa: E402
import retrieval_v2_measurements  # noqa: E402
import retrieval_v2_orchestration  # noqa: E402
import retrieval_v2_outputs  # noqa: E402
import retrieval_v2_run  # noqa: E402
import retrieval_v2_selection_gates  # noqa: E402
import retrieval_v2_workspace  # noqa: E402
import run_retrieval_v2  # noqa: E402


def _lexical_sweep_runs_first(calls) -> bool:
    """The five lexical configurations are measured before any model call."""
    if any("--model-id" in argv for argv, _deadline in calls[:5]):
        return False
    return "--model-id" in calls[5][0]


def _every_model_call_uses(calls, winner: str) -> bool:
    model_calls = [argv for argv, _deadline in calls if "--model-id" in argv]
    return all(_argv_option(argv, "--lexical-config") == winner for argv in model_calls)


def _lexical_configurations(evidence_items) -> set[str]:
    return {item["lexical_configuration"] for item in evidence_items}


def _requested_spec_index(specs, argv) -> int:
    identity = (
        _argv_option(argv, "--model-id"),
        _argv_option(argv, "--variant-id"),
        _argv_option(argv, "--reranker-id"),
    )
    return next(
        index for index, spec in enumerate(specs) if spec_identity(spec) == identity
    )


def _returned_spec(specs, attack: str, requested_index: int):
    """A hostile worker answers with the wrong candidate; an honest one does not."""
    if attack == "wrong-candidate":
        return specs[1]
    return specs[requested_index]


def _lexical_worker_report(tmp_path: Path, lexical_config: str):
    return retrieval_v2_run.run_benchmark(
        retrieval_v2_corpus.load_corpus(CORPUS, SCHEMA),
        corpus_path=CORPUS,
        matrix_path=BENCHMARK / "model-matrix-v1.json",
        cache_root=tmp_path / f"lexical-{lexical_config}",
        lexical_config=lexical_config,
        test_segmenter=PinnedJieba(),
    )


def _argv_option(argv, name: str) -> str | None:
    if name not in argv:
        return None
    return argv[argv.index(name) + 1]


def _spec_for_argv(selection, argv):
    """The candidate spec the worker command line names."""
    identity = (
        _argv_option(argv, "--model-id"),
        _argv_option(argv, "--variant-id"),
        _argv_option(argv, "--reranker-id"),
    )
    return next(
        item
        for item in retrieval_v2_matrix.required_candidate_specs(selection.matrix)
        if spec_identity(item) == identity
    )


def _no_quality_claim(repo: Path, evidence_items) -> bool:
    return all(
        json.loads((repo / item["retained_path"]).read_bytes())["quality_claim"] is False
        for item in evidence_items
    )


def test_real_lexical_worker_emits_parent_accepted_canonical_bytes(tmp_path):
    cache = tmp_path / "cache"
    cache.mkdir()
    argv = retrieval_v2_orchestration.lexical_ablation_worker_arguments(cache, 60.0)[0]

    payload = retrieval_v2_orchestration.run_bounded_model_worker(argv, deadline_seconds=60.0)

    assert isinstance(payload, retrieval_v2_contract.WorkerPayload)
    assert payload.canonical_bytes == retrieval_v2_outputs.canonical_report_bytes(payload.report)
    assert payload.report["methodology"]["lexical_configuration"]["id"] == "L0"
    retrieval_v2_orchestration.validate_lexical_worker_payload(
        payload,
        matrix=json.loads((BENCHMARK / "model-matrix-v1.json").read_bytes()),
        corpus_path=CORPUS,
    )


def test_authoritative_worker_arguments_use_frozen_lexical_winner(tmp_path):
    cache = tmp_path.resolve()
    lexical = retrieval_v2_orchestration.lexical_ablation_worker_arguments(cache, 30.0)
    assert [argv[argv.index("--lexical-config") + 1] for argv in lexical] == [
        "L0",
        "L1",
        "L2",
        "L3",
        "L4",
    ]
    candidate = retrieval_v2_orchestration.candidate_worker_arguments(
        {
            "embedding": {
                "id": "BAAI/bge-small-en-v1.5",
                "variant_id": "float32-384d",
            },
            "reranker": None,
        },
        cache,
        30.0,
        lexical_config="L3",
    )
    assert candidate[candidate.index("--lexical-config") + 1] == "L3"


def test_model_worker_parent_timeout_remains_hard_bound(tmp_path, monkeypatch):
    import inspect

    assert retrieval_v2_contract.DEFAULT_LEXICAL_DEADLINE_SECONDS == 30.0
    assert (
        inspect.signature(retrieval_v2_run.run_benchmark)
        .parameters["lexical_deadline_seconds"]
        .default
        == 30.0
    )
    observed = []

    def timed_out(command, *, timeout, env=None):
        del command, env
        observed.append(timeout)
        raise TimeoutError("real benchmark worker deadline exceeded")

    monkeypatch.setattr(retrieval_v2_measurements, "run_process_tree", timed_out)
    with pytest.raises(TimeoutError, match="deadline exceeded"):
        retrieval_v2_orchestration.run_bounded_model_worker(
            ["--adapter", "model-matrix"], deadline_seconds=1200.0
        )
    assert observed == [1200.0]


def test_empty_stderr_worker_failure_reports_termination_details(tmp_path, monkeypatch):
    monkeypatch.setattr(
        retrieval_v2_measurements,
        "run_process_tree",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], -9, b"", b""),
    )

    with pytest.raises(
        ValueError,
        match=r"returncode=-9.*timeout_seconds=1200\.0.*termination=signal-9.*stderr=empty",
    ):
        retrieval_v2_orchestration.run_bounded_model_worker(
            ["--adapter", "model-matrix"], deadline_seconds=1200.0
        )


def test_mocked_orchestration_is_plumbing_only_and_uses_retained_lexical_winner(
    tmp_path, monkeypatch
):
    selection = retrieval_v2_matrix.load_model_selection(
        BENCHMARK / "model-matrix-v1.json",
        CORPUS,
        model_id="BAAI/bge-small-en-v1.5",
        variant_id="float32-384d",
    )
    repo = tmp_path / "repo"
    baseline = repo / selection.matrix["selection"]["baseline"]["raw_report_path"]
    baseline.parent.mkdir(parents=True)
    baseline.write_bytes((BENCHMARK / "baseline-2026-07-16-retrieval.json").read_bytes())
    shared_cache = tmp_path / "cache"
    shared_cache.mkdir()
    sentinel = shared_cache / "prefetched-model.sentinel"
    sentinel.write_bytes(b"prefetched")
    calls = []
    workspace_paths = []
    model_cache_identities = []
    lexical_reports = []

    def transport(argv, *, deadline_seconds):
        calls.append((argv, deadline_seconds))
        worker_cache = Path(argv[argv.index("--cache-root") + 1])
        assert worker_cache == shared_cache.resolve()
        assert (worker_cache / sentinel.name).read_bytes() == b"prefetched"
        lexical_config = argv[argv.index("--lexical-config") + 1]
        if "--model-id" not in argv:
            report = _lexical_worker_report(tmp_path, lexical_config)
            lexical_reports.append(report)
            return retrieval_v2_contract.WorkerPayload(report, retrieval_v2_outputs.canonical_report_bytes(report))
        workspace = retrieval_v2_workspace.create_run_workspace(worker_cache)
        workspace_paths.append(workspace.path)
        model_cache_identities.append(workspace.model_cache_identity)
        retrieval_v2_workspace.cleanup_run_workspace(workspace)
        spec = _spec_for_argv(selection, argv)
        report = json.loads(
            candidate_report(selection, spec, offset=len(calls) - 1)
        )
        report["quality_claim"] = False
        report["methodology"]["lexical_configuration"] = retrieval_v2_contract.LEXICAL_CONFIGURATIONS[
            lexical_config
        ]
        return retrieval_v2_contract.WorkerPayload(report, retrieval_v2_outputs.canonical_report_bytes(report))

    monkeypatch.setattr(retrieval_v2_orchestration, "run_bounded_model_worker", transport)
    monkeypatch.setattr(
        retrieval_v2_environment,
        "verify_locked_environment",
        lambda *args, **kwargs: {
            "packages": {},
            "package_map_sha256": retrieval_v2_environment.sha256_json({}),
            "uv_lock_sha256": retrieval_v2_environment.sha256_file(ROOT / "uv.lock"),
        },
    )

    artifact = retrieval_v2_orchestration.orchestrate_selection(
        matrix_path=BENCHMARK / "model-matrix-v1.json",
        corpus_path=CORPUS,
        repo_root=repo,
        output_path=repo / "benchmark/results/selection.json",
        measured_at="2026-07-17T12:00:00Z",
        cache_root=shared_cache,
        deadline_seconds=30.0,
    )

    assert len(calls) == len(retrieval_v2_matrix.required_candidate_specs(selection.matrix)) + 5
    assert _lexical_sweep_runs_first(calls)
    winner = retrieval_v2_selection_gates.select_lexical_winner(lexical_reports)["id"]
    assert _every_model_call_uses(calls, winner)
    assert len(set(workspace_paths)) == len(workspace_paths)
    assert len(set(model_cache_identities)) == 1
    assert not list((shared_cache / "runs").iterdir())
    assert artifact["schema_version"] == "retrieval-comparison/v1"
    assert artifact["quality_claim"] is False
    assert artifact["release_evidence"] is False
    lexical_evidence = [
        item for item in artifact["candidate_reports"] if "lexical_configuration" in item
    ]
    assert _lexical_configurations(lexical_evidence) == set(retrieval_v2_contract.LEXICAL_CONFIGURATIONS)
    model_evidence = [item for item in artifact["candidate_reports"] if "candidate" in item]
    assert _no_quality_claim(repo, model_evidence)


def test_authoritative_degraded_error_names_requested_candidate_and_fallback(
    tmp_path, monkeypatch
):
    selection = retrieval_v2_matrix.load_model_selection(
        BENCHMARK / "model-matrix-v1.json",
        CORPUS,
        model_id="BAAI/bge-small-en-v1.5",
        variant_id="float32-384d",
    )
    requested = retrieval_v2_matrix.required_candidate_specs(selection.matrix)[0]
    report = json.loads(candidate_report(selection, requested, offset=0))
    report["quality_claim"] = False
    report["effective_mode"] = "lexical-L0"
    report["fallback_reason"] = "OSError: prefetched model not found"

    def transport(argv, **kwargs):
        del kwargs
        if "--model-id" in argv:
            degraded = json.loads(json.dumps(report))
            lexical = argv[argv.index("--lexical-config") + 1]
            degraded["effective_mode"] = f"lexical-{lexical}"
            return retrieval_v2_contract.WorkerPayload(degraded, retrieval_v2_outputs.canonical_report_bytes(degraded))
        lexical = argv[argv.index("--lexical-config") + 1]
        lexical_report = retrieval_v2_run.run_benchmark(
            retrieval_v2_corpus.load_corpus(CORPUS, SCHEMA),
            corpus_path=CORPUS,
            matrix_path=BENCHMARK / "model-matrix-v1.json",
            cache_root=tmp_path / f"lexical-{lexical}",
            lexical_config=lexical,
            test_segmenter=PinnedJieba(),
        )
        return retrieval_v2_contract.WorkerPayload(
            lexical_report, retrieval_v2_outputs.canonical_report_bytes(lexical_report)
        )

    monkeypatch.setattr(retrieval_v2_orchestration, "run_bounded_model_worker", transport)

    with pytest.raises(ValueError) as failure:
        retrieval_v2_orchestration.orchestrate_selection(
            matrix_path=BENCHMARK / "model-matrix-v1.json",
            corpus_path=CORPUS,
            repo_root=tmp_path / "repo",
            output_path=tmp_path / "repo/benchmark/results/selection.json",
            measured_at="2026-07-17T12:00:00Z",
            cache_root=tmp_path / "cache",
            deadline_seconds=30.0,
        )

    message = str(failure.value)
    assert requested["embedding"]["id"] in message
    assert "fallback_reason=OSError: prefetched model not found" in message


def test_authoritative_orchestration_has_no_public_callback_or_forgeable_result(tmp_path):
    import inspect

    assert "worker" not in inspect.signature(retrieval_v2_orchestration.orchestrate_selection).parameters
    for module in (retrieval_v2_orchestration, run_retrieval_v2):
        assert not hasattr(module, "ParentAttestedResult")
        assert not hasattr(module, "_ParentAttestedResult")
        assert not hasattr(module, "_ATTESTATION_CAPABILITY")
        assert not hasattr(module, "_orchestrate_selection_impl")
        assert not hasattr(module, "_consume_execution_bound_payload")
        assert not hasattr(module, "_make_execution_bound_worker")
    with pytest.raises(TypeError):
        retrieval_v2_orchestration.orchestrate_selection(
            matrix_path=BENCHMARK / "model-matrix-v1.json",
            corpus_path=CORPUS,
            repo_root=tmp_path,
            output_path=tmp_path / "benchmark/results/selection.json",
            measured_at="2026-07-17T12:00:00Z",
            cache_root=tmp_path / "cache",
            deadline_seconds=30.0,
            worker=lambda spec: spec,
        )


def test_mutated_completeness_helpers_cannot_attest_authoritative_evidence(
    tmp_path, monkeypatch
):
    matrix = json.loads((BENCHMARK / "model-matrix-v1.json").read_bytes())
    repo = tmp_path / "repo"
    baseline = repo / matrix["selection"]["baseline"]["raw_report_path"]
    baseline.parent.mkdir(parents=True)
    baseline.write_bytes((BENCHMARK / "baseline-2026-07-16-retrieval.json").read_bytes())
    output = repo / "benchmark/results/no-winner.json"
    trusted_worker = retrieval_v2_orchestration.run_bounded_model_worker
    trusted_process_runner = retrieval_v2_measurements.run_process_tree
    aggregate = retrieval_v2_aggregation.aggregate_reports

    lexical_argv = retrieval_v2_orchestration.lexical_ablation_worker_arguments
    monkeypatch.setattr(
        retrieval_v2_orchestration,
        "lexical_ablation_worker_arguments",
        lambda cache, deadline: lexical_argv(cache, deadline)[:1],
    )
    monkeypatch.setattr(retrieval_v2_selection_gates, "select_lexical_winner", lambda _reports: {"id": "L0"})
    monkeypatch.setattr(retrieval_v2_matrix, "required_candidate_specs", lambda _matrix: [])
    monkeypatch.setattr(retrieval_v2_aggregation, "aggregate_reports", lambda *args, **kwargs: aggregate(*args, **kwargs))
    with pytest.raises(ValueError, match="authoritative.*contract|completeness"):
        retrieval_v2_orchestration.orchestrate_selection(
            matrix_path=BENCHMARK / "model-matrix-v1.json",
            corpus_path=CORPUS,
            repo_root=repo,
            output_path=output,
            measured_at="2026-07-17T12:00:00Z",
            cache_root=tmp_path / "cache",
            deadline_seconds=60.0,
        )

    assert retrieval_v2_orchestration.run_bounded_model_worker is trusted_worker
    assert retrieval_v2_measurements.run_process_tree is trusted_process_runner
    assert not output.exists()
    assert matrix["selection"]["default_embedding"] is None
    assert matrix["selection"]["default_reranker"] is None


def test_stale_out_of_band_payloads_with_mutated_enumeration_fail_closed(
    tmp_path, monkeypatch
):
    matrix = json.loads((BENCHMARK / "model-matrix-v1.json").read_bytes())
    repo = tmp_path / "repo"
    baseline = repo / matrix["selection"]["baseline"]["raw_report_path"]
    baseline.parent.mkdir(parents=True)
    baseline.write_bytes((BENCHMARK / "baseline-2026-07-16-retrieval.json").read_bytes())
    stale = {}
    for lexical in retrieval_v2_contract.LEXICAL_CONFIGURATIONS:
        report = retrieval_v2_run.run_benchmark(
            retrieval_v2_corpus.load_corpus(CORPUS, SCHEMA),
            corpus_path=CORPUS,
            matrix_path=BENCHMARK / "model-matrix-v1.json",
            cache_root=tmp_path / f"stale-{lexical}",
            lexical_config=lexical,
            test_segmenter=PinnedJieba(),
        )
        stale[lexical] = retrieval_v2_contract.WorkerPayload(
            report, retrieval_v2_outputs.canonical_report_bytes(report)
        )

    def replaced_worker(argv, **_kwargs):
        assert "--model-id" not in argv
        return stale[argv[argv.index("--lexical-config") + 1]]

    monkeypatch.setattr(retrieval_v2_matrix, "required_candidate_specs", lambda _matrix: [])
    monkeypatch.setattr(retrieval_v2_orchestration, "run_bounded_model_worker", replaced_worker)
    output = repo / "benchmark/results/stale.json"
    with pytest.raises(ValueError, match="authoritative completeness contract"):
        retrieval_v2_orchestration.orchestrate_selection(
            matrix_path=BENCHMARK / "model-matrix-v1.json",
            corpus_path=CORPUS,
            repo_root=repo,
            output_path=output,
            measured_at="2026-07-17T12:00:00Z",
            cache_root=tmp_path / "cache",
            deadline_seconds=30.0,
        )
    assert not output.exists()


def _candidate_worker_payload(selection, specs, attack, requested_index, lexical):
    """The worker's candidate payload, bound to the spec the attack returns."""
    returned_spec = _returned_spec(specs, attack, requested_index)
    report = json.loads(
        candidate_report(selection, returned_spec, offset=requested_index)
    )
    report["quality_claim"] = False
    report["methodology"]["lexical_configuration"] = retrieval_v2_contract.LEXICAL_CONFIGURATIONS[
        lexical
    ]
    return retrieval_v2_contract.WorkerPayload(report, retrieval_v2_outputs.canonical_report_bytes(report))


@pytest.mark.parametrize("attack", ["wrong-candidate", "replay"])
def test_replaced_worker_wrong_binding_or_replay_cannot_publish(
    tmp_path, monkeypatch, attack
):
    selection = retrieval_v2_matrix.load_model_selection(
        BENCHMARK / "model-matrix-v1.json",
        CORPUS,
        model_id="BAAI/bge-small-en-v1.5",
        variant_id="float32-384d",
    )
    specs = retrieval_v2_matrix.required_candidate_specs(selection.matrix)
    # Payloads by requested spec index; a replay hands back the one for index 0.
    payloads: dict[int, object] = {}

    def replaced_worker(argv, **_kwargs):
        lexical = argv[argv.index("--lexical-config") + 1]
        if "--model-id" not in argv:
            report = _lexical_worker_report(tmp_path, lexical)
            return retrieval_v2_contract.WorkerPayload(report, retrieval_v2_outputs.canonical_report_bytes(report))
        requested_index = _requested_spec_index(specs, argv)
        if attack == "replay" and requested_index == 1:
            return payloads.get(0)
        payloads[requested_index] = _candidate_worker_payload(
            selection, specs, attack, requested_index, lexical
        )
        return payloads[requested_index]

    monkeypatch.setattr(retrieval_v2_orchestration, "run_bounded_model_worker", replaced_worker)
    monkeypatch.setattr(retrieval_v2_environment, "verify_locked_environment", lambda *_args, **_kwargs: {})
    with pytest.raises(ValueError, match="requested matrix candidate"):
        retrieval_v2_orchestration.orchestrate_selection(
            matrix_path=BENCHMARK / "model-matrix-v1.json",
            corpus_path=CORPUS,
            repo_root=tmp_path / "repo",
            output_path=tmp_path / "repo/benchmark/results/adversarial.json",
            measured_at="2026-07-17T12:00:00Z",
            cache_root=tmp_path / "cache",
            deadline_seconds=30.0,
        )
