"""Raw LF positions are mechanical; context protection and binding remain fresh."""
from dataclasses import replace

import compile_pipeline
import compile_receipts
import pytest
from llm_wiki import compile_pipeline_operations, compile_pipeline_prompts, compile_pipeline_sources

from tests.compile_binds_the_protected_source_view_helpers import _legacy_packet
from tests.compile_paths import point_compile_path
from tests.compile_reuses_raw_row_positions_within_one_layout_helpers import (
    _bound_choices,
    _context_pages,
    _packing_result,
)


class _ObservedBytes(bytes):
    calls = 0

    def decode(self, *args, **kwargs):
        import sys

        if sys._getframe(1).f_code.co_name in ('_collect_choice_row', 'raw_choice_row_positions'):
            type(self).calls += 1
        return super().decode(*args, **kwargs)


def test_full_layout_does_not_decode_same_raw_source_for_every_row(tmp_path, monkeypatch):
    inputs = _legacy_packet(tmp_path, monkeypatch, [f'Факт {index} is settled.' for index in range(30)])
    _ObservedBytes.calls = 0
    source = replace(inputs.sources[0], content=_ObservedBytes(inputs.sources[0].content))
    inputs = replace(inputs, sources=(source,))
    prompt, schema = compile_pipeline_prompts.draft_layout(inputs)
    assert source.content.decode() in prompt
    assert schema['type'] == 'object'
    assert _ObservedBytes.calls <= 1, 'same complete source was decoded repeatedly for individual rows'


def _original_position(content, character_offset):
    byte_offset = len(content.decode('utf-8')[:character_offset].encode('utf-8'))
    return byte_offset, content[:byte_offset].count(b'\n') + 1


@pytest.mark.parametrize('raw', [b'', b'a', b'a\n', b'a\r\nb\r\n',
                               'До\n\n事实\nlast'.encode(), b'\x00\nlast'])
def test_exact_raw_lf_positions_preserve_utf8_and_crlf(raw):
    positions = compile_pipeline_prompts.raw_choice_row_positions(raw)
    for character_offset, actual in positions.items():
        assert actual == _original_position(raw, character_offset)
    assert len(positions) == raw.count(b'\n') + 1


def _old_position(source, character_offset):
    return _original_position(source.content, character_offset)


def test_entire_layout_schema_and_bindings_equal_original_position_logic(tmp_path, monkeypatch):
    inputs = _legacy_packet(tmp_path, monkeypatch, ['Первый факт.', '', 'Second fact.', 'Last fact.'])
    context = b'Complete independent context.\r\n'
    page = compile_receipts.SourceSnapshot('knowledge/notes/a-context.md', context, compile_pipeline.sha256_bytes(context))
    inputs = replace(inputs, sources=tuple(sorted((*inputs.sources, page), key=lambda source: source.logical_path)))
    actual = compile_pipeline_prompts.draft_layout(inputs), _bound_choices(inputs)
    point_compile_path(monkeypatch, 'choice_row_position', _old_position)
    expected = compile_pipeline_prompts.draft_layout(inputs), _bound_choices(inputs)
    assert actual == expected
    assert context.decode() in actual[0][0]


def test_scope_restores_on_exception_and_different_source_bytes():
    first = compile_receipts.SourceSnapshot('knowledge/daily/2026-09-25.md', b'a\nb', 'unused')
    second = replace(first, content=b'long\nb')
    with compile_pipeline_prompts.layout_protection_scope():
        assert compile_pipeline_prompts.choice_row_position(first, 2) == (2, 2)
        with pytest.raises(RuntimeError), compile_pipeline_prompts.layout_protection_scope():
            assert compile_pipeline_prompts.choice_row_position(second, 5) == (5, 2)
            raise RuntimeError('neutral scope failure')
        assert compile_pipeline_prompts.choice_row_position(first, 2) == (2, 2)
    assert compile_pipeline_operations.LAYOUT_PROTECTION.get() is None


@pytest.mark.parametrize('row_count', [100, 750])
def test_full_optional_selection_matches_baseline_and_records_cost(tmp_path, monkeypatch, record_property, row_count):
    import time

    inputs = _legacy_packet(tmp_path, monkeypatch, [f'Факт {index} is settled.' for index in range(row_count)])
    parts = tuple(compile_pipeline_sources.daily_parts(inputs.dailies[0].logical_path, inputs.dailies[0].content))
    inputs = replace(inputs, dailies=parts, sources=(*inputs.sources, *_context_pages()))
    current = compile_pipeline_prompts.choice_row_position
    point_compile_path(monkeypatch, 'choice_row_position', _old_position)
    wall, cpu = time.monotonic(), time.process_time()
    expected = _packing_result(inputs)
    record_property('baseline_wall', time.monotonic() - wall)
    record_property('baseline_cpu', time.process_time() - cpu)
    point_compile_path(monkeypatch, 'choice_row_position', current)
    wall, cpu = time.monotonic(), time.process_time()
    actual = _packing_result(inputs)
    record_property('candidate_wall', time.monotonic() - wall)
    record_property('candidate_cpu', time.process_time() - cpu)
    assert actual == expected
    assert actual[0]
    for batch in actual[0]:
        assert set(source.logical_path for source in batch.inputs.sources) == set(source.logical_path for source in inputs.sources)


def test_invalid_utf8_and_missing_row_are_refused_without_scope_leak():
    with pytest.raises(UnicodeDecodeError):
        compile_pipeline_prompts.raw_choice_row_positions(b'\xff\n')
    source = compile_receipts.SourceSnapshot('knowledge/daily/2026-09-25.md', b'fact\n', 'unused')
    with pytest.raises(KeyError):
        with compile_pipeline_prompts.layout_protection_scope():
            compile_pipeline_prompts.choice_row_position(source, 2)
    assert compile_pipeline_operations.LAYOUT_PROTECTION.get() is None
