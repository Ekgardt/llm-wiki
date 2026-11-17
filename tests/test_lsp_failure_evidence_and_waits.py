from __future__ import annotations

import os
import subprocess
import threading
import time
from collections import deque
from collections.abc import Iterator
from pathlib import Path

import pytest
from llm_wiki import lsp_process_failure, lsp_process_state
from llm_wiki.lsp_process_limits import STDERR_TAIL_BYTES
from llm_wiki.lsp_process_owner import OwnerDirectory
from llm_wiki.lsp_process_state import (
    CleanupResult,
    FailureEvidenceIdentity,
    FailureIntent,
    Generation,
    LifecycleCoordinator,
    LspProcessHandle,
    StderrWake,
)

from tests.slow_machine import SHORT_TIMEOUT

OWNER_NONCE = "a" * 32
GENERATION_NONCE = "b" * 32
OTHER_NONCE = "c" * 32
VALID_TIMESTAMP = "2026-11-17T08:30:00.123456Z"


def _expired() -> float:
    return time.monotonic() - 1.0


def _later() -> float:
    return time.monotonic() + SHORT_TIMEOUT


def _owned_coordinator(tmp_path: Path) -> LifecycleCoordinator:
    owner = OwnerDirectory(tmp_path / OWNER_NONCE, parent_handle=0, parent_identity=None)
    return LifecycleCoordinator(owner)


def _identity(code: str) -> FailureEvidenceIdentity:
    return FailureEvidenceIdentity(code, OWNER_NONCE, GENERATION_NONCE, 41)


def _intent(owner_fatal: bool) -> FailureIntent:
    return FailureIntent(GENERATION_NONCE, "exited", owner_fatal, time.monotonic())


def _valid_failure_record() -> dict[str, object]:
    return {
        "code": "process_exited",
        "generation_nonce": GENERATION_NONCE,
        "owner_nonce": OWNER_NONCE,
        "timestamp": VALID_TIMESTAMP,
        "server_pid": 41,
        "stderr_tail": "server stopped",
    }


def _validate(record: dict[str, object]) -> None:
    lsp_process_state.validate_failure_record(
        record,
        code="process_exited",
        owner_nonce=OWNER_NONCE,
        generation_nonce=GENERATION_NONCE,
        pid=41,
        stderr_tail="server stopped",
    )


class _ProcessThatNeverExits:
    def __init__(self, wait_error: BaseException | None = None) -> None:
        self.wait_error = wait_error

    def poll(self) -> int | None:
        return None

    def wait(self, timeout: float) -> int:
        if self.wait_error is not None:
            raise self.wait_error
        return 0


class _ProcessView(LspProcessHandle):
    def __init__(self, coordinator: LifecycleCoordinator) -> None:
        self._coordinator = coordinator
        self.generation_nonce = GENERATION_NONCE


@pytest.fixture
def stuck_thread() -> Iterator[threading.Thread]:
    release = threading.Event()
    thread = threading.Thread(target=release.wait, daemon=True)
    thread.start()
    yield thread
    release.set()
    thread.join()


@pytest.fixture
def held_coordinator(tmp_path: Path) -> Iterator[LifecycleCoordinator]:
    coordinator = _owned_coordinator(tmp_path)
    coordinator.lock.acquire()
    yield coordinator
    coordinator.lock.release()


def test_a_failure_without_any_generation_nonce_is_not_remembered(tmp_path: Path) -> None:
    coordinator = _owned_coordinator(tmp_path)

    lsp_process_failure.remember_mandatory_terminal_failure(None, coordinator, "process_exited")

    assert coordinator.mandatory_failure_intent is None


def test_a_long_stderr_tail_keeps_its_newest_words_within_the_record_bound() -> None:
    generation = Generation(GENERATION_NONCE, None, None)
    generation.stderr.extend(f"line {index} of server output\n".encode() for index in range(400))

    tail = lsp_process_failure.failure_stderr_tail(generation)

    assert tail is not None
    assert tail.endswith("line 399 of server output")
    assert len(tail) < STDERR_TAIL_BYTES


