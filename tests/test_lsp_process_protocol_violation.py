"""Protocol violations, restart timeouts and sticky failures end an LSP child correctly."""

from __future__ import annotations

import contextlib
import json
import subprocess
import threading
import time
from pathlib import Path

import lsp_protocol
import pytest
from llm_wiki import (
    lsp_process_cleanup,
    lsp_process_failure,
    lsp_process_generation,
    lsp_process_launch,
    lsp_process_limits,
    lsp_process_owner,
    lsp_process_recovery,
    lsp_process_restart,
    lsp_process_state,
)
from lsp_process import (
    LspProcess,
    ProcessState,
)
from lsp_protocol import (
    MAX_FRAME_BYTES,
    ProtocolViolation,
)

from tests.lsp_process_helpers import (
    _no_lsp_lifecycle_owner_leaks,  # noqa: F401
    )
from tests.lsp_process_scenario_helpers import (
    _BARRIER_SECONDS,
    _COMPLETES_WHILE_HELD_SECONDS,
    _DEADLINE_SECONDS,
    _HOLD_SECONDS,
    _STARTUP_BUDGET_SECONDS,
    OWNER_NONCE,
    _all_children_reaped,
    _await_descendant_pid,
    _await_pid_exit,
    _cleanup_error_types,
    _close_if_owned,
    _close_owned_process,
    _command,
    _coordinator_wait,
    _lock_is_free,
    _pid_alive,
    _poll_until,
    _start,
)
from tests.slow_machine import SHORT_TIMEOUT


def _all_deadlines_ahead(attempts) -> bool:
    return all(deadline > started for started, deadline in attempts)


def _capture_start_error(returned: list, tmp_path: Path) -> BaseException | None:
    try:
        returned.append(_start(tmp_path, "--lifecycle", "--sleep-seconds", "30"))
    except BaseException as raised:  # noqa: BLE001 - the failure is the subject
        return raised
    return None


