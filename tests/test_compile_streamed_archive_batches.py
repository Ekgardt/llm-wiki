"""The archive planner must never materialize the complete daily snapshot."""
import io
import weakref
import zipfile
from contextlib import contextmanager
from dataclasses import replace

import compile_controller as controller
import compile_memory as compiler
import pytest

from tests.test_compile_groups_complete_sources_by_qualified_capacity import (
    MODEL,
    _candidates,
    _inputs,
    _minimum,
)
from tests.test_compile_keeps_physically_overlapping_existing_context import (
    _native_context,
    _overlap_inputs,
    _part_version_inputs,
    _repeated_part_inputs,
)
from tests.test_compile_keeps_physically_overlapping_existing_context import (
    vault as vault,
)


@contextmanager
def _archive(inputs):
    output = io.BytesIO()
    digest = controller.compile_inputs_digest(inputs)
    controller._write_archive_snapshot(inputs, digest, output)
    with zipfile.ZipFile(output) as archive:
        yield controller.CompileInputArchive(archive, digest)


def _materialized(batch):
    inputs = batch.inputs
    return replace(batch, inputs=replace(inputs, sources=tuple(inputs.sources),
                                         targets=tuple(inputs.targets),
                                         vault_files=tuple(inputs.vault_files)))


def _refuse_materialization(*args):
    raise AssertionError('whole snapshot materialized')


@pytest.mark.parametrize('fixture', [_overlap_inputs, _part_version_inputs, _repeated_part_inputs])
@pytest.mark.parametrize('context_pending', [False, True])
def test_archive_batches_preserve_required_and_optional_context(vault, fixture, context_pending, monkeypatch):
    inputs, _ = fixture(vault[0])
    expected = compiler._pack_compile_batches(inputs, model=None, context_pending=context_pending)
    with _archive(inputs) as archive:
        monkeypatch.setattr(archive, 'planning_inputs', _refuse_materialization)
        monkeypatch.setattr(archive, 'materialize', _refuse_materialization)
        actual = tuple(archive.iter_batches(model=None, context_pending=context_pending))
        assert tuple(_materialized(batch) for batch in actual) == expected


def test_archive_batches_keep_native_atomic_units(tmp_path, monkeypatch):
    inputs, _ = _native_context(tmp_path, monkeypatch)
    subset = compiler._subset_compile_inputs(inputs, {part.part_key for part in inputs.dailies},
                                              {'knowledge/notes/native-rule.md'})
    candidates = _candidates(_minimum(subset))
    expected = compiler.pack_compile_batches(inputs, model=MODEL, planning_candidates=candidates)
    with _archive(inputs) as archive:
        actual = archive.iter_batches(model=MODEL, planning_candidates=candidates)
        assert tuple(_materialized(batch) for batch in actual) == expected


def _many_days(count=8):
    first = _inputs()
    daily = first.dailies[0]
    source = first.sources[0]
    paths = [f'knowledge/daily/2026-10-{day:02}.md' for day in range(1, count + 1)]
    return compiler.CompileInputs(tuple(replace(daily, logical_path=path) for path in paths),
                                  tuple(replace(source, logical_path=path) for path in paths), ())


@pytest.mark.parametrize('count', [2, 8])
def test_archive_planner_releases_finished_daily_sources(monkeypatch, count):
    inputs = _many_days(count)
    expected = compiler.pack_compile_batches(inputs, model=MODEL)
    assert len(expected) == len(inputs.dailies)
    references = []
    restore = controller._restored_archive_record

    def observe(kind, value):
        record = restore(kind, value)
        if kind is compiler.DailySnapshot:
            references.append((record.logical_path, weakref.ref(record)))
        return record

    monkeypatch.setattr(controller, '_restored_archive_record', observe)
    with _archive(inputs) as archive:
        batches = archive.iter_batches(model=MODEL)
        first = next(batches)
        assert _materialized(first) == expected[0]
        del first
        second = next(batches)
        assert _materialized(second) == expected[1]
        first_path = inputs.dailies[0].logical_path
        first_records = [reference for path, reference in references if path == first_path]
        assert first_records
        assert all(reference() is None for reference in first_records)
        assert tuple(_materialized(batch) for batch in batches) == expected[2:]


def test_streamed_qualified_capacity_preserves_cross_day_grouping():
    inputs = _inputs()
    candidates = _candidates(_minimum(inputs))
    expected = compiler.pack_compile_batches(inputs, model=MODEL, planning_candidates=candidates)
    assert len(expected) == 1
    with _archive(inputs) as archive:
        actual = archive.iter_batches(model=MODEL, planning_candidates=candidates)
        assert tuple(_materialized(batch) for batch in actual) == expected


