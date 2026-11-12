import secret_redact as redactor


def test_repeated_source_lines_do_not_repeat_original_pattern_passes(monkeypatch):
    import sys
    seen = []
    original = redactor.redact_patterns.__code__

    def observed(frame, event, arg):
        if event == "call" and frame.f_code is original:
            seen.append(frame.f_locals["text"])

    scope = getattr(redactor, 'line_redaction_scope', None)
    from contextlib import nullcontext
    context = nullcontext() if scope is None else scope({})
    previous = sys.getprofile()
    sys.setprofile(observed)
    try:
        with context:
            first = redactor.redact_secrets('api_key=exampleexampleexample\ncommon unchanged source\nfirst context')
            second = redactor.redact_secrets('api_key=exampleexampleexample\ncommon unchanged source\nsecond context')
    finally:
        sys.setprofile(previous)
    assert first == 'api_key=[REDACTED]\ncommon unchanged source\nfirst context'
    assert second == 'api_key=[REDACTED]\ncommon unchanged source\nsecond context'
    assert seen.count('common unchanged source') == 1
    assert len(seen) == 4


def test_multiline_pem_uses_original_whole_text_pass(monkeypatch):
    seen = []
    original = redactor.redact_patterns

    def observed(text):
        seen.append(text)
        return original(text)

    monkeypatch.setattr(redactor, 'redact_patterns', observed)
    text = 'before\n-----BEGIN RSA PRIVATE KEY-----\nAAABBCC\n-----END RSA PRIVATE KEY-----\nafter'
    expected = redactor.redact_secrets(text)
    seen.clear()
    with redactor.line_redaction_scope({}):
        assert redactor.redact_secrets(text) == expected
    assert seen == [text]


def test_changed_threshold_invalidates_retained_pure_lines(monkeypatch):
    text = 'api_key="exampleexampleexample"'
    cache = {}
    with redactor.line_redaction_scope(cache):
        assert redactor.redact_secrets(text) == 'api_key="[REDACTED]"'
        monkeypatch.setattr(redactor, 'MIN_CREDENTIAL_VALUE_CHARS', 100)
        assert redactor.redact_secrets(text) == text


def test_unknown_token_pattern_uses_full_original_rule(monkeypatch):
    import re
    rule = (re.compile('first\nsecond'), '[CUSTOM]')
    cache = {}
    with redactor.line_redaction_scope(cache):
        assert redactor.redact_secrets('first\nsecond') == 'first\nsecond'
        monkeypatch.setattr(redactor, 'PATTERNS', [*redactor.PATTERNS, rule])
        assert redactor.redact_secrets('first\nsecond') == '[CUSTOM]'


def test_scope_restores_parent_after_same_exception():
    import pytest
    parent = {}
    child = {}
    error = ValueError('same error')
    with redactor.line_redaction_scope(parent):
        with pytest.raises(ValueError) as caught, redactor.line_redaction_scope(child):
            assert redactor.LINE_REDACTION_CACHE.get() is child
            raise error
        assert caught.value is error
        assert redactor.LINE_REDACTION_CACHE.get() is parent
    assert redactor.LINE_REDACTION_CACHE.get() is None


def test_multiline_global_stages_match_uncached_original():
    token = 'AbCdEfGhIjKlMnOpQrStUvWxYz0123456789ABcDefGhIjKl'
    texts = [
        'curl \\\n --user person:test https://example.test\nnext command',
        '-----BEGIN RSA PRIVATE KEY-----\nno closing marker\nremaining text',
        token + '\nembeddedprefix' + token + 'suffix',
        'api_key="exampleexampleexample"\r\nAuthorization: Bearer exampleexampleexample\r\n',
        'token:\u2028exampleexampleexample\nsecret:\u2029exampleexampleexample',
    ]
    expected = [redactor.redact_secrets(text) for text in texts]
    with redactor.line_redaction_scope({}):
        assert [redactor.redact_secrets(text) for text in texts] == expected


def test_current_registry_cannot_approve_an_unreviewed_multiline_rule(monkeypatch):
    import re
    changed = [*redactor.PATTERNS, (re.compile('first\nsecond'), '[CUSTOM]')]
    monkeypatch.setattr(redactor, 'PATTERNS', changed)
    monkeypatch.setattr(redactor, 'LF_TOKEN_RULES', tuple(changed))
    with redactor.line_redaction_scope({}):
        assert redactor.redact_secrets('first\nsecond') == '[CUSTOM]'


def test_replaced_callback_keeps_full_text_context(monkeypatch):
    def context_sensitive(match):
        return '[WHOLE]' if '\n' in match.string else '[LINE]'

    monkeypatch.setattr(redactor, 'replace_named_value', context_sensitive)
    with redactor.line_redaction_scope({}):
        assert redactor.redact_secrets('api_key="exampleexampleexample"\ncontext') == '[WHOLE]\ncontext'