def test_a_process_still_running_after_the_deadline_is_a_timeout() -> None:
    with pytest.raises(TimeoutError, match="did not exit before deadline"):
        lsp_process_failure.await_process_exit(_ProcessThatNeverExits(), _expired())


def test_a_wait_that_runs_out_of_time_is_reported_as_a_timeout() -> None:
    expired_wait = subprocess.TimeoutExpired(["server"], 5.0)

    with pytest.raises(TimeoutError, match="did not exit") as raised:
        lsp_process_failure.await_process_exit(_ProcessThatNeverExits(expired_wait), _later())

    assert raised.value.__cause__ is expired_wait


def test_no_stream_owner_and_the_calling_thread_need_no_wait() -> None:
    lsp_process_failure.await_stream_owner(None, _expired())
    lsp_process_failure.await_stream_owner(threading.current_thread(), _expired())


def test_a_stream_owner_still_running_after_the_deadline_is_a_timeout(
    stuck_thread: threading.Thread,
) -> None:
    with pytest.raises(TimeoutError, match="streams did not drain"):
        lsp_process_failure.await_stream_owner(stuck_thread, _expired())


def test_a_thread_cannot_join_itself() -> None:
    assert not lsp_process_failure.join_owned_thread(threading.current_thread(), _later())


def test_failure_evidence_needs_an_owner_directory() -> None:
    coordinator = LifecycleCoordinator(None)

    with pytest.raises(RuntimeError, match="owner is unavailable"):
        lsp_process_failure.ensure_failure_evidence(None, coordinator, "process_exited", _later())


def test_failure_evidence_needs_the_terminal_code_it_names(tmp_path: Path) -> None:
    coordinator = _owned_coordinator(tmp_path)

    with pytest.raises(RuntimeError, match="does not match terminal identity"):
        lsp_process_failure.ensure_failure_evidence(None, coordinator, "process_exited", _later())


def test_failure_evidence_needs_a_generation_to_name(tmp_path: Path) -> None:
    coordinator = _owned_coordinator(tmp_path)
    coordinator.terminal_code = "process_exited"

    with pytest.raises(RuntimeError, match="generation identity is unavailable"):
        lsp_process_failure.ensure_failure_evidence(None, coordinator, "process_exited", _later())


def test_written_evidence_still_settles_the_identity_it_was_written_under(
    tmp_path: Path,
) -> None:
    coordinator = _owned_coordinator(tmp_path)
    coordinator.terminal_code = "process_exited"
    coordinator.startup_generation_nonce = GENERATION_NONCE
    coordinator.cleanup_result.succeeded("evidence")

    lsp_process_failure.ensure_failure_evidence(None, coordinator, "process_exited", _later())

    assert coordinator.failure_evidence_identity == FailureEvidenceIdentity(
        "process_exited", OWNER_NONCE, GENERATION_NONCE, None
    )


def test_a_settled_identity_for_another_code_refuses_the_evidence(tmp_path: Path) -> None:
    coordinator = _owned_coordinator(tmp_path)
    coordinator.terminal_code = "process_exited"
    coordinator.failure_evidence_identity = _identity("heartbeat_failed")

    with pytest.raises(RuntimeError, match="not terminal-code exact"):
        lsp_process_failure.ensure_failure_evidence(None, coordinator, "process_exited", _later())


def test_evidence_is_not_committed_when_the_terminal_identity_moved(tmp_path: Path) -> None:
    coordinator = _owned_coordinator(tmp_path)
    coordinator.terminal_code = "process_exited"
    coordinator.failure_evidence_identity = _identity("process_exited")

    with pytest.raises(RuntimeError, match="changed during evidence write"):
        lsp_process_failure._commit_failure_evidence(
            None, coordinator, _identity("heartbeat_failed"), _later()
        )

    assert coordinator.cleanup_result.evidence == "pending"