def _await_process_exit(child, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while child.poll() is None and time.monotonic() < deadline:
        time.sleep(0.01)


def _retry_owned_cleanup(coordinator, cleanup_error) -> None:
    """Finish whatever cleanup this coordinator still owns, by either route."""
    if coordinator is None or not lsp_process_state.coordinator_has_ownership(coordinator):
        return
    if cleanup_error is not None:
        cleanup_error.retry_cleanup(time.monotonic() + 5)
        return
    lsp_process_cleanup.retry_startup_cleanup(coordinator, time.monotonic() + 5)


def _cause_chain(error: BaseException) -> list[BaseException]:
    chain: list[BaseException] = []
    current: BaseException | None = error
    while current is not None:
        chain.append(current)
        current = current.__cause__
    return chain


def _drive_retained_cleanup(error: BaseException | None) -> None:
    """Finish cleanup for a startup that returned ownership with its error."""
    if not isinstance(error, lsp_process_cleanup.StartupCleanupError):
        return
    coordinator = error.coordinator
    assert coordinator is not None
    lsp_process_cleanup.drive_cleanup(
        None,
        time.monotonic() + 5,
        terminal=True,
        failure_code="startup_failed",
        coordinator_override=coordinator,
    )


def _await_children_reaped(children, deadline: float) -> None:
    while not _all_children_reaped(children) and time.monotonic() < deadline:
        time.sleep(0.01)


def test_blocked_protocol_owner_retains_retryable_generation_and_lease(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    process = _start(tmp_path, "--lifecycle")
    coordinator = process._coordinator
    generation = coordinator.active
    assert generation is not None and generation.protocol is not None
    protocol = generation.protocol
    join_owners = protocol._join_owners
    released = threading.Event()

    def blocked(deadline: float) -> None:
        if not released.is_set():
            raise TimeoutError("protocol owner blocked")
        join_owners(deadline)

    monkeypatch.setattr(protocol, "_join_owners", blocked)
    with pytest.raises(TimeoutError, match="protocol owner blocked"):
        process.shutdown(time.monotonic() + 5)

    assert generation.protocol is protocol
    assert coordinator.cleanup_result.ownership_pending is True
    assert (process.owner_root / "lease.json").is_file()

    released.set()
    process.shutdown(time.monotonic() + 5)
    assert not process.owner_root.exists()


def _cyclic_value() -> list[object]:
    value: list[object] = []
    value.append(value)
    return value


def _invalid_request_params(case: str) -> object:
    """One payload per way a caller can violate the JSON wire contract."""
    cases = {
        "cycle": _cyclic_value,
        "nan": lambda: {"value": float("nan")},
        "inf": lambda: {"value": float("inf")},
        "surrogate": lambda: {"value": "\ud800"},
        "object": lambda: {"value": object()},
        "oversize": lambda: {"value": "x" * MAX_FRAME_BYTES},
    }
    return cases[case]()


@pytest.mark.parametrize("case", ["cycle", "nan", "inf", "surrogate", "object", "oversize"])
def test_caller_json_violation_never_restarts_and_valid_follow_up_works(
    tmp_path: Path,
    case: str,
) -> None:
    owner = tmp_path / f"{len(case):032x}"
    process = LspProcess.start(_command("--lifecycle"), cwd=tmp_path, owner_root=owner)

    with pytest.raises(ProtocolViolation):
        process.request(
            "invalid",
            _invalid_request_params(case),
            deadline=time.monotonic() + _STARTUP_BUDGET_SECONDS,
        )

    assert process.restart_count == 0
    assert process.request("echo", {"valid": True}, deadline=time.monotonic() + SHORT_TIMEOUT) == {
        "valid": True
    }
    process.close(time.monotonic() + 5)


def test_caller_json_violation_is_not_retried_after_concurrent_restart(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    process = _start(tmp_path, "--lifecycle")
    first_generation = process.generation_nonce
    real_encode = lsp_protocol.encode_frame
    encoding = threading.Event()
    release_encoding = threading.Event()
    attempts = 0
    request_errors: list[BaseException] = []

    def block_invalid_encoding(message: object) -> bytes:
        nonlocal attempts
        if isinstance(message, dict) and message.get("method") == "invalid":
            attempts += 1
            if attempts == 1:
                encoding.set()
                assert release_encoding.wait(SHORT_TIMEOUT)
        return real_encode(message)

    def request_invalid() -> None:
        try:
            process.request(
                "invalid",
                _invalid_request_params("cycle"),
                deadline=time.monotonic() + _STARTUP_BUDGET_SECONDS,
            )
        except BaseException as error:
            request_errors.append(error)

    monkeypatch.setattr(lsp_protocol, "encode_frame", block_invalid_encoding)
    request_thread = threading.Thread(target=request_invalid)
    request_thread.start()
    assert encoding.wait(SHORT_TIMEOUT)
    process.restart(time.monotonic() + 5)
    assert process.generation_nonce != first_generation
    release_encoding.set()
    request_thread.join(SHORT_TIMEOUT)

    assert not request_thread.is_alive()
    assert len(request_errors) == 1
    assert isinstance(request_errors[0], ProtocolViolation)
    assert attempts == 1
    assert process.restart_count == 1
    assert process.request("echo", {"valid": True}, deadline=time.monotonic() + SHORT_TIMEOUT) == {
        "valid": True
    }
    process.close(time.monotonic() + 5)


def test_expired_deadline_waiting_for_transition_lock_changes_nothing(
    tmp_path: Path,
) -> None:
    process = _start(tmp_path, "--lifecycle")
    coordinator = process._coordinator
    generation = coordinator.active
    assert generation is not None
    acquired = threading.Event()
    release = threading.Event()
    before = (
        process.process,
        process.protocol,
        process.generation_nonce,
        process.restart_count,
        process.state,
        coordinator.phase,
        coordinator.recovery_attempted,
        generation.expected_exit.is_set(),
        coordinator.recovery_wake.is_set(),
    )

    def hold_transition() -> None:
        with coordinator.condition:
            acquired.set()
            assert release.wait(_HOLD_SECONDS)

    holder = threading.Thread(target=hold_transition)
    holder.start()
    assert acquired.wait(SHORT_TIMEOUT)
    deadline = time.monotonic() + 0.03
    try:
        with pytest.raises(TimeoutError, match="lifecycle|transition"):
            process.restart(deadline)
        assert time.monotonic() <= deadline + 0.2
        assert (
            process.process,
            process.protocol,
            process.generation_nonce,
            process.restart_count,
            process.state,
            coordinator.phase,
            coordinator.recovery_attempted,
            generation.expected_exit.is_set(),
            coordinator.recovery_wake.is_set(),
        ) == before
        assert coordinator.candidate is None
    finally:
        release.set()
        holder.join(SHORT_TIMEOUT)
        process.close(time.monotonic() + 5)


def test_explicit_restart_timeout_after_pending_hands_off_one_recovery_worker(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    process = _start(tmp_path, "--lifecycle")
    coordinator = process._coordinator
    recovery = coordinator.recovery_thread
    assert recovery is not None
    first_generation = process.generation_nonce
    seize = threading.Event()
    held = threading.Event()
    release = threading.Event()
    restart_thread = threading.current_thread()
    release_lifecycle = lsp_process_state.release_lifecycle
    prepare_generation = lsp_process_generation.prepare_generation
    prepared_by: list[threading.Thread] = []
    armed = True

    def hold_transition() -> None:
        assert seize.wait(_BARRIER_SECONDS)
        with coordinator.condition:
            held.set()
            assert release.wait(_HOLD_SECONDS)

    def release_then_contend(current: lsp_process_state.LifecycleCoordinator) -> None:
        nonlocal armed
        pending = (
            current is coordinator
            and armed
            and threading.current_thread() is restart_thread
            and current.phase is lsp_process_state.LifecyclePhase.RECOVERY_PENDING
        )
        release_lifecycle(current)
        if pending:
            armed = False
            seize.set()
            assert held.wait(_BARRIER_SECONDS)

    def observe_prepare(*args: object, **kwargs: object) -> lsp_process_state.Generation:
        prepared_by.append(threading.current_thread())
        return prepare_generation(*args, **kwargs)  # type: ignore[arg-type]

    holder = threading.Thread(target=hold_transition)
    holder.start()
    monkeypatch.setattr(lsp_process_state, "release_lifecycle", release_then_contend)
    monkeypatch.setattr(lsp_process_generation, "prepare_generation", observe_prepare)

    try:
        with pytest.raises(TimeoutError, match="lifecycle transition lock"):
            process.restart(time.monotonic() + 0.1)

        assert coordinator.phase is lsp_process_state.LifecyclePhase.RECOVERY_PENDING
        assert process.restart_count == 0
        assert prepared_by == []

        release.set()
        holder.join(SHORT_TIMEOUT)

        assert _coordinator_wait(process, lambda: process.restart_count == 1, timeout=5)
        assert coordinator.phase is lsp_process_state.LifecyclePhase.RUNNING
        assert process.generation_nonce != first_generation
        assert prepared_by == [recovery]
        assert process.request("echo", {"ok": True}, deadline=time.monotonic() + 2) == {
            "ok": True
        }
    finally:
        release.set()
        holder.join(SHORT_TIMEOUT)
        if lsp_process_state.coordinator_has_ownership(coordinator):
            process.close(time.monotonic() + 5)
    monkeypatch.setattr(lsp_process_state, "release_lifecycle", release_lifecycle)
    monkeypatch.setattr(lsp_process_generation, "prepare_generation", prepare_generation)
    _exercise_explicit_restart_retirement_deadline_finishes_without_caller_retry(
        monkeypatch, tmp_path
    )


def _exercise_explicit_restart_retirement_deadline_finishes_without_caller_retry(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    process = _start(
        tmp_path,
        "--lifecycle",
        "--spawn-descendant",
        "--sleep-seconds",
        "30",
    )
    coordinator = process._coordinator
    generation = coordinator.active
    recovery = coordinator.recovery_thread
    assert generation is not None
    assert generation.tree is not None
    assert recovery is not None
    tree = generation.tree
    terminate = lsp_process_state.ProcessTree.terminate
    fresh_cleanup_started = threading.Event()
    allow_fresh_cleanup = threading.Event()
    caller_deadline = time.monotonic() + 0.05

    def hold_tree_cleanup(deadline: float) -> None:
        if deadline <= caller_deadline:
            threading.Event().wait(max(0.0, deadline - time.monotonic()) + 0.005)
            raise TimeoutError("restart caller retirement deadline expired")
        fresh_cleanup_started.set()
        if not allow_fresh_cleanup.wait(max(0.0, deadline - time.monotonic())):
            raise TimeoutError("fresh autonomous cleanup stayed blocked")

    def deadline_sensitive_terminate(current: object, *, deadline: float) -> None:
        if current is tree:
            hold_tree_cleanup(deadline)
        terminate(current, deadline=deadline)  # type: ignore[arg-type]

    monkeypatch.setattr(
        lsp_process_state.ProcessTree,
        "terminate",
        deadline_sensitive_terminate,
    )
    try:
        with pytest.raises(
            TimeoutError, match="restart caller retirement deadline expired"
        ):
            process.restart(caller_deadline)

        assert coordinator.recovery_request_pending.is_set()
        assert coordinator.recovery_request_nonce == generation.nonce
        assert _coordinator_wait(
            process,
            lambda: fresh_cleanup_started.is_set()
            or coordinator.phase is lsp_process_state.LifecyclePhase.STOPPED_FAILURE,
            timeout=2,
        )

        allow_fresh_cleanup.set()
        assert _coordinator_wait(
            process,
            lambda: coordinator.phase is lsp_process_state.LifecyclePhase.STOPPED_FAILURE,
            timeout=5,
        )
        recovery.join(SHORT_TIMEOUT)

        assert process.restart_count == 0
        assert process.state is ProcessState.FAILED
        assert not recovery.is_alive()
        assert coordinator.recovery_request_nonce is None
        assert not coordinator.recovery_request_pending.is_set()
        assert not lsp_process_state.coordinator_has_ownership(coordinator)
    finally:
        allow_fresh_cleanup.set()
        monkeypatch.setattr(lsp_process_state.ProcessTree, "terminate", terminate)
        if lsp_process_state.coordinator_has_ownership(coordinator):
            process.close(time.monotonic() + 5)


def _exercise_recovery_owned_candidate_failure_retains_worker_for_terminal_retry(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    process = LspProcess.start(
        _command("--lifecycle", "--sleep-seconds", "30"),
        cwd=tmp_path,
        owner_root=tmp_path / ("c" * 32),
    )
    coordinator = process._coordinator
    recovery = coordinator.recovery_thread
    owner = coordinator.owner_directory
    assert recovery is not None and owner is not None
    caller = threading.current_thread()
    restart_generation = lsp_process_restart.restart_generation
    remove_lease = lsp_process_owner.OwnerDirectory.remove_lease
    retry_started = threading.Event()
    allow_retry = threading.Event()
    retry_threads: list[threading.Thread] = []
    remove_calls = 0

    def expire_caller_restart(instance: LspProcess, deadline: float) -> None:
        if threading.current_thread() is caller:
            raise TimeoutError("explicit restart caller deadline expired")
        restart_generation(instance, deadline)

    def fail_restart_spawn(
        _cls: type[lsp_process_state.ProcessTree],
        *_args: object,
        **_kwargs: object,
    ) -> lsp_process_state.ProcessTree:
        raise OSError("recovery candidate spawn failed")

    def transient_remove_lease(current: lsp_process_owner.OwnerDirectory) -> None:
        nonlocal remove_calls
        if current is owner:
            remove_calls += 1
            if remove_calls == 1:
                raise OSError("recovery cleanup lease removal failed")
            retry_threads.append(threading.current_thread())
            retry_started.set()
            assert allow_retry.wait(SHORT_TIMEOUT)
        remove_lease(current)

    monkeypatch.setattr(lsp_process_restart, "restart_generation", expire_caller_restart)
    monkeypatch.setattr(
        lsp_process_state.ProcessTree,
        "_spawn_with_deadline",
        classmethod(fail_restart_spawn),
    )
    monkeypatch.setattr(
        lsp_process_owner.OwnerDirectory,
        "remove_lease",
        transient_remove_lease,
    )
    try:
        with pytest.raises(TimeoutError, match="explicit restart caller deadline expired"):
            process.restart(time.monotonic() + 0.05)

        assert retry_started.wait(SHORT_TIMEOUT)
        assert retry_threads == [recovery]
        assert recovery.is_alive()

        allow_retry.set()
        assert _coordinator_wait(
            process,
            lambda: coordinator.phase is lsp_process_state.LifecyclePhase.STOPPED_FAILURE,
            timeout=5,
        )
        recovery.join(SHORT_TIMEOUT)

        assert remove_calls == 2
        assert not recovery.is_alive()
        assert not coordinator.recovery_request_pending.is_set()
        assert not lsp_process_state.coordinator_has_ownership(coordinator)
    finally:
        allow_retry.set()
        monkeypatch.setattr(
            lsp_process_owner.OwnerDirectory,
            "remove_lease",
            remove_lease,
        )
        if lsp_process_state.coordinator_has_ownership(coordinator):
            process.close(time.monotonic() + 5)


def test_terminal_intent_lock_respects_deadline_and_cannot_commit_success(
    tmp_path: Path,
) -> None:
    process = _start(tmp_path, "--lifecycle")
    coordinator = process._coordinator
    lsp_process_failure.stop_recovery_owner(coordinator, time.monotonic() + 1)
    assert lsp_process_state.queue_owner_failure(coordinator, "injected pending failure")
    held = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    errors: list[BaseException] = []

    def hold_terminal_intent() -> None:
        coordinator.terminal_intent_lock.acquire()
        try:
            held.set()
            assert release.wait(_HOLD_SECONDS)
        finally:
            coordinator.terminal_intent_lock.release()

    def shutdown() -> None:
        try:
            process.shutdown(time.monotonic() + _DEADLINE_SECONDS)
        except BaseException as error:
            errors.append(error)
        finally:
            finished.set()

    holder = threading.Thread(target=hold_terminal_intent)
    holder.start()
    assert held.wait(_BARRIER_SECONDS)
    closer = threading.Thread(target=shutdown)
    watch_started = time.monotonic()
    closer.start()
    completed_before_release = finished.wait(_COMPLETES_WHILE_HELD_SECONDS)
    waited = time.monotonic() - watch_started
    try:
        assert completed_before_release is True, (
            f"shutdown never returned while the terminal intent lock was held: "
            f"waited {waited:.3f}s of {_COMPLETES_WHILE_HELD_SECONDS}s for a "
            f"{_DEADLINE_SECONDS}s deadline; lock free="
            f"{_lock_is_free(coordinator.terminal_intent_lock)}; "
            f"holder alive={holder.is_alive()}; closer alive={closer.is_alive()}; "
            f"phase={coordinator.phase}; errors={errors!r}"
        )
        assert len(errors) == 1 and isinstance(errors[0], TimeoutError)
        assert coordinator.pending_failure_intents == 1
        assert coordinator.success_committed is False
        assert (process.owner_root / "lease.json").is_file()
        assert lsp_process_state.coordinator_has_ownership(coordinator)
    finally:
        release.set()
        holder.join(_BARRIER_SECONDS)
        closer.join(_BARRIER_SECONDS)
        if lsp_process_state.coordinator_has_ownership(coordinator):
            process.close(time.monotonic() + 5)

    assert coordinator.terminal_outcome == "failure"
    assert (process.owner_root / "failure.json").is_file()


@pytest.mark.parametrize("worker_prefix", ["lsp-recovery-", "lsp-heartbeat-"])
def test_startup_worker_start_past_original_deadline_never_returns_success(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    worker_prefix: str,
) -> None:
    children: list[subprocess.Popen[bytes]] = []
    real_popen = subprocess.Popen
    real_start = threading.Thread.start
    returned: list[LspProcess] = []
    error: BaseException | None = None

    def popen(*args: object, **kwargs: object) -> subprocess.Popen[bytes]:
        child = real_popen(*args, **kwargs)
        children.append(child)
        return child

    def delayed_start(thread: threading.Thread) -> None:
        if thread.name.startswith(worker_prefix):
            time.sleep(lsp_process_limits.STARTUP_WAIT_SECONDS + 0.05)
        real_start(thread)

    monkeypatch.setattr(lsp_process_state.subprocess, "Popen", popen)
    monkeypatch.setattr(threading.Thread, "start", delayed_start)
    started = time.monotonic()
    try:
        error = _capture_start_error(returned, tmp_path)
    finally:
        for process in returned:
            process.close(time.monotonic() + 5)
        _drive_retained_cleanup(error)

    assert time.monotonic() - started >= lsp_process_limits.STARTUP_WAIT_SECONDS
    assert returned == []
    assert error is not None
    assert any(isinstance(item, TimeoutError) for item in _cause_chain(error))
    settle_deadline = time.monotonic() + 2
    _await_children_reaped(children, settle_deadline)
    assert children and _all_children_reaped(children)


def test_startup_terminal_intent_timeout_returns_retryable_cleanup_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    pid_file = tmp_path / "startup-terminal-intent-descendant.pid"
    held = threading.Event()
    release = threading.Event()
    captured: list[LspProcess] = []
    holders: list[threading.Thread] = []

    def fail_with_terminal_intent_lock(
        instance: LspProcess,
        deadline: float | None = None,
    ) -> None:
        coordinator = instance._coordinator
        assert deadline is not None
        _await_descendant_pid(pid_file, timeout=max(0.0, deadline - time.monotonic()))

        def hold_terminal_intent() -> None:
            coordinator.terminal_intent_lock.acquire()
            try:
                held.set()
                release.wait(_HOLD_SECONDS)
            finally:
                coordinator.terminal_intent_lock.release()

        holder = threading.Thread(target=hold_terminal_intent)
        holders.append(holder)
        captured.append(instance)
        holder.start()
        assert held.wait(_BARRIER_SECONDS)
        # The failure path owns a fresh cleanup deadline, independently of
        # startup. Expire that deadline only after the real lock is held.
        monkeypatch.setattr(lsp_process_launch, "fresh_cleanup_deadline", time.monotonic)
        raise RuntimeError("startup failed while terminal intent lock stayed held")

    monkeypatch.setattr(
        lsp_process_recovery, "start_lifecycle_workers", fail_with_terminal_intent_lock
    )
    # Keep the normal, platform-qualified startup budget and ACL hardening.
    owner_root = tmp_path / OWNER_NONCE
    coordinator: lsp_process_state.LifecycleCoordinator | None = None
    cleanup_error: lsp_process_cleanup.StartupCleanupError | None = None

    try:
        with pytest.raises(lsp_process_cleanup.StartupCleanupError) as raised:
            _start(
                tmp_path,
                "--lifecycle",
                "--spawn-descendant",
                "--descendant-pid-file",
                str(pid_file),
            )

        cleanup_error = raised.value
        assert len(captured) == 1
        instance = captured[0]
        coordinator = instance._coordinator
        generation_nonce = instance.generation_nonce
        server_pid = instance.process.pid
        assert isinstance(raised.value.__cause__, RuntimeError)
        assert callable(raised.value.retry_cleanup)
        assert coordinator.phase is lsp_process_state.LifecyclePhase.CLEANUP_PENDING
        assert coordinator.cleanup_result.ownership_pending is True
        assert "TimeoutError" in _cleanup_error_types(coordinator)
        assert lsp_process_state.coordinator_has_ownership(coordinator)
        assert coordinator in lsp_process_state.pending_startup_cleanup_snapshot()
        assert owner_root.is_dir()
        assert (owner_root / "owner.json").is_file()
        assert (owner_root / "lease.json").is_file()

        _await_process_exit(instance.process)
        assert instance.process.poll() is not None
        assert pid_file.is_file()
        descendant = int(pid_file.read_text(encoding="ascii"))
        _await_pid_exit(descendant)
        _await_pid_exit(descendant)
        assert not _pid_alive(descendant)

        release.set()
        holders[0].join(SHORT_TIMEOUT)
        raised.value.retry_cleanup(time.monotonic() + 5)

        failure = json.loads((owner_root / "failure.json").read_bytes())
        lsp_process_state.validate_failure_record(
            failure,
            code="startup_failed",
            owner_nonce=OWNER_NONCE,
            generation_nonce=generation_nonce,
            pid=server_pid,
        )
        assert coordinator.phase is lsp_process_state.LifecyclePhase.STOPPED_FAILURE
        assert owner_root.is_dir()
        assert not (owner_root / "lease.json").exists()
        assert coordinator not in lsp_process_state.pending_startup_cleanup_snapshot()
    finally:
        release.set()
        for holder in holders:
            holder.join(SHORT_TIMEOUT)
        if coordinator is None and captured:
            coordinator = captured[0]._coordinator
        _retry_owned_cleanup(coordinator, cleanup_error)


def test_autonomous_restart_failure_with_expired_transition_lock_is_sticky(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    process = _start(tmp_path, "--lifecycle")
    coordinator = process._coordinator
    recovery = coordinator.recovery_thread
    heartbeat = coordinator.heartbeat_thread
    assert recovery is not None and heartbeat is not None
    generation = coordinator.active
    assert generation is not None
    generation_nonce = process.generation_nonce
    server_pid = process.process.pid
    graceful_cleanup_seconds = lsp_process_limits.GRACEFUL_CLEANUP_SECONDS
    restart_generation = lsp_process_restart.restart_generation
    monkeypatch.setattr(lsp_process_limits, "GRACEFUL_CLEANUP_SECONDS", 0.05)
    held = threading.Event()
    release = threading.Event()
    terminal_attempted = threading.Event()
    holders: list[threading.Thread] = []
    remember_background_error = lsp_process_state.remember_background_cleanup_error

    def fail_restart(_instance: LspProcess, deadline: float) -> None:
        def hold_transition() -> None:
            with coordinator.condition:
                held.set()
                release.wait(_HOLD_SECONDS)

        holder = threading.Thread(target=hold_transition)
        holders.append(holder)
        holder.start()
        assert held.wait(_BARRIER_SECONDS)
        remaining = deadline - time.monotonic()
        if remaining > 0:
            time.sleep(remaining + 0.01)
        raise RuntimeError("autonomous restart failed with transition lock held")

    def observe_background_error(
        current: lsp_process_state.LifecycleCoordinator,
        error: BaseException,
    ) -> None:
        remember_background_error(current, error)
        if current is coordinator:
            terminal_attempted.set()

    monkeypatch.setattr(lsp_process_restart, "restart_generation", fail_restart)
    monkeypatch.setattr(
        lsp_process_state, "remember_background_cleanup_error", observe_background_error
    )

    try:
        assert lsp_process_state.queue_generation_failure(
            coordinator, generation, "injected autonomous restart failure"
        )
        assert terminal_attempted.wait(SHORT_TIMEOUT)

        assert len(holders) == 1
        assert coordinator.mandatory_failure_intent == lsp_process_state.FailureEvidenceIdentity(
            "restart_failed",
            process.owner_nonce,
            generation_nonce,
            server_pid,
        )
        assert coordinator.phase is not lsp_process_state.LifecyclePhase.STOPPED_SUCCESS
        assert lsp_process_state.coordinator_has_ownership(coordinator)
        assert process.owner_root.is_dir()
        assert (process.owner_root / "owner.json").is_file()
        assert (process.owner_root / "lease.json").is_file()
        assert recovery.is_alive()
        assert heartbeat.is_alive()

        release.set()
        holders[0].join(SHORT_TIMEOUT)

        assert _coordinator_wait(
            process,
            lambda: coordinator.phase is lsp_process_state.LifecyclePhase.STOPPED_FAILURE,
            timeout=5,
        )
        recovery.join(SHORT_TIMEOUT)
        heartbeat.join(SHORT_TIMEOUT)

        failure = json.loads((process.owner_root / "failure.json").read_bytes())
        lsp_process_state.validate_failure_record(
            failure,
            code="restart_failed",
            owner_nonce=process.owner_nonce,
            generation_nonce=generation_nonce,
            pid=server_pid,
        )
        assert coordinator.phase is lsp_process_state.LifecyclePhase.STOPPED_FAILURE
        assert process.owner_root.is_dir()
        assert not (process.owner_root / "lease.json").exists()
        assert not recovery.is_alive()
        assert not heartbeat.is_alive()
        assert not lsp_process_state.coordinator_has_ownership(coordinator)
    finally:
        release.set()
        for holder in holders:
            holder.join(SHORT_TIMEOUT)
        _close_owned_process(process, coordinator)
    monkeypatch.setattr(
        lsp_process_limits, "GRACEFUL_CLEANUP_SECONDS", graceful_cleanup_seconds
    )
    monkeypatch.setattr(lsp_process_restart, "restart_generation", restart_generation)
    monkeypatch.setattr(
        lsp_process_state, "remember_background_cleanup_error", remember_background_error
    )
    _exercise_recovery_owned_candidate_failure_retains_worker_for_terminal_retry(
        monkeypatch, tmp_path
    )


def test_persistent_autonomous_cleanup_fault_retries_with_fresh_bounded_budgets(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(lsp_process_limits, "GRACEFUL_CLEANUP_SECONDS", 0.02)
    monkeypatch.setattr(lsp_process_limits, "RECOVERY_RETRY_SECONDS", 0.04, raising=False)
    process = _start(tmp_path, "--lifecycle")
    coordinator = process._coordinator
    recovery = coordinator.recovery_thread
    heartbeat = coordinator.heartbeat_thread
    generation = coordinator.active
    assert None not in (recovery, heartbeat, generation)
    held = threading.Event()
    release = threading.Event()
    first_cleanup = threading.Event()
    terminal_attempts: list[tuple[float, float]] = []
    terminal_failure = lsp_process_cleanup.terminal_failure_lsp_process

    def hold_transition() -> None:
        with coordinator.condition:
            held.set()
            assert release.wait(_HOLD_SECONDS)

    holder = threading.Thread(target=hold_transition)

    def fail_restart(_instance: LspProcess, deadline: float) -> None:
        holder.start()
        assert held.wait(_BARRIER_SECONDS)
        threading.Event().wait(max(0.0, deadline - time.monotonic()) + 0.005)
        raise RuntimeError("persistent autonomous restart failure")

    def observe_terminal_failure(
        instance: LspProcess,
        code: str,
        deadline: float,
    ) -> None:
        terminal_attempts.append((time.monotonic(), deadline))
        first_cleanup.set()
        terminal_failure(instance, code, deadline)

    monkeypatch.setattr(lsp_process_restart, "restart_generation", fail_restart)
    monkeypatch.setattr(
        lsp_process_cleanup,
        "terminal_failure_lsp_process",
        observe_terminal_failure,
    )

    try:
        assert lsp_process_state.queue_generation_failure(
            coordinator, generation, "injected persistent restart failure"
        )
        assert first_cleanup.wait(SHORT_TIMEOUT)
        # Retries are spaced by the recovery interval, so a fixed wait counts
        # how fast the machine is rather than how the retry behaves. Wait for
        # the second attempt instead, and bound the count by the time actually
        # spent waiting, which still catches a runaway loop.
        waiting_since = time.monotonic()
        assert _poll_until(lambda: len(terminal_attempts) >= 2, 5)
        elapsed = time.monotonic() - waiting_since
        attempts = list(terminal_attempts)
        assert len(attempts) <= int(elapsed / lsp_process_limits.RECOVERY_RETRY_SECONDS) + 3
        assert _all_deadlines_ahead(attempts)
        assert recovery.is_alive()
        assert heartbeat.is_alive()
        assert coordinator.recovery_thread is recovery
        assert coordinator.heartbeat_thread is heartbeat
        assert coordinator.cleanup_result.ownership_pending is True
        assert (process.owner_root / "lease.json").is_file()

        release.set()
        holder.join(SHORT_TIMEOUT)
        assert _coordinator_wait(
            process,
            lambda: coordinator.phase is lsp_process_state.LifecyclePhase.STOPPED_FAILURE,
            timeout=5,
        )
        assert not (process.owner_root / "lease.json").exists()
    finally:
        release.set()
        holder.join(SHORT_TIMEOUT)
        # The injected fault and the 0.02 s cleanup budget are the subject of
        # the test, not of its teardown. Undo them first, and tolerate the
        # background failure the coordinator already recorded — on a slow
        # machine `close` re-raises it and buries the real result.
        monkeypatch.undo()
        with contextlib.suppress(RuntimeError):
            _close_if_owned(process, coordinator)


def test_second_explicit_restart_failure_with_held_transition_lock_is_sticky(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    process = _start(tmp_path, "--lifecycle")
    process.restart(time.monotonic() + 5)
    coordinator = process._coordinator
    generation_nonce = process.generation_nonce
    server_pid = process.process.pid
    held = threading.Event()
    seize = threading.Event()
    release = threading.Event()
    restart_thread = threading.current_thread()
    release_lifecycle = lsp_process_state.release_lifecycle
    armed = True

    def hold_transition() -> None:
        assert seize.wait(_BARRIER_SECONDS)
        with coordinator.condition:
            held.set()
            release.wait(_HOLD_SECONDS)

    def release_then_contend(current: lsp_process_state.LifecycleCoordinator) -> None:
        nonlocal armed
        release_lifecycle(current)
        if current is coordinator and armed and threading.current_thread() is restart_thread:
            armed = False
            seize.set()
            assert held.wait(_BARRIER_SECONDS)

    holder = threading.Thread(target=hold_transition)
    holder.start()
    monkeypatch.setattr(lsp_process_state, "release_lifecycle", release_then_contend)

    try:
        with pytest.raises(TimeoutError, match="lifecycle transition lock"):
            process.restart(time.monotonic() + 0.1)

        assert coordinator.mandatory_failure_intent == lsp_process_state.FailureEvidenceIdentity(
            "process_exited",
            process.owner_nonce,
            generation_nonce,
            server_pid,
        )
        assert coordinator.phase is not lsp_process_state.LifecyclePhase.STOPPED_SUCCESS
        assert lsp_process_state.coordinator_has_ownership(coordinator)

        release.set()
        holder.join(SHORT_TIMEOUT)
        process.close(time.monotonic() + 5)

        failure = json.loads((process.owner_root / "failure.json").read_bytes())
        lsp_process_state.validate_failure_record(
            failure,
            code="process_exited",
            owner_nonce=process.owner_nonce,
            generation_nonce=generation_nonce,
            pid=server_pid,
        )
        assert coordinator.phase is lsp_process_state.LifecyclePhase.STOPPED_FAILURE
        assert process.owner_root.is_dir()
        assert not (process.owner_root / "lease.json").exists()
    finally:
        release.set()
        holder.join(SHORT_TIMEOUT)
        if lsp_process_state.coordinator_has_ownership(coordinator):
            process.close(time.monotonic() + 5)
