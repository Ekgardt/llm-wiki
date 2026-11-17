from __future__ import annotations

import subprocess
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import cast

import pytest
from llm_wiki import (
    lsp_process_cleanup,
    lsp_process_limits,
    lsp_process_recovery,
    lsp_process_restart,
    lsp_process_state,
)
from llm_wiki.lsp_process_owner import OwnerDirectory
from llm_wiki.lsp_process_state import (
    FailureIntent,
    Generation,
    LifecycleCoordinator,
    LifecyclePhase,
    LspProcessHandle,
    ProcessState,
)
from lsp_protocol import LspProtocol

OWNER_NONCE = "a" * 32
GENERATION_NONCE = "b" * 32
OTHER_NONCE = "c" * 32


def _expired() -> float:
    return time.monotonic() - 1.0


def _later() -> float:
    return time.monotonic() + 5.0


def _shortly() -> float:
    return time.monotonic() + 0.05


class _FakeServer:
    def __init__(self, exit_code: int | None) -> None:
        self.exit_code = exit_code
        self.pid = 4242

    def poll(self) -> int | None:
        return self.exit_code


class _FakeProtocol:
    fatal = False


class _ProcessView(LspProcessHandle):
    def __init__(self, coordinator: LifecycleCoordinator, cwd: Path | None = None) -> None:
        self._coordinator = coordinator
        self.owner_nonce = OWNER_NONCE
        self.generation_nonce = GENERATION_NONCE
        self.restart_count = 0
        self.state = ProcessState.PROCESS_RUNNING
        self._cwd = cwd
        self._command = ("server",)
        self.process = _FakeServer(None)


def _owned_coordinator(tmp_path: Path) -> LifecycleCoordinator:
    owner = OwnerDirectory(tmp_path / OWNER_NONCE, parent_handle=0, parent_identity=None)
    return LifecycleCoordinator(owner)


def _candidate(exit_code: int | None) -> Generation:
    generation = Generation(GENERATION_NONCE, None, None)
    generation.process = cast("subprocess.Popen[bytes]", _FakeServer(exit_code))
    generation.protocol = cast(LspProtocol, _FakeProtocol())
    return generation


def _queued_intent(coordinator: LifecycleCoordinator, owner_fatal: bool) -> FailureIntent:
    intent = FailureIntent(GENERATION_NONCE, "exited", owner_fatal, time.monotonic())
    lsp_process_state.enqueue_failure_intent(coordinator, intent)
    return coordinator.failure_queue.get_nowait()


def _finished_thread() -> threading.Thread:
    thread = threading.Thread(target=int)
    thread.start()
    thread.join()
    return thread


@pytest.fixture
def stuck_thread() -> Iterator[threading.Thread]:
    release = threading.Event()
    thread = threading.Thread(target=release.wait, daemon=True)
    thread.start()
    yield thread
    release.set()
    thread.join()


@pytest.fixture
def held_lifecycle(tmp_path: Path) -> Iterator[LifecycleCoordinator]:
    coordinator = _owned_coordinator(tmp_path)
    coordinator.lock.acquire()
    yield coordinator
    coordinator.lock.release()


def test_starting_workers_builds_only_the_missing_heartbeat(tmp_path: Path) -> None:
    coordinator = _owned_coordinator(tmp_path)
    finished_recovery = _finished_thread()
    coordinator.recovery_thread = finished_recovery
    coordinator.heartbeat_stop.set()
    coordinator.heartbeat_wake.set()

    lsp_process_recovery.start_lifecycle_workers(_ProcessView(coordinator))

    heartbeat = coordinator.heartbeat_thread
    assert heartbeat is not None
    heartbeat.join()
    assert heartbeat.name == f"lsp-heartbeat-{OWNER_NONCE}"
    assert coordinator.recovery_thread is finished_recovery


def test_a_previous_recovery_thread_is_asked_to_stop_and_awaited(tmp_path: Path) -> None:
    coordinator = _owned_coordinator(tmp_path)
    previous = threading.Thread(target=coordinator.recovery_stop.wait, daemon=True)
    previous.start()
    coordinator.recovery_thread = previous

    lsp_process_recovery._stop_previous_recovery(coordinator, _later())

    assert not previous.is_alive()
    assert coordinator.recovery_wake.is_set()


