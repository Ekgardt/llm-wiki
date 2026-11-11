"""Existing secret masking invariants.

Historical INVARIANT 3: H-014 bootstrap, L-001 truncation order.
"""
import base64
import hashlib
import hmac
import json


def _url_safe(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b'=').decode('ascii')


def _signed_test_jwt() -> str:
    header = _url_safe(json.dumps({'alg': 'HS256', 'typ': 'JWT'}).encode())
    claims = _url_safe(json.dumps({'sub': '1234567890'}).encode())
    signature = hmac.new(b'test-only-key', f'{header}.{claims}'.encode(), hashlib.sha256).digest()
    return f'{header}.{claims}.{_url_safe(signature)}'


class TestRedactionBeforePersistence:
    """All text written to durable storage must pass through redact_secrets."""

    WRITE_FUNCTIONS = [
        "compile_pipeline.py",
        "query_memory.py",
        "flush_memory.py",
        "daily_log_append.py",
        "bootstrap_project.py",
    ]

    def test_secret_redact_importable(self):
        """secret_redact must be importable with zero deps."""
        from secret_redact import redact_secrets

        assert callable(redact_secrets)

    def test_redact_catches_bearer(self):
        from secret_redact import redact_secrets

        out = redact_secrets("Authorization: Bearer sk-abcdefghijklmnopqrstuvwxyz012345")
        assert "sk-abcdefghijklmnopqrstuvwxyz012345" not in out

    def test_redact_catches_aws_key(self):
        from secret_redact import redact_secrets

        out = redact_secrets("AWS_KEY=AKIAIOSFODNN7EXAMPLE")
        assert "AKIAIOSFODNN7EXAMPLE" not in out

    def test_redact_catches_jwt(self):
        from secret_redact import redact_secrets

        jwt = _signed_test_jwt()
        out = redact_secrets(f"token: {jwt}")
        assert jwt not in out

    def test_entropy_token_redacted(self):
        from secret_redact import redact_secrets

        out = redact_secrets("entropy=short-secret entropy: another-secret")
        assert out == "entropy=[REDACTED] entropy: [REDACTED]"

    def test_redact_catches_hyphenated_provider_key(self):
        """Modern provider keys carry hyphenated prefixes (sk-ant-…, sk-proj-…)."""
        from secret_redact import redact_secrets

        key = "sk-ant-api03-QWERTYUIOPASDFGHJKLZXCVBNM1234"
        out = redact_secrets(f"call failed with {key}")
        assert key not in out
        assert "[REDACTED_API_KEY]" in out

    def test_redact_keeps_a_key_prefix_that_is_part_of_a_word(self):
        """`task-` ends in `sk-`, and that blocked every compile this vault ran.

        The page slug `dead-task-retirement-and-restore-decision` contains
        `sk-retirement-and-restore-decision`, which the provider-key pattern
        matched. The fail-closed DLP boundary then quarantined the transaction,
        so the memory pipeline could not write at all. A real key starts a
        token; a suffix inside a word does not.
        """
        from secret_redact import redact_secrets

        for text in (
            "- [[knowledge/notes/dead-task-retirement-and-restore-decision]] — a page",
            "Recorded in `dead-task-retirement-and-restore-decision.md`.",
            "see risk-assessment-and-mitigation-plan for the rest",
        ):
            assert redact_secrets(text) == text

    def test_redact_still_catches_a_key_after_punctuation(self):
        """Boundary means token start, not whitespace: `=`, quotes and `(` count."""
        from secret_redact import redact_secrets

        key = "sk-ant-api03-QWERTYUIOPASDFGHJKLZXCVBNM1234"
        for text in (f"OPENAI_API_KEY={key}", f'value "{key}"', f"({key})"):
            assert key not in redact_secrets(text)

    def test_redact_keeps_hyphenated_prose(self):
        """A long hyphenated identifier is not a key just because it is long."""
        from secret_redact import redact_secrets

        text = "branch fix/linux-installer-and-transient-cleanup is ready"
        assert redact_secrets(text) == text

    def test_redact_keeps_macos_temporary_paths(self):
        """A macOS temp path is 41 characters of `[A-Za-z0-9/]` at entropy 4.46."""
        from secret_redact import redact_secrets

        path = (
            "/private/var/folders/_5/zjnzxgh147qcg3bb5cg2wvqw0000gn/T/"
            "pytest-of-runner/pytest-0/test_case0/Project With Spaces"
        )
        assert redact_secrets(path) == path

    def test_redact_still_catches_base64_with_slashes(self):
        """A real blob keeps long dense runs between its separators."""
        from secret_redact import redact_secrets

        blob = "QUJDREVGR0hJSktMTU5PUFFSU1RVVldY/YWJjZGVmZ2hpamtsbW5vcHFyc3R1dnd4"
        out = redact_secrets(f"payload {blob}")
        assert blob not in out
        assert "[REDACTED_TOKEN]" in out

    def test_redact_does_not_redact_sha256(self):
        """Git SHA hashes (pure hex) must NOT be redacted."""
        from secret_redact import redact_secrets

        sha = "a" * 64  # 64 hex chars = SHA-256
        out = redact_secrets(f"commit {sha}")
        assert sha in out, "SHA-256 hash was incorrectly redacted"

    def test_redact_does_not_redact_normal_text(self):
        """Normal prose with alphanumeric strings over 40 chars should survive."""
        from secret_redact import redact_secrets

        text = "The quick brown fox jumps over the lazy dog and then runs away"
        out = redact_secrets(text)
        # The text should be unchanged (no high-entropy base64 matches)
        assert "quick brown fox" in out


def test_redact_secrets_strips_bearer():
    from secret_redact import redact_secrets

    out = redact_secrets("Authorization: Bearer sk-abcdefghijklmnopqrstuvwxyz012345")
    assert "sk-abcdefghijklmnopqrstuvwxyz012345" not in out
    assert "REDACTED" in out
