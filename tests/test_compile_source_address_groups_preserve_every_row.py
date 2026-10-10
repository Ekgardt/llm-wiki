"""Compress only repeated display metadata, never source rows or authority."""
import re

import compile_memory as compiler


def _decoded_groups(text):
    return [(timestamp, source_line, file_line)
            for timestamp, body in re.findall(r'ENTRY ([^\n]+)\n((?:source_line=[^\n]+\n?)+)', text)
            for source_line, file_line in _decoded_rows(body)]


def _decoded_rows(text):
    return [pair for row in re.findall(r'source_line=(\d+)(?:\.\.(\d+))? locator=LF:(\d+)(?:\.\.(\d+))?', text)
            for pair in _expanded_address(*row)]


def _expanded_address(first_id, last_id, first_lf, last_lf):
    ids = range(int(first_id), int(last_id or first_id) + 1)
    lfs = range(int(first_lf), int(last_lf or first_lf) + 1)
    assert len(ids) == len(lfs) and len(ids) > 0
    return list(zip(ids, lfs))


def test_address_groups_preserve_every_ordered_row_and_repeated_entry():
    rows = [dict(source_path='knowledge/daily/2026-09-27.md', timestamp=timestamp,
                 source_line=index * 7, file_line=index * 3)
            for index, timestamp in enumerate(['12:26:31', '12:26:31', '13:00:00', '12:26:31'], 1)]
    text = compiler._source_address_table(rows)
    expected = [(row['timestamp'], row['source_line'], row['file_line']) for row in rows]
    assert _decoded_groups(text) == expected
    assert text.count('ENTRY 12:26:31') == 2
    assert text.count('FILE: knowledge/daily/2026-09-27.md') == 1


def test_dense_entry_does_not_repeat_entry_metadata_for_every_row():
    rows = [dict(source_path='knowledge/daily/2026-09-27.md', timestamp='12:26:31',
                 source_line=index + 50, file_line=index + 1) for index in range(402)]
    text = compiler._source_address_table(rows)
    assert text.count('12:26:31') == 1
    assert _decoded_groups(text) == [('12:26:31', row['source_line'], row['file_line']) for row in rows]
