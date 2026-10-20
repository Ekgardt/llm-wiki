"""Source authority survives bounded reads, Unicode boundaries and owner exit."""
from dataclasses import replace

import evidence_resolver as evidence
import pytest
from bounded_io import IO_CHUNK_BYTES

from tests.test_compile_verified_original_ranges import _original


def _packet(raw):
    start = raw.index(b'Fact')
    return evidence.EvidenceRef('2026-10-20', evidence.sha256_bytes(raw), '12:34:56', start, start + 4)


@pytest.mark.parametrize('reuse', [False, True])
def test_verified_range_resolves_identical_physical_evidence(tmp_path, reuse):
    raw = b'## [12:34:56] event\nFact\n' + b'x' * (IO_CHUNK_BYTES * 3)
    ref = _packet(raw)
    resolver = evidence.EvidenceResolver(tmp_path)
    expected = resolver.resolve_bytes(ref, raw, source_path=tmp_path / 'day.md')
    with _original(raw) as content:
        assert resolver.resolve_bytes(ref, content, source_path=tmp_path / 'day.md',
                                      reuse_immutable=reuse) == expected


@pytest.mark.parametrize('suffix', [b'\xff', b'\xe2\x82'])
def test_entire_verified_source_must_be_utf8_even_outside_citation(tmp_path, suffix):
    raw = b'## [12:34:56] event\nFact\n' + b'x' * IO_CHUNK_BYTES + suffix
    resolver = evidence.EvidenceResolver(tmp_path)
    with _original(raw) as content, pytest.raises(evidence.EvidenceResolutionError, match='UTF-8'):
        resolver.resolve_bytes(_packet(raw), content, source_path=tmp_path / 'day.md', reuse_immutable=True)


def test_cached_range_proof_never_outlives_its_owner(tmp_path):
    raw = b'## [12:34:56] event\nFact\n'
    resolver = evidence.EvidenceResolver(tmp_path)
    ref = _packet(raw)
    with _original(raw) as content:
        resolver.resolve_bytes(ref, content, source_path=tmp_path / 'day.md', reuse_immutable=True)
    with pytest.raises(ValueError, match='scope is closed'):
        resolver.resolve_bytes(ref, content, source_path=tmp_path / 'day.md', reuse_immutable=True)


@pytest.mark.parametrize('fields', [
    {'source_sha256': '0' * 64}, {'block_id': 'other'}, {'byte_end': 1000},
])
def test_verified_source_cannot_authorize_a_different_reference(tmp_path, fields):
    raw = b'## [12:34:56] event\nFact\n'
    resolver = evidence.EvidenceResolver(tmp_path)
    ref = _packet(raw)
    with _original(raw) as content:
        resolver.resolve_bytes(ref, content, source_path=tmp_path / 'day.md', reuse_immutable=True)
        with pytest.raises(evidence.EvidenceResolutionError):
            resolver.resolve_bytes(replace(ref, **fields), content, source_path=tmp_path / 'day.md',
                                   reuse_immutable=True)


def test_large_block_hash_and_utf8_validation_do_not_materialize_the_block(tmp_path, monkeypatch):
    from bounded_io import VerifiedByteRange

    raw = b'## [12:34:56] event\nFact\n' + ('я\n' * IO_CHUNK_BYTES).encode()
    expected = evidence.EvidenceResolver(tmp_path).resolve_bytes(_packet(raw), raw, source_path=tmp_path)
    read = VerifiedByteRange.__getitem__

    def bounded_slice(content, key):
        result = read(content, key)
        assert not isinstance(result, bytes) or len(result) <= IO_CHUNK_BYTES
        return result

    monkeypatch.setattr(VerifiedByteRange, '__getitem__', bounded_slice)
    with _original(raw) as content:
        actual = evidence.EvidenceResolver(tmp_path).resolve_bytes(_packet(raw), content, source_path=tmp_path)
    assert actual == expected


