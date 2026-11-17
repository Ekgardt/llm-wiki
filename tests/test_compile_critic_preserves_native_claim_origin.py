"""Two user lines share physical provenance without sharing a citation index."""
import json

import compile_pipeline
import pytest
from llm_wiki import compile_pipeline_base, compile_pipeline_operations, compile_pipeline_prompts

from tests.compile_claims_producer_helpers import _candidate
from tests.compile_critic_preserves_native_claim_origin_helpers import _native_operation


def test_actual_critic_keeps_the_selected_line_inside_one_native_container(tmp_path, monkeypatch):
    inputs, operation = _native_operation(tmp_path, monkeypatch)
    bindings = [compile_pipeline_operations.evidence_binding(item, inputs) for item in operation['evidence']]
    assert bindings[0]['reference'] == bindings[1]['reference']

    prompt = compile_pipeline_prompts.critique_prompt(inputs, [operation])

    encoded, cited = prompt.split('\nOPERATIONS\n', 1)[1].split('\nCITED EVIDENCE\n', 1)
    reviewed = json.loads(encoded)[0]
    assert [claim['evidence_index'] for claim in reviewed['claims']] == [1]
    addresses = {row['evidence_index']: row for row in json.loads(cited)}
    assert addresses[1]['operation_slug'] == reviewed['slug']
    assert addresses[1]['native_event']['line_index'] == 1
    assert addresses[1]['quoted_text'] == 'The lease expires in 30 seconds.'
    serialized = json.loads(compile_pipeline.canonical_json_bytes(operation))
    assert 'evidence_index' not in serialized['claims'][0]


def test_a_reordered_native_evidence_array_cannot_retarget_a_derived_claim(tmp_path, monkeypatch):
    inputs, operation = _native_operation(tmp_path, monkeypatch)
    operation['evidence'].reverse()

    with pytest.raises(ValueError, match='claim.*(origin|citation|ambiguous)'):
        compile_pipeline_prompts.critique_prompt(inputs, [operation])


def test_historical_claim_with_ambiguous_container_provenance_is_not_guessed(tmp_path, monkeypatch):
    inputs, operation = _native_operation(tmp_path, monkeypatch)
    operation = json.loads(compile_pipeline.canonical_json_bytes(operation))

    with pytest.raises(ValueError, match='claim.*ambiguous'):
        compile_pipeline_prompts.critique_prompt(inputs, [operation])


def test_feedback_preserves_selected_native_line_and_recreates_the_exact_ledger(tmp_path, monkeypatch):
    inputs, operation = _native_operation(tmp_path, monkeypatch)
    reviews = [{'slug': operation['slug'], 'verdict': 'drop', 'reason': 'Check the scoped meaning.'}]
    original = compile_pipeline.canonical_json_bytes(operation)

    feedback = compile_pipeline_prompts.validated_critique_feedback([operation], reviews, inputs=inputs)

    record = json.loads(feedback[0])
    candidate = record['operations'][0]['claims'][0]
    assert candidate['evidence_index'] == 1
    assert set(candidate) <= set(compile_pipeline_base.CLAIM_CANDIDATE_SCHEMA['properties'])
    assert record['reviews'] == reviews
    restored = compile_pipeline_operations.with_derived_claims(record['operations'], inputs)[0]
    assert compile_pipeline.canonical_json_bytes(restored) == original
    assert compile_pipeline_prompts.owned_claim_origin(restored['claims'][0]) == compile_pipeline_prompts.owned_claim_origin(operation['claims'][0])
    assert compile_pipeline.canonical_json_bytes(operation) == original
    assert len(feedback[0].encode()) < len(original)


def test_feedback_retains_historical_claim_records_when_exact_origin_is_unavailable(tmp_path, monkeypatch):
    inputs, operation = _native_operation(tmp_path, monkeypatch)
    historical = json.loads(compile_pipeline.canonical_json_bytes(operation))
    reviews = [{'slug': operation['slug'], 'verdict': 'drop', 'reason': 'Check the meaning.'}]

    feedback = compile_pipeline_prompts.validated_critique_feedback([historical], reviews, inputs=inputs)

    assert compile_pipeline.canonical_json_bytes(json.loads(feedback[0])['operations'][0]) == compile_pipeline.canonical_json_bytes(historical)


def test_semantic_feedback_keeps_complete_repartition_validation(tmp_path, monkeypatch):
    from tests.repair_capacity_repartitions_complete_source_work_helpers import (
        _batch,
        _combined_feedback,
        _parts,
        _records,
    )
    from tests.the_compile_decides_what_the_snapshot_already_knows_helpers import (
        vault as vault_fixture,
    )

    fixture = vault_fixture.__wrapped__(tmp_path, monkeypatch)
    batch, original_feedback = _batch(fixture[0])
    record = json.loads(original_feedback[0])
    for operation in record['operations']:
        operation['claims'] = [_candidate(subject=operation['slug'])]
    compile_pipeline_operations.with_derived_claims(record['operations'], batch.inputs)
    feedback = compile_pipeline_prompts.validated_critique_feedback(record['operations'], record['reviews'], inputs=batch.inputs)

    children = compile_pipeline.repartition_compile_repair(batch, feedback)

    assert len(children) == 2
    assert _parts(children) == list(batch.inputs.dailies)
    combined = _combined_feedback(children)
    assert _records(combined, 'operations') == _records(feedback, 'operations')
    assert _records(combined, 'reviews') == _records(feedback, 'reviews')