def test_no_recovery_thread_is_adopted_without_a_pending_request(tmp_path: Path) -> None:
    coordinator = _owned_coordinator(tmp_path)

    lsp_process_recovery._start_replacement_recovery(
        _ProcessView(coordinator), coordinator, _later()
    )

    assert coordinator.recovery_thread is None


def test_a_live_recovery_thread_is_kept_and_woken_instead_of_replaced(
    tmp_path: Path, stuck_thread: threading.Thread
) -> None:
    coordinator = _owned_coordinator(tmp_path)
    coordinator.recovery_thread = stuck_thread
    coordinator.recovery_stop.set()
    coordinator.recovery_request_pending.set()

    replacement = lsp_process_recovery._adopt_recovery_thread_locked(
        _ProcessView(coordinator), coordinator
    )

    assert replacement is None
    assert not coordinator.recovery_stop.is_set()
    assert coordinator.recovery_wake.is_set()
    assert coordinator.recovery_thread is stuck_thread


def test_a_serving_recovery_thread_is_only_woken(
    tmp_path: Path, stuck_thread: threading.Thread
) -> None:
    coordinator = _owned_coordinator(tmp_path)
    coordinator.recovery_thread = stuck_thread

    lsp_process_recovery._start_replacement_recovery(
        _ProcessView(coordinator), coordinator, _later()
    )

    assert coordinator.recovery_wake.is_set()
    assert coordinator.recovery_thread is stuck_thread


def test_a_stopping_recovery_with_work_owed_does_not_wait_or_back_off(tmp_path: Path) -> None:
    coordinator = _owned_coordinator(tmp_path)
    coordinator.recovery_stop.set()
    state = lsp_process_recovery.RecoveryState(request_retry=True)

    lsp_process_recovery._recovery_wait(_ProcessView(coordinator), coordinator, state)

    assert state.retry_seconds == lsp_process_limits.RECOVERY_RETRY_SECONDS


@pytest.mark.parametrize(("stopping", "retry"), [(False, True), (True, False)])
def test_a_recovery_request_without_the_driver_is_retried_only_while_running(
    tmp_path: Path, stopping: bool, retry: bool
) -> None:
    coordinator = _owned_coordinator(tmp_path)
    coordinator.recovery_request_pending.set()
    if stopping:
        coordinator.recovery_stop.set()

    outcome = lsp_process_recovery.process_recovery_request(_ProcessView(coordinator), _expired())

    assert outcome == (retry, None)


def test_a_stopping_recovery_does_not_retry_terminal_cleanup(tmp_path: Path) -> None:
    coordinator = _owned_coordinator(tmp_path)
    coordinator.recovery_stop.set()

    assert not lsp_process_recovery._retry_autonomous_terminal_cleanup(
        _ProcessView(coordinator), "process_exited"
    )


def test_a_request_to_an_ended_and_released_lifecycle_is_cleared(tmp_path: Path) -> None:
    coordinator = _owned_coordinator(tmp_path)
    coordinator.recovery_request_nonce = GENERATION_NONCE
    coordinator.recovery_request_pending.set()
    coordinator.terminal_outcome = "failure"
    coordinator.cleanup_result.ownership_pending = False

    outcome = lsp_process_recovery.process_recovery_request(_ProcessView(coordinator), _later())

    assert outcome == (False, None)
    assert coordinator.recovery_request_nonce is None
    assert not coordinator.recovery_request_pending.is_set()


def test_a_request_for_a_generation_that_is_not_ours_is_cleared(tmp_path: Path) -> None:
    coordinator = _owned_coordinator(tmp_path)
    coordinator.active = Generation(GENERATION_NONCE, None, None)
    coordinator.recovery_request_nonce = OTHER_NONCE
    coordinator.recovery_request_pending.set()

    outcome = lsp_process_recovery.process_recovery_request(_ProcessView(coordinator), _later())

    assert outcome == (False, None)
    assert coordinator.recovery_request_nonce is None


def test_a_request_for_a_retired_generation_outside_recovery_waits(tmp_path: Path) -> None:
    coordinator = _owned_coordinator(tmp_path)
    coordinator.retired.append(Generation(OTHER_NONCE, None, None))
    coordinator.recovery_request_nonce = OTHER_NONCE
    coordinator.recovery_request_pending.set()

    outcome = lsp_process_recovery.process_recovery_request(_ProcessView(coordinator), _later())

    assert outcome == (True, None)
    assert coordinator.recovery_request_nonce == OTHER_NONCE


