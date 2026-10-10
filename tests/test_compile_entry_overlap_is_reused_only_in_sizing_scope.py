"""Repeated immutable metadata work must not rescan the source's entry table."""
from dataclasses import replace
from types import SimpleNamespace

import compile_memory as compiler


class ObservedEntries(tuple):
    def __new__(cls, entries):
        instance = super().__new__(cls, entries)
        instance.scans = 0
        return instance

    def __iter__(self):
        self.scans += 1
        return super().__iter__()


def _part(entries):
    return compiler.DailySnapshot('knowledge/daily/2026-10-02.md', b'part', 'digest',
        byte_start=4, byte_end=8, original_entries=entries)


def _measure():
    return SimpleNamespace(choice_bindings={}, choice_resolver=None,
                           journal_indexes=None, partitions=None)


def test_sizing_scans_immutable_entry_overlap_once_and_returns_fresh_lists():
    entries = ObservedEntries((('before', 0, 4), ('selected', 4, 8), ('after', 8, 12)))
    part = _part(entries)
    with compiler._measure_choice_resolution(_measure()):
        first = compiler._part_entry_ids(part)
        first.append('forged')
        assert compiler._part_entry_ids(part) == ['selected']
        assert compiler._part_entry_ids(part) == ['selected']
    assert entries.scans == 1
    assert compiler._part_entry_ids(part) == ['selected']
    assert entries.scans == 2  # Normal callers never inherit a sizing approval.


def test_replacement_with_the_same_source_key_is_a_different_metadata_input():
    entries = ObservedEntries((('selected', 4, 8),))
    part = _part(entries)
    changed = replace(part, original_entries=(('replacement', 4, 8),))
    with compiler._measure_choice_resolution(_measure()):
        assert compiler._part_entry_ids(part) == ['selected']
        assert compiler._part_entry_ids(changed) == ['replacement']
        assert compiler._part_entry_ids(part) == ['selected']
    assert entries.scans == 1
