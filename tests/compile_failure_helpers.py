"""Helpers shared by test_compile_failure.py and the test files importing them."""
from __future__ import annotations

from tests.compile_paths import point_compile_path


def _attempt(monkeypatch, outcomes: list[str]):
    """One provider attempt whose draft stage yields the given failure codes."""
    from llm_wiki import compile_pipeline_prompts, compile_pipeline_sources

    calls: list[int] = []

    def staged(self, descriptor, actions):
        index = len(calls)
        calls.append(1)
        if index >= len(outcomes):
            return "plan"
        return self._record("critique", descriptor, outcomes[index])

    point_compile_path(monkeypatch, "probe_candidate", lambda _d: True)
    monkeypatch.setattr(
        compile_pipeline_prompts.CompileAttempt, "_action", lambda self, _d: ((), ())
    )
    monkeypatch.setattr(
        compile_pipeline_prompts.CompileAttempt, "_cached", lambda self, _a, _d: None
    )
    monkeypatch.setattr(compile_pipeline_prompts.CompileAttempt, "_drafted", staged)
    attempt = compile_pipeline_prompts.CompileAttempt(
        compile_pipeline_sources.CompileInputs((), (), ()), None, None, None
    )
    return attempt, calls
