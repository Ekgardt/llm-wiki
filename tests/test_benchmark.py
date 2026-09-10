"""`run_benchmark.py` is the retrieval-v2 entry point; the legacy gate is retired."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


def _benchmark_module():
    path = Path(__file__).resolve().parent.parent / "benchmark" / "run_benchmark.py"
    spec = importlib.util.spec_from_file_location("run_benchmark", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_the_default_run_is_retrieval_v2(monkeypatch):
    benchmark = _benchmark_module()
    received: list[list[str]] = []
    fake = type(sys)("run_retrieval_v2")
    fake.main = lambda args: received.append(list(args)) or 0
    monkeypatch.setitem(sys.modules, "run_retrieval_v2", fake)

    assert benchmark.main(["--retrieval-v2", "--json"]) == 0
    assert received == [["--json"]]


def test_the_legacy_gate_is_retired(capsys):
    benchmark = _benchmark_module()

    assert benchmark.main(["--legacy-only"]) == 2
    assert "retired" in capsys.readouterr().err


def test_no_legacy_corpus_ships():
    assert not (Path(__file__).resolve().parent.parent / "benchmark" / "legacy-60-v1.json").exists()
