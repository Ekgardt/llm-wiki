"""Archive source scopes must finish before publication and intentional deletion."""
from __future__ import annotations

import archive_daily
import pytest

from tests.test_archive_daily_bagit import _archiver
from tests.test_archive_daily_bagit import archive_vault as archive_vault


def test_archive_reads_sources_without_whole_file_buffers(archive_vault, monkeypatch):
    root, state, daily = archive_vault
    original = archive_daily.read_stable_bytes

    def read(path, bound, **kwargs):
        if kwargs.get('label') in {'daily archive source', 'daily duplicate', 'archive quarantine copy'}:
            raise AssertionError('archive loaded a complete source')
        return original(path, bound, **kwargs)

    monkeypatch.setattr(archive_daily, 'read_stable_bytes', read)
    archived = _archiver(root, state).archive(daily.stem)
    assert archived.state == 'archived'
    assert not daily.exists()


def test_source_change_after_build_blocks_publication(archive_vault, monkeypatch):
    root, state, daily = archive_vault
    archiver = _archiver(root, state)
    original = archiver._build_bag_contents

    def changed(*args, **kwargs):
        result = original(*args, **kwargs)
        daily.write_bytes(daily.read_bytes() + b'changed after copy\n')
        return result

    monkeypatch.setattr(archiver, '_build_bag_contents', changed)
    with pytest.raises((OSError, ValueError, RuntimeError)):
        archiver.archive(daily.stem)
    assert daily.exists()
    assert not list(archiver.archive_root.rglob('bag-*'))
    assert not list(archiver.archive_root.rglob('*.building-*'))


def test_source_ranges_are_closed_before_archive_publication(archive_vault, monkeypatch):
    from contextlib import contextmanager

    root, state, daily = archive_vault
    archiver = _archiver(root, state)
    original_open, original_publish = archiver._open_archive_source, archiver._publish_build
    ranges = []

    @contextmanager
    def opened(source):
        with original_open(source) as content:
            ranges.append(content)
            yield content

    def publish(*args):
        assert ranges and all(content.stream.closed for content in ranges)
        return original_publish(*args)

    monkeypatch.setattr(archiver, '_open_archive_source', opened)
    monkeypatch.setattr(archiver, '_publish_build', publish)
    assert archiver.archive(daily.stem).state == 'archived'


def test_quarantine_copy_streams_and_preserves_exact_bytes(tmp_path, monkeypatch):
    source, staging = tmp_path / 'source', tmp_path / 'staging'
    source.mkdir()
    staging.mkdir()
    file = source / 'data'
    file.write_bytes(b'archive evidence\n' * 20000)
    monkeypatch.setattr(archive_daily, 'read_stable_bytes', _forbid_eager_copy)
    archive_daily.DailyArchiver._copy_files(source, staging, [file])
    assert (staging / 'data').read_bytes() == file.read_bytes()


def _forbid_eager_copy(*args, **kwargs):
    raise AssertionError('quarantine copy loaded a complete file')
