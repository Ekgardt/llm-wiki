"""Repeated immutable metadata work must not rescan the source's entry table."""
from dataclasses import replace
from types import SimpleNamespace

import compile_memory as compiler
import pytest

from tests.test_compile_binds_the_protected_source_view import _legacy_packet


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


def _actual_parts(tmp_path, monkeypatch):
    inputs = _legacy_packet(tmp_path, monkeypatch, ['A complete durable fact.'])
    daily = inputs.dailies[0]
    part = compiler._daily_parts(daily.logical_path, daily.content)[0]
    return replace(inputs, dailies=(part,))


def _observed_overlap_scans(monkeypatch):
    calls = []
    original = compiler._uncached_part_entry_ids

    def observed(part):
        calls.append(part)
        return original(part)

    monkeypatch.setattr(compiler, '_uncached_part_entry_ids', observed)
    return calls


def _byte_count(measure, inputs):
    return measure({part.part_key for part in inputs.dailies})


def _context_selection(measure, inputs):
    paths = {part.part_key for part in inputs.dailies}
    return measure.fitting_context(paths, (), compiler._compile_budget(None))


def _token_count(measure, inputs):
    return compiler._measure_token_subset(measure, inputs)


def _token_measure(inputs):
    return compiler._TokenBatchMeasure(inputs, None, {})


@pytest.mark.parametrize('route', ['bytes', 'context', 'tokens'])
def test_layout_probe_uses_the_measure_owned_entry_overlap(tmp_path, monkeypatch, route):
    inputs = _actual_parts(tmp_path, monkeypatch)
    scans = _observed_overlap_scans(monkeypatch)
    factories = {'bytes': compiler._ByteBatchMeasure,
                 'context': compiler._ByteBatchMeasure, 'tokens': _token_measure}
    calls = {'bytes': _byte_count, 'context': _context_selection, 'tokens': _token_count}
    measure = factories[route](inputs)
    scans.clear()

    first = calls[route](measure, inputs)
    assert calls[route](measure, inputs) == first
    assert len(scans) == 1
    assert compiler._SOURCE_CHOICE_BINDINGS.get() is None
    assert compiler._part_entry_ids(inputs.dailies[0]) == ['13:51:26']
    assert len(scans) == 2
