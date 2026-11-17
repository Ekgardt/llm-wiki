"""Retained answer bytes and refusals agree with the pre-transfer implementation."""
import json
from dataclasses import asdict, replace
from pathlib import Path

import pytest
from context_budget import TokenCount, TokenUsage
from llm_wiki import compile_answer_records as records
from llm_wiki import compile_event_records as progress
from llm_wiki.model_records import BackendResponse, LLMResult, ProviderDescriptor

ORIGINAL = json.loads((Path(__file__).parent / 'fixtures/original-records.json').read_text(encoding='utf-8'))


def _dispatch():
    return progress.CompileEvent(**ORIGINAL['dispatch'])


def _count(document):
    if document is None:
        return None
    return TokenCount(**document)


def _input_response(case):
    document = dict(case['input'])
    usage = TokenUsage(**document.pop('usage'))
    if case['kind'] == 'BackendResponse':
        return BackendResponse(usage=usage, **document)
    identity = dict(document.pop('descriptor'))
    identity['fallback_from'] = tuple(identity['fallback_from'])
    document['input_token_count'] = _count(document['input_token_count'])
    return LLMResult(ProviderDescriptor(**identity), usage=usage, **document)


@pytest.mark.parametrize('case', ORIGINAL['cases'])
def test_original_inputs_produce_identical_saved_bytes(case):
    assert records.encoded_answer(_dispatch(), _input_response(case)) == case['encoded'].encode()


@pytest.mark.parametrize('case', ORIGINAL['cases'])
def test_original_saved_answers_decode_to_the_same_values(case):
    decoded = records.decoded_answer(_dispatch(), case['encoded'].encode())
    assert type(decoded).__name__ == case['kind']
    assert decoded.text == case['expected']['text']
    assert asdict(decoded.usage) == case['expected']['usage']
    assert records.encoded_answer(_dispatch(), decoded) == case['encoded'].encode()


@pytest.mark.parametrize('case', ORIGINAL['invalid'], ids=lambda case: case['case'])
def test_original_malformed_records_keep_their_refusal(case):
    with pytest.raises(progress.ProgressCorrupt) as caught:
        records.decoded_answer(_dispatch(), case['encoded'].encode())
    assert type(caught.value).__name__ == case['error_type']
    assert str(caught.value) == case['message']


def test_delivery_change_and_added_artifact_do_not_change_call_identity():
    dispatch = _dispatch()
    answer = records.answer_event(dispatch, 'redelivered-task')
    saved = replace(answer, details={**answer.details, 'artifact': {
        'reference': 'run/queue-results/' + 'a' * 64 + '.result', 'sha256': 'b' * 64,
    }})
    assert records.require_same_call(saved, dispatch) is None
    assert records.require_same_call(replace(dispatch, task_id='redelivered-task'), dispatch) is None
    assert records.call_binding(saved)['model_call']['phase'] == 'answer'


@pytest.mark.parametrize('field,value', [
    ('work_id', 'f' * 64), ('source_digest', 'f' * 64),
    ('sequence', 123), ('stage', 'review_ready'), ('outcome', 'failed'),
])
def test_changed_call_or_progress_is_not_accepted(field, value):
    dispatch = _dispatch()
    with pytest.raises(progress.ProgressConflict, match='identity or progress changed'):
        records.require_same_call(replace(dispatch, **{field: value}), dispatch)


def test_absent_checkpoint_and_changed_request_are_refused():
    dispatch = _dispatch()
    with pytest.raises(progress.ProgressConflict, match='not recorded'):
        records.require_same_call(None, dispatch)
    call = {**dispatch.details['model_call'], 'request_digest': 'f' * 64}
    changed = replace(dispatch, details={'model_call': call})
    with pytest.raises(progress.ProgressConflict, match='identity or progress changed'):
        records.require_same_call(changed, dispatch)


@pytest.mark.parametrize('failure', ['provider_timeout', 'provider_error', 'unavailable'])
def test_uncertain_provider_failures_cannot_be_encoded_as_completed_answers(failure):
    descriptor = ProviderDescriptor('fake', None, {}, {}, 0, ())
    result = LLMResult(descriptor, None, True, failure, 'prompt')
    with pytest.raises(ValueError, match='uncertain'):
        records.encoded_answer(_dispatch(), result)
