"""Two user lines share physical provenance without sharing a citation index."""
import json

import compile_memory as compiler
import pytest

from tests.test_compile_claims_producer import _candidate, _operation
from tests.test_native_compile_projection_has_complete_citation_address import _projected_inputs


def _native_operation(tmp_path, monkeypatch):
    inputs = _projected_inputs(tmp_path, monkeypatch, 'The bicycle is red.\nThe lease expires in 30 seconds.')
    choices = compiler._source_line_choices(inputs)
    operation = _operation([_candidate(evidence_index=1)])
    operation['evidence'] = [compiler._expand_choice_fields(
        {'source_line': row['source_line'], 'claim': row['quoted_text']}, choices) for row in choices]
    compiler._with_derived_claims([operation], inputs)
    return inputs, operation


def test_actual_critic_keeps_the_selected_line_inside_one_native_container(tmp_path, monkeypatch):
    inputs, operation = _native_operation(tmp_path, monkeypatch)
    bindings = [compiler._evidence_binding(item, inputs) for item in operation['evidence']]
    assert bindings[0]['reference'] == bindings[1]['reference']

    prompt = compiler._critique_prompt(inputs, [operation])

    assert '"evidence_index":1' in prompt.replace(' ', '')
    assert '"evidence_index":0' not in prompt.replace(' ', '')
    serialized = json.loads(compiler.canonical_json_bytes(operation))
    assert 'evidence_index' not in serialized['claims'][0]


def test_a_reordered_native_evidence_array_cannot_retarget_a_derived_claim(tmp_path, monkeypatch):
    inputs, operation = _native_operation(tmp_path, monkeypatch)
    operation['evidence'].reverse()

    with pytest.raises(ValueError, match='claim.*(origin|citation|ambiguous)'):
        compiler._critique_prompt(inputs, [operation])


def test_historical_claim_with_ambiguous_container_provenance_is_not_guessed(tmp_path, monkeypatch):
    inputs, operation = _native_operation(tmp_path, monkeypatch)
    operation = json.loads(compiler.canonical_json_bytes(operation))

    with pytest.raises(ValueError, match='claim.*ambiguous'):
        compiler._critique_prompt(inputs, [operation])


def test_feedback_preserves_selected_native_line_and_recreates_the_exact_ledger(tmp_path, monkeypatch):
    inputs, operation = _native_operation(tmp_path, monkeypatch)
    reviews = [{'slug': operation['slug'], 'verdict': 'drop', 'reason': 'Check the scoped meaning.'}]
    original = compiler.canonical_json_bytes(operation)

    feedback = compiler._validated_critique_feedback([operation], reviews, inputs=inputs)

    record = json.loads(feedback[0])
    candidate = record['operations'][0]['claims'][0]
    assert candidate['evidence_index'] == 1
    assert set(candidate) <= set(compiler.CLAIM_CANDIDATE_SCHEMA['properties'])
    assert record['reviews'] == reviews
    restored = compiler._with_derived_claims(record['operations'], inputs)[0]
    assert compiler.canonical_json_bytes(restored) == original
    assert compiler._owned_claim_origin(restored['claims'][0]) == compiler._owned_claim_origin(operation['claims'][0])
    assert compiler.canonical_json_bytes(operation) == original
    assert len(feedback[0].encode()) < len(original)


def test_feedback_retains_historical_claim_records_when_exact_origin_is_unavailable(tmp_path, monkeypatch):
    inputs, operation = _native_operation(tmp_path, monkeypatch)
    historical = json.loads(compiler.canonical_json_bytes(operation))
    reviews = [{'slug': operation['slug'], 'verdict': 'drop', 'reason': 'Check the meaning.'}]

    feedback = compiler._validated_critique_feedback([historical], reviews, inputs=inputs)

    assert compiler.canonical_json_bytes(json.loads(feedback[0])['operations'][0]) == compiler.canonical_json_bytes(historical)


def test_semantic_feedback_keeps_complete_repartition_validation(tmp_path, monkeypatch):
    from tests.test_repair_capacity_repartitions_complete_source_work import (
        _batch,
        _combined_feedback,
        _parts,
        _records,
    )
    from tests.test_the_compile_decides_what_the_snapshot_already_knows import (
        vault as vault_fixture,
    )

    fixture = vault_fixture.__wrapped__(tmp_path, monkeypatch)
    batch, original_feedback = _batch(fixture[0])
    record = json.loads(original_feedback[0])
    for operation in record['operations']:
        operation['claims'] = [_candidate(subject=operation['slug'])]
    compiler._with_derived_claims(record['operations'], batch.inputs)
    feedback = compiler._validated_critique_feedback(record['operations'], record['reviews'], inputs=batch.inputs)

    children = compiler._repartition_compile_repair(batch, feedback)

    assert len(children) == 2
    assert _parts(children) == list(batch.inputs.dailies)
    combined = _combined_feedback(children)
    assert _records(combined, 'operations') == _records(feedback, 'operations')
    assert _records(combined, 'reviews') == _records(feedback, 'reviews')
