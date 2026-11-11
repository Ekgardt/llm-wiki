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
    "curl https://example.test & date -u +%H:%M:%S",
])
def test_a_noncredential_argument_is_preserved(text):
    assert redact_secrets(text) == text


_CREDENTIAL = ":".join(("user", "privatepw"))


@pytest.mark.parametrize("command", [
    "curl -u CREDENTIAL https://example.test",
    "curl --user=CREDENTIAL https://example.test",
    'curl -u "CREDENTIAL with spaces" https://example.test',
    "curl -u 'CREDENTIAL with spaces' https://example.test",
    "date -u +%H:%M:%S; curl -u CREDENTIAL https://example.test",
    "curl -H 'X-Example: one;two' -u CREDENTIAL https://example.test",
    "bash -c 'curl -u CREDENTIAL https://example.test'",
    "/usr/bin/curl -u CREDENTIAL https://example.test",
    '"/usr/bin/curl" -u CREDENTIAL https://example.test',
    "CURL.EXE -u CREDENTIAL https://example.test",
    "curl \\\n  -u CREDENTIAL https://example.test",
    "curl `\n  -u CREDENTIAL https://example.test",
    "curl ^\n  -u CREDENTIAL https://example.test",
    "curl ^\r\n  -u CREDENTIAL https://example.test",
    "curl https://example.test/?a=1^&b=2 -u CREDENTIAL",
    'curl.exe -H "A: b" ^& -u CREDENTIAL https://example.test',
    "curl ^| -u CREDENTIAL https://example.test",
])
def test_credentials_still_redact(command):
    redacted = redact_secrets(command.replace("CREDENTIAL", _CREDENTIAL))
    assert "privatepw" not in redacted
    assert "with spaces" not in redacted
    assert redact_secrets(redacted) == redacted
