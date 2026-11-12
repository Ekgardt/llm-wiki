"""Text already redacted stays byte for byte; a marker never hides more credential text."""

import pytest


@pytest.mark.parametrize("suffix", ["", ")", "]", "}", "`", "``", "`)", "))"])
@pytest.mark.parametrize("prefix", ["https://example.invalid/?token=", "--password="])
def test_redacted_values_keep_their_closing_markup(prefix, suffix):
    from model_dlp import DLPPolicy, require_safe_model_output
    from secret_redact import redact_secrets

    text = prefix + "[REDACTED]" + suffix
    assert redact_secrets(text) == text
    require_safe_model_output(text, DLPPolicy())


@pytest.mark.parametrize("value", ["freshCredential123", "[REDACTED]freshCredential123", "[REDACTED])freshCredential123"])
def test_a_redaction_marker_does_not_hide_more_credential_text(value):
    from model_dlp import DLPContentBlocked, DLPPolicy, require_safe_model_output
    from secret_redact import redact_secrets

    text = "https://example.invalid/?token=" + value
    assert value not in redact_secrets(text)
    with pytest.raises(DLPContentBlocked):
        require_safe_model_output(text, DLPPolicy())


def test_a_redacted_source_quote_survives_transport_and_immutable_validation():
    from llm_wiki.compile_pipeline_claims import (
        sole_quote_offset,
    )
    from model_dlp import DLPPolicy, redact_for_transport, require_safe_model_output

    line = "A previously redacted endpoint (`https://example.invalid/?token=[REDACTED]`) was logged."
    transported = redact_for_transport(line, DLPPolicy())
    assert sole_quote_offset(line.encode(), transported.encode()) == 0
    require_safe_model_output(transported, DLPPolicy())


@pytest.mark.parametrize('marker', ['[redacted]', '[Redacted]', '[REDACTED]'])
@pytest.mark.parametrize('template', [
    'https://example.invalid/?token={}', '--password={}',
    'mysql -p{}', 'sshpass -p {}', 'docker login -p {}',
    'mysql -p"{}"', 'sshpass -p \'{}\'', '{{"password":"{}"}}',
])
def test_existing_placeholder_case_does_not_trigger_publication_dlp(marker, template):
    from model_dlp import require_safe_publication
    from secret_redact import redact_secrets

    text = template.format(marker)
    assert redact_secrets(text) == text
    require_safe_publication(text.encode())


@pytest.mark.parametrize('value', ['[redacted]freshCredential123', '[redacted_freshCredential123]'])
@pytest.mark.parametrize('template', ['https://example.invalid/?token={}', 'mysql -p"{}"', '{{"password":"{}"}}'])
def test_placeholder_spelling_does_not_exempt_extra_secret_text(value, template):
    from model_dlp import DLPContentBlocked, require_safe_publication
    from secret_redact import redact_secrets

    text = template.format(value)
    assert value not in redact_secrets(text)
    with pytest.raises(DLPContentBlocked):
        require_safe_publication(text.encode())


def test_every_rule_template_is_valid_for_its_pattern():
    """A callable replacement parses its template only on a match, so a broken rule is caught here."""
    import secret_redact

    for pattern, replacement in secret_redact.PATTERNS:
        pattern.sub(replacement, "")
