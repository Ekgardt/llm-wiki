"""Authority must outlive an in-flight renewal on success and on failure."""
from __future__ import annotations

import threading
from types import SimpleNamespace

import flush_memory
import integration_adapter
import pytest

from tests.slow_machine import SHORT_TIMEOUT


def _keeper(kind, monkeypatch):
    monkeypatch.setattr(flush_memory, "CAPTURE_KEEPALIVE_SECONDS", 0.01)
    if kind == "worker":
        return flush_memory._CaptureKeepAlive(None, None, None, None, None, None)
    owner = SimpleNamespace(heartbeat_seconds=0.01, ttl_seconds=30)
    keeper = integration_adapter._CapturePublicationKeepAlive(None, None, owner, None)
    keeper._attempt_seconds = 0.01
    monkeypatch.setattr(keeper, "_require_live", lambda: None)
    return keeper


def _exit(keeper, failure, finished, errors):
    try:
        keeper.__exit__(failure, None, None)
    except Exception as error:  # noqa: BLE001 - record the closing thread's actual outcome
        errors.append(error)
    finally:
        finished.set()


@pytest.mark.parametrize("kind", ["worker", "publisher"])
@pytest.mark.parametrize("failure", [None, ValueError])
def test_authority_scope_waits_for_inflight_renewal(kind, failure, monkeypatch):
    keeper = _keeper(kind, monkeypatch)
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    errors = []

    def held_renewal():
        entered.set()
        assert release.wait(timeout=SHORT_TIMEOUT)

    monkeypatch.setattr(keeper, "_renew", held_renewal)
    keeper.__enter__()
    assert entered.wait(timeout=SHORT_TIMEOUT)
    closer = threading.Thread(target=_exit, args=(keeper, failure, finished, errors))
    closer.start()
    try:
        assert keeper._stop.wait(timeout=SHORT_TIMEOUT)
        # Five accelerated former join budgets: the renewal is deliberately
        # held by an event, not a guessed database scheduling delay.
        assert not finished.wait(timeout=0.1), "authority scope ended during renewal"
    finally:
        release.set()
        closer.join(timeout=SHORT_TIMEOUT)
        keeper._thread.join(timeout=SHORT_TIMEOUT)
    assert finished.is_set()
    assert not errors
    assert not keeper._thread.is_alive()
