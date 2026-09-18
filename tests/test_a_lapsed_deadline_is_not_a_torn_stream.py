"""What the transport calls fatal, and what it merely reports.

Finding K-B17 of the 2026-09-17 audit: the reader thread could end without the
transport becoming fatal, and a deadline that lapsed either side of a complete
write made the whole transport fatal -- one slow caller restarting the server.
Research: docs/research/2026-09-18-sess-a-lapsed-deadline-is-not-a-torn-stream.md
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path

import pyright_session as pyright_session_module
import pytest
from lsp_process import GenerationLaunch
from lsp_protocol import LspProtocol, ProtocolViolation
from pyright_session import PyrightSession
from repository_scope import resolve_repository_scope

from tests.code_kernel_helpers import create_python_repository
from tests.fake_lsp_server import FakeLspServer
from tests.slow_machine import SHORT_TIMEOUT
from tests.test_lsp_protocol import (
    _BlockingReader,
    _BlockingWriter,
    _protocol_with_streams,
)


@pytest.fixture
def fake_server() -> FakeLspServer:
    server = FakeLspServer()
    yield server
    server.close()


def _await(predicate) -> bool:
    deadline = time.monotonic() + SHORT_TIMEOUT
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return False


def test_any_reader_failure_makes_the_transport_fatal(
    fake_server, monkeypatch: pytest.MonkeyPatch
) -> None:
    reasons: list[str] = []
    sent = threading.Event()

    def dispatch(_self, _message, *, generation_nonce: str) -> None:
        raise KeyError("a dispatch bug outside the expected error types")

    def handler(peer) -> None:
        peer.send({"jsonrpc": "2.0", "method": "$/progress", "params": {}})
        sent.set()

    monkeypatch.setattr(LspProtocol, "_dispatch_message", dispatch)
    protocol = fake_server.start(handler, fatal_callback=reasons.append)
    assert sent.wait(SHORT_TIMEOUT)

    # `_become_fatal` sets the flag, completes pending work, stops I/O and only
    # then reports the reason, so waiting on the flag can find an empty list -
    # a gap Windows widens past the poll, with a CancelSynchronousIo call that
    # drops the GIL and a socketpair it emulates over loopback TCP. Waiting on
    # the reason still asserts the flag, and a reason that never comes still
    # fails.
    assert (_await(lambda: bool(reasons)), protocol.fatal, reasons) == (
        True,
        True,
        ["failed to read LSP stdout"],
    )


def test_a_write_that_finished_late_is_not_fatal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reader = _BlockingReader()
    writer = _BlockingWriter(block_after=100)
    protocol = _protocol_with_streams(reader, writer)
    real_write_frame = LspProtocol._write_frame

    def write_then_lapse(current: LspProtocol, task: object) -> None:
        real_write_frame(current, task)
        task.deadline = time.monotonic() - 1.0

    monkeypatch.setattr(LspProtocol, "_write_frame", write_then_lapse)
    try:
        protocol.notify("test/late", {}, deadline=time.monotonic() + SHORT_TIMEOUT)
        assert (_await(lambda: len(writer.frames) == 1), protocol.fatal) == (
            True,
            False,
        )
    finally:
        protocol.close(time.monotonic() + SHORT_TIMEOUT)


def test_a_write_that_never_started_is_not_fatal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reader = _BlockingReader()
    writer = _BlockingWriter(block_after=100)
    protocol = _protocol_with_streams(reader, writer)
    real_take = LspProtocol._take_task_locked

    def lapse_before_write(current: LspProtocol, task: object):
        taken = real_take(current, task)
        task.deadline = time.monotonic() - 1.0
        return taken

    monkeypatch.setattr(LspProtocol, "_take_task_locked", lapse_before_write)
    try:
        with pytest.raises(ProtocolViolation) as raised:
            protocol.notify(
                "test/never", {}, deadline=time.monotonic() + SHORT_TIMEOUT
            )
        assert (
            isinstance(raised.value.__cause__, TimeoutError),
            writer.frames,
            protocol.fatal,
        ) == (True, [], False)
    finally:
        protocol.close(time.monotonic() + SHORT_TIMEOUT)


def _gated_session(tmp_path: Path, state_root: Path) -> PyrightSession:
    from lsp_profiles import TYPESCRIPT_PROFILE
    from pyright_profile import PyrightIdentity

    repository = create_python_repository(tmp_path / "repo")
    return PyrightSession(
        resolve_repository_scope(repository),
        PyrightIdentity(
            status="missing",
            source=None,
            version=None,
            node_executable=None,
            node_version=None,
            node_major=None,
            server_executable=None,
            executable_sha256=None,
            package_sha256=None,
            initialization_options_sha256="a" * 64,
            configuration_sha256="b" * 64,
            qualified=False,
            degradation_codes=(),
        ),
        state_root=state_root,
        profile=TYPESCRIPT_PROFILE,
    )


def test_a_server_that_opens_no_token_does_not_cost_the_whole_deadline(
    tmp_path: Path, state_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        pyright_session_module, "_PROGRESS_GATE_GRACE_SECONDS", 0.05
    )
    session = _gated_session(tmp_path, state_root)
    started = time.monotonic()
    try:
        session._await_progress_gate(started + SHORT_TIMEOUT)
        assert time.monotonic() - started < SHORT_TIMEOUT / 2
    finally:
        session.close(deadline=time.monotonic() + SHORT_TIMEOUT)


def test_a_server_that_opened_a_token_still_gets_the_whole_deadline(
    tmp_path: Path, state_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        pyright_session_module, "_PROGRESS_GATE_GRACE_SECONDS", 0.01
    )
    session = _gated_session(tmp_path, state_root)
    session._retain_work_done_token("loading")
    started = time.monotonic()
    try:
        session._await_progress_gate(started + 0.2)
        assert time.monotonic() - started >= 0.2
    finally:
        session.close(deadline=time.monotonic() + SHORT_TIMEOUT)


@pytest.mark.skipif(
    os.name != "posix", reason="the verified launch copy is the POSIX mechanism"
)
def test_the_native_launch_copy_keeps_its_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Windows has no copy to name: `__enter__` verifies the source in place.

    `_LaunchServerGuard.__enter__` returns a `GenerationLaunch` only under
    `os.name == "posix"`; elsewhere it returns the guard, which is what
    `lsp_process` expects of it. Same mark as the guard's neighbours in
    `tests/test_pyright_session.py`.
    """
    import hashlib

    owner = tmp_path / "run" / "lsp" / ("a" * 32)
    owner.mkdir(parents=True)
    server = tmp_path / "server.bin"
    server.write_bytes(b"native server\n")
    guard = pyright_session_module._LaunchServerGuard(
        server,
        hashlib.sha256(server.read_bytes()).hexdigest(),
        command=(str(server),),
        owner_root=owner,
        deadline=time.monotonic() + SHORT_TIMEOUT,
        native=True,
    )
    with guard as launch:
        assert isinstance(launch, GenerationLaunch)
        copy = Path(launch.command[0])
        kept = (copy.parent == owner, copy.exists())
    assert (kept, copy.exists(), copy.read_bytes()) == (
        (True, True),
        True,
        b"native server\n",
    )