def test_a_heartbeat_thread_that_does_not_stop_in_time_is_a_timeout(
    tmp_path: Path, stuck_thread: threading.Thread
) -> None:
    coordinator = _owned_coordinator(tmp_path)
    coordinator.heartbeat_thread = stuck_thread

    with pytest.raises(TimeoutError, match="heartbeat thread did not stop"):
        lsp_process_failure.stop_heartbeat_owner(coordinator, _expired(), allow_expired=True)

    assert coordinator.heartbeat_stop.is_set()
    assert coordinator.heartbeat_thread is stuck_thread


def test_a_recovery_thread_that_does_not_stop_in_time_is_a_timeout(
    tmp_path: Path, stuck_thread: threading.Thread
) -> None:
    coordinator = _owned_coordinator(tmp_path)
    coordinator.recovery_thread = stuck_thread

    with pytest.raises(TimeoutError, match="recovery thread did not stop"):
        lsp_process_failure.stop_recovery_owner(coordinator, _expired(), allow_expired=True)

    assert coordinator.recovery_stop.is_set()
    assert coordinator.recovery_thread is stuck_thread


def test_failure_intents_go_back_to_the_queue_when_the_lifecycle_is_busy(
    held_coordinator: LifecycleCoordinator,
) -> None:
    intent = _intent(owner_fatal=True)
    lsp_process_state.enqueue_failure_intent(held_coordinator, intent)
    held_coordinator.recovery_wake.clear()

    with pytest.raises(TimeoutError, match="transition lock"):
        lsp_process_failure.drain_terminal_failures(None, held_coordinator, _expired())

    assert held_coordinator.failure_queue.get_nowait() is intent
    assert held_coordinator.recovery_wake.is_set()
    assert held_coordinator.pending_failure_intents == 1


def test_an_unknown_cleanup_step_cannot_fail() -> None:
    with pytest.raises(ValueError, match="unknown LSP cleanup step"):
        CleanupResult().failed("coffee_break", RuntimeError("no"))


@pytest.mark.parametrize(("step", "status"), [("evidence", "failed"), ("nap", "success")])
def test_a_cleanup_step_resolves_only_to_a_known_success(step: str, status: str) -> None:
    with pytest.raises(ValueError, match="invalid LSP cleanup resolution"):
        CleanupResult().succeeded(step, status)


def test_a_released_wake_read_end_is_no_longer_owned() -> None:
    read_fd, write_fd = os.pipe()
    wake = StderrWake(read_fd, write_fd)
    wake.abandon()

    with pytest.raises(OSError, match="ownership was released"):
        wake.owned_read_fd()


def _inherited_environment(system_root: str | None) -> dict[str, str]:
    inherited = {"PATH": "/usr/bin"}
    if system_root is None:
        return inherited
    return {**inherited, "SYSTEMROOT": system_root}