def test_a_recovery_plan_out_of_reach_is_retried(
    held_lifecycle: LifecycleCoordinator,
) -> None:
    held_lifecycle.recovery_request_nonce = GENERATION_NONCE

    outcome = lsp_process_recovery.process_recovery_request(
        _ProcessView(held_lifecycle), _shortly()
    )

    assert outcome == (True, None)


def test_an_intent_that_cannot_claim_the_driver_goes_back_to_the_queue(tmp_path: Path) -> None:
    coordinator = _owned_coordinator(tmp_path)
    intent = _queued_intent(coordinator, owner_fatal=False)
    coordinator.recovery_wake.clear()

    code = lsp_process_recovery.process_failure_intent(
        _ProcessView(coordinator), intent, _expired()
    )

    assert code is None
    assert coordinator.failure_queue.get_nowait() is intent
    assert coordinator.recovery_wake.is_set()


def test_an_intent_already_seen_is_only_acknowledged(tmp_path: Path) -> None:
    coordinator = _owned_coordinator(tmp_path)
    intent = _queued_intent(coordinator, owner_fatal=False)
    coordinator.seen_failures.add((GENERATION_NONCE, False))

    code = lsp_process_recovery.process_failure_intent(_ProcessView(coordinator), intent, _later())

    assert code is None
    assert coordinator.pending_failure_intents == 0


def test_an_intent_before_startup_completes_waits_in_the_queue(tmp_path: Path) -> None:
    coordinator = _owned_coordinator(tmp_path)
    intent = _queued_intent(coordinator, owner_fatal=False)

    code = lsp_process_recovery.process_failure_intent(_ProcessView(coordinator), intent, _later())

    assert code is None
    assert coordinator.failure_queue.get_nowait() is intent
    assert coordinator.pending_failure_intents == 1


def test_an_intent_for_a_generation_no_longer_active_is_stale(tmp_path: Path) -> None:
    coordinator = _owned_coordinator(tmp_path)
    coordinator.startup_complete = True
    intent = _queued_intent(coordinator, owner_fatal=False)

    code = lsp_process_recovery.process_failure_intent(_ProcessView(coordinator), intent, _later())

    assert code is None
    assert (GENERATION_NONCE, False) in coordinator.seen_failures
    assert coordinator.pending_failure_intents == 0
    assert coordinator.terminal_outcome is None


def test_an_intent_out_of_lifecycle_reach_goes_back_to_the_queue(
    held_lifecycle: LifecycleCoordinator,
) -> None:
    intent = _queued_intent(held_lifecycle, owner_fatal=False)

    code = lsp_process_recovery.process_failure_intent(
        _ProcessView(held_lifecycle), intent, _shortly()
    )

    assert code is None
    assert held_lifecycle.failure_queue.get_nowait() is intent


def test_a_terminal_cleanup_that_raises_is_remembered_and_owed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refusing_cleanup(*arguments: object, **options: object) -> list[BaseException]:
        raise PermissionError(13, "handle held elsewhere")

    coordinator = _owned_coordinator(tmp_path)
    coordinator.startup_complete = True
    intent = _queued_intent(coordinator, owner_fatal=True)
    monkeypatch.setattr(lsp_process_cleanup, "drive_cleanup", refusing_cleanup)

    view = _ProcessView(coordinator)
    code = lsp_process_recovery.process_failure_intent(view, intent, _later())

    assert code == "heartbeat_failed"
    assert coordinator.phase is LifecyclePhase.STOPPING_FAILURE
    assert view.state is ProcessState.DEGRADED
    error = lsp_process_state.take_background_cleanup_error(coordinator)
    assert str(error) == "LSP background cleanup failed (PermissionError, code 13)"


