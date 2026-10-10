"""The operation deadline includes opening its operational backend."""

import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import markdown_transaction  # noqa: E402
import mcp_server  # noqa: E402
import memory_queue  # noqa: E402


class _Actor:
    def cancel(self, target, **kwargs):
        return True

    def redrive(self, target, **kwargs):
        return "replacement"

    def recover(self, **kwargs):
        return []

    def undo(self, target, **kwargs):
        return SimpleNamespace(id="undo", state="prepared", error_code=None)

    def apply(self, target, **kwargs):
        return SimpleNamespace(id="undo", state="committed", error_code=None)


@pytest.mark.parametrize("action", ["queue-cancel", "queue-redrive", "transaction-recover", "transaction-undo"])
def test_backend_admission_receives_the_operation_deadline(monkeypatch, action):
    received = []

    def factory(vault, state_root, *, deadline=None):
        received.append(deadline)
        return _Actor()

    monkeypatch.setattr(memory_queue, "active_or_legacy_memory_queue", factory)
    monkeypatch.setattr(markdown_transaction, "active_or_legacy_coordinator", factory)
    deadline = time.monotonic() + 5
    report = mcp_server._doctor(action=action, target_id="target", repair=True, deadline=deadline)
    assert report["overall_status"] == "ok"
    assert received == [deadline]