@pytest.mark.parametrize("system_root", [None, "/definitely/not/a/windows/root"])
def test_windows_needs_an_existing_system_root(
    system_root: str | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    inherited = _inherited_environment(system_root)
    monkeypatch.setattr(os, "name", "nt")

    with pytest.raises(ValueError, match="SYSTEMROOT"):
        lsp_process_state.lsp_environment(inherited)


def test_a_profile_environment_is_added_after_the_allowlist() -> None:
    environment = lsp_process_state.lsp_environment(
        {"PATH": "/usr/bin", "SECRET_TOKEN": "x"}, {"GOFLAGS": "-mod=mod"}
    )

    assert environment == {"PATH": "/usr/bin", "GOFLAGS": "-mod=mod"}


def test_a_profile_environment_with_a_nul_is_refused() -> None:
    with pytest.raises(ValueError, match="must not contain NUL"):
        lsp_process_state.lsp_environment({"PATH": "/usr/bin"}, {"GOFLAGS": "bad\0"})


def test_an_expired_lease_deadline_is_refused_without_waiting(tmp_path: Path) -> None:
    coordinator = _owned_coordinator(tmp_path)

    with pytest.raises(TimeoutError, match="lease lock deadline expired"):
        lsp_process_state.acquire_lease(coordinator, _expired())

    assert not coordinator.lease_lock.locked()


def test_a_second_candidate_cannot_take_the_lifecycle(tmp_path: Path) -> None:
    coordinator = _owned_coordinator(tmp_path)
    first = Generation(GENERATION_NONCE, None, None)
    lsp_process_state.assign_candidate(coordinator, first, _later())

    with pytest.raises(RuntimeError, match="another LSP candidate"):
        lsp_process_state.assign_candidate(
            coordinator, Generation(OTHER_NONCE, None, None), _later()
        )

    assert coordinator.candidate is first


def test_no_failure_is_queued_after_success_was_committed(tmp_path: Path) -> None:
    coordinator = _owned_coordinator(tmp_path)
    coordinator.success_committed = True
    generation = Generation(GENERATION_NONCE, None, None)

    assert not lsp_process_state.queue_generation_failure(coordinator, generation, "exited")
    assert not generation.failure_queued
    assert coordinator.pending_failure_intents == 0


def test_acknowledging_more_intents_than_were_queued_is_refused(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="acknowledgement underflow"):
        lsp_process_state.acknowledge_failure_intent(_owned_coordinator(tmp_path))


def test_windows_needs_no_stderr_wake_pipe(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(os, "name", "nt")

    assert lsp_process_state.new_stderr_wake() is None


def _drain_pipe(contents: bytes, wake: StderrWake | None) -> bytes:
    read_fd, write_fd = os.pipe()
    os.write(write_fd, contents)
    os.close(write_fd)
    chunks: deque[bytes] = deque()
    with os.fdopen(read_fd, "rb", buffering=0) as stream:
        lsp_process_state.drain_stderr(stream, chunks, [0], threading.Lock(), wake)
    return b"".join(chunks)


def test_without_a_wake_pipe_stderr_is_read_to_its_end() -> None:
    assert _drain_pipe(b"server says goodbye\n", None) == b"server says goodbye\n"


def test_a_woken_drain_takes_what_is_waiting_and_stops_at_its_end() -> None:
    wake_read, wake_write = os.pipe()
    wake = StderrWake(wake_read, wake_write)
    wake.request()

    assert _drain_pipe(b"last words\n", wake) == b"last words\n"
    assert wake.read_fd is None


def _without(field: str) -> dict[str, object]:
    record = _valid_failure_record()
    del record[field]
    return record


def _with(field: str, value: object) -> dict[str, object]:
    record = _valid_failure_record()
    record[field] = value
    return record


@pytest.mark.parametrize(
    ("record", "message"),
    [
        (_without("timestamp"), "invalid shape"),
        (_with("extra", 1), "invalid shape"),
        (_with("owner_nonce", "A" * 32), "invalid owner_nonce"),
        (_with("generation_nonce", 7), "invalid generation_nonce"),
        (_with("timestamp", "2026-13-01T00:00:00Z"), "invalid timestamp"),
        (_with("timestamp", "2026-11-17T08:30:00.000000Z"), "invalid timestamp"),
        (_with("server_pid", True), "invalid server_pid"),
        (_with("server_pid", 0), "invalid server_pid"),
        (_with("stderr_tail", 5), "invalid stderr_tail"),
        (_with("stderr_tail", "x" * STDERR_TAIL_BYTES), "invalid stderr_tail"),
    ],
)
def test_a_malformed_failure_record_is_refused(record: dict[str, object], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        _validate(record)


def test_a_well_formed_failure_record_is_accepted() -> None:
    _validate(_valid_failure_record())


def test_a_process_view_follows_the_candidate_before_any_retired_generation(
    tmp_path: Path,
) -> None:
    coordinator = _owned_coordinator(tmp_path)
    candidate = Generation(OTHER_NONCE, None, None)
    coordinator.candidate = candidate
    coordinator.retired.append(Generation(GENERATION_NONCE, None, None))

    assert _ProcessView(coordinator)._projected_generation() is candidate
