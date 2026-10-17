"""A nightly the scheduler missed is asked for by the next session start.

See `docs/research/2026-09-17-a-missed-nightly-is-caught-up-and-codex-keeps-its-stop.md`.
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import integration_adapter  # noqa: E402
import session_start_context  # noqa: E402


def test_the_session_start_maintenance_pass_asks_for_a_missed_nightly(monkeypatch):
    """Not the hook itself: the detached pass, which costs the session nothing."""
    asked: list[str] = []
    monkeypatch.setattr(integration_adapter, "_run_maintenance_command", lambda *a: None)
    monkeypatch.setattr(integration_adapter, "spawn_compile_if_idle", lambda **_kwargs: None)
    monkeypatch.setattr(
        session_start_context,
        "maybe_spawn_nightly_catchup",
        lambda: asked.append("nightly"),
    )

    exit_code = integration_adapter._run_session_start_maintenance()

    assert (exit_code, asked) == (0, ["nightly"])


def test_a_catch_up_that_raises_does_not_break_the_maintenance_pass(tmp_path, monkeypatch):
    def _explode() -> None:
        raise RuntimeError("state is locked; api_key=sample-sensitive-value\n[forged] success")

    monkeypatch.setattr(integration_adapter, "STATE_ROOT", tmp_path)
    monkeypatch.setattr(integration_adapter, "_run_maintenance_command", lambda *a: None)
    monkeypatch.setattr(integration_adapter, "spawn_compile_if_idle", lambda **_kwargs: None)
    monkeypatch.setattr(session_start_context, "maybe_spawn_nightly_catchup", _explode)

    assert integration_adapter._run_session_start_maintenance() == 0
    recorded = (tmp_path / "logs/hook-errors.log").read_text()
    assert "session-start nightly catch-up: RuntimeError: state is locked" in recorded
    assert "sample-sensitive-value" not in recorded
    assert len(recorded.splitlines()) == 1


def test_hook_errors_cannot_forge_another_log_line(tmp_path, monkeypatch):
    monkeypatch.setattr(integration_adapter, "STATE_ROOT", tmp_path)

    integration_adapter._log_hook_error("session-start compile", "first\nsecond\tthird")

    recorded = (tmp_path / "logs/hook-errors.log").read_text()
    assert len(recorded.splitlines()) == 1
    assert recorded.endswith("session-start compile: first second third\n")


def _storage_failure(*_args, **_kwargs):
    raise OSError("storage refused; api_key=sample-sensitive-value\n[forged] success")


@pytest.fixture
def maintenance_errors(tmp_path, monkeypatch):
    monkeypatch.setattr(session_start_context, "STATE_ROOT", tmp_path)
    monkeypatch.setattr(session_start_context, "update_state", _storage_failure)
    return tmp_path


def _assert_recorded_failure(root, stage):
    text = (root / "logs/hook-errors.log").read_text()
    assert stage in text
    assert "OSError: storage refused" in text
    assert "sample-sensitive-value" not in text
    assert len(text.splitlines()) == 1


def test_failed_nightly_claim_keeps_its_redacted_cause(maintenance_errors):
    assert session_start_context._claim_nightly_catchup("2026-09-30") is False
    _assert_recorded_failure(maintenance_errors, "nightly catch-up claim")


def test_failed_claim_release_keeps_its_redacted_cause(maintenance_errors):
    session_start_context._release_nightly_claim("2026-09-30")
    _assert_recorded_failure(maintenance_errors, "nightly catch-up release")


def test_failed_transaction_recovery_keeps_its_redacted_cause(maintenance_errors, monkeypatch):
    monkeypatch.setattr(session_start_context, "validate_runtime_file", lambda *_a, **_k: True)
    coordinator = SimpleNamespace(recover=_storage_failure)
    monkeypatch.setattr("markdown_transaction.active_or_legacy_coordinator", lambda *_a: coordinator)

    session_start_context._recover_transactions()

    _assert_recorded_failure(maintenance_errors, "session-start transaction recovery")


def test_refused_recovery_database_keeps_its_redacted_cause(maintenance_errors, monkeypatch):
    monkeypatch.setattr(session_start_context, "validate_runtime_file", _storage_failure)

    session_start_context._recover_transactions()

    _assert_recorded_failure(maintenance_errors, "session-start recovery database")


def test_missing_recovery_database_is_a_normal_noop(maintenance_errors, monkeypatch):
    def missing(*_args, **_kwargs):
        raise FileNotFoundError("no transactions yet")

    monkeypatch.setattr(session_start_context, "validate_runtime_file", missing)
    session_start_context._recover_transactions()
    assert not (maintenance_errors / "logs/hook-errors.log").exists()
