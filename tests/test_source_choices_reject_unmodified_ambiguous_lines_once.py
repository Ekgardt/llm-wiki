"""An unchanged ambiguous row cannot gain authority from a protected alias."""
import compile_pipeline
import pytest
from llm_wiki import compile_pipeline_operations, compile_pipeline_prompts

from tests.compile_binds_the_protected_source_view_helpers import _legacy_packet, _policy
from tests.compile_paths import point_compile_path


def _observe_projected_fallback(monkeypatch):
    calls = []
    original = compile_pipeline_operations.bound_protected_legacy_evidence

    def observed(date, timestamp, quote, inputs):
        calls.append((date, timestamp, quote))
        return original(date, timestamp, quote, inputs)

    point_compile_path(monkeypatch, 'bound_protected_legacy_evidence', observed)
    return calls


def test_unchanged_ambiguous_source_rows_are_rejected_without_reprotecting_the_packet(tmp_path, monkeypatch):
    inputs = _legacy_packet(tmp_path, monkeypatch, [str(number) for number in range(30)])
    calls = _observe_projected_fallback(monkeypatch)
    choices = compile_pipeline_prompts.source_line_choices(inputs)
    assert choices
    assert calls == []
    assert not any(row['quoted_text'] == '1' for row in choices)


def test_changed_source_rows_still_use_the_protected_binding_checks(tmp_path, monkeypatch):
    inputs = _legacy_packet(tmp_path, monkeypatch, ['The label is private-word.'])
    _policy(monkeypatch, tmp_path)
    calls = _observe_projected_fallback(monkeypatch)
    choices = compile_pipeline_prompts.source_line_choices(inputs)
    assert calls
    assert any(row['quoted_text'] == 'The label is [REDACTED_LITERAL].' for row in choices)


def test_colliding_protected_rows_still_grant_no_source_choice(tmp_path, monkeypatch):
    inputs = _legacy_packet(tmp_path, monkeypatch, ['The label is first-word.', 'The label is second-word.'])
    _policy(monkeypatch, tmp_path, literals=('first-word', 'second-word'))
    calls = _observe_projected_fallback(monkeypatch)
    choices = compile_pipeline_prompts.source_line_choices(inputs)
    assert calls
    assert not any(row['quoted_text'] == 'The label is [REDACTED_LITERAL].' for row in choices)


def test_original_row_proof_does_not_restrict_another_quote_in_the_same_input(tmp_path, monkeypatch):
    inputs = _legacy_packet(tmp_path, monkeypatch, ['A plain exact fact.', 'The label is private-word.'])
    _policy(monkeypatch, tmp_path)
    original = compile_pipeline_operations.evidence_binding

    def observed(item, selected):
        nested = dict(item, quoted_text='The label is [REDACTED_LITERAL].')
        binding = original(nested, selected)
        assert binding['quote_sha256'] == compile_pipeline.sha256_bytes(b'The label is private-word.')
        return original(item, selected)

    point_compile_path(monkeypatch, 'evidence_binding', observed)
    assert any(row['quoted_text'] == 'A plain exact fact.' for row in compile_pipeline_prompts.source_line_choices(inputs))
    assert compile_pipeline_operations.SOURCE_CHOICE_ORIGINAL.get() is None


def test_an_exception_releases_the_original_row_proof(tmp_path, monkeypatch):
    inputs = _legacy_packet(tmp_path, monkeypatch, ['A plain exact fact.'])

    def failed(item, selected):
        raise RuntimeError('controlled boundary failure')

    point_compile_path(monkeypatch, 'evidence_binding', failed)
    with pytest.raises(RuntimeError, match='controlled boundary failure'):
        compile_pipeline_prompts.source_line_choices(inputs)
    assert compile_pipeline_operations.SOURCE_CHOICE_ORIGINAL.get() is None
