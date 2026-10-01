"""Repeated timestamp selection must not hide the failed literal from retry."""

import json

import pytest
from compile_cache import CompileCache
from llm_client import LLMResult
from markdown_transaction import MarkdownCoordinator

from tests.test_compile_transactions import _draft_response, _pass_review, _provider
from tests.test_compile_transactions import vault as vault

EXACT = "A durable `exact-byte` observation."
ALTERED = "A durable exact-byte observation."


def _inputs(root):
    import compile_memory as compiler

    daily = root / "knowledge/daily/2026-07-14.md"
    daily.write_text(
        "## [10:00:00] prompt | first\nA different entry.\n"
        f"## [10:00:00] pre-compact | second\n{EXACT}\n",
    )
    return compiler.snapshot_compile_inputs([daily])


def _evidence(quote):
    return {"daily_date": "2026-07-14", "timestamp": "10:00:00",
            "quoted_text": quote, "claim": "The source is immutable."}


def test_repeated_timestamp_names_the_rejected_literal(vault):
    import compile_memory as compiler

    inputs = _inputs(vault[0])
    with pytest.raises(ValueError) as refused:
        compiler._bound_evidence_block(_evidence(ALTERED), inputs)
    assert json.dumps(ALTERED) in str(refused.value)
    assert "exact occurrences: 0" in str(refused.value)
    assert "backticks" in str(refused.value)
    binding, block = compiler._bound_evidence_block(_evidence(EXACT), inputs)
    assert binding["quote_text"] == EXACT
    assert EXACT.encode() in block


def test_failed_literal_reaches_the_next_provider_prompt(vault, monkeypatch):
    import compile_memory as compiler

    root, state = vault
    inputs = _inputs(root)
    corrected = json.loads(_draft_response())
    corrected["operations"][0]["evidence"][0] = _evidence(EXACT)
    replies = [_draft_response(), json.dumps(corrected), _pass_review()]
    prompts = []
    monkeypatch.setattr(compiler, "provider_candidates", lambda *a, **kw: [_provider()])
    monkeypatch.setattr(compiler, "probe_candidate", lambda descriptor: True)

    def call(descriptor, prompt, system_prompt, **kwargs):
        prompts.append(prompt)
        return LLMResult(descriptor, replies.pop(0), True, None, "native")

    monkeypatch.setattr(compiler, "call_candidate", call)
    resolved = compiler.resolve_compile_plan(
        inputs, CompileCache(state), coordinator=MarkdownCoordinator(root, state),
    )
    assert len(prompts) == 3
    feedback = json.loads(prompts[1].split("PREVIOUS VALIDATION FAILURE")[-1].splitlines()[1])
    assert "quoted_text=" + json.dumps(ALTERED) in feedback
    assert "exact occurrences: 0" in feedback
    assert compiler.validate_compile_plan(resolved.plan, inputs)
