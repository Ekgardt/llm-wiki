"""Repeated resolution must inspect only the canonical entry candidates."""
import evidence_resolver as resolver_module
import pytest
from evidence_resolver import EvidenceRef, EvidenceResolver, daily_entries, sha256_bytes


def test_repeated_resolution_does_not_walk_unrelated_entries(tmp_path, monkeypatch):
    content = b'## [10:00:00] event\nFirst.\n## [11:00:00] event\nChosen.\n## [12:00:00] event\nLast.\n'
    start = content.index(b'Chosen.')
    reference = EvidenceRef('2026-09-27', sha256_bytes(content), '11:00:00', start, start + 7)
    resolver = EvidenceResolver(tmp_path)
    original = resolver_module._sole_entry_span
    inspected = []

    def observe(ref, entries):
        inspected.append(len(entries))
        return original(ref, entries)

    monkeypatch.setattr(resolver_module, '_sole_entry_span', observe)
    actual = [resolver.resolve_bytes(reference, content, source_path=tmp_path/'day.md',
                                    reuse_immutable=True) for _ in range(3)]
    assert all(item.bytes == b'Chosen.' for item in actual)
    assert inspected == [1, 1, 1]
    assert len(daily_entries(content)) == 3


def test_entry_index_preserves_repeated_clock_and_exact_span(tmp_path):
    content = b'## [11:00:00] event\nFirst.\n## [11:00:00] event\nSecond.\n'
    entries = tuple(daily_entries(content))
    resolver = EvidenceResolver(tmp_path)
    assert resolver.declaring_entries(content, entries, '11:00:00') == [(a, b) for _, a, b in entries]
    for offset in (content.index(b'First.'), content.index(b'Second.')):
        assert resolver.entry_ids_at(content, entries, offset) == ('11:00:00',)
    assert resolver.entry_ids_at(content, entries, len(content)) == ()
    assert resolver.entry_ids_at(content, entries, -1) == ()


def test_poisoned_metadata_remains_a_hint_not_a_source_proof(tmp_path):
    content = b'## [11:00:00] event\nOriginal.\n'
    resolver = EvidenceResolver(tmp_path)
    genuine = tuple(daily_entries(content))
    assert resolver.entry_ids_at(content, genuine, 0) == ('11:00:00',)
    poison = (('22:00:00', 0, len(content)),)
    assert resolver.entry_ids_at(content, poison, 0) == ('22:00:00',)
    start = content.index(b'Original.')
    ref = EvidenceRef('2026-09-27', sha256_bytes(content), '22:00:00', start, start+9)
    with pytest.raises(resolver_module.EvidenceResolutionError, match='block'):
        resolver.resolve_bytes(ref, content, source_path=tmp_path/'day.md', reuse_immutable=True)
    assert all(item[1] is not poison for item in resolver._immutable_entry_metadata.values())


def test_mutable_entry_metadata_is_checked_again(tmp_path):
    content = b'## [11:00:00] event\nOriginal.\n'
    resolver = EvidenceResolver(tmp_path)
    entries = daily_entries(content)
    assert resolver.entry_ids_at(content, entries, 0) == ('11:00:00',)
    entries[:] = [('22:00:00', 0, len(content))]
    assert resolver.entry_ids_at(content, entries, 0) == ('22:00:00',)
    assert not resolver._immutable_entry_metadata


def test_new_bytes_and_wrong_digest_cannot_borrow_entry_authority(tmp_path):
    content = b'## [11:00:00] event\nOriginal.\n'
    resolver = EvidenceResolver(tmp_path)
    entries = tuple(daily_entries(content))
    assert resolver.entry_ids_at(content, entries, 0) == ('11:00:00',)
    changed = content.replace(b'Original.', b'Changed!.')
    start = changed.index(b'Changed!')
    ref = EvidenceRef('2026-09-27', sha256_bytes(content), '11:00:00', start, start+9)
    with pytest.raises(resolver_module.EvidenceResolutionError, match='hash'):
        resolver.resolve_bytes(ref, changed, source_path=tmp_path/'day.md', reuse_immutable=True)
    with pytest.raises(TypeError, match='immutable'):
        resolver.entry_ids_at(bytearray(content), entries, 0)
