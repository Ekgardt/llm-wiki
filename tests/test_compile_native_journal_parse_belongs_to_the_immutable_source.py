"""Pure journal parsing is reusable; permanent native proof remains live."""
from dataclasses import replace

import compile_memory as compiler
import fact_keys
import pytest

from tests.test_native_compile_uses_whole_container import _native_inputs


def _two_units(root, monkeypatch):
    inputs, _ = _native_inputs(root, monkeypatch, "A" * 18000 + "\nActual fact.")
    path = root / inputs.dailies[0].logical_path
    prefix, marker, body = path.read_bytes().partition(b"\n<!-- llm-wiki-operation:")
    block = marker + body
    separator = b"\n## [00:00:01] Separator\n" + b"Z" * (compiler.MAX_DAILY_PART_BYTES * 2) + b"\n"
    path.write_bytes(prefix + block + separator + block)
    inputs = compiler.snapshot_compile_inputs([path])
    frames = inputs.dailies[0].native_frames
    assert len(frames) == 2
    keys = [{part.part_key for part in inputs.dailies
             if part.byte_start < frame.byte_end and part.byte_end > frame.byte_start}
            for frame in frames]
    return inputs, keys


def _observe_parser(monkeypatch):
    seen = []
    original = fact_keys._native_journal_index

    def parse(content):
        seen.append(content)
        return original(content)

    monkeypatch.setattr(fact_keys, "_native_journal_index", parse)
    return seen


def test_two_native_selections_parse_the_shared_immutable_daily_once(tmp_path, monkeypatch):
    inputs, keys = _two_units(tmp_path, monkeypatch)
    expected = [len(compiler._draft_prompt_text(compiler._subset_compile_inputs(inputs, key)).encode())
                for key in keys]
    seen = _observe_parser(monkeypatch)
    measure = compiler._ByteBatchMeasure(inputs)
    assert [measure(key) for key in keys] == expected
    assert len(seen) == 1
    assert seen[0] is inputs.dailies[0].original_content


def test_each_measure_has_its_own_parse_lifetime(tmp_path, monkeypatch):
    inputs, keys = _two_units(tmp_path, monkeypatch)
    seen = _observe_parser(monkeypatch)
    compiler._ByteBatchMeasure(inputs)(keys[0])
    compiler._ByteBatchMeasure(inputs)(keys[0])
    assert len(seen) == 2


def test_changed_original_bytes_are_not_the_cached_journal(tmp_path, monkeypatch):
    inputs, keys = _two_units(tmp_path, monkeypatch)
    measure = compiler._ByteBatchMeasure(inputs)
    measure(keys[0])
    for key in keys[1]:
        part = measure.dailies[key][0]
        changed = part.original_content.replace(b"Captured event", b"Replaced event")
        measure.dailies[key] = [replace(part, original_content=changed)]
    with pytest.raises(ValueError, match="native frame cache|canonical"):
        measure(keys[1])


def test_reused_pure_index_does_not_reuse_permanent_head_authority(tmp_path, monkeypatch):
    inputs, keys = _two_units(tmp_path, monkeypatch)
    measure = compiler._ByteBatchMeasure(inputs)
    measure(keys[0])
    head = next(tmp_path.glob("knowledge/raw/sessions/**/*.breadcrumb.md"))
    head.write_bytes(head.read_bytes() + b"changed\n")
    with pytest.raises(ValueError):
        measure(keys[1])


def test_reused_index_rejects_forged_native_frame(tmp_path, monkeypatch):
    inputs, keys = _two_units(tmp_path, monkeypatch)
    measure = compiler._ByteBatchMeasure(inputs)
    measure(keys[0])
    frame = replace(inputs.dailies[0].native_frames[1], text="Fabricated fact.")
    for key in keys[1]:
        part = measure.dailies[key][0]
        frames = (part.native_frames[0], frame)
        measure.dailies[key] = [replace(part, native_frames=frames)]
    with pytest.raises(ValueError, match="native frame cache"):
        measure(keys[1])


def test_final_batch_rechecks_permanent_head_with_the_measurement_index(tmp_path, monkeypatch):
    inputs, keys = _two_units(tmp_path, monkeypatch)
    measure = compiler._ByteBatchMeasure(inputs)
    measure(keys[0])
    head = next(tmp_path.glob("knowledge/raw/sessions/**/*.breadcrumb.md"))
    head.write_bytes(head.read_bytes() + b"changed\n")
    with pytest.raises(ValueError):
        compiler._compile_batch(inputs, keys[0], compiler._compile_budget(None), None, None,
                                journal_indexes=measure.journal_indexes)


def test_mutable_bytes_do_not_enter_the_immutable_parse_memo(monkeypatch):
    memo = compiler._NativeJournalIndexes()
    seen = _observe_parser(monkeypatch)
    content = bytearray(b"# Day\n")
    assert memo.for_content(content) == {}
    content.extend(b"changed\n")
    assert memo.for_content(content) == {}
    assert len(seen) == 2
    assert memo.sources == {}


def test_final_count_restores_previous_partition_context(monkeypatch):
    previous = ({"old": object()}, {"old": object()})
    expected = ({"new": object()}, {"new": object()})
    token = compiler._SOURCE_CHOICE_PARSING.set(previous)

    def count(*args):
        assert compiler._SOURCE_CHOICE_PARSING.get() is not previous
        assert compiler._SOURCE_CHOICE_PARSING.get() == expected
        return 17

    monkeypatch.setattr(compiler, "_draft_prompt_count", count)
    try:
        assert compiler._count_with_partition_proofs(None, None, None, *expected) == 17
        assert compiler._SOURCE_CHOICE_PARSING.get() is previous
    finally:
        compiler._SOURCE_CHOICE_PARSING.reset(token)


def test_final_count_restores_context_and_propagates_error(monkeypatch):
    previous = ({"old": object()}, {"old": object()})
    token = compiler._SOURCE_CHOICE_PARSING.set(previous)
    failure = ValueError("invalid original source")

    def count(*args):
        raise failure

    monkeypatch.setattr(compiler, "_draft_prompt_count", count)
    try:
        with pytest.raises(ValueError) as caught:
            compiler._count_with_partition_proofs(None, None, None, {}, {})
        assert caught.value is failure
        assert compiler._SOURCE_CHOICE_PARSING.get() is previous
    finally:
        compiler._SOURCE_CHOICE_PARSING.reset(token)
