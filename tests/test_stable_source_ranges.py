"""Live file ranges retain content proof and a checked, bounded owner."""
import hashlib
import os

import bounded_io as bio
import pytest


def test_live_range_is_verified_without_whole_file_materialization(tmp_path, monkeypatch):
    path = tmp_path / 'daily.md'
    raw = b'complete physical evidence\n' * 10000
    path.write_bytes(raw)
    read = bio._read_original_block
    sizes = []

    def observe(stream, size, deadline):
        sizes.append(size)
        return read(stream, size, deadline)

    monkeypatch.setattr(bio, '_read_original_block', observe)
    with bio.open_stable_range(path, len(raw)) as source:
        assert type(source) is bio.VerifiedByteRange
        assert source.digest == hashlib.sha256(raw).hexdigest()
        assert sum(sizes) == len(raw)
        assert max(sizes) <= bio.IO_CHUNK_BYTES
        assert source[31:99] == raw[31:99]
    assert source.stream.closed
    with pytest.raises(ValueError, match='closed'):
        source[:1]


@pytest.mark.parametrize('mutation', ['replace', 'append', 'rewrite'])
def test_live_range_rejects_changed_source_even_for_cached_block(tmp_path, mutation):
    path = tmp_path / 'daily.md'
    path.write_bytes(b'original evidence')
    with pytest.raises(bio.SourceChangedDuringRead):
        with bio.open_stable_range(path, 1024) as source:
            assert source[:1] == b'o'
            _mutate(path, mutation)
            source[:1]


def _mutate(path, mutation):
    if mutation == 'replace':
        path.rename(path.with_suffix('.old'))
    mode = 'ab' if mutation == 'append' else 'wb'
    with path.open(mode) as output:
        output.write(b'changed evidence!')


@pytest.mark.skipif(os.name == 'nt', reason='Windows ctime does not represent Unix metadata change')
def test_live_range_rejects_rewrite_with_restored_mtime(tmp_path):
    path = tmp_path / 'daily.md'
    path.write_bytes(b'first')
    before = path.stat()
    with pytest.raises((bio.SourceChangedDuringRead, ValueError), match='changed'):
        with bio.open_stable_range(path, 1024) as source:
            path.write_bytes(b'other')
            os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
            source[:1]


def test_live_range_validates_on_normal_exit_without_further_reads(tmp_path):
    path = tmp_path / 'daily.md'
    path.write_bytes(b'first')
    with pytest.raises(bio.SourceChangedDuringRead):
        with bio.open_stable_range(path, 1024):
            path.write_bytes(b'changed')


def test_final_content_check_rejects_rewrite_with_unchanged_stat(tmp_path, monkeypatch):
    path = tmp_path / 'daily.md'
    path.write_bytes(b'first')
    before = path.stat()
    # Model a filesystem whose timestamps cannot distinguish these two writes.
    monkeypatch.setattr(bio.os, 'fstat', lambda descriptor: before)
    with pytest.raises(bio.SourceChangedDuringRead, match='content changed'):
        with bio.open_stable_range(path, 1024) as source:
            assert source[:1] == b'f'
            path.write_bytes(b'other')


def test_live_range_closes_on_consumer_failure(tmp_path):
    path = tmp_path / 'daily.md'
    path.write_bytes(b'first')
    with pytest.raises(RuntimeError, match='consumer failed'):
        with bio.open_stable_range(path, 1024) as source:
            raise RuntimeError('consumer failed')
    assert source.stream.closed


def test_live_range_rejects_symlink(tmp_path):
    path = tmp_path / 'daily.md'
    path.write_bytes(b'first')
    link = tmp_path / 'linked.md'
    link.symlink_to(path)
    with pytest.raises(PermissionError):
        with bio.open_stable_range(link, 1024):
            pytest.fail('symlink admitted')
