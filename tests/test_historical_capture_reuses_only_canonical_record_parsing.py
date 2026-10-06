"""Canonical record parsing may be reused; every external read stays fresh."""
from unittest.mock import Mock

import breadcrumb_evidence as evidence
import breadcrumb_protocol as protocol
import doctor
import pytest
import reliable_memory as reliability
from reliable_memory import canonical_json_bytes

from tests.test_breadcrumb_evidence import _source
from tests.test_breadcrumb_protocol import _encoded
from tests.test_doctor_current_v4_exact_historical_work import native_work as native_work
from tests.test_doctor_exact_legacy_source_outcome import _reader


def test_repeated_capture_validates_each_exact_record_only_once(native_work, monkeypatch):
    history, batch, _path = native_work
    logical = batch.inputs.dailies[0].logical_path
    validation = Mock(wraps=reliability.validate_schema_object)
    reads = Mock(wraps=evidence._read_source_document)
    monkeypatch.setattr(reliability, "validate_schema_object", validation)
    monkeypatch.setattr(evidence, "_read_source_document", reads)
    with _reader(history) as (_database, reader):
        first = doctor._CurrentHistoricalSourceWork(reader, history[4])._capture(logical)
        first_reads = reads.call_count
        second = doctor._CurrentHistoricalSourceWork(reader, history[4])._capture(logical)
    assert first == second
    assert reads.call_count == 2 * first_reads
    assert validation.call_count > 0
    distinct = {(canonical_json_bytes(call.args[0]), canonical_json_bytes(call.args[1]))
                for call in validation.call_args_list}
    assert validation.call_count == len(distinct)


def test_cached_record_returns_fresh_mutable_values():
    anchor, _parts, _manifest = _encoded(b"complete")
    parsing = protocol._RecordParsing()
    with protocol._using_record_parsing(parsing):
        first = protocol.read_anchor(anchor)
        first.pop("intent_id")
        assert protocol.read_anchor(anchor)["intent_id"] == "a" * 64
    assert protocol._RECORD_PARSING.get() is None


@pytest.mark.parametrize("damage", ["version", "extra", "encoding"])
def test_changed_record_bytes_are_not_a_cached_proof(damage):
    anchor, _parts, _manifest = _encoded(b"complete")
    record = protocol.read_anchor(anchor)
    changed = dict(record, schema_version="unknown")
    alternatives = {"version": canonical_json_bytes(changed),
                    "extra": canonical_json_bytes(dict(record, forged=True)), "encoding": anchor + b" "}
    with protocol._using_record_parsing(protocol._RecordParsing()):
        assert protocol.read_anchor(anchor) == record
        with pytest.raises(ValueError):
            protocol.read_anchor(alternatives[damage])
    assert protocol._RECORD_PARSING.get() is None


def test_schema_drift_cannot_reuse_identical_record_bytes(tmp_path, monkeypatch):
    anchor, _parts, _manifest = _encoded(b"complete")
    name = "breadcrumb-occurrence-v1.json"
    original = (protocol.SCHEMAS / name).read_bytes()
    path = tmp_path / name
    path.write_bytes(original)
    monkeypatch.setattr(protocol, "SCHEMAS", tmp_path)
    with protocol._using_record_parsing(protocol._RecordParsing()):
        assert protocol.read_anchor(anchor)["intent_id"] == "a" * 64
        path.write_bytes(b"{}")
        with pytest.raises(reliability.SchemaValidationError, match="schema changed"):
            protocol.read_anchor(anchor)


@pytest.mark.parametrize("target", ["head", "part"])
def test_each_external_document_is_revalidated_after_record_reuse(target):
    head, documents = _source(b"complete")
    with protocol._using_record_parsing(protocol._RecordParsing()):
        assert evidence.restore_source(head, documents.__getitem__) == b"complete"
        path = {"head": head, "part": next(iter(documents))}[target]
        documents[path] += b"changed\n"
        with pytest.raises(ValueError):
            evidence.restore_source(head, documents.__getitem__)


def test_expired_reader_cannot_use_cached_canonical_records():
    anchor, _parts, _manifest = _encoded(b"complete")
    active = Mock(side_effect=[None, None, TimeoutError("reader expired")])
    with protocol._using_record_parsing(protocol._RecordParsing(active)):
        assert protocol.read_anchor(anchor)["intent_id"] == "a" * 64
        with pytest.raises(TimeoutError, match="expired"):
            protocol.read_anchor(anchor)
    assert protocol._RECORD_PARSING.get() is None


def test_arbitrary_decoder_object_cannot_supply_record_proof():
    forged = Mock()
    with pytest.raises(ValueError, match="canonical reader-owned"):
        with protocol._using_record_parsing(forged):
            pytest.fail("forged parser entered the scope")
    forged.read.assert_not_called()


def test_physical_transport_bound_remains_live_on_a_cache_hit(monkeypatch):
    anchor, _parts, _manifest = _encoded(b"complete")
    with protocol._using_record_parsing(protocol._RecordParsing()):
        assert protocol.read_anchor(anchor)["intent_id"] == "a" * 64
        monkeypatch.setattr(protocol, "MAX_CAPTURE_INTENT_BYTES", len(anchor) - 1)
        with pytest.raises(ValueError, match="physical record exceeds"):
            protocol.read_anchor(anchor)
