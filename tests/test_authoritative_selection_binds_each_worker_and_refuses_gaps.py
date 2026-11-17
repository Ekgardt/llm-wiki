from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from tests import retrieval_v2_helpers as helpers
from tests import retrieval_v2_reports_helpers as reports_helpers
from tests.retrieval_v2_helpers import BENCHMARK, CORPUS, ROOT, SCHEMA, spec_identity
from tests.slow_machine import LONG_TIMEOUT

assert str(BENCHMARK) in sys.path, "tests.retrieval_v2_helpers puts benchmark/ on sys.path"

import retrieval_v2_aggregation  # noqa: E402
import retrieval_v2_contract as contract  # noqa: E402
import retrieval_v2_corpus as corpus_module  # noqa: E402
import retrieval_v2_environment as environment  # noqa: E402
import retrieval_v2_matrix as matrix_module  # noqa: E402
import retrieval_v2_orchestration as orchestration  # noqa: E402
import retrieval_v2_outputs as outputs  # noqa: E402
import retrieval_v2_recompute  # noqa: E402
import retrieval_v2_run as run_module  # noqa: E402
import retrieval_v2_selection_gates as gates  # noqa: E402

MATRIX_PATH = BENCHMARK / "model-matrix-v1.json"
MEASURED_AT = "2026-07-17T12:00:00Z"

FAKE_RUNNER = """
import json, shutil, sys
argv = sys.argv[1:]
options = dict(zip(argv[:-1:2], argv[1:-1:2]))
names = ("--model-id", "--variant-id", "--reranker-id", "--lexical-config")
key = "|".join(options.get(name, "") for name in names)
shutil.copyfile(json.load(open(sys.argv[0] + ".index.json"))[key], options["--output"])
"""


def _payload(report: dict[str, Any]) -> Any:
    return contract.WorkerPayload(report, outputs.canonical_report_bytes(report))


def _lexical_report(cache_root: Path, lexical_config: str) -> dict[str, Any]:
    report: dict[str, Any] = run_module.run_benchmark(
        corpus_module.load_corpus(CORPUS, SCHEMA),
        corpus_path=CORPUS,
        matrix_path=MATRIX_PATH,
        cache_root=cache_root / f"lexical-{lexical_config}",
        lexical_config=lexical_config,
        test_segmenter=helpers.PinnedJieba(),
    )
    return report


