"""Streaming capture releases finished daily parts without weakening evidence."""
import weakref
from dataclasses import replace

import pytest
from llm_wiki import compile_pipeline_sources

from tests.compile_daily_part_lifetime_helpers import _long_day
from tests.compile_transactions_helpers import vault as vault


@pytest.mark.parametrize('receipts', [False, True])
def test_capture_releases_earlier_parts_while_same_daily_is_open(vault, receipts):
    path = _long_day(vault[0])
    compiled = (lambda path, digest: False) if receipts else None
    records = compile_pipeline_sources.iter_compile_records([path], compiled=compiled)
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
    logical = compile_pipeline_sources.logical_path(path)
    parts = compile_pipeline_sources.daily_parts(logical, path.read_bytes())
    completed = {part.sha256 for part in parts[::2]}
    def compiled(path, digest):
        return digest in completed

    expected = compile_pipeline_sources.snapshot_compile_inputs([path], compiled=compiled)
    # Streaming originals are borrowed ranges: compare their exact bytes while owned.
    actual = tuple(replace(record, original_content=record.original_content[:])
                   for group, record in compile_pipeline_sources.iter_compile_records(
                       [path], compiled=compiled) if group == 'dailies')
    assert actual == expected.dailies
    assert actual
