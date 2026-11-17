"""Compress only repeated display metadata, never source rows or authority."""

from llm_wiki import compile_pipeline_prompts

from tests.compile_source_address_groups_preserve_every_row_helpers import _decoded_groups


def test_address_groups_preserve_every_ordered_row_and_repeated_entry():
    rows = [dict(source_path='knowledge/daily/2026-09-27.md', timestamp=timestamp,
                 source_line=index * 7, file_line=index * 3)
            for index, timestamp in enumerate(['12:26:31', '12:26:31', '13:00:00', '12:26:31'], 1)]
    text = compile_pipeline_prompts.source_address_table(rows)
    expected = [(row['timestamp'], row['source_line'], row['file_line']) for row in rows]
    assert _decoded_groups(text) == expected
    assert text.count('ENTRY 12:26:31') == 2
    assert text.count('FILE: knowledge/daily/2026-09-27.md') == 1


def test_dense_entry_does_not_repeat_entry_metadata_for_every_row():
    rows = [dict(source_path='knowledge/daily/2026-09-27.md', timestamp='12:26:31',
                 source_line=index + 50, file_line=index + 1) for index in range(402)]
    text = compile_pipeline_prompts.source_address_table(rows)
    assert text.count('12:26:31') == 1
    assert _decoded_groups(text) == [('12:26:31', row['source_line'], row['file_line']) for row in rows]
