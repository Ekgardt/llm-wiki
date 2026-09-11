"""One probe answers `alive | dead | unknown`; doubt never reads as dead."""

from __future__ import annotations

import errno
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import process_liveness  # noqa: E402


def test_our_own_process_is_alive() -> None:
    assert process_liveness.process_state(os.getpid()) == "alive"
    assert process_liveness.pid_alive(os.getpid()) is True


@pytest.mark.parametrize("pid", [0, -1, True, "12", None, 2**40])
def test_a_pid_that_is_not_a_positive_integer_is_unknown_and_never_dead(pid) -> None:
    assert process_liveness.process_state(pid) == "unknown"
    assert process_liveness.pid_alive(pid) is True


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX kill(2) semantics")
def test_esrch_is_dead_and_eperm_is_alive_in_doubt(monkeypatch) -> None:
    def refuse(_pid, _signal):
        raise PermissionError(errno.EPERM, "operation not permitted")

    monkeypatch.setattr(process_liveness.os, "kill", refuse)
    assert process_liveness.process_state(4242) == "unknown"
    assert process_liveness.pid_alive(4242) is True

    def gone(_pid, _signal):
        raise ProcessLookupError(errno.ESRCH, "no such process")

    monkeypatch.setattr(process_liveness.os, "kill", gone)
    assert process_liveness.process_state(4242) == "dead"
    assert process_liveness.pid_alive(4242) is False


def test_the_three_legacy_locks_ask_the_same_probe(monkeypatch) -> None:
    """Audit OPS-08: memory_state, markdown_transaction and doctor delegate."""
    import doctor
    import markdown_transaction
    import memory_state

    monkeypatch.setattr(process_liveness, "process_state", lambda _pid: "unknown")

    assert memory_state._is_pid_alive(4242) is True
    assert markdown_transaction._pid_alive(4242) is True
    assert doctor._pid_alive(4242) is True
    assert doctor._lsp_pid_state(4242) == "unknown"