@pytest.fixture(scope="module")
def lexical_l0_report(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    return _lexical_report(tmp_path_factory.mktemp("lexical"), "L0")


def _selection() -> Any:
    return matrix_module.load_model_selection(
        MATRIX_PATH, CORPUS, model_id="BAAI/bge-small-en-v1.5", variant_id="float32-384d"
    )


def _candidate(spec: dict[str, Any], lexical: str, offset: int) -> dict[str, Any]:
    report: dict[str, Any] = json.loads(
        reports_helpers.candidate_report(_selection(), spec, offset=offset)
    )
    report["quality_claim"] = False
    report["methodology"]["lexical_configuration"] = contract.LEXICAL_CONFIGURATIONS[lexical]
    return report


def _trusted(**overrides: object) -> Any:
    bindings: dict[str, object] = {
        "worker": orchestration.run_bounded_model_worker,
        "process_runner": object(),
        "candidate_enumerator": matrix_module.required_candidate_specs,
        "lexical_arguments": orchestration.lexical_ablation_worker_arguments,
        "lexical_selector": gates.select_lexical_winner,
        "candidate_arguments": orchestration.candidate_worker_arguments,
        "candidate_key": retrieval_v2_recompute.candidate_key,
        "aggregator": retrieval_v2_aggregation.aggregate_reports,
        "lexical_contract_sha256": environment.sha256_json(contract.LEXICAL_CONFIGURATIONS),
        "lexical_levels": ("L0", "L1", "L2", "L3", "L4"),
        "candidate_count": 27,
    }
    bindings.update(overrides)
    return orchestration._TrustedBindings(**bindings)


def _write_report(directory: Path, name: str, report: dict[str, Any]) -> str:
    path = directory / name
    path.write_bytes(outputs.canonical_report_bytes(report))
    return str(path)


def _index_key(spec: dict[str, Any], lexical: str) -> str:
    model_id, variant_id, reranker_id = spec_identity(spec)
    return "|".join((model_id, variant_id, str(reranker_id or ""), lexical))


def _prepared_runner(tmp_path: Path) -> Path:
    reports = tmp_path / "prepared"
    reports.mkdir()
    lexical = {level: _lexical_report(tmp_path, level) for level in contract.LEXICAL_CONFIGURATIONS}
    winner = gates.select_lexical_winner(list(lexical.values()))["id"]
    index = {
        f"|||{level}": _write_report(reports, f"lexical-{level}.json", report)
        for level, report in lexical.items()
    }
    specs = matrix_module.required_candidate_specs(_selection().matrix)
    for offset, spec in enumerate(specs):
        index[_index_key(spec, winner)] = _write_report(
            reports, f"candidate-{offset}.json", _candidate(spec, winner, offset)
        )
    runner = tmp_path / "fake_runner.py"
    runner.write_text(FAKE_RUNNER, encoding="utf-8")
    Path(f"{runner}.index.json").write_text(json.dumps(index), encoding="utf-8")
    return runner


def _repository_with_baseline(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    matrix = json.loads(MATRIX_PATH.read_bytes())
    baseline = repo / matrix["selection"]["baseline"]["raw_report_path"]
    baseline.parent.mkdir(parents=True)
    baseline.write_bytes((BENCHMARK / "baseline-2026-07-16-retrieval.json").read_bytes())
    return repo


def _locked_environment(*args: object, **kwargs: object) -> dict[str, object]:
    return {
        "packages": {},
        "package_map_sha256": environment.sha256_json({}),
        "uv_lock_sha256": environment.sha256_file(ROOT / "uv.lock"),
    }


def test_a_selection_run_through_the_bound_transport_publishes_release_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(orchestration, "RUNNER_PATH", _prepared_runner(tmp_path))
    monkeypatch.setattr(environment, "verify_locked_environment", _locked_environment)
    repo = _repository_with_baseline(tmp_path)
    cache = tmp_path / "cache"
    cache.mkdir()

    artifact = orchestration.orchestrate_selection(
        matrix_path=MATRIX_PATH,
        corpus_path=CORPUS,
        repo_root=repo,
        output_path="benchmark/results/selection.json",
        measured_at=MEASURED_AT,
        cache_root=cache,
        deadline_seconds=LONG_TIMEOUT,
    )

    published = json.loads((repo / "benchmark/results/selection.json").read_bytes())
    retained = sorted((repo / "benchmark/results/reports").iterdir())
    assert published == artifact
    assert artifact["schema_version"] == "retrieval-selection/v1"
    assert artifact["release_evidence"] is True
    assert artifact["quality_claim"] is (artifact["selected"] is not None)
    assert len(retained) == 27 + 5


def test_a_ledger_binds_a_payload_only_to_the_invocation_that_produced_it() -> None:
    produced = _payload({"quality_claim": False})

    def worker(argv: list[str], *, deadline_seconds: float) -> Any:
        return produced

    ledger = orchestration._WorkerLedger(worker, 5.0, True)
    ledger.run(["--a"], {"candidate": None})
    replayed = orchestration._WorkerLedger(worker, 5.0, True)
    replayed.run(["--a"], {"candidate": None})

    assert ledger.consume(produced, ["--a"], {"candidate": None}) is True
    assert ledger.consume(produced, ["--a"], {"candidate": None}) is False
    assert replayed.consume(produced, ["--b"], {"candidate": None}) is False


def test_a_ledger_rejects_a_record_minted_under_another_nonce() -> None:
    produced = _payload({"quality_claim": False})
    ledger = orchestration._WorkerLedger(lambda argv, deadline_seconds: produced, 5.0, True)
    ledger.run(["--a"], {})
    nonce, *rest = ledger.records[id(produced)]
    ledger.records[id(produced)] = (bytes(bytearray(nonce)), *rest)

    assert ledger.consume(produced, ["--a"], {}) is False


def test_an_untrusted_transport_records_nothing() -> None:
    produced = _payload({"quality_claim": False})
    ledger = orchestration._WorkerLedger(lambda argv, deadline_seconds: produced, 5.0, False)

    assert ledger.run(["--a"], {}) is produced
    assert ledger.records == {}


def test_bindings_missing_a_transport_are_refused() -> None:
    with pytest.raises(TypeError, match="requires its bound subprocess transport"):
        _trusted(worker=None).require_intact()


@pytest.mark.parametrize(
    "specs",
    [
        [{"embedding": {"id": "a"}, "reranker": None}],
        [{"embedding": {"id": "a"}, "reranker": None}] * 27,
    ],
)
def test_a_candidate_enumeration_that_is_short_or_repeats_is_refused(
    specs: list[dict[str, Any]],
) -> None:
    trusted = _trusted(candidate_key=lambda spec: spec["embedding"]["id"])

    with pytest.raises(ValueError, match="exactly 27 unique matrix candidates"):
        orchestration._require_candidate_specs(specs, trusted)


def test_lexical_levels_other_than_l0_through_l4_are_refused() -> None:
    with pytest.raises(ValueError, match="exact L0 through L4 configurations"):
        orchestration._authoritative_inputs(_trusted(lexical_levels=("L0",)), MATRIX_PATH)


@pytest.mark.parametrize(("cache_root", "deadline"), [(None, 1.0), ("cache", None)])
def test_an_authoritative_run_needs_a_cache_and_a_deadline(
    cache_root: object, deadline: object
) -> None:
    with pytest.raises(ValueError, match="requires cache root and deadline"):
        orchestration._require_cache_and_deadline(cache_root, deadline)


def test_a_lexical_worker_that_answers_with_a_plain_dictionary_is_refused(tmp_path: Path) -> None:
    ledger = orchestration._WorkerLedger(lambda argv, deadline_seconds: {}, 5.0, False)

    with pytest.raises(TypeError, match="lexical worker transport returned an invalid payload"):
        orchestration._run_lexical_workers(ledger, _trusted(), tmp_path, 5.0, {}, CORPUS)


def test_a_candidate_worker_that_answers_with_a_plain_dictionary_is_refused() -> None:
    with pytest.raises(TypeError, match="authoritative worker transport returned an invalid"):
        orchestration._require_candidate_payload({}, {}, "L0")


def test_a_candidate_measured_with_another_lexical_configuration_is_refused() -> None:
    report = {
        "effective_mode": contract.MODEL_MATRIX_ADAPTER_KIND,
        "methodology": {"lexical_configuration": {"id": "L1"}},
    }

    with pytest.raises(ValueError, match="does not use the frozen lexical winner"):
        orchestration._require_undegraded_candidate(report, {}, "L2")


def _lexical_results(levels: list[str]) -> list[Any]:
    return [_payload({"methodology": {"lexical_configuration": {"id": level}}}) for level in levels]


@pytest.mark.parametrize(
    ("levels", "candidates"),
    [
        (["L0", "L1", "L2", "L3"], 27),
        (["L0", "L0", "L1", "L2", "L3"], 27),
        (["L0", "L1", "L2", "L3", "L4"], 26),
    ],
)
def test_selection_evidence_missing_a_level_or_a_candidate_is_incomplete(
    levels: list[str], candidates: int
) -> None:
    with pytest.raises(ValueError, match="selection evidence is incomplete"):
        orchestration._require_complete_selection_evidence(
            _lexical_results(levels), [object()] * candidates, _trusted()
        )


def test_an_existing_selection_output_is_never_overwritten(tmp_path: Path) -> None:
    (tmp_path / "selection.json").write_text("{}", encoding="utf-8")

    with pytest.raises(ValueError, match="selection output already exists"):
        orchestration._authoritative_output(tmp_path, "selection.json")


def test_a_relative_selection_output_is_placed_under_the_repository(tmp_path: Path) -> None:
    assert orchestration._authoritative_output(tmp_path, "out/selection.json") == (
        tmp_path / "out" / "selection.json"
    )


def _matrix() -> dict[str, Any]:
    matrix: dict[str, Any] = json.loads(MATRIX_PATH.read_bytes())
    return matrix


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("corpus_sha256", "0" * 64),
        ("matrix_sha256", "0" * 64),
        ("benchmark_contract_sha256", "0" * 64),
        ("benchmark_runner_sha256", "0" * 64),
        ("quality_claim", True),
    ],
)
def test_a_lexical_worker_bound_to_other_inputs_is_incomparable(
    lexical_l0_report: dict[str, Any], field: str, value: object
) -> None:
    report = json.loads(json.dumps(lexical_l0_report))
    report[field] = value

    with pytest.raises(ValueError, match="lexical worker provenance is incomplete or incomparable"):
        orchestration.validate_lexical_worker_payload(
            _payload(report), matrix=_matrix(), corpus_path=CORPUS
        )


