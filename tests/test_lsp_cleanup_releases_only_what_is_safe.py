from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass, field
from typing import cast

import lsp_process_tree
import pytest
from llm_wiki import lsp_process_cleanup, lsp_process_failure, lsp_process_state
from llm_wiki.lsp_process_cleanup import StartupCleanupError
from llm_wiki.lsp_process_owner import OwnerDirectory
from llm_wiki.lsp_process_state import Generation, LifecycleCoordinator, LifecyclePhase
from lsp_protocol import LspProtocol

from tests.slow_machine import LONG_TIMEOUT, PAUSE_TIMEOUT, SHORT_TIMEOUT

NONCE = "d" * 32
SHORT_DEADLINE_SECONDS = 0.1


@dataclass
class FakeTree:
    terminate_error: BaseException | None = None
    close_error: BaseException | None = None
    closed: int = 0

    def terminate(self, *, deadline: float) -> None:
        if self.terminate_error is not None:
            raise self.terminate_error

    def close(self) -> None:
        if self.close_error is not None:
            raise self.close_error
        self.closed += 1


@dataclass
class FakePipe:
    close_error: BaseException | None = None
    closed: bool = False

    def close(self) -> None:
        if self.close_error is not None:
            raise self.close_error
        self.closed = True


@dataclass
class FakeServer:
    exit_code: int | None = 0
    stdin: FakePipe = field(default_factory=FakePipe)
    stdout: FakePipe = field(default_factory=FakePipe)
    stderr: FakePipe = field(default_factory=FakePipe)

    def poll(self) -> int | None:
        return self.exit_code


class FinishedProtocol:
    def _stop_io_for_process_cleanup(self) -> None:
        return None

    def _finish_io_after_process_exit(self, deadline: float) -> None:
        return None


@dataclass
class FakeOwner:
    scratch_error: BaseException | None = None
    close_error: BaseException | None = None
    owner_permissions_verified: bool = True
    _closed: bool = False
    steps: list[str] = field(default_factory=list)

    def remove_lease(self) -> None:
        self.steps.append("lease")

    def remove_success_scratch(self) -> None:
        self.steps.append("scratch")
        if self.scratch_error is not None:
            raise self.scratch_error

    def close(self) -> None:
        self.steps.append("close")
        if self.close_error is not None:
            raise self.close_error
        self._closed = True


def _generation(server: FakeServer | None = None, tree: FakeTree | None = None) -> Generation:
    generation = Generation(NONCE, None, None)
    generation.tree = cast(lsp_process_state.ProcessTree, tree or FakeTree())
    generation.process = cast(object, server or FakeServer())
    generation.protocol = cast(LspProtocol, FinishedProtocol())
    return generation


def _coordinator_with(generation: Generation | None) -> LifecycleCoordinator:
    coordinator = LifecycleCoordinator(owner_directory=None)
    coordinator.phase = LifecyclePhase.RUNNING
    coordinator.active = generation
    return coordinator


def _release(
    coordinator: LifecycleCoordinator, seconds: float = LONG_TIMEOUT
) -> list[BaseException]:
    return lsp_process_cleanup.drive_cleanup(
        None, time.monotonic() + seconds, terminal=False, coordinator_override=coordinator
    )


def _blocked_thread(release: threading.Event) -> threading.Thread:
    thread = threading.Thread(target=release.wait, args=(PAUSE_TIMEOUT,), daemon=True)
    thread.start()
    return thread


def test_an_exited_generation_is_released_completely() -> None:
    server = FakeServer()
    generation = _generation(server)
    coordinator = _coordinator_with(generation)

    assert _release(coordinator) == []

    assert generation.released
    assert server.stdin.closed and server.stdout.closed and server.stderr.closed
    assert not coordinator.cleanup_result.ownership_pending
    assert coordinator.cleanup_result.protocol_stop == "success"


def test_a_pipe_that_will_not_close_keeps_the_process_held() -> None:
    server = FakeServer(stdin=FakePipe(close_error=OSError(9, "bad descriptor")))
    generation = _generation(server)
    coordinator = _coordinator_with(generation)

    errors = _release(coordinator)

    assert [str(error) for error in errors] == ["[Errno 9] bad descriptor"]
    assert coordinator.cleanup_result.protocol_stop == "failed"
    assert generation.process is cast(object, server)
    assert server.stdout.closed
    assert coordinator.cleanup_result.ownership_pending
    assert coordinator.phase is LifecyclePhase.CLEANUP_PENDING


def test_a_stderr_reader_that_does_not_stop_keeps_its_pipe_and_reports_the_join() -> None:
    release = threading.Event()
    server = FakeServer()
    generation = _generation(server)
    generation.stderr_thread = _blocked_thread(release)
    coordinator = _coordinator_with(generation)
    try:
        errors = _release(coordinator, SHORT_DEADLINE_SECONDS)
    finally:
        release.set()

    assert [type(error) for error in errors] == [TimeoutError]
    assert coordinator.cleanup_result.generation_joins == "failed"
    assert not server.stderr.closed
    assert server.stdin.closed
    assert generation.process is cast(object, server)


