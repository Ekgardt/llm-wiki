"""A cheap candidate scan must preserve the original credential matcher exactly."""
import random
import re

import pytest
import secret_redact as redact


class ObservedPattern:
    def __init__(self, pattern):
        self.pattern = pattern
        self.sub_chars = 0
        self.matches = 0

    def sub(self, replacement, text):
        self.sub_chars += len(text)
        return self.pattern.sub(replacement, text)

    def match(self, text, position):
        self.matches += 1
        return self.pattern.match(text, position)


def test_prose_does_not_run_the_credential_name_matcher_at_every_word(monkeypatch):
    pattern = ObservedPattern(redact._NAMED_VALUE_PATTERNS[1])
    monkeypatch.setattr(redact, '_CREDENTIAL_VALUE_PATTERN', pattern, raising=False)
    monkeypatch.setattr(redact, '_NAMED_VALUE_PATTERNS', (redact._NAMED_VALUE_PATTERNS[0], pattern))
    text = 'Ordinary prose without assignments. ' * 1000 + '\napi_key="synthetic-credential"'
    assert '[REDACTED]' in redact._redact_named_values(text)
    assert pattern.sub_chars == 0
    assert pattern.matches == 1


def _original_output(text):
    output = text
    for pattern in redact._NAMED_VALUE_PATTERNS:
        output = pattern.sub(redact._replace_named_value, output)
    return output


@pytest.mark.parametrize('text', [
    'token="synthetic-password" password="another-value"',
    'token=abcdefghi,password=another-value',
    'token="password=another-value"; api_key=abcdefghijk',
    'Authorization: Bearer abcdefghijklmnopqrstuvwxyz',
    'PWD=/home/user OLDPWD=/tmp password_reset_url=https://example.test',
    'api_key:\nabcdefghijk',
    'api_key\u2028=abcdefghijk',
    'ключ_secret = "abcdefghijk"',
    'İtoken="abcdefghijk"',
    r'\"client_secret\":\"abcdefghijk\"',
    r'"client_secret": "abc\\def"',
    'token: str\nprivate_key = next(iterator)\nlease_token=NULL',
    'x' * 50 + 'token="abcdefghijk"',
    'x' * 51 + 'token="abcdefghijk"',
    'a.token="abcdefghijk"; -secret=abcdefghijk',
    "'api_key' : 'abcd efghi'",
    'password=abcdefghijk\r\napi_key="abcdefghi"',
])
def test_indexed_redaction_matches_original_detector_bytes(text):
    assert redact._redact_named_values(text) == _original_output(text)


def test_seeded_mixed_grammar_has_identical_original_redactions():
    rng = random.Random(20261009)
    names = ['token', 'secret', 'client_secret', 'AWS_SECRET_ACCESS_KEY', 'PWD',
             'api_key', 'private-key', 'tokenizer_name', 'ключ_secret', 'İtoken',
             'x' * 50 + 'token', 'x' * 51 + 'token']
    separators = ['=', ':', ' = ', "':", '\\":', '\t:\t', '\u2028=']
    values = ['abcdefghijk', '"abcdefghi"', "'abcd efghijk'", 'NULL',
              'next(iterator)', '"password=abcdefghi"', '\\"abcdefghi\\"',
              '${{ secrets.TOKEN }}', 'abcdefghijk,password=another-value']
    for _ in range(2000):
        fragments = [rng.choice(names) + rng.choice(separators) + rng.choice(values)
                     for _ in range(rng.randrange(1, 5))]
        text = rng.choice(['\n', '\r\n', ' ', ';']).join(fragments)
        assert redact._redact_named_values(text) == _original_output(text)


def test_unknown_added_pattern_uses_its_own_original_substitution(monkeypatch):
    extra = re.compile(r'(UNUSUAL=)([^\s]+)')
    monkeypatch.setattr(redact, '_NAMED_VALUE_PATTERNS', (*redact._NAMED_VALUE_PATTERNS, extra))
    text = 'UNUSUAL=abcdefghijk token="another-value"'
    assert redact._redact_named_values(text) == _original_output(text)
