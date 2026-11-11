"""An incomplete startup is held in a bounded cleanup registry until it is cleaned."""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from pathlib import Path

import lsp_process_tree
import lsp_protocol
import pytest
from llm_wiki import (
    lsp_process_cleanup,
    lsp_process_generation,
    lsp_process_limits,
    lsp_process_owner,
    lsp_process_recovery,
    lsp_process_state,
)
from lsp_process import (
    LspProcess,
)

from tests.lsp_process_helpers import (
    _no_lsp_lifecycle_owner_leaks,  # noqa: F401
    )
from tests.lsp_process_scenario_helpers import OWNER_NONCE, _command, _start, _windows_handle_status
from tests.slow_machine import SHORT_TIMEOUT


def _all_messages_equal(errors, message: str) -> bool:
    return all(str(error) == message for error in errors)


def _owned_coordinators(coordinators) -> list:
    return [
        coordinator
        for coordinator in coordinators
        if lsp_process_state.coordinator_has_ownership(coordinator)
    ]


def _unregister_all(coordinators) -> None:
    for coordinator in coordinators:
        lsp_process_state.unregister_startup_cleanup(coordinator)


def _unregister_new_cleanup_owners(baseline: set[int]) -> None:
    for coordinator in lsp_process_state.pending_startup_cleanup_snapshot():
        if id(coordinator) not in baseline:
            lsp_process_state.unregister_startup_cleanup(coordinator)


def _pending_cleanup_ids() -> set[int]:
    return {id(item) for item in lsp_process_state.pending_startup_cleanup_snapshot()}


def _collect_start_failure(failures: list, tmp_path: Path, nonce: int) -> None:
    try:
        LspProcess.start(
            _command(), cwd=tmp_path, owner_root=tmp_path / f"{nonce:032x}"
        )
    except BaseException as error:  # noqa: BLE001 - the failure is the subject
        failures.append(error)


def _collect_lifecycle_start_failure(failures: list, tmp_path: Path, owner: Path) -> None:
    try:
        LspProcess.start(_command("--lifecycle"), cwd=tmp_path, owner_root=owner)
    except BaseException as error:
        failures.append(error)


def _retry_owned_startup_cleanup(coordinator) -> None:
    if lsp_process_state.coordinator_has_ownership(coordinator):
        lsp_process_cleanup.retry_startup_cleanup(
            coordinator,
            time.monotonic() + 5,
        )


