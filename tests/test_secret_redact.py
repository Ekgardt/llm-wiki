

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
