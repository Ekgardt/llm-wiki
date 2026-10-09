import re
import sys

import pytest
import secret_redact as redactor


def test_repeated_context_does_not_rescan_the_same_command_line():
    seen = []
    original = redactor._command_line.__code__

    def observed(frame, event, arg):
        if event == 'call' and frame.f_code is original:
            seen.append(frame.f_locals['line'])

    previous = sys.getprofile()
    sys.setprofile(observed)
    try:
        with redactor.line_redaction_scope({}):
            first = redactor.redact_secrets('mysql -ppasswordvalue\nfirst context\n')
            second = redactor.redact_secrets('mysql -ppasswordvalue\nsecond context\n')
    finally:
        sys.setprofile(previous)
    assert first == 'mysql -p[REDACTED]\nfirst context\n'
    assert second == 'mysql -p[REDACTED]\nsecond context\n'
    assert seen.count('mysql -ppasswordvalue\n') == 1


@pytest.mark.parametrize('text', [
    'mysql -ppasswordvalue\r\nsshpass -p "two words" ssh host\n',
    'docker login -p passwordvalue\u2028mysql -potherpassword\u2029last',
    'curl \\\n --user person:passwordvalue https://example.test\nmysql -p db\n',
    '-----BEGIN RSA PRIVATE KEY-----\nAAABBCC\n-----END RSA PRIVATE KEY-----\n',
])
def test_scoped_command_lines_preserve_the_whole_original_pipeline(text):
    expected = redactor.redact_secrets(text)
    with redactor.line_redaction_scope({}):
        assert redactor.redact_secrets(text) == expected
        assert redactor.redact_secrets(text) == expected


def test_changed_command_rule_never_reuses_an_old_result(monkeypatch):
    text = 'othercli -ppasswordvalue\n'
    with redactor.line_redaction_scope({}):
        assert redactor.redact_secrets(text) == text
        monkeypatch.setattr(redactor, '_PASSWORD_COMMAND', re.compile('(othercli)'))
        assert redactor.redact_secrets(text) == 'othercli -p[REDACTED]\n'


def test_replaced_command_callback_runs_on_every_call(monkeypatch):
    seen = []

    def replaced(line):
        seen.append(line)
        return line + str(len(seen))

    text = 'ordinary line\n'
    with redactor.line_redaction_scope({}):
        assert redactor.redact_secrets(text) == text
        monkeypatch.setattr(redactor, '_command_line', replaced)
        assert redactor.redact_secrets(text) == text + '1'
        assert redactor.redact_secrets(text) == text + '2'
    assert seen == [text, text]


@pytest.mark.parametrize(('rule', 'text'), [
    ('_ATTACHED_PASSWORD', 'mysql -ppasswordvalue\n'),
    ('_ANY_PASSWORD', 'sshpass -p passwordvalue\n'),
])
def test_changed_password_flag_pattern_runs_the_current_rule(monkeypatch, rule, text):
    with redactor.line_redaction_scope({}):
        assert '[REDACTED]' in redactor.redact_secrets(text)
        monkeypatch.setattr(redactor, rule, re.compile('(?!)()'))
        assert redactor.redact_secrets(text) == text


def test_replacing_the_original_callback_code_cannot_reuse_its_old_result(monkeypatch):
    def unchanged(line):
        return line

    text = 'mysql -ppasswordvalue\n'
    with redactor.line_redaction_scope({}):
        assert redactor.redact_secrets(text) == 'mysql -p[REDACTED]\n'
        monkeypatch.setattr(redactor._command_line, '__code__', unchanged.__code__)
        assert redactor.redact_secrets(text) == text


def test_a_changed_broken_rule_keeps_its_original_error(monkeypatch):
    text = 'mysql -ppasswordvalue\n'
    with redactor.line_redaction_scope({}):
        assert redactor.redact_secrets(text) == 'mysql -p[REDACTED]\n'
        monkeypatch.setattr(redactor, '_ATTACHED_PASSWORD', re.compile('(?!)'))
        with pytest.raises(re.error, match='invalid group reference'):
            redactor.redact_secrets(text)


def test_policy_changes_still_apply_to_reused_command_lines():
    from model_dlp import DLPPolicy, redact_for_transport

    text = 'ordinary source\nnext source'
    first = DLPPolicy((), frozenset(), frozenset())
    second = DLPPolicy((text,), frozenset(), frozenset())
    with redactor.line_redaction_scope({}):
        assert redact_for_transport(text, first) == text
        assert redact_for_transport(text, second) == '[REDACTED_LITERAL]'
