"""A secret written as JSON, in a URL or under a long key name is still a secret.

The redactor caught `api_key=…` and passed the same key written as JSON, a database
URL password, `AWS_SECRET_ACCESS_KEY=…` and GitLab tokens. See
docs/research/2026-09-25-a-secret-in-json-is-still-a-secret.md.
"""

from __future__ import annotations

import json

import pytest
from secret_redact import redact_secrets

SECRET = "a8f3k29dkq84mzp1x"


@pytest.mark.parametrize(
    ("text", "kept"),
    [
        (f'{{"api_key": "{SECRET}"}}', '{"api_key": "[REDACTED]"}'),
        ('{"password": "Hunter2024!xyz"}', '{"password": "[REDACTED]"}'),
        ('{"client_secret": "Hunter$2024?x"}', '{"client_secret": "[REDACTED]"}'),
        (f'"Authorization": "Bearer {SECRET}"', '"Authorization": "Bearer [REDACTED]"'),
        (f"AWS_SECRET_ACCESS_KEY={SECRET}", "AWS_SECRET_ACCESS_KEY=[REDACTED]"),
        ("postgres://admin:S3cr3tPass@db.example:5432/app", "postgres://admin:[REDACTED]@db.example:5432/app"),
        ("glpat-" + "x" * 20, "[REDACTED_GITLAB_TOKEN]"),
    ],
)
def test_each_form_is_redacted(text: str, kept: str) -> None:
    assert redact_secrets(text) == kept


def test_a_redacted_json_line_is_still_json() -> None:
    line = json.dumps({"api_key": SECRET, "model": "claude-sonnet-5"})

    assert json.loads(redact_secrets(line)) == {"api_key": "[REDACTED]", "model": "claude-sonnet-5"}


@pytest.mark.parametrize(
    "text",
    [
        "lease_token: str",
        "token = next(iterator)",
        "GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}",
        '{"token": "${{ secrets.X }}"}',
        "max_tokens = 1024",
        "password_hash = owner_record.password_hash",
    ],
)
def test_code_and_references_are_left_alone(text: str) -> None:
    assert redact_secrets(text) == text
