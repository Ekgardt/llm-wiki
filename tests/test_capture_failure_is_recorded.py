"""A capture the adapter swallows must still leave a trace.

Measured on the installed vault on 2026-08-26: every `session_end` through
`integration_adapter.py` raised
`ReliabilityV3ValidationError: legacy_protocol_unquiesced`, printed
`integration_adapter: capture skipped`, and exited 0. No session record had
been written since the 2026-08-24 backfill, `run/state.json` counted no
capture failures, and `logs/capture-failures.jsonl` held nothing — the loss
was invisible to the operator, to doctor, and to the session-start report.

The hook must still never break the session, so the swallow stays. What it
may not do is stay quiet about it.
"""

from __future__ import annotations

import io
import sys
from unittest.mock import patch

import integration_adapter


class _Recorder:
    def __init__(self):
        self.calls = []

    def __call__(self, kind, reason, **fields):
        self.calls.append((kind, reason, fields))


def _run_failing_session_end(monkeypatch, error):
    def explode(_args):
        raise error

    monkeypatch.setattr(integration_adapter, "_run_cli_event", explode)
    with patch.object(sys, "stdin", io.StringIO("{}")):
        return integration_adapter.main(
            ["--source", "claude", "--event", "session_end"]
        )


def test_a_swallowed_capture_failure_is_recorded_with_its_reason(monkeypatch):
    recorder = _Recorder()
    monkeypatch.setattr(
        "capture_diagnostics.record_capture_failure", recorder, raising=True
    )

    _run_failing_session_end(monkeypatch, RuntimeError("legacy_protocol_unquiesced"))

    assert len(recorder.calls) == 1
    kind, reason, _fields = recorder.calls[0]
    assert "session_end" in kind
    assert reason.startswith("RuntimeError: legacy_protocol_unquiesced")


def test_a_failure_that_cannot_be_recorded_still_does_not_break_the_hook(monkeypatch):
    def refuse(*_args, **_fields):
        raise OSError("state is unwritable")

    monkeypatch.setattr(
        "capture_diagnostics.record_capture_failure", refuse, raising=True
    )

    assert _run_failing_session_end(monkeypatch, RuntimeError("boom")) == 0


def test_asking_for_help_is_not_recorded_as_a_lost_capture(monkeypatch):
    recorder = _Recorder()
    monkeypatch.setattr(
        "capture_diagnostics.record_capture_failure", recorder, raising=True
    )

    with patch.object(sys, "stdin", io.StringIO("{}")):
        integration_adapter.main(["--help"])

    assert recorder.calls == []
