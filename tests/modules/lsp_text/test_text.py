"""Control normalization is independent of credential and path redaction."""
import pytest
from llm_wiki.lsp_text import is_control, normalize_log_text


@pytest.mark.parametrize('value', ['', 'a  b', 'Pyright β 😀', 'e\u0301', 'x\u00a0y'])
def test_ordinary_unicode_and_spacing_are_preserved(value):
    assert normalize_log_text(value) == value


@pytest.mark.parametrize('separator', ['\r\n', '\t', '\v', '\f', '\0', '\x01', '\x7f', '\x85', '\u2028', '\u2029'])
def test_token_separators_become_one_safe_space(separator):
    assert normalize_log_text('one' + separator + 'two') == 'one two'
    assert normalize_log_text('one ' + separator + 'two') == 'one two'


@pytest.mark.parametrize('character', ['\u00ad', '\u200b', '\u200d', '\u202e', '\u2066', '\ufeff'])
def test_format_controls_cannot_hide_a_key(character):
    assert is_control(character)
    assert normalize_log_text('TO' + character + 'KEN=value') == 'TOKEN=value'


@pytest.mark.parametrize('sequence', [
    '\x1b[31m', '\x9b38;5;196m', '\x1b]title\x07', '\x1b]title\x1b\\',
    '\x9dtitle\x9c', '\x1bPpayload\x1b\\', '\x90payload\x9c',
    '\x1bXpayload\x1b\\', '\x98payload\x9c', '\x1b^payload\x1b\\',
    '\x9epayload\x9c', '\x1b_payload\x1b\\', '\x9fpayload\x9c',
    '\x1bc', '\x1b#B', '\x1b( B',
])
def test_complete_control_sequences_are_removed_as_a_unit(sequence):
    assert normalize_log_text('before' + sequence + 'after') == 'beforeafter'


@pytest.mark.parametrize('sequence', [
    '\x1b', '\x1b[31', '\x9b31', '\x1b]title', '\x9dtitle', '\x1bPpayload',
    '\x90payload', '\x1bXpayload', '\x98payload', '\x1b^payload',
    '\x9epayload', '\x1b_payload', '\x9fpayload', '\x1b(',
])
def test_incomplete_sequence_payload_is_not_emitted(sequence):
    assert normalize_log_text('before' + sequence) == 'before'


@pytest.mark.parametrize('sequence', [
    '\x1b[12;\x1b[31m', '\x9b12;\x9dtitle\x9c',
    '\x1b]title\x90payload\x1b\\', '\x1bPpayload\x1b]title\x07',
    '\x1b#\x9b31m', '\x1b \x1b[31m',
])
def test_nested_introducer_restarts_the_current_sequence(sequence):
    assert normalize_log_text('before' + sequence + 'after') == 'beforeafter'


def test_bell_only_terminates_the_osc_string_mode():
    assert normalize_log_text('a\x1bPpayload\x07still-payload\x1b\\z') == 'az'
    assert normalize_log_text('a\x1b]payload\x07z') == 'az'


def test_normalization_does_not_claim_to_redact_secrets_or_paths():
    value = 'TOKEN=visible /example/private.py'
    assert normalize_log_text(value) == value


def test_raw_unicode_validation_remains_the_callers_responsibility():
    assert not is_control('\ud800')
    assert normalize_log_text('\ud800') == '\ud800'
