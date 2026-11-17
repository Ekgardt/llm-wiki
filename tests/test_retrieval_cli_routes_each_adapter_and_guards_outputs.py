from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path
from typing import Any

import pytest

importlib.import_module("tests.retrieval_v2_helpers")
cli = importlib.import_module("retrieval_v2_cli")
contract = importlib.import_module("retrieval_v2_contract")
orchestration = importlib.import_module("retrieval_v2_orchestration")
run_module = importlib.import_module("retrieval_v2_run")

MATRIX_OPTIONS = [
    "--adapter",
    "model-matrix",
    "--model-id",
    "model-a",
    "--variant-id",
    "variant-a",
    "--lexical-config",
    "L0",
    "--vector-backend",
    "numpy-exact",
    "--deadline-seconds",
    "30",
]


def _matrix_argv(cache_root: Path, *extra: str) -> list[str]:
    return [*MATRIX_OPTIONS, "--cache-root", str(cache_root), *extra]


def _gated_report(passed: bool, degraded: bool) -> dict[str, Any]:
    return {
        "adapter_kind": "model-matrix",
        "quality_claim": False,
        "gates": {"passed_for_orchestration": passed, "degraded": degraded},
    }


class RecordingWorker:
    def __init__(self, answer: object) -> None:
        self.answer = answer
        self.calls: list[tuple[list[str], float | None]] = []

    def __call__(self, argv: list[str], *, deadline_seconds: float | None) -> object:
        self.calls.append((argv, deadline_seconds))
        return self.answer


@pytest.mark.parametrize(
    ("legacy", "message"),
    [
        (["--semantic"], "use --adapter model-matrix instead of legacy --semantic/--report"),
        (["--report"], "use --adapter model-matrix instead of legacy --semantic/--report"),
        (["--model", "x"], "use exact --model-id and --variant-id selectors"),
    ],
)
def test_a_legacy_flag_is_refused_with_its_replacement(
    legacy: list[str], message: str, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(legacy) == 2

    assert capsys.readouterr().err == f"retrieval-v2 error: {message}\n"


def _authoritative_argv(tmp_path: Path, *extra: str) -> list[str]:
    return [
        "--adapter",
        "authoritative-selection",
        "--cache-root",
        str(tmp_path / "cache"),
        "--selection-output",
        str(tmp_path / "selection.json"),
        "--measured-at",
        "2026-11-17T00:00:00Z",
        "--deadline-seconds",
        "60",
        *extra,
    ]


def test_authoritative_selection_hands_every_option_over_and_reports_release_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    received: dict[str, Any] = {}

    def orchestrate(**options: Any) -> dict[str, Any]:
        received.update(options)
        return {"release_evidence": True}

    monkeypatch.setattr(cli, "orchestrate_selection", orchestrate)

    assert cli.main(_authoritative_argv(tmp_path)) == 0

    assert capsys.readouterr().out == (
        "retrieval-v2 authoritative selection complete; release_evidence=true\n"
    )
    assert received["output_path"] == tmp_path / "selection.json"
    assert received["deadline_seconds"] == 60.0
    assert received["measured_at"] == "2026-11-17T00:00:00Z"


def test_authoritative_selection_prints_the_artifact_as_json_when_asked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        cli, "orchestrate_selection", lambda **options: {"release_evidence": False, "b": 1}
    )

    assert cli.main(_authoritative_argv(tmp_path, "--json")) == 0

    assert capsys.readouterr().out == '{\n  "b": 1,\n  "release_evidence": false\n}\n'


def test_aggregation_compares_the_raw_reports_without_release_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    received: list[object] = []

    def aggregate(raw_reports: list[Path], **options: Any) -> dict[str, Any]:
        received.append(raw_reports)
        return {"release_evidence": False}

    monkeypatch.setattr(cli, "aggregate_selection", aggregate)
    raw = tmp_path / "raw.json"
    argv = [
        "--adapter",
        "selection-aggregation",
        "--raw-report",
        str(raw),
        "--selection-output",
        str(tmp_path / "selection.json"),
        "--measured-at",
        "2026-11-17T00:00:00Z",
    ]

    assert cli.main(argv) == 0

    assert capsys.readouterr().out == (
        "retrieval-v2 report comparison complete; release_evidence=false\n"
    )
    assert received == [[raw]]