@pytest.mark.parametrize('transfer', [1, 2, 3, 7])
def test_unicode_and_line_indexes_cross_physical_transfer_boundaries(tmp_path, monkeypatch, transfer):
    import compile_controller

    monkeypatch.setattr(compile_controller, 'IO_CHUNK_BYTES', transfer)
    raw = '## [12:34:56] event\nя😀\r\nFact\n## [12:34:56] second\nё\n'.encode()
    resolver = evidence.EvidenceResolver(tmp_path)
    expected = resolver.resolve_bytes(_packet(raw), raw, source_path=tmp_path)
    with _original(raw) as content:
        assert resolver.resolve_bytes(_packet(raw), content, source_path=tmp_path) == expected
        entries = tuple(evidence.daily_entries(raw))
        assert resolver.entry_ids_at(content, entries, raw.index(b'Fact')) == ('12:34:56',)
        assert resolver.declaring_entries(content, entries, '12:34:56') == [(a, b) for _, a, b in entries]


def test_utf8_citation_boundary_is_checked_even_with_cached_range_proof(tmp_path):
    raw = '## [12:34:56] event\nFact я\n'.encode()
    resolver = evidence.EvidenceResolver(tmp_path)
    with _original(raw) as content:
        resolver.resolve_bytes(_packet(raw), content, source_path=tmp_path)
        bad = replace(_packet(raw), byte_start=raw.index('я'.encode()) + 1, byte_end=len(raw) - 1)
        with pytest.raises(evidence.EvidenceResolutionError, match='UTF-8 boundaries'):
            resolver.resolve_bytes(bad, content, source_path=tmp_path)


def test_cached_metadata_rejects_expired_range(tmp_path, monkeypatch):
    import bounded_io

    clock = [0.0]
    monkeypatch.setattr(bounded_io.time, 'monotonic', lambda: clock[0])
    raw = b'## [12:34:56] event\nFact\n'
    resolver = evidence.EvidenceResolver(tmp_path)
    with _original(raw, deadline=1.0) as content:
        resolver.resolve_bytes(_packet(raw), content, source_path=tmp_path)
        clock[0] = 2.0
        with pytest.raises(TimeoutError):
            resolver.entry_ids_at(content, tuple(evidence.daily_entries(raw)), 0)


def test_modified_stream_cannot_borrow_cached_evidence_proof(tmp_path):
    import hashlib
    import io

    from bounded_io import VerifiedByteRange

    raw = b'## [12:34:56] event\nFact\n' + b'x' * (IO_CHUNK_BYTES * 2)
    resolver = evidence.EvidenceResolver(tmp_path)
    with io.BytesIO(raw) as stream:
        content = VerifiedByteRange(stream, len(raw), hashlib.sha256(raw).hexdigest())
        resolver.resolve_bytes(_packet(raw), content, source_path=tmp_path)
        assert content[-1] == ord('x')  # Evict the already captured first block.
        stream.seek(raw.index(b'Fact'))
        stream.write(b'Fake')
        with pytest.raises(ValueError, match='changed after verification'):
            resolver.resolve_bytes(_packet(raw), content, source_path=tmp_path)


@pytest.mark.parametrize('field', ['size', 'digest', 'hashes', 'chunk_size', 'deadline', 'stream'])
def test_verified_source_identity_cannot_be_reassigned(field):
    from dataclasses import FrozenInstanceError

    with _original(b'abc') as content, pytest.raises(FrozenInstanceError):
        setattr(content, field, None)


def test_foreign_object_with_digest_is_not_source_authority(tmp_path):
    from types import SimpleNamespace

    raw = b'## [12:34:56] event\nFact\n'
    forged = SimpleNamespace(digest=evidence.sha256_bytes(raw), size=len(raw), content=raw)
    with pytest.raises(TypeError, match='immutable'):
        evidence.EvidenceResolver(tmp_path).resolve_bytes(_packet(raw), forged, source_path=tmp_path)


def test_unverified_subclass_cannot_bypass_source_admission(tmp_path):
    import io

    from bounded_io import VerifiedByteRange

    class Unverified(VerifiedByteRange):
        def __post_init__(self):
            pass

    raw = b'## [12:34:56] event\nFact\n'
    with io.BytesIO(b'forged') as stream:
        forged = Unverified(stream, len(raw), evidence.sha256_bytes(raw))
        with pytest.raises(TypeError, match='immutable'):
            evidence.EvidenceResolver(tmp_path).resolve_bytes(_packet(raw), forged, source_path=tmp_path)
