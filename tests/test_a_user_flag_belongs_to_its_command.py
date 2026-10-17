"""A date format is not HTTP credentials; real curl credentials remain hidden."""

import pytest
from secret_redact import redact_secrets


@pytest.mark.parametrize("text", [
    "date -u +%H:%M:%S",
    "date --utc +%H:%M:%S",
    "sort -u name:value",
    "curl https://example.test; date -u +%H:%M:%S",
    "curl https://example.test && date -u +%H:%M:%S",
    "curl https://example.test\ndate -u +%H:%M:%S",
])
def test_a_noncredential_argument_is_preserved(text):
    assert redact_secrets(text) == text


@pytest.mark.parametrize("command", [
    "curl -u user:privatepw https://example.test",
    "curl --user=user:privatepw https://example.test",
    'curl -u "user:privatepw with spaces" https://example.test',
    "curl -u 'user:privatepw with spaces' https://example.test",
    "date -u +%H:%M:%S; curl -u user:privatepw https://example.test",
    "curl -H 'X-Example: one;two' -u user:privatepw https://example.test",
    "bash -c 'curl -u user:privatepw https://example.test'",
    "/usr/bin/curl -u user:privatepw https://example.test",
    '"/usr/bin/curl" -u user:privatepw https://example.test',
    "CURL.EXE -u user:privatepw https://example.test",
    "curl \\\n  -u user:privatepw https://example.test",
    "curl `\n  -u user:privatepw https://example.test",
])
def test_credentials_still_redact(command):
    redacted = redact_secrets(command)
    assert "privatepw" not in redacted
    assert "with spaces" not in redacted
    assert redact_secrets(redacted) == redacted
