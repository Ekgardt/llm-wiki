"""Packing and final fit include the local Codex input's actual framing."""
import json

import compile_pipeline
import llm_client as client
from context_budget import ContextBudget
from llm_wiki import (
    compile_pipeline_base,
    compile_pipeline_batches,
    compile_pipeline_prompts,
    compile_pipeline_sources,
)

from tests.codex_counts_the_prepared_invocation_helpers import _service
from tests.compile_packing_counts_original_entry_metadata_helpers import _inputs
from tests.compile_plans_the_local_provider_layout_helpers import _adversarial_count, _byte_count
from tests.llm_descriptors_helpers import _write_dlp_policy


def _codex_text(prompt, system, schema):
    protected = client.protected_transport(client.prompted_system(system, schema, 'prompt'), prompt, schema)
    assert not isinstance(protected, client.Blocked)
    return client.codex_prompt(protected.system_prompt, protected.prompt)


def test_codex_draft_planning_counts_the_complete_local_stdin(monkeypatch):
    monkeypatch.setenv('MEMORY_LLM_PROVIDER', 'codex')
    inputs = compile_pipeline_sources.CompileInputs((), (), ())
    schema = json.loads(compile_pipeline.canonical_json_bytes(compile_pipeline_base.RAW_PLAN_SCHEMA))
    evidence = schema['properties']['operations']['items']['properties']['evidence']
    evidence['items'] = json.loads(compile_pipeline.canonical_json_bytes(compile_pipeline_base.LEGACY_EVIDENCE_SCHEMA))
    actual = _codex_text(compile_pipeline_prompts.draft_prompt(inputs), compile_pipeline_base.DRAFT_SYSTEM, schema)
    assert compile_pipeline_prompts.draft_prompt_text(inputs) == actual


def test_final_fit_rejects_the_old_unframed_budget(monkeypatch):
    monkeypatch.setenv('MEMORY_LLM_PROVIDER', 'codex')
    prompt, system, schema = 'Полный e\u0301 факт', 'system', {'type': 'object'}
    old = f'{system}\n{compile_pipeline.canonical_json_bytes(schema).decode()}\n{prompt}'
    budget = ContextBudget(None, len(old.encode()) + 2, 1, 1)
    assert len(_codex_text(prompt, system, schema).encode()) > budget.available_input_tokens
    assert not compile_pipeline_prompts.compile_prompt_fits(prompt, system=system, schema=schema, model=None,
                                            token_adapters=None, budget=budget)


def test_token_counter_checks_each_layout_not_the_longest_utf8(monkeypatch):
    monkeypatch.delenv('MEMORY_LLM_PROVIDER', raising=False)
    inputs = compile_pipeline_sources.CompileInputs((), (), ())
    count = compile_pipeline_batches.draft_prompt_count(inputs, 'model', {'model': _adversarial_count})
    assert count.tokens == 10000
    assert compile_pipeline_batches.TokenBatchMeasure(inputs, 'model', {'model': _adversarial_count})(set()) == 10000


def _budget_for(text):
    return ContextBudget('proof-model', len(text.encode()) + 2, 1, 1)


def test_changed_dlp_payload_is_checked_again_before_dispatch(monkeypatch, tmp_path):
    _, _, captured = _service(monkeypatch, tmp_path)
    descriptor = client.provider_candidates('codex', max_tokens=4000)[0]
    initial = _codex_text('x', 'system', None)
    policy = tmp_path / 'policy.json'
    _write_dlp_policy(policy, literals=('x',))
    monkeypatch.setenv('LLM_WIKI_DLP_POLICY', str(policy))
    assert len(_codex_text('x', 'system', None).encode()) > len(initial.encode())
    result = client.call_candidate(descriptor, 'x', 'system', max_tokens=4000,
                                   available=True, input_budget=_budget_for(initial),
                                   token_adapters={'proof-model': _byte_count})
    assert result.failure_class == 'context_overflow' and result.text is None
    assert captured == []


def test_a_fitting_prepared_codex_payload_is_dispatched_once(monkeypatch, tmp_path):
    _, selected, captured = _service(monkeypatch, tmp_path)
    descriptor = client.provider_candidates('codex', max_tokens=4000)[0]
    text = _codex_text('exact e\u0301 task', 'system', None)
    result = client.call_candidate(descriptor, 'exact e\u0301 task', 'system', max_tokens=4000,
                                   available=True, input_budget=_budget_for(text),
                                   token_adapters={'proof-model': _byte_count})
    assert result.text == 'answer' and result.failure_class is None
    assert len(captured) == 1 and captured[0][1] == text and selected['calls'] == 1


def test_persisted_packing_count_is_the_maximum_adapter_count(monkeypatch):
    monkeypatch.delenv('MEMORY_LLM_PROVIDER', raising=False)
    inputs = _inputs()
    keys = {part.part_key for part in inputs.dailies}
    batch = compile_pipeline_batches.compile_batch(inputs, keys, compile_pipeline_base.compile_budget('model'),
                                    model='model', token_adapters={'model': _adversarial_count})
    assert batch.packing.measured_input_tokens == 10000


def test_a_forced_non_codex_backend_does_not_gain_codex_framing(monkeypatch):
    monkeypatch.setenv('MEMORY_LLM_PROVIDER', 'fake')
    descriptor = client.provider_candidates('fake')[0]
    text = client.planning_input_text('user', 'system', {'type': 'object'})
    assert text == client.planning_input_text('user', 'system', {'type': 'object'}, descriptor=descriptor)
    assert not text.startswith('SYSTEM:') and client.TASK_FRAME not in text


def test_opaque_custom_backend_keeps_its_existing_dispatch_contract(monkeypatch):
    descriptor = client.provider_candidates('codex', max_tokens=4000)[0]
    calls = []

    def backend(*args):
        calls.append(args)
        return 'answer'

    monkeypatch.setitem(client.BACKENDS, 'codex', backend)
    result = client.call_candidate(descriptor, 'larger than this budget', 'system', max_tokens=4000,
                                   available=True, input_budget=ContextBudget(None, 3, 1, 1))
    assert result.text == 'answer' and len(calls) == 1
