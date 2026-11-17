"""Helpers shared by test_compile_source_address_groups_preserve_every_row.py and the test files importing them."""

import re


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
