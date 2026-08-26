

def test_the_prefixes_a_2026_scanner_catches_are_redacted():
    """Prefixed secrets are the cheap catch; the list had five GitHub gaps."""
    from secret_redact import redact_secrets

    samples = {
        "gho_": "gho_" + "a" * 30,
        "github_pat_": "github_pat_" + "A1b2" * 8,
        "stripe": "sk_live_" + "a" * 24,
        "restricted": "rk_test_" + "b" * 24,
        "npm": "npm_" + "c" * 36,
        "huggingface": "hf_" + "d" * 34,
        "pypi": "pypi-" + "e" * 40,
        "google_oauth": "GOCSPX-" + "f" * 24,
        "slack_app": "xapp-1-" + "G" * 20,
    }

    for name, secret in samples.items():
        redacted = redact_secrets(f"value {secret} end")
        assert secret not in redacted, f"{name} survived redaction"
        assert "[REDACTED" in redacted


def test_an_ordinary_slug_that_merely_contains_a_prefix_is_left_alone():
    """The 2026-08-22 boundary lesson holds for every prefix, not just `sk-`."""
    from secret_redact import redact_secrets

    prose = "see dead-task-retirement-and-restore-decision and hugoff_notes"

    assert redact_secrets(prose) == prose


# A value with no recognised prefix and under forty characters, so only the
# key/value rules can catch it. If one of these survives, that rule is gone.
_UNPREFIXED_SECRET = "Zx8Qw2Lp5Rt9Yv3Nb6Mk1Hj4Gd7Fs0A"

_CREDENTIAL_LINES = {
    "env assignment": f"API_KEY={_UNPREFIXED_SECRET}",
    "quoted assignment": f'token = "{_UNPREFIXED_SECRET}"',
    "single quoted": f"password = '{_UNPREFIXED_SECRET}'",
    "yaml scalar": f"client_secret: {_UNPREFIXED_SECRET}",
    "http header": f"Authorization: Bearer {_UNPREFIXED_SECRET}",
    "trailing comma": f'connect(token="{_UNPREFIXED_SECRET}", timeout=5)',
    "url query": f"https://example.test/v1?api_key={_UNPREFIXED_SECRET}",
}


def test_a_credential_named_key_still_redacts_a_real_value_in_every_shape():
    """Narrowing the key/value rules to the value must not empty them."""
    from secret_redact import redact_secrets

    for shape, line in _CREDENTIAL_LINES.items():
        redacted = redact_secrets(line)
        assert _UNPREFIXED_SECRET not in redacted, f"{shape} survived redaction"
        assert "[REDACTED]" in redacted, shape


def test_a_vendor_prefixed_secret_outranks_the_identifier_shape():
    """`ghp_…` is `[A-Za-z_]\\w*` — an identifier — and still a real token.

    The reference rule must not hand these to the prefix pass: downstream code
    asserts, hashes and stores the exact marker the key/value rule produces.
    """
    from secret_redact import redact_secrets

    line = "Bearer token: ghp_abcdefghijklmnopqrstuvwxyz012345"

    assert redact_secrets(line) == "Bearer token: [REDACTED]"


_NON_CREDENTIAL_LINES = {
    "type annotation": "    lease_token: str",
    "expression": "        token = next(iterator)",
    "reference": "    token = owner_token",
    "attribute": "    token = lease.token",
    "workflow reference": "  GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}",
    "sql placeholder": 'UPDATE q SET lease_token = ? WHERE id = ?',
    "sql statement": "SET lease_token=NULL,lease_expires_at=NULL",
    "block opener": 'class CancellationToken:\n    """Read-only handle."""',
    "screaming constant": "    token: NO_CONTRADICTIONS",
    "keyword": "    token = None",
}


def test_a_credential_named_key_without_a_credential_value_is_left_alone():
    """The key names a slot; a type, an expression or a pointer is not a secret."""
    from secret_redact import redact_secrets

    for shape, line in _NON_CREDENTIAL_LINES.items():
        assert redact_secrets(line) == line, shape


_BLOBS_INSIDE_SLASH_RUNS = {
    "npm integrity": (
        "sha512-03S/vmS5lF1S/tVbKc2WNXCMq8JWCwta/"
        "qIYjj1jvqbQhoy+N3NgBzHTSmUlbYD6DJwqQ5XHf108QujoqeURvw=="
    ),
    "secret in a url path": (
        "https://example.test/callback/"
        "8Qw2Lp5Rt9Yv3Nb6Mk1Hj4Gd7Fs0AzX8qW2lP5rT9yV/next"
    ),
}


def test_a_blob_reached_through_slashes_is_still_redacted():
    """`/` is a base64 character; a run that hides a payload is not a path."""
    from secret_redact import redact_secrets

    for shape, text in _BLOBS_INSIDE_SLASH_RUNS.items():
        assert "[REDACTED_TOKEN]" in redact_secrets(text), shape


_URLS_THAT_ARE_NOT_SECRETS = {
    "gist digest": (
        "https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f"
    ),
    "pinned tree": (
        "https://huggingface.co/model-300m/tree/"
        "57c266a740f537b4dc058e1b0cda161fd15afa75"
    ),
    "documentation words": (
        "https://developer.apple.com/library/archive/documentation/MacOSX/"
        "Conceptual/BPSystemStartup/Chapters/CreatingLaunchdJobs"
    ),
    "macos temporary directory": "/var/folders/5/zjnzxgh147qcg3bb5cg2wvqw0000gn/T/pytest",
}


def test_a_path_whose_entropy_comes_from_joining_words_is_left_alone():
    """A bare digest is exempt; the same digest inside a URL must be too."""
    from secret_redact import redact_secrets

    for shape, text in _URLS_THAT_ARE_NOT_SECRETS.items():
        assert redact_secrets(text) == text, shape