def test_a_cleanup_with_no_coordinator_is_a_programming_error() -> None:
    with pytest.raises(RuntimeError, match="requires a lifecycle coordinator"):
        lsp_process_cleanup.drive_cleanup(None, time.monotonic() + 1, terminal=False)


def test_a_cleanup_that_cannot_reach_the_lifecycle_lock_keeps_ownership() -> None:
    generation = _generation()
    coordinator = _coordinator_with(generation)
    coordinator.lock.acquire()
    try:
        errors = _release(coordinator, -1.0)
    finally:
        coordinator.lock.release()

    assert [type(error) for error in errors] == [TimeoutError]
    assert coordinator.cleanup_result.generation_joins == "failed"
    assert coordinator.cleanup_result.ownership_pending
    assert not generation.released


def test_a_cleanup_whose_closing_pass_is_locked_out_keeps_ownership() -> None:
    coordinator = _coordinator_with(None)

    class LockHoldingTree(FakeTree):
        def terminate(self, *, deadline: float) -> None:
            coordinator.lock.acquire()

    generation = _generation(tree=LockHoldingTree())
    coordinator.active = generation
    try:
        errors = _release(coordinator, SHORT_DEADLINE_SECONDS)
    finally:
        coordinator.lock.release()

    assert [type(error) for error in errors] == [TimeoutError]
    assert coordinator.cleanup_result.generation_joins == "failed"
    assert coordinator.cleanup_result.ownership_pending
    assert coordinator.phase is LifecyclePhase.RUNNING


