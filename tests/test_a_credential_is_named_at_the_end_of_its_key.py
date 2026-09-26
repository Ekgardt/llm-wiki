"""Redaction keeps code and paths, catches command-line and quoted secrets, keeps JSON valid.

Audit 2026-09-26 A-2, B-2, B-3: docs/research/2026-09-26-a-credential-is-named-at-the-end-of-its-key.md
"""
from __future__ import annotations

import json

import pytest
from secret_redact import redact_secrets

KEPT = [
    'api_key = os.environ["API_KEY"]',
    'password = config["db"]["password"]',
    'csrf_token = request.form["csrf"]',
    "tokenizer_name: intfloat/multilingual-e5-small",
    "TOKENIZER_PATH=/opt/models/e5/tokenizer.json",
    "PWD=/srv/app",
    "NEXT_PUBLIC_TOKEN_URL=https://example.test/t",
    "password_reset_url: /accounts/reset/",
    '"secretName": "prod-db-credentials"',
    "http://localhost:8080@",
    "password: required",
    "docker login --password-stdin",
]
SECRET = "S3cretPassw0rd"  # gitleaks:allow — invented input the redactor must hide
LEAKED = [
    f"curl -u admin:{SECRET} https://example.test",  # gitleaks:allow — invented input the redactor must hide
    f"mysql -u root -p{SECRET} db",
    f"docker login -u me --password {SECRET}",
    f'password="{SECRET} with spaces"',
    f"postgres://user:p@{SECRET}@host/db",
    f"client_secret=abcdefghijklmnop{SECRET}"[:30],
    "password: correcthorsebatteryS3cretPassw0rd",  # gitleaks:allow — invented input the redactor must hide
]


@pytest.mark.parametrize("line", KEPT)
def test_code_and_paths_are_left_alone(line: str) -> None:
    assert redact_secrets(line) == line


@pytest.mark.parametrize("line", LEAKED)
def test_command_line_and_quoted_secrets_are_redacted(line: str) -> None:
    assert SECRET[:8] not in redact_secrets(line)


def test_basic_authorization_and_long_letter_values_are_redacted() -> None:
    text = "Authorization: Basic dXNlcjpwYXNzd29yZDEyMw==\nclient_secret=abcdefghijklmnop"

    redacted = redact_secrets(text)

    assert ("dXNlcj" in redacted, "abcdefghijklmnop" in redacted) == (False, False)


def test_a_json_line_stays_json() -> None:
    line = json.dumps({"content": 'export PGPASSWORD=hunter22hunter" and "password": "xyz12345678"'})

    redacted = redact_secrets(line)

    assert (json.loads(redacted)["content"].count("[REDACTED]"), "hunter22" in redacted) == (2, False)
