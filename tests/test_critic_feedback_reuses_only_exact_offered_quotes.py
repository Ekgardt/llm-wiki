"""Correction can reuse a complete offered quote, but never alter the reviewed data."""
import json

import compile_memory as compiler
import pytest

from tests.test_compile_critic_drop_cannot_complete_source_work import _proposed
from tests.test_native_compile_projection_has_complete_citation_address import _projected_inputs


def _reviewed_native(tmp_path, monkeypatch):
    quote = 'The complete recorded Cedar detail: ' + 'x' * 18000
    inputs = _projected_inputs(tmp_path, monkeypatch, quote)
    choices = compiler._source_line_choices(inputs)
    row = choices[0]
    operation = _proposed()
    operation['evidence'] = [compiler._expand_choice_fields(
        {'source_line': row['source_line'], 'claim': 'The complete recorded detail exists.'}, choices)]
    passed = {**operation, 'slug': 'independently-reviewed-detail', 'summary': 'A distinct scoped observation.'}
    reviews = [dict(slug=operation['slug'], verdict='drop', reason='Correct the scope without losing its evidence.'),
               dict(slug=passed['slug'], verdict='pass', reason='Keep this independently supported proposal.')]
    feedback = compiler._validated_critique_feedback([operation, passed], reviews)
    return inputs, choices, feedback


def _rendered_records(prompt):
    suffix = prompt.split('CORRECTION RECORDS\n', 1)[1]
    return [json.loads(line) for line in suffix.splitlines()]


def test_repeated_feedback_quotes_use_current_offered_ids_without_losing_any_reviewed_record(tmp_path, monkeypatch):
    inputs, choices, feedback = _reviewed_native(tmp_path, monkeypatch)
    initial, _schema = compiler._draft_layout(inputs)

    prompt, _schema = compiler._draft_layout(inputs, critic_feedback=feedback)

    records = _rendered_records(prompt)
    original = [json.loads(record) for record in feedback]
    restored = [{**record, 'operations': [compiler._expand_operation_choices(operation, choices)
                 for operation in record['operations']]} for record in records]
    assert restored == original
    assert len(prompt.encode()) < len((initial + '\n'.join(feedback)).encode())
    assert choices[0]['quoted_text'] in prompt
    assert all(operation['evidence'][0]['source_line'] == choices[0]['source_line']
               for operation in records[0]['operations'])
    assert records[0]['reviews'] == original[0]['reviews']


@pytest.mark.parametrize('change', ['quote', 'native-selector', 'timestamp'])
def test_a_nonmatching_reviewed_citation_remains_verbatim(tmp_path, monkeypatch, change):
    inputs, _choices, feedback = _reviewed_native(tmp_path, monkeypatch)
    record = json.loads(feedback[0])
    evidence = record['operations'][0]['evidence'][0]
    changes = {'quote': {'quoted_text': evidence['quoted_text'] + 'different'},
               'native-selector': {'native_event': {**evidence['native_event'], 'line_index': 999}},
               'timestamp': {'timestamp': '00:00:01'}}
    evidence.update(changes[change])
    encoded = compiler.canonical_json_bytes(record).decode()

    prompt, _schema = compiler._draft_layout(inputs, critic_feedback=(encoded,))

    assert _rendered_records(prompt)[0]['operations'][0]['evidence'][0] == evidence
    assert record['operations'][0]['body_markdown'] in prompt


def test_actual_repair_dispatch_does_not_repeat_an_already_offered_complete_quote(tmp_path, monkeypatch):
    from compile_cache import CompileCache

    from tests.test_the_compile_decides_what_the_snapshot_already_knows import _provider

    inputs, choices, feedback = _reviewed_native(tmp_path, monkeypatch)
    initial, _schema = compiler._draft_layout(inputs)
    quote = choices[0]['quoted_text']
    attempt = compiler._CompileAttempt(inputs, CompileCache(tmp_path / 'runtime'), None, None)
    attempt.critic_feedback = feedback

    def examine(prompt, *_args):
        assert prompt.count(quote) == initial.count(quote)
        return False

    monkeypatch.setattr(attempt, '_fits', examine)
    provider = _provider()

    assert attempt._drafted(provider, attempt._actions(provider)) is None


def test_roundtrip_must_preserve_json_scalar_types_not_only_python_equality(tmp_path, monkeypatch):
    inputs, _choices, feedback = _reviewed_native(tmp_path, monkeypatch)
    record = json.loads(feedback[0])
    record['operations'][0]['claims'] = [{'evidence_index': 0}]
    encoded = compiler.canonical_json_bytes(record).decode()
    original_expand = compiler._expand_operation_choices
    changed_slug = record['operations'][0]['slug']

    def changed_scalar(operation, choices):
        restored = original_expand(operation, choices)
        if restored['slug'] == changed_slug:
            restored['claims'] = [{'evidence_index': False}]
        return restored

    monkeypatch.setattr(compiler, '_expand_operation_choices', changed_scalar)

    prompt, _schema = compiler._draft_layout(inputs, critic_feedback=(encoded,))

    assert compiler.canonical_json_bytes(_rendered_records(prompt)[0]) == compiler.canonical_json_bytes(record)


def test_compiler_owned_claim_quotes_are_reconstructed_exactly_instead_of_copied_twice(tmp_path, monkeypatch):
    from tests.test_compile_claims_producer import _candidate

    inputs, choices, feedback = _reviewed_native(tmp_path, monkeypatch)
    record = json.loads(feedback[0])
    record['operations'][0]['claims'] = [_candidate(subject='Cedar', relation='has-state',
        value={'type': 'string', 'value': 'recorded'})]
    compiler._with_derived_claims(record['operations'], inputs)
    feedback = compiler._validated_critique_feedback(record['operations'], record['reviews'], inputs=inputs)
    original_claim = record['operations'][0]['claims'][0]
    assert original_claim['text'] == original_claim['evidence']['text']
    assert len(original_claim['text']) > len(choices[0]['quoted_text'])

    prompt, _schema = compiler._draft_layout(inputs, critic_feedback=feedback)

    displayed = _rendered_records(prompt)
    projected_claim = displayed[0]['operations'][0]['claims'][0]
    assert 'text' not in projected_claim and 'evidence' not in projected_claim
    restored = [compiler._expand_operation_choices(operation, choices) for operation in displayed[0]['operations']]
    compiler._with_derived_claims(restored, inputs)
    assert compiler.canonical_json_bytes(restored) == compiler.canonical_json_bytes(record['operations'])
    assert displayed[0]['reviews'] == record['reviews']
    assert record['operations'][0]['claims'][0] == original_claim
