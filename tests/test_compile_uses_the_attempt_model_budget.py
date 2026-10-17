"""A batch budget cannot outrun the provider actually asked to process it."""
from dataclasses import replace
from types import SimpleNamespace

import compile_memory as compiler
import llm_client as client

from tests.test_compile_packing_counts_original_entry_metadata import _inputs


def _attempt():
    inputs = _inputs()
    batch = compiler.pack_compile_batches(inputs, model=None)[0]
    packing = replace(batch.packing, max_input_tokens=90000)
    return compiler._CompileAttempt(inputs, None, replace(batch, packing=packing), None)


def _descriptor(window):
    return SimpleNamespace(model="actual-attempt-model",
                           _codex_basis=SimpleNamespace(planning_window=window))


def test_final_fit_uses_the_smaller_advertised_attempt_window(monkeypatch):
    observed = []

    def fit(*args, **kwargs):
        observed.append(kwargs["budget"])
        return False

    monkeypatch.setattr(compiler, "_compile_prompt_fits", fit)
    assert not _attempt()._fits("prompt", "system", {}, _descriptor(60000))
    assert observed[0].max_input_tokens == 60000
    assert observed[0].model == "actual-attempt-model"


def test_dispatch_uses_the_same_bounded_attempt_budget(monkeypatch):
    observed = []

    def call(*args, **kwargs):
        observed.append(kwargs["input_budget"])
        return "intercepted"

    monkeypatch.setattr(compiler, "call_candidate", call)
    assert _attempt()._call(_descriptor(60000), "prompt", "system", {}) == "intercepted"
    assert observed[0].max_input_tokens == 60000


def test_unknown_capacity_does_not_invent_a_provider_window(monkeypatch):
    observed = []

    def fit(*args, **kwargs):
        observed.append(kwargs["budget"])
        return True

    monkeypatch.setattr(compiler, "_compile_prompt_fits", fit)
    assert _attempt()._fits("prompt", "system", {}, _descriptor(None))
    assert observed[0].max_input_tokens == 90000


def test_refresh_retains_the_selected_model_and_declared_budget(monkeypatch):
    inputs = _inputs()
    batch = compiler.pack_compile_batches(inputs, model="actual-attempt-model")[0]
    seen = []
    empty = compiler.CompileInputs((), (), ())
    monkeypatch.setattr(compiler, "snapshot_compile_inputs", lambda paths: empty)
    original = compiler.pack_compile_batches

    def pack(inputs, **kwargs):
        seen.append(kwargs["model"])
        return original(inputs, **kwargs)

    monkeypatch.setattr(compiler, "pack_compile_batches", pack)
    compiler._refresh_compile_batch(batch)
    assert seen == ["actual-attempt-model"]


def test_run_resolves_once_before_packing_and_refresh_keeps_the_owned_descriptor(monkeypatch):
    descriptor = client.provider_candidates("codex", max_tokens=4000)[0]
    selected = replace(descriptor, model="actual-attempt-model")
    calls = []

    def resolve(candidate, *, deadline):
        calls.append(candidate)
        assert deadline <= compiler.time.monotonic() + client.worst_case_call_seconds("codex")
        return SimpleNamespace(descriptor=selected)

    monkeypatch.setattr(compiler, "provider_candidates", lambda *args, **kwargs: [descriptor])
    monkeypatch.setattr(compiler, "probe_candidate", lambda candidate: True)
    monkeypatch.setattr(compiler, "resolve_codex_planning_basis", resolve)
    batches, refused = compiler._pack_for_run(_inputs(), float("inf"))
    assert not refused and calls == [descriptor]
    batch = batches[0]
    assert batch.planning_model == "actual-attempt-model"
    assert compiler._compile_candidate_chain(batch)[0] is selected
    monkeypatch.setattr(compiler, "snapshot_compile_inputs",
                        lambda paths: compiler.CompileInputs((), (), ()))
    refreshed = compiler._refresh_compile_batch(batch)
    assert compiler._compile_candidate_chain(refreshed)[0] is selected
    assert calls == [descriptor]


def test_each_fallback_keeps_its_own_model_and_capacity(monkeypatch):
    codex = client.provider_candidates("codex")[0]
    fallback = client.provider_candidates("fake")[0]
    owned = replace(codex, model="actual-attempt-model",
                    _codex_basis=SimpleNamespace(planning_window=60000))
    monkeypatch.setattr(compiler, "provider_candidates", lambda *args, **kwargs: [codex, fallback])
    monkeypatch.setattr(compiler, "probe_candidate", lambda candidate: True)
    monkeypatch.setattr(compiler, "resolve_codex_planning_basis",
                        lambda candidate, **kwargs: SimpleNamespace(descriptor=owned))
    batches, _ = compiler._pack_for_run(_inputs(), float("inf"))
    assert compiler._compile_candidate_chain(batches[0]) == (owned, fallback)
    attempt = _attempt()
    assert compiler._attempt_input_budget(attempt.batch, owned).max_input_tokens == 60000
    assert compiler._attempt_input_budget(attempt.batch, fallback).model == fallback.model
    assert compiler._attempt_input_budget(attempt.batch, fallback).max_input_tokens == 90000
