"""Actual encoded byte pressure preserves protected state and error meaning."""
import errno
import json

import pytest
from llm_wiki.state_json_values import (
    decoded_state,
    serialized_state,
    state_bytes,
    trim_state_to_budget,
)


@pytest.mark.parametrize('raw', [b'\xff', b'{', b'[]', b'null', b'"text"', b'1'])
def test_unreadable_or_non_object_state_is_rejected(raw):
    assert decoded_state(raw) is None


def test_encoding_preserves_unicode_order_and_exact_boundary():
    state = {'z': 'Привет', 'a': [1, True, None]}
    expected = json.dumps(state, indent=2, ensure_ascii=False)
    budget = len(expected.encode('utf-8'))
    assert state_bytes(state) == budget
    assert serialized_state(state, budget) == expected
    assert decoded_state(expected.encode('utf-8')) == state


def test_only_oldest_entry_of_the_first_disposable_map_is_evicted():
    state = {'tool_capture_dedupe': {'first': 'x' * 200, 'second': 'keep'},
             'prompt_capture_dedupe': {'later': 'keep'}, 'pending': ['protected']}
    expected = {'tool_capture_dedupe': {'second': 'keep'},
                'prompt_capture_dedupe': {'later': 'keep'}, 'pending': ['protected']}
    assert trim_state_to_budget(state, state_bytes(expected)) == 1
    assert state == expected


def test_empty_and_non_mapping_caches_are_not_deleted():
    state = {'tool_capture_dedupe': [], 'prompt_capture_dedupe': {}, 'pending': 'protected'}
    before = dict(state)
    assert trim_state_to_budget(state, 0) == 0
    assert state == before


def test_oversized_protected_state_refuses_after_existing_cache_mutation():
    state = {'project_checkpoint_reducers': {'old': 'cache'}, 'pending': 'protected'}
    with pytest.raises(OSError) as caught:
        serialized_state(state, 0)
    assert caught.value.errno == errno.EFBIG
    assert state == {'project_checkpoint_reducers': {}, 'pending': 'protected'}


def test_non_serializable_values_keep_encoder_errors():
    with pytest.raises(TypeError):
        serialized_state({'pending': object()}, 1024)
