"""Incremental planning must preserve atomic sources and exact eager batches."""
import weakref

import compile_receipts
import pytest
from llm_wiki import compile_pipeline_batches, compile_pipeline_sources

from tests.compile_groups_complete_sources_by_qualified_capacity_helpers import MODEL, _inputs
from tests.compile_keeps_complete_tool_lines_helpers import _tool_parts
from tests.compile_paths import point_compile_path


def test_atomic_iterator_reads_only_through_next_boundary():
    consumed = []

    def records():
        for value in (1, 2, 4, 5, 7):
            consumed.append(value)
            yield value

    units = compile_receipts.iter_native_part_units(records(), join=lambda left, right: right == left + 1)
    assert consumed == []
    assert next(units) == [1, 2]
    assert consumed == [1, 2, 4]
    assert list(units) == [[4, 5], [7]]


def test_atomic_iterator_preserves_complete_tool_line():
    parts, _ = _tool_parts()
    assert list(compile_receipts.iter_native_part_units(iter(parts))) == [parts]
    assert compile_pipeline_sources.native_part_units(parts) == [parts]


def test_batch_iterator_constructs_and_releases_one_batch_at_a_time(monkeypatch):
    inputs = _inputs()
    expected = compile_pipeline_batches.pack_compile_batches(inputs, model=MODEL)
    original = compile_pipeline_batches.compile_batch
    references = []

    def observe(*args, **kwargs):
        batch = original(*args, **kwargs)
        references.append(weakref.ref(batch))
        return batch

    point_compile_path(monkeypatch, 'compile_batch', observe)
    batches = compile_pipeline_batches.iter_compile_batches(inputs, model=MODEL)
    assert references == []
    first = next(batches)
    assert first == expected[0]
    assert len(references) == 1
    del first
    second = next(batches)
    assert second == expected[1]
    assert references[0]() is None
    assert len(references) == 2
    with pytest.raises(StopIteration):
        next(batches)


def test_batch_iterator_propagates_late_construction_error(monkeypatch):
    inputs = _inputs()
    original = compile_pipeline_batches.compile_batch
    calls = []

    def construct(*args, **kwargs):
        calls.append(None)
        if len(calls) == 2:
            raise ValueError('late invalid source')
        return original(*args, **kwargs)

    point_compile_path(monkeypatch, 'compile_batch', construct)
    batches = compile_pipeline_batches.iter_compile_batches(inputs, model=MODEL)
    next(batches)
    with pytest.raises(ValueError, match='late invalid source'):
        next(batches)
    calls.clear()
    with pytest.raises(ValueError, match='late invalid source'):
        compile_pipeline_batches.pack_compile_batches(inputs, model=MODEL)
