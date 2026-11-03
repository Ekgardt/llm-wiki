"""Reader-local parsing is reusable only while exact records remain valid."""
import breadcrumb_protocol as protocol
import pytest
from llm_wiki.formats.schema import SchemaValidationError

from tests.module_context import context_reader
from tests.modules.breadcrumb_protocol.test_boundary import ROOT
from tests.test_breadcrumb_protocol import _encoded

read_source = context_reader(ROOT, ROOT / 'docs/modules/breadcrumb-protocol-context.json')


def test_cached_records_return_independent_values_and_restore_nested_contexts():
    anchor, _, _ = _encoded(b'complete event')
    outer, inner = protocol._RecordParsing(), protocol._RecordParsing()
    with protocol._using_record_parsing(outer):
        first = protocol.read_anchor(anchor)
        first['intent_id'] = 'changed'
        assert protocol.read_anchor(anchor)['intent_id'] == 'a' * 64
        with protocol._using_record_parsing(inner):
            assert protocol.read_anchor(anchor)['intent_id'] == 'a' * 64
        assert protocol._RECORD_PARSING.get() is outer
    assert protocol._RECORD_PARSING.get() is None


def test_schema_drift_refuses_a_previously_valid_record(tmp_path, monkeypatch):
    anchor, _, _ = _encoded(b'complete event')
    name = 'breadcrumb-occurrence-v1.json'
    path = tmp_path / name
    path.write_text(read_source(ROOT / 'scripts/schemas' / name), encoding='utf-8')
    monkeypatch.setattr(protocol, 'SCHEMAS', tmp_path)
    with protocol._using_record_parsing(protocol._RecordParsing()):
        assert protocol.read_anchor(anchor)['intent_id'] == 'a' * 64
        path.write_text('{}', encoding='utf-8')
        with pytest.raises(SchemaValidationError, match='schema changed'):
            protocol.read_anchor(anchor)
    assert protocol._RECORD_PARSING.get() is None


def test_expired_callback_refuses_reused_record_and_resets_context():
    anchor, _, _ = _encoded(b'complete event')
    parsing = protocol._RecordParsing()
    with pytest.raises(TimeoutError, match='expired'):
        with protocol._using_record_parsing(parsing):
            protocol.read_anchor(anchor)
            parsing.active = _expired
            protocol.read_anchor(anchor)
    assert protocol._RECORD_PARSING.get() is None


def _expired():
    raise TimeoutError('expired')


def test_mutable_record_is_revalidated_after_changes():
    anchor, _, _ = _encoded(b'complete event')
    mutable = bytearray(anchor)
    with protocol._using_record_parsing(protocol._RecordParsing()):
        assert protocol.read_anchor(mutable)['intent_id'] == 'a' * 64
        mutable.extend(b' ')
        with pytest.raises(ValueError, match='not canonical'):
            protocol.read_anchor(mutable)
