"""Archive readers retain shared source fields, not every emitted part body."""
import sys
from collections import Counter
from dataclasses import replace

import compile_memory as compiler
import pytest

from tests.test_compile_daily_part_lifetime import _long_day
from tests.test_compile_streamed_archive_batches import _archive, _materialized_daily
from tests.test_compile_transactions import vault as vault


def _observe_parts(reader, digests, probes, monkeypatch):
    read = reader.blob

    def observed(digest):
        value = read(digest)
        if digest in digests:
            probes[digest] = value
        return value

    monkeypatch.setattr(reader, 'blob', observed)


def test_archive_releases_part_bytes_before_its_daily_ends(vault, monkeypatch):
    inputs = compiler.snapshot_compile_inputs([_long_day(vault[0])])
    digests = {part.sha256 for part in inputs.dailies}
    assert inputs.dailies[0].original_sha256 not in digests
    probes = {}
    with _archive(inputs) as reader:
        _observe_parts(reader, digests, probes, monkeypatch)
        records = reader.daily_records()
        first = next(records)
        digest = first.sha256
        del first
        for _ in range(3):
            next(records)
        # Only this probe and getrefcount's argument retain the actual bytes.
        # Measure before pytest's assertion rewrite creates its own references.
        reference_count = sys.getrefcount(probes[digest])
        assert reference_count == 2
        records.close()


def test_archive_reuses_verified_original_and_metadata(vault, monkeypatch):
    inputs = compiler.snapshot_compile_inputs([_long_day(vault[0])])
    with _archive(inputs) as reader:
        reads = Counter()
        opens = Counter()
        read = reader.archive.read
        open_member = reader.archive.open

        def observed(name, *args, **kwargs):
            reads[name] += 1
            return read(name, *args, **kwargs)

        def opened(name, *args, **kwargs):
            opens[name] += 1
            return open_member(name, *args, **kwargs)

        monkeypatch.setattr(reader.archive, 'read', observed)
        monkeypatch.setattr(reader.archive, 'open', opened)
        metadata = reader.record_metadata('dailies', 0)
        assert tuple(_materialized_daily(part) for part in reader.daily_records()) == inputs.dailies
        shared = {metadata[field] for field in (
            'original_entries', 'native_frames')}
        assert all(reads['blobs/' + digest] == 0 for digest in shared)
        assert all(opens['blobs/' + digest] == 1 for digest in shared)
        assert reads['blobs/' + metadata['original_content']] == 0


def test_archive_still_rejects_corrupt_later_part(vault, monkeypatch):
    inputs = compiler.snapshot_compile_inputs([_long_day(vault[0])])
    with _archive(inputs) as reader:
        bad_member = 'blobs/' + inputs.dailies[-1].sha256
        read = reader.archive.read

        def corrupted(name, *args, **kwargs):
            if name == bad_member:
                return b'corrupt'
            return read(name, *args, **kwargs)

        monkeypatch.setattr(reader.archive, 'read', corrupted)
        records = reader.daily_records()
        assert _materialized_daily(next(records)) == inputs.dailies[0]
        with pytest.raises(ValueError, match='snapshot content digest mismatch'):
            tuple(records)


def test_same_path_new_original_releases_previous_reuse_scope(vault, monkeypatch):
    path = _long_day(vault[0])
    first = compiler.snapshot_compile_inputs([path])
    path.write_bytes(path.read_bytes().replace(b'AAAA', b'ZZZZ'))
    second = compiler.snapshot_compile_inputs([path])
    inputs = replace(first, dailies=first.dailies + second.dailies + first.dailies)
    with _archive(inputs) as reader:
        counts = Counter()
        read = reader.archive.open

        def observed(name, *args, **kwargs):
            counts[name] += 1
            return read(name, *args, **kwargs)

        monkeypatch.setattr(reader.archive, 'open', observed)
        assert tuple(_materialized_daily(part) for part in reader.daily_records()) == inputs.dailies
        assert counts['blobs/' + first.dailies[0].original_sha256] == 2
        assert counts['blobs/' + second.dailies[0].original_sha256] == 1