def test_owners_are_left_alone_when_the_driver_is_out_of_reach(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    coordinator = _owned_coordinator(tmp_path)
    monkeypatch.setattr(lsp_process_limits, "GRACEFUL_CLEANUP_SECONDS", 0.0)

    assert lsp_process_restart.retain_autonomous_cleanup_owners(_ProcessView(coordinator))
    assert coordinator.recovery_thread is None


def test_owners_already_released_are_not_retained(tmp_path: Path) -> None:
    coordinator = _owned_coordinator(tmp_path)
    coordinator.cleanup_result.ownership_pending = False

    assert not lsp_process_restart.retain_autonomous_cleanup_owners(_ProcessView(coordinator))
    assert coordinator.heartbeat_thread is None


def test_owners_held_by_another_live_recovery_thread_are_not_taken(
    tmp_path: Path, stuck_thread: threading.Thread
) -> None:
    coordinator = _owned_coordinator(tmp_path)
    coordinator.recovery_thread = stuck_thread

    assert not lsp_process_restart.retain_autonomous_cleanup_owners(_ProcessView(coordinator))
    assert coordinator.recovery_thread is stuck_thread


def test_a_retained_heartbeat_that_cannot_start_is_remembered(tmp_path: Path) -> None:
    coordinator = _owned_coordinator(tmp_path)

    lsp_process_restart._start_retained_heartbeat(coordinator, _finished_thread())

    error = lsp_process_state.take_background_cleanup_error(coordinator)
    assert str(error) == "LSP background cleanup failed (RuntimeError)"


def test_a_closed_process_cannot_restart(tmp_path: Path) -> None:
    coordinator = _owned_coordinator(tmp_path)
    coordinator.terminal_outcome = "failure"

    with pytest.raises(RuntimeError, match="LSP process is closed"):
        lsp_process_restart.prepare_restart_generation_owned(_ProcessView(coordinator), _later())

    assert coordinator.phase is LifecyclePhase.STARTING


def test_a_restart_waits_for_every_retired_generation_to_let_go(tmp_path: Path) -> None:
    coordinator = _owned_coordinator(tmp_path)
    holding = Generation(OTHER_NONCE, None, None, windows_job=9)
    coordinator.retired.append(holding)

    with pytest.raises(TimeoutError, match="retired generation cleanup is incomplete"):
        lsp_process_restart._require_retirement_complete(coordinator, [])


def test_a_restart_needs_its_working_directory(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="restart inputs are unavailable"):
        lsp_process_restart._require_restart_inputs(_ProcessView(_owned_coordinator(tmp_path)))


def test_a_restart_commit_needs_an_owner_directory(tmp_path: Path) -> None:
    view = _ProcessView(LifecycleCoordinator(None), tmp_path)

    with pytest.raises(RuntimeError, match="owner directory is unavailable"):
        lsp_process_restart.commit_restart_generation_owned(
            view, _candidate(None), ProcessState.PROCESS_RUNNING, _later()
        )


@pytest.mark.parametrize("candidate", [Generation(GENERATION_NONCE, None, None), _candidate(0)])
def test_a_dead_or_unconnected_candidate_is_not_committed(
    tmp_path: Path, candidate: Generation
) -> None:
    coordinator = _owned_coordinator(tmp_path)
    coordinator.candidate = candidate

    with pytest.raises(RuntimeError, match="failed before commit"):
        lsp_process_restart.commit_restart_generation_owned(
            _ProcessView(coordinator, tmp_path), candidate, ProcessState.PROCESS_RUNNING, _later()
        )

    assert coordinator.active is None


def test_a_candidate_is_not_committed_after_the_process_became_terminal(tmp_path: Path) -> None:
    coordinator = _owned_coordinator(tmp_path)
    candidate = _candidate(None)
    coordinator.candidate = candidate
    coordinator.terminal_outcome = "failure"

    with pytest.raises(RuntimeError, match="became terminal during restart"):
        lsp_process_restart.commit_restart_generation_owned(
            _ProcessView(coordinator, tmp_path), candidate, ProcessState.PROCESS_RUNNING, _later()
        )


def test_a_candidate_that_lost_its_place_is_not_committed(tmp_path: Path) -> None:
    coordinator = _owned_coordinator(tmp_path)
    coordinator.candidate = Generation(OTHER_NONCE, None, None)

    with pytest.raises(RuntimeError, match="lost lifecycle ownership"):
        lsp_process_restart.commit_restart_generation_owned(
            _ProcessView(coordinator, tmp_path),
            _candidate(None),
            ProcessState.PROCESS_RUNNING,
            _later(),
        )


def test_a_candidate_that_died_before_the_locked_check_is_not_committed(tmp_path: Path) -> None:
    coordinator = _owned_coordinator(tmp_path)
    candidate = _candidate(1)
    coordinator.candidate = candidate

    with pytest.raises(RuntimeError, match="failed before commit"):
        lsp_process_restart._check_commit_preconditions_locked(coordinator, candidate)
