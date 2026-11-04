"""Source-authority and facade consumers retain the native reader contract."""
from unittest.mock import Mock

import event_envelope as envelope
import fact_keys
import pytest

from tests.test_native_user_frames_keep_their_physical_evidence import _frame


@pytest.mark.parametrize('method', ['_validate_native_input', '_require_native_physical_input'])
def test_source_authority_also_decodes_its_native_input_once(method, monkeypatch):
    encoded = _frame('Complete source-authority line.')
    authority = fact_keys._NativeSourceAuthority((), None, float('inf'))
    authority._input = Mock(return_value=encoded.encode())
    authority._has_daily_frame = Mock(return_value=True)
    decode = Mock(wraps=envelope.json.loads)
    monkeypatch.setattr(envelope.json, 'loads', decode)
    getattr(authority, method)('verified-head')
    assert decode.call_count == 1
    authority._input.assert_called_once_with('verified-head')


def test_constructor_facade_keeps_identical_public_objects():
    from llm_wiki import event_envelope_values

    for name in ('EventEnvelope', 'build_event_envelope', 'canonical_agent', '_canonical', '_require_payload_fields'):
        assert getattr(envelope, name) is getattr(event_envelope_values, name)
