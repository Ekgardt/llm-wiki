"""Helpers shared by test_native_compile_projection_has_complete_citation_address.py and the test files importing them."""

from llm_wiki import compile_pipeline_sources

from tests.native_compile_uses_whole_container_helpers import _native_inputs


def _projected_inputs(root, monkeypatch, prompt):
    inputs, _ = _native_inputs(root, monkeypatch, prompt)
    return compile_pipeline_sources.subset_compile_inputs(inputs, {part.part_key for part in inputs.dailies})
