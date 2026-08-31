"""The durable failure trail must name which invocation failed.

`args.event or "unknown"` filed every `--capture-worker` and `--maintenance`
failure under `adapter_unknown`. On this vault that mislabelled 22
`intent_fence_lost` rows as publisher failures, when only the worker can raise
that string with no event attached — two different fences, one error string, and
a trail row that named neither.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import integration_adapter  # noqa: E402


def _args(**overrides) -> argparse.Namespace:
    values = {"event": None, "capture_worker": False, "maintenance": False}
    values.update(overrides)
    return argparse.Namespace(**values)


def test_a_capture_worker_failure_names_the_capture_worker() -> None:
    assert integration_adapter._failed_operation(_args(capture_worker=True)) == (
        "capture_worker"
    )


def test_a_maintenance_failure_names_maintenance() -> None:
    assert integration_adapter._failed_operation(_args(maintenance=True)) == (
        "maintenance"
    )


def test_a_lifecycle_failure_still_names_its_event() -> None:
    assert integration_adapter._failed_operation(_args(event="session_end")) == (
        "session_end"
    )


def test_an_unparsed_argv_is_not_confused_with_a_missing_event() -> None:
    """`--help` and a malformed argv fail before `args` exists; say so."""
    assert (
        integration_adapter._failed_operation(None),
        integration_adapter._failed_operation(_args()),
    ) == ("unparsed", "unknown")


def test_the_trail_records_the_worker_label(monkeypatch) -> None:
    """End to end through the recorder the adapter actually calls."""
    recorded: list[tuple[str, str]] = []
    import capture_diagnostics

    monkeypatch.setattr(
        capture_diagnostics,
        "record_capture_failure",
        lambda kind, reason: recorded.append((kind, reason)),
    )

    integration_adapter._record_cli_capture_failure(
        _args(capture_worker=True), RuntimeError("intent_fence_lost")
    )

    assert recorded == [("adapter_capture_worker", "RuntimeError: intent_fence_lost")]