@pytest.mark.parametrize(
    "mutate",
    [
        lambda report: report["methodology"].update(environment_provenance={}),
        lambda report: report["methodology"]["lexical_configuration"].update(id="L9"),
    ],
)
def test_a_lexical_worker_with_foreign_methodology_is_incomparable(
    lexical_l0_report: dict[str, Any], mutate: Any
) -> None:
    report = json.loads(json.dumps(lexical_l0_report))
    mutate(report)

    with pytest.raises(ValueError, match="lexical worker provenance is incomplete or incomparable"):
        orchestration.validate_lexical_worker_payload(
            _payload(report), matrix=_matrix(), corpus_path=CORPUS
        )


def test_a_lexical_worker_whose_bytes_are_not_canonical_is_refused(
    lexical_l0_report: dict[str, Any],
) -> None:
    payload = contract.WorkerPayload(lexical_l0_report, b"{}")

    with pytest.raises(ValueError, match="lexical worker report is not canonical"):
        orchestration.validate_lexical_worker_payload(payload, matrix=_matrix(), corpus_path=CORPUS)


def _parsed(*argv: str) -> argparse.Namespace:
    namespace: argparse.Namespace = orchestration.parser().parse_args(list(argv))
    return namespace


AUTHORITATIVE = (
    "--adapter",
    "authoritative-selection",
    "--cache-root",
    "cache",
    "--selection-output",
    "out.json",
    "--measured-at",
    MEASURED_AT,
)
AGGREGATION = (
    "--adapter",
    "selection-aggregation",
    "--raw-report",
    "a.json",
    "--selection-output",
    "out.json",
    "--measured-at",
    MEASURED_AT,
)
MATRIX_RUN = (
    "--adapter",
    "model-matrix",
    "--model-id",
    "m",
    "--variant-id",
    "v",
    "--cache-root",
    "cache",
    "--lexical-config",
    "L0",
    "--vector-backend",
    "numpy-exact",
)


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        (AUTHORITATIVE, "requires cache root, selection output, measured-at, and deadline"),
        ((*AUTHORITATIVE, "--deadline-seconds", "inf"), "requires cache root"),
        ((*AUTHORITATIVE, "--deadline-seconds", "5", "--model-id", "m"), "closed matrix"),
        ((*AUTHORITATIVE, "--deadline-seconds", "5", "--allow-download"), "closed matrix"),
        (AGGREGATION[:4], "requires explicit --raw-report, --selection-output"),
        ((*AGGREGATION, "--cache-root", "cache"), "does not accept model-run options"),
        ((*AGGREGATION, "--allow-download"), "does not accept model-run options"),
        (MATRIX_RUN[:4], "requires explicit --variant-id, --cache-root"),
        (MATRIX_RUN, "requires positive --deadline-seconds"),
        ((*MATRIX_RUN, "--deadline-seconds", "-1"), "requires positive --deadline-seconds"),
        ((*MATRIX_RUN, "--deadline-seconds", "5", "--rerank-depth", "10"), "depths 10, 20, and 50"),
        (("--model-id", "m"), "real-model options require --adapter model-matrix"),
        (("--allow-download",), "real-model options require --adapter model-matrix"),
    ],
)
def test_each_adapter_refuses_options_it_does_not_own(argv: tuple[str, ...], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        orchestration.validate_cli_args(_parsed(*argv))


@pytest.mark.parametrize(
    "argv",
    [
        (*AUTHORITATIVE, "--deadline-seconds", "5"),
        AGGREGATION,
        (*MATRIX_RUN, "--deadline-seconds", "5"),
        ("--lexical-config", "L2"),
    ],
)
def test_each_adapter_accepts_its_complete_options(argv: tuple[str, ...]) -> None:
    orchestration.validate_cli_args(_parsed(*argv))


def test_worker_arguments_replace_the_output_and_drop_parent_only_flags(tmp_path: Path) -> None:
    argv = ["--json", "--output", "parent.json", "--adapter", "lexical", "--internal-worker"]

    assert orchestration._worker_arguments(argv, tmp_path / "report.json") == [
        "--adapter",
        "lexical",
        "--output",
        str(tmp_path / "report.json"),
        "--internal-worker",
    ]


def _matrix_candidate_report(**changes: object) -> dict[str, Any]:
    spec = matrix_module.required_candidate_specs(_selection().matrix)[0]
    report = _candidate(spec, "L0", 0)
    report.update(changes)
    return report


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"quality_claim": True}, "attempted to self-attest"),
        ({"release_evidence": True}, "attempted to self-attest"),
        ({"effective_mode": "lexical-L0"}, "degraded worker payload"),
        ({"acquisition_mode": "download"}, "provenance is incomplete"),
        ({"vector_backend": "usearch-exact"}, "environment provenance mismatch"),
    ],
)
def test_a_candidate_payload_that_claims_or_hides_something_is_refused(
    changes: dict[str, object], message: str
) -> None:
    report = _matrix_candidate_report(**changes)

    with pytest.raises(ValueError, match=message):
        orchestration.validate_worker_payload(report, outputs.canonical_report_bytes(report))


