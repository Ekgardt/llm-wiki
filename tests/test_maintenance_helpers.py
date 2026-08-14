from __future__ import annotations

import subprocess

import maintenance_helpers


def test_run_step_redacts_stdout_and_stderr_before_logging(monkeypatch):
    token = "sk-abcdefghijklmnopqrstuvwxyz012345"
    pem = (
        "-----BEGIN PRIVATE KEY-----\n"
        "c2VjcmV0LWtleS1tYXRlcmlhbA==\n"
        "-----END PRIVATE KEY-----"
    )
    monkeypatch.setattr(
        maintenance_helpers.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0],
            1,
            stdout=f"captured {token}\n",
            stderr=pem,
        ),
    )
    logs = []

    assert maintenance_helpers.run_step(["tool"], logs.append, "scan") == 1

    rendered = "\n".join(logs)
    assert token not in rendered
    assert "c2VjcmV0LWtleS1tYXRlcmlhbA" not in rendered
    assert "[REDACTED" in rendered


def test_run_step_redacts_os_error_before_logging(monkeypatch):
    token = "ghp_abcdefghijklmnopqrstuvwxyz012345"

    def fail(*args, **kwargs):
        raise OSError(f"failed with token={token}")

    monkeypatch.setattr(maintenance_helpers.subprocess, "run", fail)
    logs = []

    assert maintenance_helpers.run_step(["tool"], logs.append, "scan") == 2

    rendered = "\n".join(logs)
    assert token not in rendered
    assert "[REDACTED" in rendered


def test_run_step_timeout_message_contains_no_command_data(monkeypatch):
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], timeout=1)

    monkeypatch.setattr(maintenance_helpers.subprocess, "run", timeout)
    logs = []

    assert maintenance_helpers.run_step(["tool", "secret-argument"], logs.append, "scan") == 2
    assert "secret-argument" not in "\n".join(logs)
