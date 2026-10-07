"""Source IDs and visible FILE row locators must have explicit different roles."""

import compile_memory as compiler
import pytest

from tests.test_compile_binds_the_protected_source_view import _legacy_packet
from tests.test_compile_source_address_groups_preserve_every_row import _decoded_rows


def test_address_columns_name_output_and_non_output_roles():
    rows = [dict(source_path='knowledge/daily/2026-09-27.md',
                 timestamp='13:51:26', source_line=144, file_line=44)]
    table = compiler._source_address_table(rows)
    assert 'OUTPUT source_line | LOCATOR ONLY: visible FILE LF row (NOT an output ID)' in table
    assert _decoded_rows(table) == [(144, 44)]


def test_rendered_request_explicitly_forbids_returning_locator_column(tmp_path, monkeypatch):
    inputs = _legacy_packet(tmp_path, monkeypatch, ['A durable exact fact.'])
    prompt = compiler._draft_prompt(inputs)
    assert 'Return only the integer labelled source_line from an offered row' in prompt
    assert 'The locator=LF: label is a display-only address inside the visible FILE body, never an output ID or output field.' in prompt


def test_visible_locator_never_gains_source_authority(tmp_path, monkeypatch):
    inputs = _legacy_packet(tmp_path, monkeypatch, ['A durable exact fact.'])
    choices = compiler._source_line_choices(inputs)
    offered = {row['source_line'] for row in choices}
    wrong = next(row['file_line'] for row in choices if row['file_line'] not in offered)
    with pytest.raises(ValueError, match='absent from selected sources'):
        compiler._expand_source_line_evidence({'source_line': wrong, 'claim': 'A fact.'}, inputs)


def test_display_keeps_raw_source_context_and_exact_binding(tmp_path, monkeypatch):
    inputs = _legacy_packet(tmp_path, monkeypatch, ['A durable exact fact.'])
    raw = b'A complete separate context fact.\n'
    context = compiler.SourceSnapshot('knowledge/notes/context.md', raw, compiler.sha256_bytes(raw))
    inputs = compiler.CompileInputs(inputs.dailies, (*inputs.sources, context), ())
    choices = compiler._source_line_choices(inputs)
    row = next(item for item in choices if item['quoted_text'] == 'A durable exact fact.')
    evidence = compiler._expand_source_line_evidence({'source_line': row['source_line'], 'claim': 'A fact.'}, inputs)
    bound = compiler._evidence_binding(evidence, inputs)
    prompt = compiler._draft_prompt(inputs)
    assert inputs.dailies[0].content.decode() in prompt
    assert raw.decode() in prompt
    assert _decoded_rows(prompt) == [(item['source_line'], item['file_line']) for item in choices]
    assert bound['source_digest'] == inputs.dailies[0].sha256
    assert evidence['quoted_text'] == 'A durable exact fact.'


def test_changed_address_program_has_a_new_draft_cache_identity():
    assert compiler.DRAFT_PROGRAM.startswith('compile-draft/v15:')
