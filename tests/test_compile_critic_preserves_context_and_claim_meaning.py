"""The critic must see selected knowledge and the meaning of verified claims."""
import json

import compile_memory as cm

from tests.test_compile_claims_producer import _candidate, _daily, _operation
from tests.test_compile_claims_producer import vault as vault


def _review_inputs(root):
    daily = _daily(root)
    note = root / 'knowledge/notes/project-rule.md'
    note.write_text('---\ntype: decision\n---\n# Project rule\nThe lease rule applies only to the selected project.\n')
    inputs = cm.snapshot_compile_inputs([daily])
    operation = _operation([_candidate()])
    return inputs, cm._with_derived_claims([operation], inputs)


def test_critic_receives_complete_selected_existing_context(vault):
    root, _state = vault
    inputs, operations = _review_inputs(root)
    prompt = cm._critique_prompt(inputs, operations)
    assert '### FILE: knowledge/notes/project-rule.md\n' in prompt
    assert 'The lease rule applies only to the selected project.' in prompt


def test_critic_receives_bound_semantic_claim_not_just_literal(vault):
    root, _state = vault
    inputs, operations = _review_inputs(root)
    prompt = cm._critique_prompt(inputs, operations)
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
    operations = cm._with_derived_claims([operation], inputs)
    prompt = cm._critique_prompt(inputs, operations)
    encoded = prompt.split('\nOPERATIONS\n', 1)[1].split('\nCITED EVIDENCE\n', 1)[0]
    claims = json.loads(encoded)[0]['claims']
    assert [record['subject'] for record in claims] == ['maintenance lease', 'project maintenance lease']
    assert [record['evidence_index'] for record in claims] == [0, 0]


def test_forged_model_project_is_not_critic_authority(vault):
    root, _state = vault
    inputs, operations = _review_inputs(root)
    operations[0]['project'] = 'invented-model-project'
    prompt = cm._critique_prompt(inputs, operations)
    assert 'invented-model-project' not in prompt


def test_unselected_existing_page_is_not_invented_context(vault):
    root, _state = vault
    inputs, operations = _review_inputs(root)
    selected = cm._subset_compile_inputs(inputs, {part.part_key for part in inputs.dailies}, set())
    prompt = cm._critique_prompt(selected, operations)
    assert '### FILE: knowledge/notes/project-rule.md\n' not in prompt


def test_selected_context_is_protected_before_actual_cli_stdin(vault, monkeypatch, tmp_path):
    import llm_client as client

    from tests.test_codex_counts_the_prepared_invocation import _service
    from tests.test_llm_descriptors import _write_dlp_policy

    root, _state = vault
    inputs, operations = _review_inputs(root)
    prompt = cm._critique_prompt(inputs, operations)
    _, _, captured = _service(monkeypatch, tmp_path)
    policy = tmp_path / 'policy.json'
    _write_dlp_policy(policy, literals=('The lease rule applies only to the selected project.',))
    monkeypatch.setenv('LLM_WIKI_DLP_POLICY', str(policy))
    descriptor = client.provider_candidates('codex', max_tokens=4000)[0]
    result = client.call_candidate(descriptor, prompt, cm.CRITIQUE_SYSTEM, max_tokens=4000, available=True)
    assert result.text == 'answer'
    assert len(captured) == 1
    assert 'The lease rule applies only to the selected project.' not in captured[0][1]
    assert 'SELECTED EXISTING CONTEXT' in captured[0][1]
