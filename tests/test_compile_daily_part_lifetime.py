"""Streaming capture releases finished daily parts without weakening evidence."""
import weakref

import compile_memory as compiler
import pytest

from tests.test_compile_transactions import vault as vault


def _long_day(root):
    path = root / 'knowledge/daily/2026-10-20.md'
    body = b'# Daily\n' + b''.join(
        f'\n## [10:00:{index:02d}] session-end | manual\n'.encode()
        + bytes([65 + index]) * compiler.MAX_DAILY_PART_BYTES
        for index in range(8))
    path.write_bytes(body)
    return path


@pytest.mark.parametrize('receipts', [False, True])
def test_capture_releases_earlier_parts_while_same_daily_is_open(vault, receipts):
    path = _long_day(vault[0])
    compiled = (lambda path, digest: False) if receipts else None
    records = compiler.iter_compile_records([path], compiled=compiled)
    group, first = next(records)
    assert group == 'dailies'
    assert first.part_count > 4
    reference = weakref.ref(first)
    del first
    for _ in range(3):
        group, following = next(records)
        assert group == 'dailies'
    assert reference() is None
    records.close()


def test_streamed_long_daily_preserves_all_parts_and_receipt_flags(vault):
    path = _long_day(vault[0])
    logical = compiler._logical_path(path)
    parts = compiler._daily_parts(logical, path.read_bytes())
    completed = {part.sha256 for part in parts[::2]}
    def compiled(path, digest):
        return digest in completed

    expected = compiler.snapshot_compile_inputs([path], compiled=compiled)
    actual = tuple(record for group, record in compiler.iter_compile_records(
        [path], compiled=compiled) if group == 'dailies')
    assert actual == expected.dailies
    assert actual
