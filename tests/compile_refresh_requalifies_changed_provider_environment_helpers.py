"""Helpers shared by test_compile_refresh_requalifies_changed_provider_environment.py and the test files importing them."""

from llm_wiki import compile_pipeline_sources


def _empty_context(paths):
    return compile_pipeline_sources.CompileInputs((), (), ())
