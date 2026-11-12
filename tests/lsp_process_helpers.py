"""Helpers shared by test_lsp_process.py and the test files importing them."""
from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest
from llm_wiki import lsp_process_state
from lsp_process import LspProcess


def _poll_until_empty(sample, timeout: float) -> list[str]:
    """Sample until nothing is left or the bound expires; the last sample wins."""
    deadline = time.monotonic() + timeout
    while True:
        remaining = sample()
        if not remaining:
            return remaining
        if time.monotonic() >= deadline:
            return remaining
        time.sleep(0.05)


def _lsp_threads() -> list[threading.Thread]:
    return [thread for thread in threading.enumerate() if thread.name.startswith("lsp-")]


def _lsp_thread_ids() -> set[int]:
    return {id(thread) for thread in _lsp_threads()}


def _leaked_lsp_threads(existing: set[int]) -> list[str]:
    return [thread.name for thread in _lsp_threads() if id(thread) not in existing]


@pytest.fixture(autouse=True)
def _no_lsp_lifecycle_owner_leaks(
    monkeypatch: pytest.MonkeyPatch,
):
    started: list[LspProcess] = []
    start = LspProcess.start.__func__
    configured_start = LspProcess.start_configured.__func__

    def tracked_start(
        cls: type[LspProcess],
        command: object,
        *,
        cwd: Path,
        owner_root: Path,
    ) -> LspProcess:
        process = start(cls, command, cwd=cwd, owner_root=owner_root)
        started.append(process)
        return process

    monkeypatch.setattr(LspProcess, "start", classmethod(tracked_start))

    def tracked_configured_start(
        cls: type[LspProcess],
        command: object,
        *,
        cwd: Path,
        owner_root: Path,
        deadline: float,
        server_request_handlers: object,
        server_notification_handlers: object,
        generation_bootstrap: object,
        bootstrap_timeout_seconds: float | None = None,
        generation_guard: object | None = None,
    ) -> LspProcess:
        options = {}
        if generation_guard is not None:
            options["generation_guard"] = generation_guard
        process = configured_start(
            cls,
            command,
            cwd=cwd,
            owner_root=owner_root,
            deadline=deadline,
            server_request_handlers=server_request_handlers,
            server_notification_handlers=server_notification_handlers,
            generation_bootstrap=generation_bootstrap,
            bootstrap_timeout_seconds=bootstrap_timeout_seconds,
            **options,
        )
        started.append(process)
        return process

    monkeypatch.setattr(
        LspProcess, "start_configured", classmethod(tracked_configured_start)
    )
    existing = _lsp_thread_ids()
    yield
    assert _still_owned(started) == []
    assert _leaked_lsp_threads(existing) == []


def _still_owned(started: list[LspProcess], seconds: float = 60) -> list[str]:
    """Owners that are still held after cleanup has had time to finish.

    `coordinator_has_ownership` answers True when it cannot take the
    coordinator lock inside its own bound, which a loaded runner can cause
    while cleanup is still in flight. Retrying keeps this a leak check rather
    than a race against the machine.
    """
    return _poll_until_empty(lambda: _owned_nonces(started), seconds)


def _owned_nonces(started: list[LspProcess]) -> list[str]:
    return [
        process.owner_nonce
        for process in started
        if lsp_process_state.coordinator_has_ownership(process._coordinator)
    ]
