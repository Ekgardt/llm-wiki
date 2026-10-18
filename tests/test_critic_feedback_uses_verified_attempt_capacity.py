"""A source packing target must not prevent a repair the selected model can fit."""
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

import compile_memory as compiler
import llm_client as client
import pytest

from tests.test_compile_uses_the_attempt_model_budget import _attempt


def _repair(window=60000, *, basis_model="repair-model", provider="codex"):
    attempt = _attempt()
    attempt.batch = replace(attempt.batch, packing=replace(
        attempt.batch.packing, max_input_tokens=16000))
    descriptor = replace(client.provider_candidates(provider, max_tokens=4000)[0],
                         model="repair-model", _codex_basis=SimpleNamespace(
                             model=basis_model, planning_window=window))
    attempt.critic_feedback = ("Correct the rejected binding against its complete source.\n" * 700,)
    prompt = compiler._draft_feedback_prompt("The complete original source.", attempt.critic_feedback)
    return attempt, descriptor, prompt


def test_complete_repair_fits_verified_capacity_without_erasing_feedback():
    attempt, descriptor, prompt = _repair()

    assert not compiler._compile_prompt_fits(
        prompt, system="system", schema={}, model=descriptor.model,
        token_adapters=None, budget=compiler._attempt_input_budget(attempt.batch, descriptor),
        descriptor=descriptor)
    assert attempt._fits(prompt, "system", {}, descriptor)
    assert attempt.critic_feedback[0] in prompt


def test_dispatch_and_preflight_use_the_same_verified_repair_window(monkeypatch):
    attempt, descriptor, prompt = _repair()
    dispatch = Mock(return_value="intercepted")
    monkeypatch.setattr(compiler, "call_candidate", dispatch)

    assert attempt._fits(prompt, "system", {}, descriptor)
    assert attempt._call(descriptor, prompt, "system", {}) == "intercepted"
    budget = dispatch.call_args.kwargs["input_budget"]
    assert budget.max_input_tokens == 60000
    assert budget.reserved_output_tokens == attempt.batch.packing.reserved_output_tokens
    assert budget.safety_margin_tokens == attempt.batch.packing.safety_margin_tokens


@pytest.mark.parametrize("window,basis_model,provider", [
    (None, "repair-model", "codex"),
    (60000, "another-model", "codex"),
    (60000, "repair-model", "fake"),
], ids=["unknown-window", "other-model", "other-provider"])
def test_unverified_capacity_does_not_admit_a_larger_repair(window, basis_model, provider):
    attempt, descriptor, prompt = _repair(window, basis_model=basis_model, provider=provider)

    assert not attempt._fits(prompt, "system", {}, descriptor)
    assert attempt.critic_feedback[0] in prompt


def test_repair_larger_than_the_real_window_remains_refused():
    attempt, descriptor, prompt = _repair(20000)

    assert not attempt._fits(prompt, "system", {}, descriptor)
    assert attempt.critic_feedback[0] in prompt


def test_initial_draft_keeps_its_original_packing_budget(monkeypatch):
    attempt, descriptor, prompt = _repair()
    attempt.critic_feedback = ()
    dispatch = Mock(return_value="intercepted")
    monkeypatch.setattr(compiler, "call_candidate", dispatch)

    assert not attempt._fits(prompt, "system", {}, descriptor)
    assert attempt._call(descriptor, "small source", "system", {}) == "intercepted"
    assert dispatch.call_args.kwargs["input_budget"].max_input_tokens == 16000


def test_first_review_can_use_verified_capacity_without_prior_rejection():
    attempt, descriptor, prompt = _repair()
    attempt.critic_feedback = ()

    assert not attempt._fits(prompt, compiler.DRAFT_SYSTEM, {}, descriptor)
    assert attempt._fits(prompt, compiler.CRITIQUE_SYSTEM, compiler.CRITIQUE_SCHEMA, descriptor)
    assert attempt.critic_feedback == ()


def test_first_review_dispatch_uses_the_same_verified_capacity(monkeypatch):
    attempt, descriptor, prompt = _repair()
    attempt.critic_feedback = ()
    dispatch = Mock(return_value="intercepted")
    monkeypatch.setattr(compiler, "call_candidate", dispatch)

    assert attempt._call(descriptor, prompt, compiler.CRITIQUE_SYSTEM,
                         compiler.CRITIQUE_SCHEMA) == "intercepted"
    budget = dispatch.call_args.kwargs["input_budget"]
    assert budget.max_input_tokens == 60000
    assert budget.reserved_output_tokens == attempt.batch.packing.reserved_output_tokens
    assert budget.safety_margin_tokens == attempt.batch.packing.safety_margin_tokens
    assert dispatch.call_args.args[1] == prompt


@pytest.mark.parametrize("window,basis_model,provider", [
    (None, "repair-model", "codex"),
    (60000, "another-model", "codex"),
    (60000, "repair-model", "fake"),
    (20000, "repair-model", "codex"),
], ids=["unknown-window", "other-model", "other-provider", "real-overflow"])
def test_first_review_still_requires_matching_sufficient_capacity(window, basis_model, provider):
    attempt, descriptor, prompt = _repair(window, basis_model=basis_model, provider=provider)
    attempt.critic_feedback = ()

    assert not attempt._fits(prompt, compiler.CRITIQUE_SYSTEM,
                             compiler.CRITIQUE_SCHEMA, descriptor)


def test_native_review_retains_full_context_and_evidence_above_packing_target(tmp_path, monkeypatch):
    from tests.test_compile_critic_preserves_native_claim_origin import _native_operation

    inputs, operation = _native_operation(tmp_path, monkeypatch)
    content = b"Existing background retained for review.\n" * 700
    context = compiler.SourceSnapshot("knowledge/notes/background.md", content,
                                      compiler.sha256_bytes(content))
    inputs = replace(inputs, sources=(*inputs.sources, context))
    template, descriptor, _ = _repair()
    batch = replace(template.batch, inputs=inputs)
    attempt = compiler._CompileAttempt(inputs, None, batch, None)
    prompt = compiler._critique_prompt(inputs, [operation])

    assert not compiler._compile_prompt_fits(
        prompt, system=compiler.CRITIQUE_SYSTEM, schema=compiler.CRITIQUE_SCHEMA,
        model=descriptor.model, token_adapters=None,
        budget=compiler._attempt_input_budget(batch, descriptor), descriptor=descriptor)
    assert attempt._critique_batches(descriptor, [operation]) == [[operation]]
    assert content.decode() in prompt
    assert 'The bicycle is red.' in prompt
    assert 'The lease expires in 30 seconds.' in prompt
    assert attempt.critic_feedback == ()