def test_acquisition_runs_the_bounded_worker_over_the_process_arguments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    worker = RecordingWorker({"receipt": "acquired"})
    monkeypatch.setattr(orchestration, "run_bounded_model_worker", worker)
    argv = _matrix_argv(tmp_path / "cache", "--allow-download", "--json")
    monkeypatch.setattr(sys, "argv", ["run_benchmark.py", *argv])

    assert cli.main(None) == 0

    assert worker.calls == [(argv, 30.0)]
    assert capsys.readouterr().out == '{"receipt":"acquired"}\n'


def test_acquisition_writes_its_receipt_to_a_new_relative_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        orchestration, "run_bounded_model_worker", RecordingWorker({"receipt": "acquired"})
    )
    monkeypatch.chdir(tmp_path)
    argv = _matrix_argv(tmp_path / "cache", "--allow-download", "--output", "out/receipt.json")

    assert cli.main(argv) == 0

    written = tmp_path / "out" / "receipt.json"
    assert written.read_text(encoding="utf-8") == '{"receipt":"acquired"}\n'
    assert capsys.readouterr().out == "model acquisition complete; quality_claim=false\n"


def test_acquisition_never_overwrites_an_existing_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(orchestration, "run_bounded_model_worker", RecordingWorker({"r": 1}))
    existing = tmp_path / "receipt.json"
    existing.write_text("kept", encoding="utf-8")
    argv = _matrix_argv(tmp_path / "cache", "--allow-download", "--output", str(existing))

    assert cli.main(argv) == 2

    assert capsys.readouterr().err == "retrieval-v2 error: --output must not already exist\n"
    assert existing.read_text(encoding="utf-8") == "kept"


