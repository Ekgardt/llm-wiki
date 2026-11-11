"""DLP policy must still apply across the pure redactor cache."""
import secret_redact as redactor


def test_policy_literal_can_still_cross_cached_line_boundary():
    from model_dlp import DLPPolicy, redact_for_transport
    policy = DLPPolicy(('first\nsecond',), frozenset(), frozenset())
    with redactor.line_redaction_scope({}):
        assert redact_for_transport('first\nsecond', policy) == '[REDACTED_LITERAL]'


def test_whole_text_allow_fingerprint_is_not_a_line_cache_authority():
    import hashlib

    from model_dlp import DLPPolicy, redact_for_transport
    text = 'api_key="exampleexampleexample"'
    policy = DLPPolicy((), frozenset({hashlib.sha256(text.encode()).hexdigest()}), frozenset())
    with redactor.line_redaction_scope({}):
        assert redact_for_transport(text, policy) == text
        assert redact_for_transport(text + '\nadditional context', policy).startswith('api_key="[REDACTED]"')


def test_policy_changes_still_apply_to_reused_command_lines():
    from model_dlp import DLPPolicy, redact_for_transport

    text = 'ordinary source\nnext source'
    first = DLPPolicy((), frozenset(), frozenset())
    second = DLPPolicy((text,), frozenset(), frozenset())
    with redactor.line_redaction_scope({}):
        assert redact_for_transport(text, first) == text
        assert redact_for_transport(text, second) == '[REDACTED_LITERAL]'
