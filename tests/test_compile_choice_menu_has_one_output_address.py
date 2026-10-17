"""Delivered source addresses distinguish output IDs from visible LF locators."""
import re

import compile_memory as compiler
import pytest
from reliable_memory import SchemaValidationError, validate_schema_object

from tests.test_compile_binds_the_protected_source_view import _legacy_packet


def _actual_confused_rows():
    return [dict(source_path='knowledge/daily/2026-09-28.md', timestamp='20:33:00',
                 source_line=194, file_line=170),
            dict(source_path='knowledge/daily/2026-09-28.md', timestamp='20:33:00',
                 source_line=196, file_line=172)]


def _evidence_schema(rows):
    schema = compiler._source_choice_schema(compiler._legacy_draft_schema(), rows)
    return schema['properties']['operations']['items']['properties']['evidence']['items']


@pytest.mark.parametrize('locator', [170, 172])
def test_actual_unoffered_locator_is_not_a_schema_choice(locator):
    with pytest.raises(SchemaValidationError):
        validate_schema_object({'source_line': locator, 'claim': 'A fact.'},
                               _evidence_schema(_actual_confused_rows()))


def test_display_has_one_explicit_output_id_and_retains_each_visible_locator():
    text = compiler._source_address_table(_actual_confused_rows())
    assert not re.search(r'^\d+ \d+$', text, re.MULTILINE)
    assert re.findall(r'source_line=(\d+) locator=LF:(\d+)', text) == [('194', '170'), ('196', '172')]


@pytest.mark.parametrize('identifier', [194, 196])
def test_offered_ids_are_valid_with_exact_two_fields(identifier):
    validate_schema_object({'source_line': identifier, 'claim': 'A fact.'},
                           _evidence_schema(_actual_confused_rows()))


def test_hybrid_response_remains_refused_by_schema_and_expander(tmp_path, monkeypatch):
    inputs = _legacy_packet(tmp_path, monkeypatch, ['An exact durable fact.'])
    choices = compiler._source_line_choices(inputs)
    item = {'source_line': choices[0]['source_line'], 'claim': 'A fact.', 'quoted_text': 'An exact durable fact.'}
    with pytest.raises(SchemaValidationError):
        validate_schema_object(item, _evidence_schema(choices))
    with pytest.raises(ValueError, match='extra or missing fields'):
        compiler._expand_source_line_evidence(item, inputs)


def test_legacy_branch_and_no_choice_schema_keep_their_contract():
    schema = compiler._legacy_draft_schema()
    assert compiler._source_choice_schema(schema, ()) is schema
    validate_schema_object({'daily_date': '2026-09-28', 'timestamp': '20:33:00',
                            'quoted_text': 'An exact durable fact.', 'claim': 'A fact.'},
                           _evidence_schema(_actual_confused_rows()))


@pytest.mark.parametrize('model', [None, 'measured'])
def test_choice_layout_is_fully_counted_with_optional_unicode_context(tmp_path, monkeypatch, model):
    from dataclasses import replace

    inputs = _legacy_packet(tmp_path, monkeypatch, ['An exact durable fact.'])
    raw = 'Context: café and 日本語.\n'.encode()
    context = compiler.SourceSnapshot('knowledge/notes/context.md', raw, compiler.sha256_bytes(raw))
    inputs = replace(inputs, sources=inputs.sources + (context,))
    paths = {part.part_key for part in inputs.dailies}
    optional = {context.logical_path}
    subset = compiler._subset_compile_inputs(inputs, paths, optional)

    def adapter(text):
        return len(text.encode('utf-8'))

    expected = adapter(compiler._draft_prompt_text(subset))
    measure = compiler._batch_measure(inputs, model, {'measured': adapter})
    assert measure(paths, optional) == expected
    assert raw.decode() in compiler._draft_prompt(subset)
    assert inputs.dailies[0].content == subset.dailies[0].content