def test_an_internal_acquisition_worker_prefetches_the_selected_models(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    selections: list[tuple[object, Path]] = []

    def prefetch(selection: object, *, cache_root: Path) -> dict[str, Any]:
        selections.append((selection, cache_root))
        return {"prefetched": True}

    monkeypatch.setattr(cli, "load_model_selection", lambda *paths, **ids: ids)
    monkeypatch.setattr(cli, "prefetch_models", prefetch)
    argv = _matrix_argv(tmp_path / "cache", "--allow-download", "--internal-worker", "--json")

    assert cli.main(argv) == 0

    expected_ids = {"model_id": "model-a", "variant_id": "variant-a", "reranker_id": None}
    assert selections == [(expected_ids, tmp_path / "cache")]
    assert capsys.readouterr().out == '{"prefetched":true}\n'


def test_a_worker_that_returns_no_payload_is_an_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(orchestration, "run_bounded_model_worker", RecordingWorker({"r": 1}))

    assert cli.main(_matrix_argv(tmp_path / "cache")) == 2

    assert capsys.readouterr().err == (
        "retrieval-v2 error: benchmark worker did not return a worker payload\n"
    )


@pytest.mark.parametrize(
    ("passed", "degraded", "exit_code"), [(True, False, 0), (True, True, 2), (False, False, 2)]
)
def test_a_validated_worker_report_on_stdout_is_marked_non_quality(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    passed: bool,
    degraded: bool,
    exit_code: int,
) -> None:
    report = _gated_report(passed, degraded)
    payload = contract.WorkerPayload(report=report, canonical_bytes=b"{}")
    validated: list[tuple[object, ...]] = []
    monkeypatch.setattr(orchestration, "run_bounded_model_worker", RecordingWorker(payload))
    monkeypatch.setattr(cli, "validate_worker_payload", lambda *given: validated.append(given))

    assert cli.main(_matrix_argv(tmp_path / "cache", "--json")) == exit_code

    printed = json.loads(capsys.readouterr().out)
    assert printed["gates"]["interpretation"] == "stdout-only-non-quality"
    assert "interpretation" not in report["gates"]
    assert validated[0][:2] == (report, b"{}")


def test_a_validated_worker_report_written_to_a_file_keeps_its_interpretation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    payload = contract.WorkerPayload(report=_gated_report(True, False), canonical_bytes=b"{}")
    monkeypatch.setattr(orchestration, "run_bounded_model_worker", RecordingWorker(payload))
    monkeypatch.setattr(cli, "validate_worker_payload", lambda *given: None)
    output = tmp_path / "report.json"

    assert cli.main(_matrix_argv(tmp_path / "cache", "--output", str(output))) == 0

    assert "interpretation" not in json.loads(output.read_text(encoding="utf-8"))["gates"]
    assert capsys.readouterr().out == (
        "retrieval-v2 model-matrix complete; quality_claim=false; release_evidence=false\n"
    )


def test_a_lexical_run_without_a_cache_root_uses_a_temporary_one_that_is_removed(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    caches: list[tuple[Path, bool]] = []

    def run_benchmark(corpus: dict[str, Any], *, cache_root: Path, **options: Any) -> Any:
        caches.append((cache_root, cache_root.is_dir()))
        return _gated_report(True, False) | {"adapter_kind": "deterministic-fake"}

    monkeypatch.setattr(run_module, "run_benchmark", run_benchmark)

    assert cli.main([]) == 0

    assert caches[0][1] is True
    assert caches[0][0].name.startswith("llm-wiki-retrieval-v2-")
    assert caches[0][0].exists() is False
    assert capsys.readouterr().out.startswith("retrieval-v2 deterministic-fake complete;")


def _lexical_report(*args: Any, **options: Any) -> dict[str, Any]:
    return _gated_report(True, False) | {"adapter_kind": "deterministic-fake"}


def test_a_relative_report_output_is_written_under_the_working_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(run_module, "run_benchmark", _lexical_report)
    monkeypatch.chdir(tmp_path)

    assert cli.main(["--cache-root", str(tmp_path / "cache"), "--output", "report.json"]) == 0

    written = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    assert written["adapter_kind"] == "deterministic-fake"


def test_a_symlinked_report_output_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(run_module, "run_benchmark", _lexical_report)
    link = tmp_path / "report.json"
    link.symlink_to(tmp_path / "elsewhere.json")

    assert cli.main(["--cache-root", str(tmp_path / "cache"), "--output", str(link)]) == 2

    assert capsys.readouterr().err == "retrieval-v2 error: --output must not be a symlink\n"
    assert (tmp_path / "elsewhere.json").exists() is False


def test_an_existing_report_output_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(run_module, "run_benchmark", _lexical_report)
    existing = tmp_path / "report.json"
    existing.write_text("kept", encoding="utf-8")

    assert cli.main(["--cache-root", str(tmp_path / "cache"), "--output", str(existing)]) == 2

    assert capsys.readouterr().err == "retrieval-v2 error: --output must not already exist\n"
    assert existing.read_text(encoding="utf-8") == "kept"


def test_an_internal_model_worker_runs_the_benchmark_itself_under_the_lexical_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    received: dict[str, Any] = {}

    def run_benchmark(corpus: dict[str, Any], **options: Any) -> dict[str, Any]:
        received.update(options)
        return _gated_report(True, False)

    monkeypatch.setattr(run_module, "run_benchmark", run_benchmark)

    assert cli.main(_matrix_argv(tmp_path / "cache", "--internal-worker", "--json")) == 0

    assert received["lexical_deadline_seconds"] == 30.0
    assert received["cache_root"] == tmp_path / "cache"
    assert json.loads(capsys.readouterr().out) == _gated_report(True, False)
