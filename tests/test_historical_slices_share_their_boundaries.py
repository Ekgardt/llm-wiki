"""Historical citation lookup keeps full hash proofs without repeated source scans."""
import hashlib

import evidence_resolver as resolver


def test_one_slice_lookup_scans_entry_offsets_once(monkeypatch):
    content = b'<!-- llm-wiki-operation:x -->\n' + b'x\n' * resolver.MAX_DAILY_PART_BYTES
    original = resolver._slice_offsets
    calls = []

    def counted(source):
        calls.append(len(source))
        return original(source)

    monkeypatch.setattr(resolver, '_slice_offsets', counted)
    assert resolver.compile_part_slice(content, hashlib.sha256(b'absent').hexdigest()) is None
    assert len(calls) == 1


def test_a_real_historical_boundary_after_the_old_cutoff_still_resolves():
    entries = [f'## [{index}] entry\nx\n'.encode() for index in range(5000)]
    historical = b''.join(entries[:4200])
    content = b''.join(entries)
    assert resolver.compile_part_slice(content, hashlib.sha256(historical).hexdigest()) == historical


def test_current_parts_do_not_need_historical_boundary_scanning(monkeypatch):
    content = ('## [one] entry\n' + 'unicode 字\n' * 5000).encode()
    bounds = resolver._daily_part_bounds(content)
    original = resolver._slice_offsets
    calls = []

    def counted(source):
        calls.append(len(source))
        return original(source)

    monkeypatch.setattr(resolver, '_slice_offsets', counted)
    parts = [content[start:end] for start, end in bounds]
    assert [resolver.compile_part_slice(content, hashlib.sha256(part).hexdigest()) for part in parts] == parts
    assert not calls
