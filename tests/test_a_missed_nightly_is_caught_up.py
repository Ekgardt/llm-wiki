"""A nightly the scheduler missed is asked for by the next session start.

See `docs/research/2026-09-17-a-missed-nightly-is-caught-up-and-codex-keeps-its-stop.md`.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import integration_adapter  # noqa: E402
import session_start_context  # noqa: E402


def test_the_session_start_maintenance_pass_asks_for_a_missed_nightly(monkeypatch):
    """Not the hook itself: the detached pass, which costs the session nothing."""
    asked: list[str] = []
    monkeypatch.setattr(integration_adapter, "_run_maintenance_command", lambda *a: None)
    monkeypatch.setattr(integration_adapter, "spawn_compile_if_idle", lambda: None)
    monkeypatch.setattr(
        session_start_context,
        "maybe_spawn_nightly_catchup",
        lambda: asked.append("nightly"),
    )

    exit_code = integration_adapter._run_session_start_maintenance()

    assert (exit_code, asked) == (0, ["nightly"])


def test_a_catch_up_that_raises_does_not_break_the_maintenance_pass(monkeypatch):
    def _explode() -> None:
        raise RuntimeError("state is locked")

    monkeypatch.setattr(integration_adapter, "_run_maintenance_command", lambda *a: None)
    monkeypatch.setattr(integration_adapter, "spawn_compile_if_idle", lambda: None)
    monkeypatch.setattr(session_start_context, "maybe_spawn_nightly_catchup", _explode)

    assert integration_adapter._run_session_start_maintenance() == 0
