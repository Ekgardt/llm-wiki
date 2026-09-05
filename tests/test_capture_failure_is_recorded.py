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


def test_a_vanished_transcript_is_not_a_failed_capture(tmp_path):
    """27 of 452 recorded losses were this, and it was the only kind still live.

    Measured 2026-09-02: sessions started outside any project keep their
    transcript under `-home-user` or `-tmp`, and by the time SessionEnd runs the
    file can already be gone. The branch turned on the path being set, so the
    reading route ran and died on `resolve(strict=True)`. Nothing is recovered
    by crashing there — if the file is gone its contents are gone — so the
    session takes the route written for having no transcript.
    """
    import integration_adapter

    missing = tmp_path / "gone.jsonl"

    assert not integration_adapter._transcript_present({"transcript_path": str(missing)})


def test_a_transcript_that_exists_is_still_read(tmp_path):
    import integration_adapter

    present = tmp_path / "there.jsonl"
    present.write_text("{}\n", encoding="utf-8")

    assert integration_adapter._transcript_present({"transcript_path": str(present)})


def test_no_path_at_all_is_still_no_transcript():
    import integration_adapter

    assert not integration_adapter._transcript_present({})
    assert not integration_adapter._transcript_present({"transcript_path": ""})
    assert not integration_adapter._transcript_present({"transcript_path": 7})


def test_a_directory_is_not_a_transcript(tmp_path):
    """`is_file` rather than `exists`: a directory would read as a transcript."""
    import integration_adapter

    assert not integration_adapter._transcript_present({"transcript_path": str(tmp_path)})
