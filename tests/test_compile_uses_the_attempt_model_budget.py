"""A batch budget cannot outrun the provider actually asked to process it."""
import time
from dataclasses import replace
from types import SimpleNamespace

import compile_pipeline
import llm_client as client
from llm_wiki import (
    compile_pipeline_base,
    compile_pipeline_batches,
    compile_pipeline_prompts,
    compile_pipeline_sources,
)

from tests.compile_packing_counts_original_entry_metadata_helpers import _inputs
from tests.compile_paths import point_compile_path
from tests.compile_uses_the_attempt_model_budget_helpers import _attempt


def _descriptor(window):
    return SimpleNamespace(provider="codex", model="actual-attempt-model",
                           _codex_basis=SimpleNamespace(model="actual-attempt-model", planning_window=window))


def test_final_fit_uses_the_smaller_advertised_attempt_window(monkeypatch):
    observed = []

    def fit(*args, **kwargs):
        observed.append(kwargs["budget"])
        return False

    point_compile_path(monkeypatch, "compile_prompt_fits", fit)
    assert not _attempt()._fits("prompt", "system", {}, _descriptor(60000))
    assert observed[0].max_input_tokens == 60000
    assert observed[0].model == "actual-attempt-model"


def test_dispatch_uses_the_same_bounded_attempt_budget(monkeypatch):
    observed = []

    def call(*args, **kwargs):
        observed.append(kwargs["input_budget"])
        return "intercepted"

    point_compile_path(monkeypatch, "call_candidate", call)
    assert _attempt()._call(_descriptor(60000), "prompt", "system", {}) == "intercepted"
    assert observed[0].max_input_tokens == 60000


def test_unknown_capacity_does_not_invent_a_provider_window(monkeypatch):
    observed = []

    def fit(*args, **kwargs):
        observed.append(kwargs["budget"])
        return True

    point_compile_path(monkeypatch, "compile_prompt_fits", fit)
    assert _attempt()._fits("prompt", "system", {}, _descriptor(None))
    assert observed[0].max_input_tokens == 90000


def test_refresh_retains_the_selected_model_and_declared_budget(monkeypatch):
    inputs = _inputs()
    batch = compile_pipeline_batches.pack_compile_batches(inputs, model="actual-attempt-model")[0]
    seen = []
    empty = compile_pipeline_sources.CompileInputs((), (), ())
    point_compile_path(monkeypatch, "snapshot_compile_inputs", lambda paths: empty)
    original = compile_pipeline_batches.pack_compile_batches

    def pack(inputs, **kwargs):
        seen.append(kwargs["model"])
        return original(inputs, **kwargs)

    point_compile_path(monkeypatch, "pack_compile_batches", pack)
    compile_pipeline_batches.refresh_compile_batch(batch)
    assert seen == ["actual-attempt-model"]


def test_run_resolves_once_before_packing_and_refresh_keeps_the_owned_descriptor(monkeypatch):
    descriptor = client.provider_candidates("codex", max_tokens=4000)[0]
    selected = replace(descriptor, model="actual-attempt-model")
    calls = []

    def resolve(candidate, *, deadline):
        calls.append(candidate)
        assert deadline <= time.monotonic() + client.worst_case_call_seconds("codex")
        return SimpleNamespace(descriptor=selected)

    point_compile_path(monkeypatch, "provider_candidates", lambda *args, **kwargs: [descriptor])
    point_compile_path(monkeypatch, "probe_candidate", lambda candidate: True)
    point_compile_path(monkeypatch, "resolve_codex_planning_basis", resolve)
    batches, refused = compile_pipeline.pack_for_run(_inputs(), float("inf"))
    assert not refused and calls == [descriptor]
    batch = batches[0]
    assert batch.planning_model == "actual-attempt-model"
    assert compile_pipeline_batches.compile_candidate_chain(batch)[0] is selected
    point_compile_path(monkeypatch, "snapshot_compile_inputs", lambda paths: compile_pipeline_sources.CompileInputs((), (), ()))
    refreshed = compile_pipeline_batches.refresh_compile_batch(batch)
    assert compile_pipeline_batches.compile_candidate_chain(refreshed)[0] is selected
    assert calls == [descriptor]


def test_each_fallback_keeps_its_own_model_and_capacity(monkeypatch):
    codex = client.provider_candidates("codex")[0]
    fallback = client.provider_candidates("fake")[0]
    owned = replace(codex, model="actual-attempt-model",
                    _codex_basis=SimpleNamespace(model="actual-attempt-model", planning_window=60000))
    point_compile_path(monkeypatch, "provider_candidates", lambda *args, **kwargs: [codex, fallback])
    point_compile_path(monkeypatch, "probe_candidate", lambda candidate: True)
    point_compile_path(monkeypatch, "resolve_codex_planning_basis", lambda candidate, **kwargs: SimpleNamespace(descriptor=owned))
    batches, _ = compile_pipeline.pack_for_run(_inputs(), float("inf"))
    assert compile_pipeline_batches.compile_candidate_chain(batches[0]) == (owned, fallback)
    attempt = _attempt()
    assert compile_pipeline_prompts.attempt_input_budget(attempt.batch, owned).max_input_tokens == 60000
    assert compile_pipeline_prompts.attempt_input_budget(attempt.batch, fallback).model == fallback.model
    assert compile_pipeline_prompts.attempt_input_budget(attempt.batch, fallback).max_input_tokens == compile_pipeline_base.compile_budget(fallback.model).max_input_tokens


def test_fallback_with_a_different_models_basis_cannot_borrow_capacity():
    fallback = SimpleNamespace(provider='codex', model='fallback-model',
                               _codex_basis=SimpleNamespace(model='another-model', planning_window=258400))
    budget = compile_pipeline_prompts.attempt_input_budget(_attempt().batch, fallback, needs_complete_context=True)
    assert budget.max_input_tokens == compile_pipeline_base.compile_budget(fallback.model).max_input_tokens
