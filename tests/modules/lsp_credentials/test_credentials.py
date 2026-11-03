"""Credential transformations, first qualified against their original owner."""
import pytest
from llm_wiki.lsp_credentials import (
    _redact_assignments,
    _redact_standalone_secrets,
    _redact_url_userinfo,
    redact_private_key_blocks,
)


@pytest.mark.parametrize(('value', 'expected'), [
    ('password="hidden" next=visible', 'password=<redacted> next=visible'),
    ("'token': 'hidden'", "'token': <redacted>"),
    ('password="a\\"b" suffix', 'password=<redacted> suffix'),
    ('password=hidden tail\nstatus=ok', 'password=<redacted>\nstatus=ok'),
    ('api-key=x\ntoken=y', 'api-key=<redacted>\ntoken=<redacted>'),
    ('password=', 'password=<redacted>'),
    ('count=3', 'count=3'),
])
def test_assignment_boundaries(value, expected):
    assert _redact_assignments(value) == expected


@pytest.mark.parametrize(('value', 'expected'), [
    ('Bearer abcdefghijklmnop', 'Bearer <redacted>'),
    ('Basic YWJjZGVmZ2g=', 'Basic <redacted>'),
    ('sk-' + 'a' * 20, '<redacted>'),
    ('ghp_' + 'a' * 20, '<redacted>'),
    ('xoxb-' + 'a' * 20, '<redacted>'),
    ('Bearer tiny', 'Bearer tiny'),
    ('ordinary text', 'ordinary text'),
])
def test_standalone_coverage_is_finite(value, expected):
    assert _redact_standalone_secrets(value) == expected


@pytest.mark.parametrize(('value', 'expected'), [
    ('https://user:pass@host/path', 'https://<redacted>@host/path'),
    ('https://a@b@host/', 'https://<redacted>@host/'),
    ('https://a@one/ ftp://b@two/', 'https://<redacted>@one/ ftp://<redacted>@two/'),
    ('https://host/path?email=a@b', 'https://host/path?email=a@b'),
    ('1bad://a@b', '1bad://a@b'),
    ('<https://a@host>', '<https://<redacted>@host>'),
    ('https://host/path', 'https://host/path'),
])
def test_authority_boundaries(value, expected):
    assert _redact_url_userinfo(value) == expected


@pytest.mark.parametrize(('value', 'expected'), [
    ('before\n-----BEGIN PRIVATE KEY-----\nabc\n-----END PRIVATE KEY-----\nafter',
     'before\n<redacted private key>\nafter'),
    ('before\n-----BEGIN RSA PRIVATE KEY-----\nabc', 'before\n<redacted private key>'),
    ('abc\n-----END PRIVATE KEY-----\nafter', '<redacted private key>\nafter'),
    ('-----BEGIN CERTIFICATE-----\nabc\n-----END CERTIFICATE-----',
     '-----BEGIN CERTIFICATE-----\nabc\n-----END CERTIFICATE-----'),
])
def test_pem_is_redacted_before_line_splitting(value, expected):
    assert redact_private_key_blocks(value) == expected
