"""A protected source view never substitutes for original physical authority."""
import compile_memory as compiler
import pytest
from evidence_resolver import EvidenceRef

from tests.test_llm_descriptors import _write_dlp_policy
from tests.test_native_compile_uses_whole_container import _native_inputs, _native_operation


def _policy(monkeypatch, tmp_path, literals=('private-word',)):
    path = tmp_path / 'dlp.json'
    _write_dlp_policy(path, literals=literals)
    monkeypatch.setenv('LLM_WIKI_DLP_POLICY', str(path))


def _legacy_packet(tmp_path, monkeypatch, lines):
    path = 'knowledge/daily/2026-09-25.md'
    raw = ('## [13:51:26] event\n' + '\n'.join(lines) + '\n').encode()
    target = tmp_path / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(raw)
    monkeypatch.setattr(compiler, 'ROOT', tmp_path)
    digest = compiler.sha256_bytes(raw)
    daily = compiler.DailySnapshot(path, raw, digest)
    source = compiler.SourceSnapshot(path, raw, digest)
    return compiler.CompileInputs((daily,), (source,), ())


def _evidence(quote):
    return dict(daily_date='2026-09-25', timestamp='13:51:26', quoted_text=quote,
                claim='The user recorded a durable source fact.')


def test_whole_protected_legacy_quote_binds_actual_original_bytes(tmp_path, monkeypatch):
    inputs = _legacy_packet(tmp_path, monkeypatch, ['The label is private-word.'])
    _policy(monkeypatch, tmp_path)
    binding = compiler._evidence_binding(_evidence('The label is [REDACTED_LITERAL].'), inputs)
    reference = EvidenceRef.parse(binding['reference'])
    physical = inputs.dailies[0].content[reference.byte_start:reference.byte_end]
    assert physical == b'The label is private-word.'
    assert binding['quote_sha256'] == compiler.sha256_bytes(physical)
    assert binding['source_digest'] == inputs.dailies[0].sha256


def test_partial_protected_quote_does_not_grant_a_source_span(tmp_path, monkeypatch):
    inputs = _legacy_packet(tmp_path, monkeypatch, ['The label is private-word.'])
    _policy(monkeypatch, tmp_path)
    with pytest.raises(ValueError):
        compiler._evidence_binding(_evidence('[REDACTED_LITERAL]'), inputs)


def test_two_original_lines_with_the_same_protected_alias_are_refused(tmp_path, monkeypatch):
    inputs = _legacy_packet(tmp_path, monkeypatch, ['The label is first-word.', 'The label is second-word.'])
    _policy(monkeypatch, tmp_path, literals=('first-word', 'second-word'))
    with pytest.raises(ValueError):
        compiler._evidence_binding(_evidence('The label is [REDACTED_LITERAL].'), inputs)


def test_context_only_quote_cannot_acquire_daily_source_authority(tmp_path, monkeypatch):
    inputs = _legacy_packet(tmp_path, monkeypatch, ['A different daily fact.'])
    raw = b'The label is private-word.'
    context = compiler.SourceSnapshot('knowledge/notes/context.md', raw, compiler.sha256_bytes(raw))
    inputs = compiler.CompileInputs(inputs.dailies, (*inputs.sources, context), ())
    _policy(monkeypatch, tmp_path)
    with pytest.raises(ValueError):
        compiler._evidence_binding(_evidence('The label is [REDACTED_LITERAL].'), inputs)


def test_redaction_crossing_physical_lines_does_not_invent_one_line(tmp_path, monkeypatch):
    inputs = _legacy_packet(tmp_path, monkeypatch, ['The label is private-', 'word.'])
    _policy(monkeypatch, tmp_path, literals=('private-\nword',))
    with pytest.raises(ValueError):
        compiler._evidence_binding(_evidence('The label is [REDACTED_LITERAL].'), inputs)


@pytest.mark.parametrize('separator', ['\n', '\u2028'])
def test_protected_native_user_line_keeps_verified_original_container(tmp_path, monkeypatch, separator):
    inputs, _ = _native_inputs(tmp_path, monkeypatch, 'First.' + separator + 'The label is private-word.')
    inputs = compiler._subset_compile_inputs(inputs, {part.part_key for part in inputs.dailies})
    operation = _native_operation(inputs, 'The label is [REDACTED_LITERAL].')
    _policy(monkeypatch, tmp_path)
    binding = compiler._evidence_binding(operation['evidence'][0], inputs)
    reference = EvidenceRef.parse(binding['reference'])
    physical = inputs.dailies[0].original_content[reference.byte_start:reference.byte_end]
    assert b'private-word' in physical
    assert binding['quote_sha256'] == compiler.sha256_bytes(physical)


def test_unrendered_native_alias_is_not_granted_by_a_cached_frame(tmp_path, monkeypatch):
    inputs, _ = _native_inputs(tmp_path, monkeypatch, 'First.\nThe label is private-word.')
    operation = _native_operation(inputs, 'The label is [REDACTED_LITERAL].')
    _policy(monkeypatch, tmp_path)
    with pytest.raises(ValueError):
        compiler._evidence_binding(operation['evidence'][0], inputs)


def test_legacy_unicode_separator_stays_in_one_physical_source_line(tmp_path, monkeypatch):
    original = 'The label is private-word.\u2028The second clause is retained.'
    inputs = _legacy_packet(tmp_path, monkeypatch, [original])
    _policy(monkeypatch, tmp_path)
    quote = 'The label is [REDACTED_LITERAL].\u2028The second clause is retained.'
    binding = compiler._evidence_binding(_evidence(quote), inputs)
    reference = EvidenceRef.parse(binding['reference'])
    assert inputs.dailies[0].content[reference.byte_start:reference.byte_end] == original.encode()
