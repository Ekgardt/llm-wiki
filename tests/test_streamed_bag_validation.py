"""Archive validation owns its stream and retains metadata, not full daily bytes."""
from __future__ import annotations

import evidence_resolver as evidence
import pytest

from tests.test_receipt_versions_preserve_historical_archive_authority import _restore_v3_bag


def _forbid_payload_read(monkeypatch):
    original = evidence.read_stable_bytes

    def read(path, bound, **kwargs):
        if kwargs.get('label') == 'archive payload':
            raise AssertionError('archive payload was materialized')
        return original(path, bound, **kwargs)

    monkeypatch.setattr(evidence, 'read_stable_bytes', read)


def test_validation_returns_metadata_without_reading_payload_whole(tmp_path, monkeypatch):
    path = _restore_v3_bag(tmp_path / 'bag')
    _forbid_payload_read(monkeypatch)
    bag = evidence.validate_bag(path)
    assert bag.manifest['payload_hash']
    assert not hasattr(bag, 'payload')


def test_open_bag_owns_range_until_validation_scope_ends(tmp_path, monkeypatch):
    path = _restore_v3_bag(tmp_path / 'bag')
    _forbid_payload_read(monkeypatch)
    with evidence.open_validated_bag(path) as (bag, payload):
        assert payload.range_digest(0, len(payload)) == bag.manifest['payload_hash']
        assert payload[:len(payload)] == bag.payload_path.read_bytes()
    with pytest.raises(ValueError, match='closed'):
        payload.require_active()


def test_changed_payload_cannot_leave_a_successful_validation_scope(tmp_path):
    path = _restore_v3_bag(tmp_path / 'bag')
    with pytest.raises(evidence.EvidenceResolutionError, match='changed'):
        with evidence.open_validated_bag(path) as (bag, payload):
            original = bag.payload_path.read_bytes()
            bag.payload_path.chmod(0o600)
            bag.payload_path.write_bytes(original.replace(b'#', b'!', 1))
    assert payload.stream.closed


def test_reader_exception_closes_archive_payload(tmp_path):
    path = _restore_v3_bag(tmp_path / 'bag')
    with pytest.raises(RuntimeError, match='consumer failure'):
        with evidence.open_validated_bag(path) as (_bag, payload):
            raise RuntimeError('consumer failure')
    assert payload.stream.closed


def test_archive_citation_reads_only_selected_evidence(tmp_path, monkeypatch):
    import json
    from pathlib import Path

    fixture = Path(__file__).parent / 'fixtures/compile-receipt-v3-bag.json'
    manifest = json.loads(json.loads(fixture.read_bytes())['files']['archive-manifest.json'])
    day = manifest['logical_daily_id']
    parent = tmp_path / 'knowledge/daily/archive' / day[:7]
    parent.mkdir(parents=True)
    path = _restore_v3_bag(parent / 'bag-original')
    item = manifest['evidence'][0]
    ref = evidence.EvidenceRef(day, manifest['source_hash'], item['block_id'],
                               item['byte_start'], item['byte_end'])
    expected = (path / 'data' / f'{day}.md').read_bytes()[ref.byte_start:ref.byte_end]
    _forbid_payload_read(monkeypatch)
    result = evidence.EvidenceResolver(tmp_path, state_root=tmp_path).resolve(ref)
    assert (result.bytes, result.location) == (expected, 'archive')


@pytest.mark.parametrize('forged', [False, True])
def test_append_proof_hashes_owned_prefix_without_copy(monkeypatch, forged):
    import io
    from types import SimpleNamespace

    from bounded_io import VerifiedByteRange
    from markdown_transaction import committed_append_content_matches
    from reliable_memory import sha256_bytes

    before, block = b'earlier evidence\n' * 10000, b'unique appended block\n'
    content = before + block + b'later evidence\n'
    source = VerifiedByteRange(io.BytesIO(content), len(content), None)
    operation = SimpleNamespace(path='daily.md', kind='update',
                                before_hash=sha256_bytes(before),
                                after_hash='0' * 64 if forged else sha256_bytes(before + block))
    record = SimpleNamespace(state='committed', operations=[operation])
    monkeypatch.setattr(VerifiedByteRange, '__getitem__', _refuse_range_copy)
    assert committed_append_content_matches(record, 'daily.md', block, source) is (not forged)


def _refuse_range_copy(*args):
    raise AssertionError('append proof materialized a source range')


def test_historical_part_search_hashes_candidates_without_whole_copies(monkeypatch):
    import io

    from bounded_io import IO_CHUNK_BYTES, VerifiedByteRange
    from reliable_memory import sha256_bytes

    prefix = b'# day\n## [evt-1] event\n' + b'x' * (4 * IO_CHUNK_BYTES) + b'\n'
    part = b'## [evt-2] event\nselected evidence\n'
    payload = prefix + part
    source = VerifiedByteRange(io.BytesIO(payload), len(payload), None)
    original = VerifiedByteRange.__getitem__

    def bounded_read(self, key):
        if isinstance(key, slice) and len(range(*key.indices(len(self)))) > IO_CHUNK_BYTES:
            raise AssertionError('historical candidate was materialized')
        return original(self, key)

    monkeypatch.setattr(VerifiedByteRange, '__getitem__', bounded_read)
    assert evidence._slice_at(source, [0, len(prefix)], sha256_bytes(part)) == part
