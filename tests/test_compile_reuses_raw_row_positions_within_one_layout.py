"""Raw LF positions are mechanical; context protection and binding remain fresh."""
from dataclasses import replace

import compile_memory as compiler
import pytest

from tests.test_compile_binds_the_protected_source_view import _legacy_packet


class _ObservedBytes(bytes):
    calls = 0

    def decode(self, *args, **kwargs):
        import sys

        if sys._getframe(1).f_code.co_name in ('_collect_choice_row', '_raw_choice_row_positions'):
            type(self).calls += 1
        return super().decode(*args, **kwargs)


def test_full_layout_does_not_decode_same_raw_source_for_every_row(tmp_path, monkeypatch):
    inputs = _legacy_packet(tmp_path, monkeypatch, [f'Факт {index} is settled.' for index in range(30)])
    _ObservedBytes.calls = 0
    source = replace(inputs.sources[0], content=_ObservedBytes(inputs.sources[0].content))
    inputs = replace(inputs, sources=(source,))
    prompt, schema = compiler._draft_layout(inputs)
    assert source.content.decode() in prompt
    assert schema['type'] == 'object'
    assert _ObservedBytes.calls <= 1, 'same complete source was decoded repeatedly for individual rows'


def _original_position(content, character_offset):
    byte_offset = len(content.decode('utf-8')[:character_offset].encode('utf-8'))
    return byte_offset, content[:byte_offset].count(b'\n') + 1


@pytest.mark.parametrize('raw', [b'', b'a', b'a\n', b'a\r\nb\r\n',
                               'До\n\n事实\nlast'.encode(), b'\x00\nlast'])
def test_exact_raw_lf_positions_preserve_utf8_and_crlf(raw):
    positions = compiler._raw_choice_row_positions(raw)
    for character_offset, actual in positions.items():
        assert actual == _original_position(raw, character_offset)
    assert len(positions) == raw.count(b'\n') + 1


def _old_position(source, character_offset):
    return _original_position(source.content, character_offset)


def _bound_choices(inputs):
    choices = compiler._source_line_choices(inputs)
    return tuple(compiler._evidence_binding(
        {'daily_date': row['daily_date'], 'timestamp': row['timestamp'],
         'quoted_text': row['quoted_text'], 'claim': 'A neutral fact.'}, inputs)
                 for row in choices)


def test_entire_layout_schema_and_bindings_equal_original_position_logic(tmp_path, monkeypatch):
    inputs = _legacy_packet(tmp_path, monkeypatch, ['Первый факт.', '', 'Second fact.', 'Last fact.'])
    context = b'Complete independent context.\r\n'
    page = compiler.SourceSnapshot('knowledge/notes/a-context.md', context, compiler.sha256_bytes(context))
    inputs = replace(inputs, sources=tuple(sorted((*inputs.sources, page), key=lambda source: source.logical_path)))
    actual = compiler._draft_layout(inputs), _bound_choices(inputs)
    monkeypatch.setattr(compiler, '_choice_row_position', _old_position)
    expected = compiler._draft_layout(inputs), _bound_choices(inputs)
    assert actual == expected
    assert context.decode() in actual[0][0]


def test_scope_restores_on_exception_and_different_source_bytes():
    first = compiler.SourceSnapshot('knowledge/daily/2026-09-25.md', b'a\nb', 'unused')
    second = replace(first, content=b'long\nb')
    with compiler._layout_protection_scope():
        assert compiler._choice_row_position(first, 2) == (2, 2)
        with pytest.raises(RuntimeError):
            with compiler._layout_protection_scope():
                assert compiler._choice_row_position(second, 5) == (5, 2)
                raise RuntimeError('neutral scope failure')
        assert compiler._choice_row_position(first, 2) == (2, 2)
    assert compiler._LAYOUT_PROTECTION.get() is None


def _context_pages():
    pages = []
    for index in range(12):
        content = f'Complete Cedar context {index}.\n'.encode()
        path = f'knowledge/notes/context-{index:02d}.md'
        pages.append(compiler.SourceSnapshot(path, content, compiler.sha256_bytes(content)))
    return tuple(pages)


def _packing_result(inputs):
    batches = compiler.pack_compile_batches(inputs, model=None)
    layouts = tuple(compiler._draft_layout(batch.inputs) for batch in batches)
    references = tuple(_bound_choices(batch.inputs) for batch in batches)
    return batches, layouts, references


@pytest.mark.parametrize('row_count', [100, 750])
def test_full_optional_selection_matches_baseline_and_records_cost(tmp_path, monkeypatch, record_property, row_count):
    import time

    inputs = _legacy_packet(tmp_path, monkeypatch, [f'Факт {index} is settled.' for index in range(row_count)])
    parts = tuple(compiler._daily_parts(inputs.dailies[0].logical_path, inputs.dailies[0].content))
    inputs = replace(inputs, dailies=parts, sources=(*inputs.sources, *_context_pages()))
    current = compiler._choice_row_position
    monkeypatch.setattr(compiler, '_choice_row_position', _old_position)
    wall, cpu = time.monotonic(), time.process_time()
    expected = _packing_result(inputs)
    record_property('baseline_wall', time.monotonic() - wall)
    record_property('baseline_cpu', time.process_time() - cpu)
    monkeypatch.setattr(compiler, '_choice_row_position', current)
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
        compiler._raw_choice_row_positions(b'\xff\n')
    source = compiler.SourceSnapshot('knowledge/daily/2026-09-25.md', b'fact\n', 'unused')
    with pytest.raises(KeyError):
        with compiler._layout_protection_scope():
            compiler._choice_row_position(source, 2)
    assert compiler._LAYOUT_PROTECTION.get() is None
