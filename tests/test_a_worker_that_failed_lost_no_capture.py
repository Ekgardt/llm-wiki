"""A capture worker's failure is retried work, and its record names the real cause.

Doctor reported "1 capture lost" for a worker run that lost nothing, with a reason
that hid its cause. Research:
`docs/research/2026-09-14-a-worker-that-failed-lost-no-capture.md`.
"""
from __future__ import annotations

import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import capture_diagnostics  # noqa: E402
from secret_redact import describe_error_chain  # noqa: E402


def _wrapped() -> RuntimeError:
    try:
        try:
            raise FileNotFoundError("run/queue-v3.sqlite3-journal vanished")
        except FileNotFoundError as cause:
            raise RuntimeError("reliability_v3_record_invalid") from cause
    except RuntimeError as error:
        return error


def test_a_worker_failure_is_deferred_whatever_it_raised():
    outcomes = (
        capture_diagnostics._outcome_of(_wrapped(), "adapter_capture_worker"),
        capture_diagnostics._outcome_of(_wrapped(), "adapter_session_end"),
    )

    assert outcomes == ("deferred", "lost")


def test_the_record_names_the_cause_the_wrapper_hid():
    assert describe_error_chain(_wrapped()) == (
        "RuntimeError: reliability_v3_record_invalid <- FileNotFoundError: run/queue-v3.sqlite3-journal vanished"
    )
