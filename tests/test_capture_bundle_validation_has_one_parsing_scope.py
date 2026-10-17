"""Canonical parsing reuse never substitutes for physical capture authority."""
from __future__ import annotations

from unittest.mock import Mock

import breadcrumb_protocol as protocol
import breadcrumb_storage as storage
import installed_memory_repair as repair
import pytest
from reliable_memory import publish_runtime_file, sha256_bytes

from tests.adopted_capture_vault import session_intent_payload
from tests.test_breadcrumb_protocol import _encoded


def _bundle(tmp_path):
    anchor, parts, manifest = _encoded(b'complete immutable input')
    identity = protocol.read_manifest(manifest)['intent_id']
    records = [(storage.anchor_path(tmp_path, identity), anchor)]
    records.extend((storage.part_path(tmp_path, identity, sha256_bytes(part)), part) for part in parts)
    for path, raw in records:
        path.parent.mkdir(parents=True, exist_ok=True)
        publish_runtime_file(path, raw, state_root=tmp_path, create_only=True, mode=0o600)
    return manifest, {'intent_id': identity}, records


def _verify(tmp_path, manifest, row):
    repair._require_capture_document(manifest, row, tmp_path, float('inf'))


def _count_parsing(monkeypatch):
    uncached = Mock(wraps=protocol._read_record_uncached)
    canonical = Mock(wraps=protocol._canonical_record)
    monkeypatch.setattr(protocol, '_read_record_uncached', uncached)
    monkeypatch.setattr(protocol, '_canonical_record', canonical)
    return uncached, canonical


def test_one_real_bundle_has_three_canonical_validations(tmp_path, monkeypatch):
    manifest, row, _records = _bundle(tmp_path)
    uncached, canonical = _count_parsing(monkeypatch)
    reads = Mock(wraps=storage._read)
    monkeypatch.setattr(storage, '_read', reads)
    _verify(tmp_path, manifest, row)
    assert uncached.call_count == 0
    assert canonical.call_count == 3
    assert reads.call_count == 2
    assert protocol._RECORD_PARSING.get() is None


def test_legacy_v1_identity_still_verified(tmp_path):
    identity, payload = session_intent_payload(b'legacy capture')
    _verify(tmp_path, payload, {'intent_id': identity})
    with pytest.raises(ValueError, match='identity'):
        _verify(tmp_path, payload, {'intent_id': 'b' * 64})
    assert protocol._RECORD_PARSING.get() is None


def test_nested_scope_restored_without_borrowing_its_records(tmp_path):
    manifest, row, _records = _bundle(tmp_path)
    outer = protocol._RecordParsing()
    with protocol._using_record_parsing(outer):
        _verify(tmp_path, manifest, row)
        assert protocol._RECORD_PARSING.get() is outer
        assert outer.records == set()
    assert protocol._RECORD_PARSING.get() is None


def test_changed_physical_part_is_rejected_and_scope_reset(tmp_path):
    manifest, row, records = _bundle(tmp_path)
    _verify(tmp_path, manifest, row)
    records[-1][0].write_bytes(b'changed complete part')
    with pytest.raises(ValueError):
        _verify(tmp_path, manifest, row)
    assert protocol._RECORD_PARSING.get() is None


def test_schema_drift_during_bundle_is_not_cached(tmp_path, monkeypatch):
    manifest, row, _records = _bundle(tmp_path)
    original = protocol._schema_bytes
    calls = []
    def changed(name):
        raw = original(name)
        calls.append(name)
        return raw if len(calls) == 1 else raw + b' '
    monkeypatch.setattr(protocol, '_schema_bytes', changed)
    with pytest.raises(ValueError):
        _verify(tmp_path, manifest, row)
    assert protocol._RECORD_PARSING.get() is None


def test_deadline_is_checked_inside_pure_parsing(tmp_path, monkeypatch):
    manifest, row, _records = _bundle(tmp_path)
    clock = Mock(side_effect=[0.0, 2.0])
    monkeypatch.setattr(repair.time, 'monotonic', clock)
    with pytest.raises(TimeoutError):
        repair._require_capture_document(manifest, row, tmp_path, 1.0)
    assert protocol._RECORD_PARSING.get() is None


def test_invalid_manifest_and_index_identity_remain_visible(tmp_path):
    manifest, row, _records = _bundle(tmp_path)
    with pytest.raises(ValueError):
        _verify(tmp_path, manifest + b' ', row)
    with pytest.raises(ValueError, match='identity'):
        _verify(tmp_path, manifest, {'intent_id': 'b' * 64})
    assert protocol._RECORD_PARSING.get() is None


def test_bundle_scopes_do_not_accumulate_between_calls(tmp_path, monkeypatch):
    manifest, row, _records = _bundle(tmp_path)
    contexts = []
    original = protocol._RecordParsing
    # Observe canonical construction without substituting its required exact type.
    init = original.__init__
    def observed(self, active=None):
        contexts.append(self)
        init(self, active)
    monkeypatch.setattr(original, '__init__', observed)
    _verify(tmp_path, manifest, row)
    _verify(tmp_path, manifest, row)
    assert len(contexts) == 2
    assert contexts[0] is not contexts[1]
    assert len(contexts[0].records) == len(contexts[1].records) == 3
    assert protocol._RECORD_PARSING.get() is None