def test_streamed_planning_preserves_nonadditive_tokenizer():
    from tests.test_compile_plans_the_local_provider_layout import _adversarial_count

    inputs = _many_days()
    adapters = {MODEL: _adversarial_count}
    expected = compiler.pack_compile_batches(inputs, model=MODEL, token_adapters=adapters)
    with _archive(inputs) as archive:
        actual = archive.iter_batches(model=MODEL, token_adapters=adapters)
        assert tuple(_materialized(batch) for batch in actual) == expected


def test_streamed_planning_refuses_invalid_dlp(vault, monkeypatch):
    inputs, _ = _overlap_inputs(vault[0])
    with _archive(inputs) as archive:
        monkeypatch.setenv('LLM_WIKI_DLP_POLICY', str(vault[0] / 'missing-policy.json'))
        with pytest.raises(ValueError):
            next(archive.iter_batches(model=None))


def test_empty_archive_has_no_batches():
    with _archive(compiler.CompileInputs((), (), ())) as archive:
        assert tuple(archive.iter_batches(model=None)) == ()


@pytest.mark.parametrize('position', [0, 1, 2])
def test_streamed_refusal_keeps_independent_parts(vault, monkeypatch, position):
    root, _ = vault
    _part_version_inputs(root)
    note = root / 'knowledge/notes/part-version.md'
    note.write_bytes(note.read_bytes() + b'\n' + b'z' * 38000)
    inputs = compiler.snapshot_compile_inputs([root / 'knowledge/daily/2026-07-14.md'])
    refused_part = inputs.dailies[-1]
    parts = list(inputs.dailies[:-1])
    parts.insert(position, refused_part)
    inputs = replace(inputs, dailies=tuple(parts))
    accepted, expected_refused = compiler.partition_packable(inputs, model=None)
    assert len(expected_refused) == 1
    refused = []
    with _archive(inputs) as archive:
        monkeypatch.setattr(archive, 'materialize', _refuse_materialization)
        monkeypatch.setattr(archive, 'planning_inputs', _refuse_materialization)
        batches = tuple(archive.iter_batches(model=None, on_refused=refused.append))
        assert tuple(part for batch in batches for part in batch.inputs.dailies) == accepted.dailies
        assert tuple(item.dailies for item in refused) == ((refused_part,),)
        assert note.read_bytes() in [source.content for source in refused[0].sources]


def test_refusal_callback_does_not_suppress_dlp_errors(vault, monkeypatch):
    inputs, _ = _overlap_inputs(vault[0])
    refused = []
    with _archive(inputs) as archive:
        monkeypatch.setenv('LLM_WIKI_DLP_POLICY', str(vault[0] / 'missing-policy.json'))
        with pytest.raises(ValueError):
            tuple(archive.iter_batches(model=None, on_refused=refused.append))
    assert not refused


def test_streamed_all_refused_reports_each_and_releases_records(vault):
    from tests.test_compile_keeps_physically_overlapping_existing_context import _large_required

    inputs = _large_required(vault[0])
    references = []

    def record_refusal(refused):
        references.append(weakref.ref(refused))
        references.extend(weakref.ref(part) for part in refused.dailies)

    with _archive(inputs) as archive:
        assert tuple(archive.iter_batches(model=None, on_refused=record_refusal)) == ()
        assert references
        assert all(reference() is None for reference in references)


def test_streamed_default_still_refuses_oversized_source(vault):
    from tests.test_compile_keeps_physically_overlapping_existing_context import _large_required

    with _archive(_large_required(vault[0])) as archive:
        with pytest.raises(ValueError, match='daily source exceeds compile input budget'):
            tuple(archive.iter_batches(model=None))


def test_failed_refusal_recording_stops_planning(vault):
    from tests.test_compile_keeps_physically_overlapping_existing_context import _large_required

    def fail(_refused):
        raise OSError('cannot save refusal')

    with _archive(_large_required(vault[0])) as archive:
        with pytest.raises(OSError, match='cannot save refusal'):
            tuple(archive.iter_batches(model=None, on_refused=fail))


def test_streamed_refusal_preserves_complete_native_unit(tmp_path, monkeypatch):
    from tests.test_native_compile_uses_whole_container import _native_inputs

    inputs, _ = _native_inputs(tmp_path, monkeypatch, 'A complete native source. ' * 1000)
    unit = max(compiler._native_part_units(inputs.dailies), key=len)
    assert len(unit) > 1
    complete = compiler._subset_compile_inputs(inputs, {part.part_key for part in unit})
    candidates = _candidates(_minimum(complete) - 1)
    accepted, expected = compiler.partition_packable(inputs, model=MODEL, planning_candidates=candidates)
    refused = []
    with _archive(inputs) as archive:
        batches = tuple(archive.iter_batches(model=MODEL, planning_candidates=candidates,
                                            on_refused=refused.append))
        assert tuple(part for batch in batches for part in batch.inputs.dailies) == accepted.dailies
        assert _daily_groups(refused) == _daily_groups(expected)
        assert tuple(unit) in _daily_groups(refused)


def _daily_groups(inputs):
    return tuple(item.dailies for item in inputs)