def test_startup_failure_terminates_child_and_retains_bounded_evidence(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    children: list[subprocess.Popen[bytes]] = []
    real_popen = subprocess.Popen

    def popen_spy(*args: object, **kwargs: object) -> subprocess.Popen[bytes]:
        child = real_popen(*args, **kwargs)
        children.append(child)
        return child

    class BrokenProtocol:
        def __init__(self, *args: object, **kwargs: object) -> None:
            raise RuntimeError("protocol startup failed")

    monkeypatch.setattr(lsp_process_state.subprocess, "Popen", popen_spy)
    monkeypatch.setattr(lsp_process_generation, "LspProtocol", BrokenProtocol)
    owner = tmp_path / OWNER_NONCE
    with pytest.raises(RuntimeError, match="protocol startup failed"):
        LspProcess.start(
            _command("--exit-while-pending"), cwd=tmp_path, owner_root=owner
        )
    assert len(children) == 1
    children[0].wait(timeout=SHORT_TIMEOUT)
    assert children[0].poll() is not None
    if os.name == "nt":
        assert _windows_handle_status(int(children[0]._handle)) == (False, 6)
    assert set(path.name for path in owner.iterdir()) == {
        "cancellation",
        "failure.json",
        "owner.json",
    }
    assert json.loads((owner / "owner.json").read_bytes())["state"] == "process_running"
    assert json.loads((owner / "failure.json").read_bytes())["code"] == "startup_failed"


def test_stderr_thread_start_failure_retains_evidence_without_masking_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    real_start = threading.Thread.start

    def fail_stderr_start(thread: threading.Thread) -> None:
        if thread.name.startswith("lsp-stderr-"):
            raise RuntimeError("stderr thread start failed")
        real_start(thread)

    monkeypatch.setattr(threading.Thread, "start", fail_stderr_start)
    owner = tmp_path / OWNER_NONCE
    with pytest.raises(RuntimeError, match="stderr thread start failed"):
        LspProcess.start(_command(), cwd=tmp_path, owner_root=owner)
    assert owner.is_dir()
    assert not (owner / "owner.json").exists()
    failure = json.loads((owner / "failure.json").read_bytes())
    assert failure["code"] == "startup_failed"
    assert "server_pid" in failure


def test_startup_owner_is_registered_before_first_owned_mutation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    create = lsp_process_owner.OwnerDirectory.create
    registered: list[bool] = []

    def observe_create(owner: lsp_process_owner.OwnerDirectory, deadline: float) -> None:
        registered.append(
            any(
                coordinator.owner_directory is owner
                for coordinator in lsp_process_state.pending_startup_cleanup_snapshot()
            )
        )
        create(owner, deadline)

    monkeypatch.setattr(lsp_process_owner.OwnerDirectory, "create", observe_create)
    process = _start(tmp_path, "--lifecycle")
    try:
        assert registered == [True]
        assert lsp_process_state.pending_startup_cleanup_snapshot() == ()
    finally:
        process.close(time.monotonic() + 5)


def test_startup_cleanup_rethrows_interruption_and_retains_registry_owner(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    captured: list[lsp_process_state.LifecycleCoordinator] = []
    real_drive_cleanup = lsp_process_cleanup.drive_cleanup
    cleanup_calls = 0

    def fail_after_ownership(
        instance: LspProcess,
        _deadline: float | None = None,
    ) -> None:
        captured.append(instance._coordinator)
        raise RuntimeError("startup failed after ownership")

    def interrupt_cleanup(*_args: object, **_kwargs: object) -> list[BaseException]:
        nonlocal cleanup_calls
        cleanup_calls += 1
        if cleanup_calls == 1:
            raise KeyboardInterrupt("startup cleanup interrupted")
        return real_drive_cleanup(*_args, **_kwargs)

    monkeypatch.setattr(lsp_process_recovery, "start_lifecycle_workers", fail_after_ownership)
    monkeypatch.setattr(lsp_process_cleanup, "drive_cleanup", interrupt_cleanup)

    coordinator: lsp_process_state.LifecycleCoordinator | None = None
    try:
        with pytest.raises(KeyboardInterrupt, match="startup cleanup interrupted"):
            _start(tmp_path, "--lifecycle")

        assert len(captured) == 1
        coordinator = captured[0]
        assert lsp_process_state.coordinator_has_ownership(coordinator)
        assert coordinator in lsp_process_state.pending_startup_cleanup_snapshot()
    finally:
        if captured:
            coordinator = captured[0]
            _retry_owned_startup_cleanup(coordinator)
    assert coordinator is not None
    assert not lsp_process_state.coordinator_has_ownership(coordinator)
    assert coordinator not in lsp_process_state.pending_startup_cleanup_snapshot()


def test_startup_cleanup_rethrows_returned_interruption_after_retaining_owner(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    captured: list[lsp_process_state.LifecycleCoordinator] = []
    real_drive_cleanup = lsp_process_cleanup.drive_cleanup
    cleanup_calls = 0

    def fail_after_ownership(
        instance: LspProcess,
        _deadline: float | None = None,
    ) -> None:
        captured.append(instance._coordinator)
        raise RuntimeError("startup failed after ownership")

    def make_interruption() -> KeyboardInterrupt:
        try:
            raise KeyboardInterrupt("returned startup cleanup interruption")
        except KeyboardInterrupt as interruption:
            return interruption

    interruption = make_interruption()

    def return_interruption(*args: object, **kwargs: object) -> list[BaseException]:
        nonlocal cleanup_calls
        cleanup_calls += 1
        if cleanup_calls == 1:
            return [interruption]
        return real_drive_cleanup(*args, **kwargs)

    monkeypatch.setattr(lsp_process_recovery, "start_lifecycle_workers", fail_after_ownership)
    monkeypatch.setattr(lsp_process_cleanup, "drive_cleanup", return_interruption)

    coordinator: lsp_process_state.LifecycleCoordinator | None = None
    try:
        with pytest.raises(
            KeyboardInterrupt,
            match="returned startup cleanup interruption",
        ) as raised:
            _start(tmp_path, "--lifecycle")

        assert raised.value is interruption
        assert isinstance(raised.value.__cause__, RuntimeError)
        assert str(raised.value.__cause__) == "startup failed after ownership"
        traceback_names: list[str] = []
        current = raised.value.__traceback__
        while current is not None:
            traceback_names.append(current.tb_frame.f_code.co_name)
            current = current.tb_next
        assert "make_interruption" in traceback_names
        assert len(captured) == 1
        coordinator = captured[0]
        assert lsp_process_state.coordinator_has_ownership(coordinator)
        assert coordinator in lsp_process_state.pending_startup_cleanup_snapshot()
    finally:
        if captured:
            coordinator = captured[0]
            _retry_owned_startup_cleanup(coordinator)
    assert coordinator is not None
    assert not lsp_process_state.coordinator_has_ownership(coordinator)
    assert coordinator not in lsp_process_state.pending_startup_cleanup_snapshot()


def _exercise_owner_open_failures_never_consume_startup_cleanup_registry(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    baseline = _pending_cleanup_ids()
    opened: list[Path] = []
    failures: list[BaseException] = []

    def fail_open(
        _cls: type[lsp_process_owner.OwnerDirectory], owner_root: Path
    ) -> lsp_process_owner.OwnerDirectory:
        opened.append(owner_root)
        raise OSError("owner directory open failed")

    def fail_cleanup(*_args: object, **_kwargs: object) -> list[BaseException]:
        raise RuntimeError("startup cleanup also failed")

    monkeypatch.setattr(
        lsp_process_owner.OwnerDirectory,
        "open",
        classmethod(fail_open),
    )
    monkeypatch.setattr(lsp_process_cleanup, "drive_cleanup", fail_cleanup)
    try:
        for index in range(lsp_process_limits.MAX_STARTUP_CLEANUP_OWNERS + 1):
            _collect_start_failure(failures, tmp_path, index + 200)

        assert len(opened) == lsp_process_limits.MAX_STARTUP_CLEANUP_OWNERS + 1
        assert len(failures) == len(opened)
        assert all(type(error) is OSError for error in failures)
        assert all(str(error) == "owner directory open failed" for error in failures)
        assert _pending_cleanup_ids() == baseline
    finally:
        _unregister_new_cleanup_owners(baseline)


def test_protocol_startup_cleanup_owner_is_adopted_for_coordinator_retry(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    class RetainedProtocol:
        def __init__(self) -> None:
            self.released = False
            self.stop_calls = 0
            self.finish_calls = 0

        def _stop_io_for_process_cleanup(self) -> None:
            self.stop_calls += 1

        def _finish_io_after_process_exit(self, _deadline: float) -> None:
            self.finish_calls += 1
            if not self.released:
                raise TimeoutError("protocol startup owner blocked")

    retained = RetainedProtocol()

    def fail_protocol(*_args: object, **_kwargs: object) -> None:
        error = lsp_protocol.ProtocolStartupCleanupError(
            retained,  # type: ignore[arg-type]
            (TimeoutError("protocol startup owner blocked"),),
        )
        raise error from RuntimeError("protocol startup failed")

    monkeypatch.setattr(lsp_process_generation, "LspProtocol", fail_protocol)
    owner = tmp_path / OWNER_NONCE

    with pytest.raises(lsp_process_cleanup.StartupCleanupError) as raised:
        LspProcess.start(_command(), cwd=tmp_path, owner_root=owner)

    coordinator = raised.value.coordinator
    assert coordinator is not None
    generations = [
        coordinator.active,
        coordinator.candidate,
        *coordinator.retired,
    ]
    assert any(
        generation is not None and generation.protocol is retained
        for generation in generations
    )
    assert retained.stop_calls == 1
    assert retained.finish_calls == 1
    assert coordinator.phase is lsp_process_state.LifecyclePhase.CLEANUP_PENDING
    assert lsp_process_state.coordinator_has_ownership(coordinator)
    assert coordinator in lsp_process_state.pending_startup_cleanup_snapshot()

    retained.released = True
    raised.value.retry_cleanup(time.monotonic() + 5)
    assert not lsp_process_state.coordinator_has_ownership(coordinator)
    assert coordinator not in lsp_process_state.pending_startup_cleanup_snapshot()


def test_released_startup_failures_do_not_exhaust_cleanup_registry(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    captured: list[lsp_process_state.LifecycleCoordinator] = []
    failures: list[BaseException] = []

    def fail_workers(
        instance: LspProcess,
        _deadline: float | None = None,
    ) -> None:
        coordinator = instance._coordinator
        protocol = instance.protocol
        stop_io = protocol._stop_io_for_process_cleanup
        first = True

        def transient_stop_error() -> None:
            nonlocal first
            if first:
                first = False
                raise OSError("transient protocol stop failure")
            stop_io()

        captured.append(coordinator)
        monkeypatch.setattr(protocol, "_stop_io_for_process_cleanup", transient_stop_error)
        raise RuntimeError("startup worker failed after ownership was released")

    monkeypatch.setattr(lsp_process_recovery, "start_lifecycle_workers", fail_workers)
    try:
        for index in range(lsp_process_limits.MAX_STARTUP_CLEANUP_OWNERS + 1):
            owner = tmp_path / f"{index + 100:032x}"
            _collect_lifecycle_start_failure(failures, tmp_path, owner)

        assert len(captured) == lsp_process_limits.MAX_STARTUP_CLEANUP_OWNERS + 1
        assert len(failures) == len(captured)
        assert all(type(error) is RuntimeError for error in failures)
        assert _all_messages_equal(
            failures, "startup worker failed after ownership was released"
        )
        assert not _owned_coordinators(captured)
        assert lsp_process_state.pending_startup_cleanup_snapshot() == ()
    finally:
        _unregister_all(captured)
    _exercise_owner_open_failures_never_consume_startup_cleanup_registry(
        monkeypatch, tmp_path
    )


def test_startup_registry_atexit_hook_retries_owned_cleanup(tmp_path: Path) -> None:
    owner = lsp_process_owner.OwnerDirectory.open(tmp_path / OWNER_NONCE)
    coordinator = lsp_process_state.LifecycleCoordinator(
        owner,
        startup_generation_nonce="b" * 32,
    )
    lsp_process_state.register_startup_cleanup(coordinator)
    owner.create(time.monotonic() + 1)

    lsp_process_cleanup.atexit_cleanup_startups()

    failure = json.loads((owner.owner_root / "failure.json").read_bytes())
    assert failure["code"] == "startup_failed"
    assert not lsp_process_state.coordinator_has_ownership(coordinator)
    assert coordinator not in lsp_process_state.pending_startup_cleanup_snapshot()


def test_startup_cleanup_registry_rejects_unbounded_ownership() -> None:
    coordinators = [
        lsp_process_state.LifecycleCoordinator(None)
        for _ in range(lsp_process_limits.MAX_STARTUP_CLEANUP_OWNERS + 1)
    ]
    try:
        for coordinator in coordinators[:-1]:
            lsp_process_state.register_startup_cleanup(coordinator)
        with pytest.raises(RuntimeError, match="registry bound"):
            lsp_process_state.register_startup_cleanup(coordinators[-1])
    finally:
        for coordinator in coordinators:
            lsp_process_state.unregister_startup_cleanup(coordinator)


@pytest.mark.skipif(os.name != "nt", reason="Windows pre-child Job ownership")
def test_process_startup_adopts_unattached_job_for_cleanup_retry(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    fault_enabled = True
    close_calls: list[int] = []

    def fail_spawn(*_args: object, **_kwargs: object) -> None:
        error = lsp_process_tree.ProcessTreeSpawnError(
            None,
            (OSError("job close failed"),),
            windows_job=37,
        )
        raise error from OSError("popen failed")

    def close_job(job: int) -> None:
        close_calls.append(job)
        if fault_enabled:
            raise OSError("job close failed")

    monkeypatch.setattr(
        lsp_process_state.ProcessTree,
        "_spawn_with_deadline",
        classmethod(lambda _cls, *args, **kwargs: fail_spawn(*args, **kwargs)),
    )
    monkeypatch.setattr(
        lsp_process_tree,
        "close_windows_handle",
        close_job,
    )

    with pytest.raises(lsp_process_cleanup.StartupCleanupError) as raised:
        LspProcess.start(_command(), cwd=tmp_path, owner_root=tmp_path / OWNER_NONCE)

    coordinator = raised.value.coordinator
    assert coordinator is not None and coordinator.candidate is not None
    assert coordinator.candidate.windows_job == 37
    assert coordinator.phase is lsp_process_state.LifecyclePhase.CLEANUP_PENDING
    assert close_calls == [37]

    fault_enabled = False
    errors = lsp_process_cleanup.drive_cleanup(
        None,
        time.monotonic() + 5,
        terminal=True,
        failure_code="startup_failed",
        coordinator_override=coordinator,
    )
    assert errors == []
    assert close_calls == [37, 37]
    assert not lsp_process_state.coordinator_has_ownership(coordinator)