@pytest.fixture
def windows_cleanup(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    closed_jobs: list[int] = []
    monkeypatch.setattr(os, "name", "nt")
    monkeypatch.setattr(lsp_process_tree, "close_windows_handle", closed_jobs.append)
    return closed_jobs


def test_a_windows_tree_and_job_are_released_once_nothing_holds_the_process(
    windows_cleanup: list[int],
) -> None:
    tree = FakeTree()
    generation = _generation(tree=tree)
    generation.windows_job = 77
    coordinator = _coordinator_with(generation)

    assert _release(coordinator) == []

    assert windows_cleanup == [77]
    assert tree.closed == 1
    assert generation.released
    assert coordinator.cleanup_result.tree_release == "success"


def test_a_windows_tree_and_job_wait_for_a_running_server(
    windows_cleanup: list[int],
) -> None:
    tree = FakeTree()
    generation = _generation(FakeServer(exit_code=None), tree)
    generation.windows_job = 77
    coordinator = _coordinator_with(generation)

    assert _release(coordinator) == []

    assert windows_cleanup == []
    assert tree.closed == 0
    assert coordinator.cleanup_result.tree_release == "pending"
    assert coordinator.phase is LifecyclePhase.CLEANUP_PENDING


def test_a_windows_tree_and_job_that_fail_to_close_stay_held(
    windows_cleanup: list[int], monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse_job(handle: int) -> None:
        raise OSError(6, "invalid job handle")

    monkeypatch.setattr(lsp_process_tree, "close_windows_handle", refuse_job)
    generation = _generation(tree=FakeTree(close_error=OSError(5, "tree busy")))
    generation.windows_job = 77
    coordinator = _coordinator_with(generation)

    errors = _release(coordinator)

    assert sorted(str(error) for error in errors) == [
        "[Errno 5] tree busy",
        "[Errno 6] invalid job handle",
    ]
    assert coordinator.cleanup_result.tree_release == "failed"
    assert generation.windows_job == 77
    assert generation.tree is not None


def _startup_coordinator(generation: Generation) -> LifecycleCoordinator:
    coordinator = LifecycleCoordinator(owner_directory=None)
    coordinator.candidate = generation
    return coordinator


def test_a_startup_cleanup_retry_raises_the_failure_it_met() -> None:
    tree = FakeTree(terminate_error=PermissionError(1, "kill refused"))
    coordinator = _startup_coordinator(_generation(FakeServer(exit_code=None), tree))

    with pytest.raises(PermissionError, match="kill refused"):
        lsp_process_cleanup.retry_startup_cleanup(coordinator, time.monotonic() + SHORT_TIMEOUT)
    assert coordinator.cleanup_result.tree_termination == "failed"


def test_a_startup_cleanup_retry_that_still_owns_a_live_server_says_so() -> None:
    coordinator = _startup_coordinator(_generation(FakeServer(exit_code=None)))

    with pytest.raises(TimeoutError, match="retains retryable ownership"):
        lsp_process_cleanup.retry_startup_cleanup(coordinator, time.monotonic() + SHORT_TIMEOUT)
    assert coordinator.terminal_code == "startup_failed"


def test_exit_cleanup_of_startups_survives_a_failing_retry() -> None:
    tree = FakeTree(terminate_error=PermissionError(1, "kill refused"))
    coordinator = _startup_coordinator(_generation(FakeServer(exit_code=None), tree))
    lsp_process_state.register_startup_cleanup(coordinator)
    try:
        lsp_process_cleanup.atexit_cleanup_startups()
        assert coordinator in lsp_process_state.pending_startup_cleanup_snapshot()
    finally:
        lsp_process_state.unregister_startup_cleanup(coordinator)


def test_a_startup_cleanup_error_without_a_coordinator_owes_nothing() -> None:
    error = StartupCleanupError("startup failed")

    error.retry_cleanup(time.monotonic() + SHORT_TIMEOUT)
    error.transfer_cleanup_ownership()

    assert error.coordinator is None


def _terminal_with_owner(owner: FakeOwner, outcome: str) -> LifecycleCoordinator:
    coordinator = LifecycleCoordinator(owner_directory=cast(OwnerDirectory, owner))
    coordinator.phase = LifecyclePhase.RUNNING
    coordinator.terminal_outcome = outcome
    coordinator.terminal_code = "process_exited"
    return coordinator


def _close_terminally(
    coordinator: LifecycleCoordinator, seconds: float = SHORT_TIMEOUT
) -> list[BaseException]:
    return lsp_process_cleanup.drive_cleanup(
        None, time.monotonic() + seconds, terminal=True, coordinator_override=coordinator
    )


def test_a_successful_close_releases_lease_scratch_and_handles_in_order() -> None:
    owner = FakeOwner()
    coordinator = _terminal_with_owner(owner, "success")

    assert _close_terminally(coordinator) == []

    assert owner.steps == ["lease", "scratch", "close"]
    assert coordinator.phase is LifecyclePhase.STOPPED_SUCCESS
    assert coordinator.owner_directory is None


def test_scratch_that_cannot_be_removed_keeps_the_owner_handles_open() -> None:
    owner = FakeOwner(scratch_error=OSError(16, "scratch busy"))
    coordinator = _terminal_with_owner(owner, "success")

    errors = _close_terminally(coordinator)

    assert [str(error) for error in errors] == ["[Errno 16] scratch busy"]
    assert owner.steps == ["lease", "scratch"]
    assert coordinator.cleanup_result.scratch_removal == "failed"
    assert coordinator.phase is LifecyclePhase.CLEANUP_PENDING


def test_owner_handles_that_cannot_close_leave_the_cleanup_pending() -> None:
    owner = FakeOwner(close_error=OSError(9, "bad descriptor"))
    coordinator = _terminal_with_owner(owner, "success")

    errors = _close_terminally(coordinator)

    assert [str(error) for error in errors] == ["[Errno 9] bad descriptor"]
    assert coordinator.cleanup_result.owner_handles == "failed"
    assert coordinator.phase is LifecyclePhase.CLEANUP_PENDING


def test_failure_evidence_that_cannot_be_written_keeps_the_lease() -> None:
    owner = FakeOwner()
    coordinator = _terminal_with_owner(owner, "failure")

    errors = _close_terminally(coordinator)

    assert errors
    assert coordinator.cleanup_result.evidence == "failed"
    assert owner.steps == []
    assert coordinator.phase is LifecyclePhase.CLEANUP_PENDING


def test_a_recovery_thread_that_will_not_stop_is_reported_against_its_join() -> None:
    owner = FakeOwner()
    coordinator = _terminal_with_owner(owner, "success")
    release = threading.Event()
    coordinator.recovery_thread = _blocked_thread(release)
    try:
        errors = _close_terminally(coordinator, SHORT_DEADLINE_SECONDS)
    finally:
        release.set()

    assert any("recovery thread did not stop" in str(error) for error in errors)
    assert coordinator.cleanup_result.recovery_join == "failed"
    assert owner.steps == []
    assert coordinator.phase is LifecyclePhase.CLEANUP_PENDING


def test_a_failure_drain_that_breaks_is_reported_against_the_recovery_join(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def broken_drain(*arguments: object) -> None:
        raise RuntimeError("failure queue unreadable")

    monkeypatch.setattr(lsp_process_failure, "drain_terminal_failures", broken_drain)
    owner = FakeOwner()
    coordinator = _terminal_with_owner(owner, "success")

    errors = _close_terminally(coordinator)

    assert [str(error) for error in errors] == ["failure queue unreadable"] * 2
    assert coordinator.cleanup_result.recovery_join == "failed"
    assert owner.steps == []
    assert coordinator.phase is LifecyclePhase.CLEANUP_PENDING
