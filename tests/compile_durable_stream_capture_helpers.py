"""Helpers shared by test_compile_durable_stream_capture.py and the test files importing them."""

from llm_wiki import runtime_roots


def _paths(case):
    inputs, _ = case
    return sorted({runtime_roots.vault_root() / part.logical_path for part in inputs.dailies})
