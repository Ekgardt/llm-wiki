"""A codex that died, and any provider that ran out of time, are named as such.

Research: `docs/research/2026-09-17-every-provider-names-a-death-and-a-deadline.md`.
"""
from __future__ import annotations

import socket
import sys
import threading
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from tests.slow_machine import SHORT_TIMEOUT  # noqa: E402

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="a shell stub stands in for the CLI")


def _a_codex_that(tmp_path: Path, monkeypatch, body: str) -> None:
    binary = tmp_path / "bin" / "codex"
    binary.parent.mkdir()
    binary.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
    binary.chmod(0o755)
    monkeypatch.setenv("PATH", f"{binary.parent}:/usr/bin:/bin")


def _failure_of(provider: str) -> str | None:
    import llm_client

    candidate = llm_client.provider_candidates(provider, max_tokens=2000)[0]
    return llm_client.call_candidate(candidate, "prompt", "system", available=True).failure_class


def test_a_codex_that_exits_nonzero_is_a_dead_provider_not_an_empty_answer(tmp_path, monkeypatch, capsys):
    _a_codex_that(tmp_path, monkeypatch, "echo 'not logged in' >&2\nexit 3")

    failure = _failure_of("codex")

    assert (failure, "status 3: not logged in" in capsys.readouterr().err) == ("provider_exited", True)


def test_a_codex_still_working_at_its_deadline_is_a_timeout(tmp_path, monkeypatch):
    _a_codex_that(tmp_path, monkeypatch, "exec sleep 30")
    monkeypatch.setenv("MEMORY_LLM_TIMEOUT_S", "1")

    assert _failure_of("codex") == "provider_timeout"


@pytest.fixture
def a_server_that_never_answers():
    """Accepts the connection and says nothing, on loopback."""
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    held: list[socket.socket] = []
    thread = threading.Thread(target=lambda: held.append(listener.accept()[0]), daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{listener.getsockname()[1]}/v1"
    listener.close()
    thread.join(timeout=SHORT_TIMEOUT)
    [connection.close() for connection in held]


def test_an_http_provider_still_working_at_its_deadline_is_a_timeout(a_server_that_never_answers, monkeypatch):
    monkeypatch.setenv("MEMORY_LLM_BASE_URL", a_server_that_never_answers)
    monkeypatch.setenv("MEMORY_LLM_TIMEOUT_S", "1")

    assert _failure_of("ollama") == "provider_timeout"
