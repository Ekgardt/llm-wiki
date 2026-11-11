"""The critic must see selected knowledge and the meaning of verified claims."""
import json

import pytest
from llm_wiki import compile_pipeline_operations, compile_pipeline_prompts, compile_pipeline_sources

from tests.compile_claims_producer_helpers import _candidate, _daily, _operation
from tests.compile_claims_producer_helpers import vault as vault


def _review_inputs(root):
    daily = _daily(root)
    note = root / 'knowledge/notes/project-rule.md'
    note.write_text('---\ntype: decision\n---\n# Project rule\nThe lease rule applies only to the selected project.\n')
    inputs = compile_pipeline_sources.snapshot_compile_inputs([daily])
    operation = _operation([_candidate()])
    return inputs, compile_pipeline_operations.with_derived_claims([operation], inputs)


def test_critic_receives_complete_selected_existing_context(vault):
    root, _state = vault
    inputs, operations = _review_inputs(root)
    prompt = compile_pipeline_prompts.critique_prompt(inputs, operations)
    assert '### FILE: knowledge/notes/project-rule.md\n' in prompt
    assert 'The lease rule applies only to the selected project.' in prompt


def test_critic_receives_bound_semantic_claim_not_just_literal(vault):
    root, _state = vault
    inputs, operations = _review_inputs(root)
    prompt = compile_pipeline_prompts.critique_prompt(inputs, operations)
    encoded = prompt.split('\nOPERATIONS\n', 1)[1].split('\nCITED EVIDENCE\n', 1)[0]
    record = json.loads(encoded)[0]['claims'][0]
    assert record['subject'] == 'maintenance lease'
    assert record['relation'] == 'ends-at'
    assert record['value'] == {'type': 'number', 'value': '30', 'unit': 'seconds'}
    assert record['evidence_index'] == 0
    assert 'id' not in record
    assert 'fingerprint' not in record


def test_distinct_claims_sharing_exact_evidence_both_reach_critic(vault):
    root, _state = vault
    inputs, _operations = _review_inputs(root)
    operation = _operation([_candidate(), _candidate(subject='project maintenance lease')])
    operations = compile_pipeline_operations.with_derived_claims([operation], inputs)
    prompt = compile_pipeline_prompts.critique_prompt(inputs, operations)
    encoded = prompt.split('\nOPERATIONS\n', 1)[1].split('\nCITED EVIDENCE\n', 1)[0]
    claims = json.loads(encoded)[0]['claims']
    assert [record['subject'] for record in claims] == ['maintenance lease', 'project maintenance lease']
    assert [record['evidence_index'] for record in claims] == [0, 0]


def test_forged_model_project_is_not_critic_authority(vault):
    root, _state = vault
    inputs, operations = _review_inputs(root)
    operations[0]['project'] = 'invented-model-project'
    prompt = compile_pipeline_prompts.critique_prompt(inputs, operations)
    assert 'invented-model-project' not in prompt


def test_sorted_citations_keep_operation_local_claim_addresses(vault):
    from tests.compile_claims_producer_helpers import QUOTE, SECOND_QUOTE, _evidence

    root, _state = vault
    inputs, _ = _review_inputs(root)
    first = _operation([_candidate()])
    first['evidence'] = [_evidence(QUOTE), _evidence(SECOND_QUOTE)]
    second = _operation([_candidate(evidence_index=1)])
    second['slug'] = 'second-lease'
    second['evidence'] = list(reversed(first['evidence']))
    operations = compile_pipeline_operations.with_derived_claims([first, second], inputs)
    prompt = compile_pipeline_prompts.critique_prompt(inputs, operations)
    citations = json.loads(prompt.split('\nCITED EVIDENCE\n', 1)[1])
    addressed = {(row['operation_slug'], row['evidence_index']): row for row in citations}
    assert len(addressed) == 4
    assert addressed[(first['slug'], 0)]['quoted_text'] == QUOTE
    assert addressed[(second['slug'], 1)]['quoted_text'] == QUOTE
    assert addressed[(first['slug'], 1)]['quoted_text'] == SECOND_QUOTE
    assert addressed[(second['slug'], 0)]['quoted_text'] == SECOND_QUOTE


def test_unselected_existing_page_is_not_invented_context(vault):
    root, _state = vault
    inputs, operations = _review_inputs(root)
    selected = compile_pipeline_sources.subset_compile_inputs(inputs, {part.part_key for part in inputs.dailies}, set())
    prompt = compile_pipeline_prompts.critique_prompt(selected, operations)
    assert '### FILE: knowledge/notes/project-rule.md\n' not in prompt


@pytest.mark.parametrize('prompt_builder,heading', [
    (compile_pipeline_prompts.critique_prompt, 'SELECTED EXISTING CONTEXT'),
    (compile_pipeline_prompts.source_review_prompt, 'COMPLETE SELECTED SOURCES'),
])
def test_selected_context_is_protected_before_actual_cli_stdin(vault, monkeypatch, tmp_path, prompt_builder, heading):
    import llm_client as client

    from tests.codex_counts_the_prepared_invocation_helpers import _service
    from tests.llm_descriptors_helpers import _write_dlp_policy

    root, _state = vault
    inputs, operations = _review_inputs(root)
    prompt = prompt_builder(inputs, operations)
    _, _, captured = _service(monkeypatch, tmp_path)
    policy = tmp_path / 'policy.json'
    _write_dlp_policy(policy, literals=('The lease rule applies only to the selected project.',))
    monkeypatch.setenv('LLM_WIKI_DLP_POLICY', str(policy))
    descriptor = client.provider_candidates('codex', max_tokens=4000)[0]
    result = client.call_candidate(descriptor, prompt, compile_pipeline_prompts.CRITIQUE_SYSTEM, max_tokens=4000, available=True)
    assert result.text == 'answer'
    assert len(captured) == 1
    assert 'The lease rule applies only to the selected project.' not in captured[0][1]
    assert heading in captured[0][1]
