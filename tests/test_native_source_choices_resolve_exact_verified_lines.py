"""Native citation choices must resolve an entire verified physical container."""
import json
from dataclasses import replace

import compile_memory as compiler
import pytest
from reliable_memory import validate_schema_object

from tests.test_compile_binds_the_protected_source_view import _policy
from tests.test_native_compile_projection_has_complete_citation_address import _projected_inputs


@pytest.mark.parametrize('prompt', ['First.\nThe test bicycle is blue.',
                                   'First.\r\nThe test bicycle is blue.\r\n',
                                   'First.\u2028The test bicycle is blue.'])
def test_native_choice_supplies_exact_line_and_complete_container(tmp_path, monkeypatch, prompt):
    inputs = _projected_inputs(tmp_path, monkeypatch, prompt)
    choices = compiler._source_line_choices(inputs)
    selected = next(row for row in choices if row['quoted_text'] == 'The test bicycle is blue.')
    evidence = compiler._expand_source_line_evidence(
        {'source_line': selected['source_line'], 'claim': 'The test bicycle is blue.'}, inputs)
    assert evidence['native_event']['line_index'] == 1
    binding = compiler._evidence_binding(evidence, inputs)
    assert json.loads(compiler._verified_claim_quote(binding, inputs))['payload']['prompt'] == prompt
    rendered, schema = compiler._draft_layout(inputs)
    assert 'NATIVE SOURCE ADDRESSES' in rendered
    assert 'locator=user_lines:' in rendered
    validate_schema_object({'operations': []}, schema)


def test_unoffered_native_choice_is_refused(tmp_path, monkeypatch):
    inputs = _projected_inputs(tmp_path, monkeypatch, 'The test bicycle is blue.')
    choices = compiler._source_line_choices(inputs)
    assert choices
    absent = max(row['source_line'] for row in choices) + 1
    with pytest.raises(ValueError, match='absent'):
        compiler._expand_source_line_evidence({'source_line': absent, 'claim': 'A fact.'}, inputs)


def test_long_line_choice_preserves_the_quote_and_original_guard(tmp_path, monkeypatch):
    line = 'Recorded detail: ' + 'x' * 18000
    inputs = _projected_inputs(tmp_path, monkeypatch, line + '\nTail.')
    choice = next(row for row in compiler._source_line_choices(inputs) if row['file_line'] == 0)
    evidence = compiler._expand_source_line_evidence(
        {'source_line': choice['source_line'], 'claim': 'The recorded detail exists.'}, inputs)
    assert evidence['quoted_text'] == line
    assert compiler._evidence_binding(evidence, inputs)
    shortened = dict(evidence, quoted_text='Recorded detail: ')
    with pytest.raises(ValueError, match='complete selected user line'):
        compiler._evidence_binding(shortened, inputs)


def test_blank_lines_do_not_make_a_false_contiguous_range(tmp_path, monkeypatch):
    inputs = _projected_inputs(tmp_path, monkeypatch, 'First.\n\nTail.')
    choices = compiler._source_line_choices(inputs)
    assert [row['file_line'] for row in choices] == [0, 2]
    addresses = compiler._source_address_table(choices)
    assert 'locator=user_lines:0..2' not in addresses
    assert 'locator=user_lines:2' in addresses


def test_repeated_text_has_distinct_exact_native_indices(tmp_path, monkeypatch):
    inputs = _projected_inputs(tmp_path, monkeypatch, 'Same.\nSame.')
    choices = compiler._source_line_choices(inputs)
    assert len({row['source_line'] for row in choices}) == 2
    assert [row['native_event']['line_index'] for row in choices] == [0, 1]


def test_choices_use_the_complete_protected_native_line(tmp_path, monkeypatch):
    inputs = _projected_inputs(tmp_path, monkeypatch, 'The label is private-word.')
    _policy(monkeypatch, tmp_path)
    choices = compiler._source_line_choices(inputs)
    assert 'private-word' not in str(choices)
    assert choices[0]['quoted_text'] == 'The label is [REDACTED_LITERAL].'
    evidence = compiler._expand_source_line_evidence(
        {'source_line': choices[0]['source_line'], 'claim': 'The label was redacted.'}, inputs)
    binding = compiler._evidence_binding(evidence, inputs)
    original = json.loads(compiler._verified_claim_quote(binding, inputs))['payload']['prompt']
    assert original == 'The label is private-word.'


def test_forged_native_projection_cannot_offer_choices(tmp_path, monkeypatch):
    inputs = _projected_inputs(tmp_path, monkeypatch, 'A genuine native fact.')
    source = inputs.sources[0]
    forged = replace(source, prompt_content=source.prompt_content + b'\nforged')
    poisoned = replace(inputs, sources=(forged,))
    with pytest.raises(ValueError, match='canonical selected source proof'):
        compiler._source_line_choices(poisoned)


def test_byte_planning_counts_the_native_choices_and_schema(tmp_path, monkeypatch):
    inputs = _projected_inputs(tmp_path, monkeypatch, 'First.\nTail.')
    measured = compiler._ByteBatchMeasure(inputs)({part.part_key for part in inputs.dailies})
    assert measured == len(compiler._draft_prompt_text(inputs).encode('utf-8'))