def test_a_candidate_payload_whose_bytes_are_not_canonical_is_refused() -> None:
    with pytest.raises(ValueError, match="worker report is not canonical"):
        orchestration.validate_worker_payload(_matrix_candidate_report(), b"{}")


def test_a_worker_that_exits_with_an_error_code_names_its_stderr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def failed(command: list[str], *, timeout: float) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(command, 3, b"", b"model missing\n")

    monkeypatch.setattr(orchestration.retrieval_v2_measurements, "run_process_tree", failed)

    with pytest.raises(
        ValueError, match=r"returncode=3.*termination=exit-code; stderr=model missing"
    ):
        orchestration.run_bounded_model_worker(["--adapter", "model-matrix"], deadline_seconds=9.0)


def _worker_writing(monkeypatch: pytest.MonkeyPatch, data: bytes) -> None:
    def written(command: list[str], *, timeout: float) -> subprocess.CompletedProcess[bytes]:
        Path(command[command.index("--output") + 1]).write_bytes(data)
        return subprocess.CompletedProcess(command, 0, b"", b"")

    monkeypatch.setattr(orchestration.retrieval_v2_measurements, "run_process_tree", written)


def test_a_worker_acquisition_receipt_is_returned_as_its_report(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    receipt = {"artifact_kind": "model-acquisition-receipt", "quality_claim": False}
    _worker_writing(monkeypatch, outputs.canonical_report_bytes(receipt))

    assert orchestration.run_bounded_model_worker([], deadline_seconds=9.0) == receipt


@pytest.mark.parametrize(
    ("report", "suffix", "message"),
    [
        ({"artifact_kind": "model-acquisition-receipt", "quality_claim": True}, b"", "receipt"),
        ({"quality_claim": False, "release_evidence": False}, b" ", "not canonical"),
        ({"quality_claim": False, "release_evidence": True}, b"", "self-attested payload"),
    ],
)
def test_a_worker_report_that_is_forged_or_self_attested_is_refused(
    monkeypatch: pytest.MonkeyPatch, report: dict[str, object], suffix: bytes, message: str
) -> None:
    _worker_writing(monkeypatch, outputs.canonical_report_bytes(report) + suffix)

    with pytest.raises(ValueError, match=message):
        orchestration.run_bounded_model_worker([], deadline_seconds=9.0)


def test_a_worker_report_transported_intact_becomes_a_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report = {"quality_claim": False, "release_evidence": False}
    _worker_writing(monkeypatch, outputs.canonical_report_bytes(report))

    payload = orchestration.run_bounded_model_worker([], deadline_seconds=9.0)

    assert payload.report == report
    assert payload.canonical_bytes == outputs.canonical_report_bytes(report)
