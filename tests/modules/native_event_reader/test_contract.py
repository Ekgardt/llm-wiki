"""Original canonical native decode assertions and their shared frame factory."""
import json
from unittest.mock import Mock

import breadcrumb_protocol as protocol
import event_envelope as envelope
import pytest


def _frame(prompt, **changes):
    event = dict(schema_version="1.0", event_type="user_prompt", payload={"prompt": prompt},
                 agent="codex", session="s", project=None, worktree=None,
                 severity=None, parent_event_id=None, source_event_id=None)
    event.update(changes)
    return json.dumps(event, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


@pytest.mark.parametrize(('kind', 'payload', 'expected'), [
    ('user_prompt', {'prompt': 'Полный текст ☃\r\n第二行'}, 'Полный текст ☃\r\n第二行'),
    ('post_tool_use', {'tool_name': 'read', 'target': 'file'}, None),
])
def test_complete_native_record_is_decoded_only_once(kind, payload, expected, monkeypatch):
    encoded = _frame('', event_type=kind, payload=payload)
    decode = Mock(wraps=envelope.json.loads)
    monkeypatch.setattr(envelope.json, 'loads', decode)
    assert envelope.native_user_text('    '+encoded+'\r\n') == expected
    assert decode.call_count == 1


@pytest.mark.parametrize('changes', [
    {'schema_version': 'future'}, {'payload': {'prompt': 4}},
    {'agent': False}, {'unexpected': 'field'},
])
def test_single_decode_still_rejects_invalid_native_records(changes):
    with pytest.raises(ValueError):
        envelope.native_user_text('    '+_frame('complete', **changes))


def test_native_fragment_retains_its_exact_json_error():
    encoded = _frame('complete')[:-1]
    with pytest.raises(json.JSONDecodeError) as caught:
        envelope.native_user_text('    '+encoded)
    assert caught.value.doc == encoded
    assert caught.value.msg == "Expecting ',' delimiter"
    assert caught.value.pos == len(encoded)


@pytest.mark.parametrize('text', [
    'ordinary prose', '    {"note":"generic"}', '    {"unfinished":',
    '    {"nested":{"event_type":"user_prompt","schema_version":"1.0"}}',
])
def test_generic_data_remains_outside_native_user_records(text):
    assert envelope.native_user_text(text) is None


def test_canonical_encoding_is_checked_on_every_decode():
    encoded = _frame('complete')
    assert envelope.native_user_text('    '+encoded) == 'complete'
    with pytest.raises(ValueError, match='encoding'):
        envelope.native_user_text('    '+encoded+' ')


def test_native_content_inside_a_physical_part_is_also_decoded_once(monkeypatch):
    encoded = _frame('Complete contained user line.')
    part = protocol.encode_parts('a'*64, encoded.encode())[0].decode()
    decode = Mock(wraps=envelope.json.loads)
    monkeypatch.setattr(envelope.json, 'loads', decode)
    assert envelope.native_part_user_text('    '+part) == 'Complete contained user line.'
    inner_calls = [call for call in decode.call_args_list if call.args[0] == encoded]
    assert len(inner_calls) == 1
