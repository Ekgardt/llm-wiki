"""The retrieval v2 command line fails closed and writes only what it was asked to write."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tests.retrieval_v2_helpers import ROOT, RUNNER

BENCHMARK = Path(__file__).resolve().parent.parent / "benchmark"
if str(BENCHMARK) not in sys.path:
    sys.path.insert(0, str(BENCHMARK))

import retrieval_v2_cli  # noqa: E402
import retrieval_v2_corpus  # noqa: E402
import retrieval_v2_orchestration  # noqa: E402
import retrieval_v2_run  # noqa: E402
import retrieval_v2_transformers  # noqa: E402


def test_cli_defaults_to_temp_cache_and_writes_only_when_requested(tmp_path):
    command = [sys.executable, str(RUNNER), "--json"]
    result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["quality_claim"] is False

    output = tmp_path / "report.json"
    result = subprocess.run(
        command + ["--output", str(output), "--cache-root", str(tmp_path / "cache")],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(output.read_text(encoding="utf-8"))["adapter_kind"] == "deterministic-fake"


def test_cli_fails_closed_for_invalid_corpus_and_paths(tmp_path):
    invalid = tmp_path / "invalid.json"
    invalid.write_text("{}", encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(RUNNER), "--corpus", str(invalid), "--json"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert not result.stdout.strip().startswith("{")

    existing_output = tmp_path / "existing.json"
    existing_output.write_text("unchanged", encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(RUNNER), "--output", str(existing_output)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert existing_output.read_text(encoding="utf-8") == "unchanged"

    dangling_target = tmp_path / "missing-target.json"
    dangling_output = tmp_path / "dangling-output.json"
    try:
        dangling_output.symlink_to(dangling_target)
    except OSError:
        pass
    else:
        result = subprocess.run(
            [sys.executable, str(RUNNER), "--output", str(dangling_output)],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode != 0
        assert not dangling_target.exists()

    result = subprocess.run(
        [sys.executable, str(RUNNER), "--model", "example/model"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0

    result = subprocess.run(
        [sys.executable, str(RUNNER), "--cache-root", str(ROOT / "cache")],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0


def test_output_publication_fails_closed_if_parent_identity_changes(tmp_path, monkeypatch):
    output_parent = tmp_path / "output"
    output = output_parent / "report.json"

    monkeypatch.setattr(retrieval_v2_corpus, "load_corpus", lambda *args: {"corpus_id": "test"})
    monkeypatch.setattr(
        retrieval_v2_run,
        "run_benchmark",
        lambda *args, **kwargs: {
            "gates": {"passed_for_orchestration": True},
            "quality_claim": False,
        },
    )
    monkeypatch.setattr(os.path, "samestat", lambda left, right: False)

    assert retrieval_v2_cli.main(["--output", str(output), "--cache-root", str(tmp_path / "cache")]) == 2


def test_run_benchmark_help_describes_v2_default_and_task_10_reservations():
    result = subprocess.run(
        [sys.executable, str(BENCHMARK / "run_benchmark.py"), "--help"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert "retrieval-v2" in result.stdout
    assert "default" in result.stdout
    assert "--legacy-only" in result.stdout
    assert "Task 10" in result.stdout


def test_v2_cli_returns_nonzero_when_orchestration_gate_fails(monkeypatch, tmp_path):
    monkeypatch.setattr(retrieval_v2_corpus, "load_corpus", lambda *args: {"corpus_id": "test"})
    monkeypatch.setattr(
        retrieval_v2_run,
        "run_benchmark",
        lambda *args, **kwargs: {
            "gates": {"passed_for_orchestration": False},
            "quality_claim": False,
        },
    )

    assert retrieval_v2_cli.main(["--cache-root", str(tmp_path / "cache")]) == 2


def test_cli_requires_explicit_valid_lexical_configuration(tmp_path):
    result = subprocess.run(
        [
            sys.executable,
            str(RUNNER),
            "--lexical-config",
            "L0",
            "--cache-root",
            str(tmp_path / "cache"),
            "--json",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode in {0, 2}
    assert json.loads(result.stdout)["methodology"]["lexical_configuration"]["id"] == "L0"
    invalid = subprocess.run(
        [sys.executable, str(RUNNER), "--lexical-config", "unknown"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert invalid.returncode != 0


def test_real_cli_contract_requires_every_explicit_selector(tmp_path):
    parser = retrieval_v2_orchestration.parser()
    base = ["--adapter", "model-matrix"]
    for args in (
        base,
        base + ["--model-id", "BAAI/bge-small-en-v1.5"],
        base
        + [
            "--model-id",
            "BAAI/bge-small-en-v1.5",
            "--variant-id",
            "float32-384d",
            "--cache-root",
            str(tmp_path),
        ],
    ):
        parsed = parser.parse_args(args)
        with pytest.raises(ValueError, match="requires explicit"):
            retrieval_v2_orchestration.validate_cli_args(parsed)

    parsed = parser.parse_args(
        base
        + [
            "--model-id",
            "BAAI/bge-small-en-v1.5",
            "--variant-id",
            "float32-384d",
            "--cache-root",
            str(tmp_path),
            "--lexical-config",
            "L0",
            "--vector-backend",
            "numpy-exact",
        ]
    )
    with pytest.raises(ValueError, match="deadline-seconds"):
        retrieval_v2_orchestration.validate_cli_args(parsed)
    parsed.deadline_seconds = 30.0
    retrieval_v2_orchestration.validate_cli_args(parsed)


def test_internal_qwen_worker_uses_overall_deadline_for_lexical_lifetime(
    tmp_path, monkeypatch
):
    np = pytest.importorskip("numpy")
    now = [0.0]
    monkeypatch.setattr(time, "perf_counter", lambda: now[0])

    def delayed_loader(*args, **kwargs):
        del args, kwargs
        now[0] = 31.0
        return lambda texts, **options: np.ones((len(texts), 384), dtype=np.float32)

    monkeypatch.setattr(retrieval_v2_transformers, "load_transformer_embedding", delayed_loader)
    output = tmp_path / "qwen-worker-report.json"
    result = retrieval_v2_cli.main(
        [
            "--internal-worker",
            "--adapter",
            "model-matrix",
            "--model-id",
            "Qwen/Qwen3-Embedding-0.6B",
            "--variant-id",
            "float32-384d",
            "--cache-root",
            str(tmp_path / "cache"),
            "--lexical-config",
            "L0",
            "--vector-backend",
            "numpy-exact",
            "--deadline-seconds",
            "1200",
            "--output",
            str(output),
        ]
    )

    assert result in {0, 2}
    report = json.loads(output.read_bytes())
    assert report["effective_mode"] == "model-matrix"
    assert report["measurements"]["model_load_ms"] == 31_000.0
