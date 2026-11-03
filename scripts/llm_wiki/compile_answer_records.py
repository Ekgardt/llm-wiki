"""Saved compile answer representation and binding, without transport or SQL."""
from __future__ import annotations

import json
from dataclasses import asdict, replace

from context_budget import TokenCount, TokenUsage

import llm_wiki.compile_event_records as progress
from llm_wiki.formats.canonical import canonical_json_bytes
from llm_wiki.model_records import BackendResponse, LLMResult, ProviderDescriptor

_COMPLETED_REFUSALS = frozenset({'empty_response', 'dlp_output_blocked', 'dlp_scan_error'})


def _answer_event(dispatch, task_id):
    call = {**dispatch.details['model_call'], 'phase': 'answer'}
    return replace(dispatch, task_id=task_id, sequence=dispatch.sequence + 1,
                   outcome='ready', details={'model_call': call})


def _call_binding(dispatch):
    return {
        'work_id': dispatch.work_id, 'source_digest': dispatch.source_digest,
        'stage': dispatch.stage, 'sequence': dispatch.sequence,
        'model_call': dispatch.details['model_call'],
    }


def _encoded_answer(dispatch, response):
    if isinstance(response, LLMResult):
        return _encoded_candidate_result(dispatch, response)
    if not isinstance(response, BackendResponse):
        raise TypeError('protected transport must return BackendResponse')
    _require_response(response)
    return canonical_json_bytes({
        'schema': 'compile-model-answer/v1', 'dispatch': _call_binding(dispatch),
        'text': response.text, 'usage': asdict(response.usage),
    })


def _require_response(response):
    if not isinstance(response.text, str):
        raise ValueError('model answer text must be a string')
    # Reuse the shared usage contract; unknown values remain None, not zero.
    TokenUsage(**asdict(response.usage))


def _decoded_answer(dispatch, data):
    try:
        value = json.loads(data)
        if canonical_json_bytes(value) != data:
            raise ValueError('model answer encoding is not canonical')
        return _decoded_response(dispatch, value)
    except (ValueError, TypeError, KeyError, AttributeError) as error:
        raise progress.ProgressCorrupt('saved model answer is invalid or belongs to another call') from error


def _require_answer_binding(dispatch, value):
    expected = {'schema', 'dispatch', 'text', 'usage'}
    if not isinstance(value, dict) or set(value) != expected:
        raise ValueError('model answer envelope changed')
    if (value['schema'], value['dispatch']) != ('compile-model-answer/v1', _call_binding(dispatch)):
        raise ValueError('model answer identity changed')


def _require_same_call(current, dispatch):
    if current is None:
        raise progress.ProgressConflict('compile checkpoint is not recorded')
    expected = _answer_event(dispatch, dispatch.task_id)
    # Task ownership may change through redelivery; sequence and call identity may not.
    details = dict(current.details)
    details.pop('artifact', None)
    identity = replace(current, task_id=dispatch.task_id, details=details)
    if identity not in (dispatch, expected):
        raise progress.ProgressConflict('compile call identity or progress changed')


def _decoded_response(dispatch, value):
    if value.get('schema') == 'compile-candidate-result/v1':
        return _decoded_candidate_result(dispatch, value)
    _require_answer_binding(dispatch, value)
    response = BackendResponse(value['text'], TokenUsage(**value['usage']))
    _require_response(response)
    return response


def _require_completed_result(result):
    if result.available is not True or result.structured_output not in {'native', 'prompt'}:
        raise ValueError('a dispatched result must describe an available prepared provider')
    if result.text is not None:
        _require_result_text(result)
        return
    _require_completed_refusal(result)


def _require_result_text(result):
    if not isinstance(result.text, str) or result.failure_class is not None:
        raise ValueError('completed answer has invalid text or a failure')


def _require_completed_refusal(result):
    # These outcomes follow an actual returned response and the shared output scan.
    if result.failure_class not in _COMPLETED_REFUSALS:
        raise ValueError('provider outcome is uncertain; a returned error is not a saved answer')


def _result_document(result):
    _require_completed_result(result)
    TokenUsage(**asdict(result.usage))
    return {
        'descriptor': result.descriptor.canonical(), 'text': result.text,
        'available': result.available, 'failure_class': result.failure_class,
        'structured_output': result.structured_output, 'usage': asdict(result.usage),
        'input_token_count': _count_document(result.input_token_count),
        'failure_detail': result.failure_detail,
    }


def _count_document(count):
    if count is None:
        return None
    TokenCount(**asdict(count))
    return asdict(count)


def _encoded_candidate_result(dispatch, result):
    return canonical_json_bytes({
        'schema': 'compile-candidate-result/v1', 'dispatch': _call_binding(dispatch),
        'result': _result_document(result),
    })


def _decoded_candidate_result(dispatch, value):
    if set(value) != {'schema', 'dispatch', 'result'} or value['dispatch'] != _call_binding(dispatch):
        raise ValueError('saved candidate result belongs to another call')
    result = _result_from_document(value['result'])
    if _result_document(result) != value['result']:
        raise ValueError('saved candidate result is not lossless')
    return result


def _result_from_document(document):
    from types import MappingProxyType

    fields = dict(document)
    descriptor = dict(fields.pop('descriptor'))
    descriptor['fallback_from'] = tuple(descriptor['fallback_from'])
    descriptor['capabilities'] = MappingProxyType(descriptor['capabilities'])
    descriptor['inference_settings'] = MappingProxyType(descriptor['inference_settings'])
    fields['usage'] = TokenUsage(**fields['usage'])
    fields['input_token_count'] = _count_from_document(fields['input_token_count'])
    return LLMResult(ProviderDescriptor(**descriptor), **fields)


def _count_from_document(value):
    if value is None:
        return None
    return TokenCount(**value)
