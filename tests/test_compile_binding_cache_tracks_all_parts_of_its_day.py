"""Sizing a new day must not repeat or weaken another day's authority binding."""
from dataclasses import replace

import compile_pipeline
import compile_receipts
import pytest
from llm_wiki import compile_pipeline_prompts, compile_pipeline_sources, runtime_roots

from tests.compile_binds_the_protected_source_view_helpers import _legacy_packet
from tests.compile_measure_reuses_only_immutable_line_bindings_helpers import _observed_bindings


def _with_day(inputs, date, text):
    raw = ('## [13:51:26] event\n' + text + '\n').encode()
    part = compile_receipts.DailySnapshot('knowledge/daily/' + date + '.md', raw,
                                  compile_pipeline.sha256_bytes(raw))
    source = compile_receipts.SourceSnapshot(part.logical_path, raw, part.sha256)
    (runtime_roots.vault_root() / part.logical_path).write_bytes(raw)
    return replace(inputs, dailies=(*inputs.dailies, part), sources=(*inputs.sources, source))


def test_unrelated_day_preserves_full_layout_without_rebinding_original_day(tmp_path, monkeypatch):
    original = _legacy_packet(tmp_path, monkeypatch, ['A complete durable fact.'])
    expanded = _with_day(original, '2026-09-26', 'Another complete fact.')
    expected = compile_pipeline_prompts.draft_layout(expanded)
    calls = _observed_bindings(monkeypatch)
    with compile_pipeline_prompts.measure_choice_resolution(compile_pipeline_prompts.ByteBatchMeasure(expanded)):
        compile_pipeline_prompts.draft_layout(original)
        actual = compile_pipeline_prompts.draft_layout(expanded)
    assert actual == expected
    assert 'A complete durable fact.' in actual[0]
    assert 'Another complete fact.' in actual[0]
    assert calls.count(('2026-09-25', '13:51:26', 'A complete durable fact.')) == 1


def test_changed_same_day_rechecks_and_refuses_new_ambiguous_quote(tmp_path, monkeypatch):
    original = _legacy_packet(tmp_path, monkeypatch, ['A complete durable fact.'])
    expanded = _legacy_packet(tmp_path, monkeypatch,
                              ['A complete durable fact.', 'A complete durable fact.'])
    expected = compile_pipeline_prompts.draft_layout(expanded)
    calls = _observed_bindings(monkeypatch)
    with compile_pipeline_prompts.measure_choice_resolution(compile_pipeline_prompts.ByteBatchMeasure(expanded)):
        compile_pipeline_prompts.draft_layout(original)
        actual = compile_pipeline_prompts.draft_layout(expanded)
    assert actual == expected
    assert calls.count(('2026-09-25', '13:51:26', 'A complete durable fact.')) > 1


def test_adding_another_part_of_same_day_never_reuses_a_now_ambiguous_binding(tmp_path, monkeypatch):
    quote = 'A complete durable fact.'
    inputs = _legacy_packet(tmp_path, monkeypatch,
                            [quote, 'x' * compile_pipeline.MAX_DAILY_PART_BYTES,
                             '## [13:51:26] second event', quote])
    part = inputs.dailies[0]
    parts = tuple(compile_pipeline_sources.daily_parts(part.logical_path, part.content))
    assert len(parts) > 1
    first = replace(inputs, dailies=(parts[0],))
    expanded = replace(inputs, dailies=parts)
    offset = part.content.index(quote.encode())
    locations = [(0, (parts[0], offset), 2)]
    item = dict(daily_date='2026-09-25', timestamp='13:51:26', quoted_text=quote,
                claim='An immutable source fact.')
    with compile_pipeline_prompts.measure_choice_resolution(compile_pipeline_prompts.ByteBatchMeasure(expanded)):
        assert compile_pipeline_prompts.source_choice_binding(item, locations, first)['source_path'] == part.logical_path
        with pytest.raises(ValueError, match='ambiguous'):
            compile_pipeline_prompts.source_choice_binding(item, locations, expanded)
