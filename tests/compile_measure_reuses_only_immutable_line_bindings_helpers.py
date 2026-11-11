"""Helpers shared by test_compile_measure_reuses_only_immutable_line_bindings.py and the test files importing them."""

from llm_wiki import compile_pipeline_operations

from tests.compile_paths import point_compile_path


def _observed_bindings(monkeypatch):
    calls = []
    original = compile_pipeline_operations.bound_legacy_evidence

    def observed(*args):
        calls.append(args[:3])
        return original(*args)

    point_compile_path(monkeypatch, 'bound_legacy_evidence', observed)
    return calls
