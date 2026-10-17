import json

import compile_memory as compiler

from tests.test_native_compile_projection_has_complete_citation_address import _projected_inputs


def test_offered_native_id_is_next_to_complete_protected_text(tmp_path, monkeypatch):
    inputs = _projected_inputs(tmp_path, monkeypatch, 'Heading.\n\nThe complete supported fact.\nSame.\nSame.')
    choices = compiler._source_line_choices(inputs)
    table = compiler._source_address_table(choices)
    for row in choices:
        expected = (f"source_line={row['source_line']} locator=user_lines:{row['file_line']} "
                    f"quoted_text={json.dumps(row['quoted_text'], ensure_ascii=False)}")
        assert expected in table
    assert [row['file_line'] for row in choices] == [0, 2, 3, 4]
    assert len({row['source_line'] for row in choices}) == 4


def test_inline_text_cannot_shorten_the_bound_native_line(tmp_path, monkeypatch):
    text = 'The complete original detail: ' + 'x' * 18000
    inputs = _projected_inputs(tmp_path, monkeypatch, text)
    choice = compiler._source_line_choices(inputs)[0]
    prompt, schema = compiler._draft_layout(inputs)
    assert f"quoted_text={json.dumps(text, ensure_ascii=False)}" in prompt
    evidence = compiler._expand_source_line_evidence(
        {'source_line': choice['source_line'], 'claim': 'The recorded detail exists.'}, inputs)
    assert evidence['quoted_text'] == text
    assert compiler._evidence_binding(evidence, inputs)
