"""Range display preserves every authenticated address, including sparse gaps."""
import re

import compile_memory as cm
import pytest

from tests.test_compile_source_address_groups_preserve_every_row import (
    _decoded_groups,
    _decoded_rows,
)


def _rows(ids, lfs, path='knowledge/daily/2026-09-27.md'):
    return [dict(source_path=path, timestamp='13:51:26', source_line=i, file_line=lf)
            for i, lf in zip(ids, lfs, strict=True)]


def test_dense_run_preserves_every_address_and_labels():
    rows = _rows(range(144, 546), range(44, 446))
    table = cm._source_address_table(rows)
    assert 'source_line=144..545 locator=LF:44..445 (paired in order)' in table
    assert _decoded_groups(table) == [(row['timestamp'], row['source_line'], row['file_line']) for row in rows]
    assert len(_decoded_rows(table)) == 402


@pytest.mark.parametrize('ids,lfs', [([144, 145, 149, 150], [44, 45, 46, 47]),
                                    ([144, 145, 146, 147], [44, 45, 49, 50]),
                                    ([194, 196], [170, 172])])
def test_sparse_missing_id_or_locator_never_becomes_an_address(ids, lfs):
    table = cm._source_address_table(_rows(ids, lfs))
    assert _decoded_rows(table) == list(zip(ids, lfs, strict=True))
    assert {row[0] for row in _decoded_rows(table)} == set(ids)
    assert {row[1] for row in _decoded_rows(table)} == set(lfs)


def test_paths_and_repeated_entry_groups_do_not_join_ranges():
    first = _rows([1, 2], [1, 2])
    second = _rows([3, 4], [3, 4], 'knowledge/daily/2026-09-28.md')
    table = cm._source_address_table(first + second)
    blocks = re.findall(r'FILE: ([^\n]+)\n(.*?)(?=FILE:|\Z)', table, re.DOTALL)
    actual = [(path, timestamp, sid, lf) for path, body in blocks for timestamp, sid, lf in _decoded_groups(body)]
    assert actual == [(row['source_path'], row['timestamp'], row['source_line'], row['file_line']) for row in first + second]


@pytest.mark.parametrize('text', ['source_line=1..4 locator=LF:2..4',
                                 'source_line=4..1 locator=LF:4..1'])
def test_test_decoder_refuses_mismatched_or_reversed_ranges(text):
    with pytest.raises(AssertionError):
        _decoded_rows(text)


def test_utf8_multiple_physical_parts_preserve_raw_source_and_every_offered_row():
    from tests.test_a_long_entry_is_cut_inside_itself import LOGICAL, _entry

    raw = _entry(1, b'\n## [12:26:31] pre-compact | session\n' +
                 b''.join(f'- точный факт номер {n}.\n'.encode() for n in range(900)))
    parts = tuple(cm._daily_parts(LOGICAL, raw))
    inputs = cm.CompileInputs(parts, (), ())
    batches = cm.pack_compile_batches(inputs, model=None)
    assert len(parts) > 1
    assert b''.join(part.content for batch in batches for part in batch.inputs.dailies) == raw
    for batch in batches:
        _assert_every_raw_batch_row(batch)


def _assert_every_raw_batch_row(batch):
    choices = cm._source_line_choices(batch.inputs)
    table = cm._source_address_table(choices)
    assert _decoded_groups(table) == [(row['timestamp'], row['source_line'], row['file_line']) for row in choices]
    prompt = cm._draft_prompt(batch.inputs)
    assert all(source.content.decode() in prompt for source in batch.inputs.sources)
