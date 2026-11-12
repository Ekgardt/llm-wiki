"""Windows child close, failure flushing and terminal identity of an LSP child."""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

import pytest
import windows_workspace
from llm_wiki import (
    lsp_process_failure,
    lsp_process_limits,
    lsp_process_owner,
    lsp_process_state,
)
from lsp_process import (
    ProcessState,
)

from tests.lsp_process_helpers import (
    _no_lsp_lifecycle_owner_leaks,  # noqa: F401
    )
from tests.lsp_process_scenario_helpers import (
    OWNER_NONCE,
    _await_descendant_pid,
    _await_pid_exit,
    _cleanup_error_steps,
    _mock_windows_artifact_owner,
    _pid_alive,
    _remove_temporaries,
    _start,
)
from tests.slow_machine import LONG_TIMEOUT, SHORT_TIMEOUT


def _await_records(records: list, count: int, timeout: float) -> None:
    """Wait for a worker to publish `count` records, bounded by the timeout."""
    deadline = time.monotonic() + timeout
    while len(records) < count and time.monotonic() < deadline:
        time.sleep(0.01)


def _entry_names(directory: Path) -> set[str]:
    return {path.name for path in directory.iterdir()}


def test_heartbeat_refreshes_lease_while_tree_cleanup_driver_is_blocked(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(lsp_process_limits, "LEASE_EXPIRY_SECONDS", 0.06)
    monkeypatch.setattr(lsp_process_limits, "GRACEFUL_CLEANUP_SECONDS", 0.05)
    pid_file = tmp_path / "blocked-cleanup-descendant.pid"
    process = _start(
        tmp_path,
        "--lifecycle",
        "--ignore-shutdown",
        "--spawn-descendant",
        "--descendant-pid-file",
        str(pid_file),
    )
    descendant = _await_descendant_pid(pid_file)
    coordinator = process._coordinator
    lsp_process_failure.stop_recovery_owner(coordinator, time.monotonic() + 1)
    generation = coordinator.active
    assert generation is not None
    assert generation.tree is not None
    tree = generation.tree
    terminate = lsp_process_state.ProcessTree.terminate
    entered = threading.Event()
    release = threading.Event()
    cleanup_errors: list[BaseException] = []
    heartbeat_records: list[dict[str, object]] = []
    lease_path = process.owner_root / "lease.json"
    initial = json.loads(lease_path.read_bytes())
    write_lease = lsp_process_owner.OwnerDirectory.write_lease

    def observe_heartbeat(current: object, record: object, **kwargs: object) -> None:
        assert isinstance(record, dict)
        if threading.current_thread() is coordinator.heartbeat_thread:
            heartbeat_records.append(dict(record))
        write_lease(current, record, **kwargs)  # type: ignore[arg-type]

    def blocked_terminate(current: object, *, deadline: float) -> None:
        if current is tree and not release.is_set():
            entered.set()
            release.wait(max(0.0, deadline - time.monotonic()))
            raise TimeoutError("tree termination stayed blocked")
        terminate(current, deadline=deadline)  # type: ignore[arg-type]

    def fail_terminally() -> None:
        try:
            process._terminal_failure("injected_failure", time.monotonic() + 2)
        except BaseException as error:
            cleanup_errors.append(error)

    monkeypatch.setattr(lsp_process_state.ProcessTree, "terminate", blocked_terminate)
    monkeypatch.setattr(
        lsp_process_owner.OwnerDirectory, "write_lease", observe_heartbeat
    )
    monkeypatch.setattr(lsp_process_limits, "HEARTBEAT_SECONDS", 0.02)
    coordinator.heartbeat_wake.set()
    cleanup = threading.Thread(target=fail_terminally)
    cleanup.start()
    assert entered.wait(LONG_TIMEOUT), cleanup_errors
    _await_records(heartbeat_records, 2, 120)
    lsp_process_state.acquire_lease(coordinator, time.monotonic() + 1)
    try:
        during = json.loads(lease_path.read_bytes())
    finally:
        lsp_process_state.release_lease(coordinator)
    descendant_live = _pid_alive(descendant)
    try:
        assert len(heartbeat_records) >= 2, (
            coordinator.heartbeat_thread,
            coordinator.heartbeat_stop.is_set(),
            coordinator.background_cleanup_error,
            coordinator.phase,
        )
        assert during["heartbeat_at"] != initial["heartbeat_at"]
        assert descendant_live is True
        assert coordinator.cleanup_result.ownership_pending is True
    finally:
        release.set()
        cleanup.join(SHORT_TIMEOUT)
        monkeypatch.setattr(lsp_process_state.ProcessTree, "terminate", terminate)
        monkeypatch.setattr(lsp_process_owner.OwnerDirectory, "write_lease", write_lease)
        process.close(time.monotonic() + 5)

    assert not cleanup.is_alive()
    assert any("tree termination stayed blocked" in str(error) for error in cleanup_errors)
    _await_pid_exit(descendant)
    assert not _pid_alive(descendant)


# The write each artifact kind exercises on the owner directory.
_MOCK_WINDOWS_ARTIFACT_WRITES = {
    "owner": lambda owner: owner.write_record("owner.json", {"state": "running"}),
    "failure": lambda owner: owner.write_record("failure.json", {"code": "injected"}),
    "lease": lambda owner: owner.write_lease({"state": "live"}),
}


def _exercise_mock_windows_artifact_write(
    owner: lsp_process_owner.OwnerDirectory,
    artifact: str,
) -> None:
    write = _MOCK_WINDOWS_ARTIFACT_WRITES.get(artifact)
    if write is None:  # pragma: no cover - parametrization is closed below
        raise AssertionError(artifact)
    write(owner)


def _failed_artifact_write_messages(
    owner: lsp_process_owner.OwnerDirectory, artifact: str, *, attempts: int
) -> list[str]:
    errors: list[str] = []
    for _attempt in range(attempts):
        with pytest.raises(OSError) as raised:
            _exercise_mock_windows_artifact_write(owner, artifact)
        errors.append(str(raised.value))
    return errors


def _expect_failure_evidence_fsync_error(coordinator, code: str) -> None:
    with pytest.raises(OSError, match="owner directory fsync failed"):
        lsp_process_failure.ensure_failure_evidence(
            None,
            coordinator,
            code,
            time.monotonic() + 1,
        )


@pytest.mark.skipif(os.name != "nt", reason="Windows native create boundary")
@pytest.mark.parametrize("artifact", ["failure", "lease"])
def test_windows_post_create_identity_failure_retains_one_temp_intent_until_retry(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    artifact: str,
) -> None:
    owner_root = tmp_path / OWNER_NONCE
    owner = lsp_process_owner.OwnerDirectory.open(owner_root)
    owner.create(time.monotonic() + 1)
    workspace = windows_workspace
    real_create = workspace.create_file
    real_identity = workspace.identity
    real_delete = workspace.delete_handle
    create_active = False
    cleanup_allowed = False
    create_calls: list[str] = []
    temporary_prefix = ".evidence-" if artifact == "failure" else ".lease-"

    def identity(handle: int, *, directory: bool) -> object:
        if create_active and not directory:
            raise OSError("temporary identity validation failed")
        return real_identity(handle, directory=directory)

    def create_file(parent: int, name: str) -> int:
        nonlocal create_active
        create_calls.append(name)
        create_active = True
        try:
            return real_create(parent, name)
        finally:
            create_active = False

    def delete_handle(handle: int) -> None:
        if not cleanup_allowed:
            raise OSError("temporary cleanup blocked")
        real_delete(handle)

    monkeypatch.setattr(workspace, "identity", identity)
    monkeypatch.setattr(workspace, "create_file", create_file)
    monkeypatch.setattr(workspace, "delete_handle", delete_handle)

    try:
        errors = _failed_artifact_write_messages(owner, artifact, attempts=2)

        assert errors == ["temporary cleanup blocked"] * 2
        assert len(create_calls) == 1
        pending = list(owner._pending_temp_names)
        assert pending == create_calls
        assert _entry_names(owner_root) == {
            "cancellation",
            pending[0],
        }

        with pytest.raises(OSError, match="temporary cleanup blocked"):
            owner.close()
        assert owner._pending_temp_names == pending
        assert owner.owner_handle is not None
        assert owner.parent_handle >= 0
        assert owner._closed is False

        cleanup_allowed = True
        owner._retry_pending_temp_names()
        assert owner._pending_temp_names == []
        assert _entry_names(owner_root) == {"cancellation"}

        owner.remove_success_scratch()
        owner.close()
        assert owner._closed is True
        assert not owner_root.exists()
    finally:
        cleanup_allowed = True
        _remove_temporaries(owner_root, f"{temporary_prefix}*.tmp")
        if not owner._closed:
            owner.remove_success_scratch()
            owner.close()


@pytest.mark.skipif(os.name != "nt", reason="Windows native create boundary")
@pytest.mark.parametrize("artifact", ["failure", "lease"])
def test_windows_precreate_failure_proves_absence_and_clears_temp_intent(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    artifact: str,
) -> None:
    owner_root = tmp_path / OWNER_NONCE
    owner = lsp_process_owner.OwnerDirectory.open(owner_root)
    owner.create(time.monotonic() + 1)
    workspace = windows_workspace
    real_open = workspace.open_deletable_file
    attempted_names: list[str] = []
    cleanup_probes: list[str] = []

    def fail_before_create(_parent: int, name: str) -> int:
        attempted_names.append(name)
        raise OSError("temporary pre-create failure")

    def open_deletable_file(parent: int, name: str) -> int:
        cleanup_probes.append(name)
        return real_open(parent, name)

    monkeypatch.setattr(workspace, "create_file", fail_before_create)
    monkeypatch.setattr(workspace, "open_deletable_file", open_deletable_file)

    try:
        with pytest.raises(OSError, match="temporary pre-create failure"):
            _exercise_mock_windows_artifact_write(owner, artifact)

        assert len(attempted_names) == 1
        assert cleanup_probes == attempted_names
        assert owner._pending_temp_names == []
        assert _entry_names(owner_root) == {"cancellation"}

        owner.remove_success_scratch()
        owner.close()
        assert owner._closed is True
        assert not owner_root.exists()
    finally:
        if not owner._closed:
            owner.remove_success_scratch()
            owner.close()


@pytest.mark.skipif(os.name != "nt", reason="Windows child-handle retry boundary")
@pytest.mark.parametrize("artifact", ["owner", "failure", "lease"])
@pytest.mark.parametrize("published", [False, True])
def test_windows_child_close_failure_is_retained_and_retried_before_owner_close(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    artifact: str,
    published: bool,
) -> None:
    close_calls: list[int] = []
    child_close_attempts = 0

    def close_handle(handle: int) -> None:
        nonlocal child_close_attempts
        close_calls.append(handle)
        if handle == 303:
            child_close_attempts += 1
            if child_close_attempts == 1:
                raise OSError("child handle close failed")

    owner = _mock_windows_artifact_owner(monkeypatch, tmp_path, close_handle)
    if not published:
        monkeypatch.setattr(
            windows_workspace,
            "write_all",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                OSError("artifact write failed")
            ),
        )

    with pytest.raises(OSError, match="child handle close failed"):
        _exercise_mock_windows_artifact_write(owner, artifact)

    assert owner._pending_child_handles == [303]
    owner.close()
    assert close_calls == [303, 303, 202, 101]
    assert owner._pending_child_handles == []
    assert owner._closed is True


@pytest.mark.skipif(os.name != "nt", reason="Windows child-handle retry boundary")
@pytest.mark.parametrize("artifact", ["owner", "failure", "lease"])
@pytest.mark.parametrize("published", [False, True])
def test_persistent_windows_child_close_failure_blocks_owner_release_until_retry(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    artifact: str,
    published: bool,
) -> None:
    child_close_allowed = False
    close_calls: list[int] = []
    create_calls: list[str] = []

    def close_handle(handle: int) -> None:
        close_calls.append(handle)
        if handle == 303 and not child_close_allowed:
            raise OSError("child handle close still failed")

    owner = _mock_windows_artifact_owner(
        monkeypatch, tmp_path, close_handle, create_calls
    )
    if not published:
        monkeypatch.setattr(
            windows_workspace,
            "write_all",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                OSError("artifact write failed")
            ),
        )
    with pytest.raises(OSError, match="child handle close still failed"):
        _exercise_mock_windows_artifact_write(owner, artifact)
    with pytest.raises(OSError, match="child handle close still failed"):
        _exercise_mock_windows_artifact_write(owner, artifact)
    with pytest.raises(OSError, match="child handle close still failed"):
        owner.close()

    assert owner.owner_handle == 202
    assert owner.parent_handle == 101
    assert owner._pending_child_handles == [303]
    assert len(create_calls) == 1
    assert close_calls == [303, 303, 303]
    child_close_allowed = True
    owner.close()
    assert close_calls == [303, 303, 303, 303, 202, 101]
    assert owner._pending_child_handles == []
    assert owner._closed is True


@pytest.mark.skipif(os.name != "nt", reason="Windows child-handle retry boundary")
def test_multiple_windows_child_close_failures_are_all_retained_for_retry(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    child_close_allowed = False
    close_calls: list[int] = []

    def close_handle(handle: int) -> None:
        close_calls.append(handle)
        if handle in {303, 404} and not child_close_allowed:
            raise OSError(f"child handle {handle} close failed")

    owner = _mock_windows_artifact_owner(monkeypatch, tmp_path, close_handle)

    with pytest.raises(OSError, match="303"):
        owner._close_child_handles(303, 404)

    assert close_calls == [303, 404]
    assert owner._pending_child_handles == [303, 404]
    child_close_allowed = True
    owner.close()
    assert close_calls == [303, 404, 303, 404, 202, 101]
    assert owner._pending_child_handles == []
    assert owner._closed is True


def test_first_terminal_identity_survives_later_heartbeat_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    # This scenario requires cleanup to complete before checking identity.
    # Separate timeout tests retain the actual short cleanup deadlines.
    monkeypatch.setattr(lsp_process_limits, "GRACEFUL_CLEANUP_SECONDS", LONG_TIMEOUT)
    process = _start(tmp_path, "--lifecycle")
    coordinator = process._coordinator
    owner = coordinator.owner_directory
    heartbeat = coordinator.heartbeat_thread
    assert owner is not None and heartbeat is not None
    generation_nonce = process.generation_nonce
    server_pid = process.process.pid
    attempted = threading.Event()
    real_write_lease = lsp_process_owner.OwnerDirectory.write_lease

    def fail_heartbeat(current: object, record: object, **kwargs: object) -> None:
        if current is owner and threading.current_thread() is heartbeat:
            attempted.set()
            raise OSError("later heartbeat failed")
        real_write_lease(current, record, **kwargs)  # type: ignore[arg-type]

    lsp_process_failure.mark_terminal_failure(
        process,
        coordinator,
        "injected_failure",
        time.monotonic() + LONG_TIMEOUT,
    )
    monkeypatch.setattr(lsp_process_owner.OwnerDirectory, "write_lease", fail_heartbeat)
    coordinator.heartbeat_wake.set()
    assert attempted.wait(SHORT_TIMEOUT)
    heartbeat.join(SHORT_TIMEOUT)
    monkeypatch.setattr(lsp_process_owner.OwnerDirectory, "write_lease", real_write_lease)
    process.close(time.monotonic() + LONG_TIMEOUT)

    failure = json.loads((process.owner_root / "failure.json").read_bytes())
    assert coordinator.terminal_code == "injected_failure"
    assert failure["code"] == "injected_failure"
    assert failure["generation_nonce"] == generation_nonce
    assert failure["server_pid"] == server_pid


@pytest.mark.skipif(os.name != "nt", reason="Windows directory flush boundary")
@pytest.mark.parametrize("artifact", ["failure", "lease"])
def test_windows_artifact_directory_flush_false_is_not_success(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    artifact: str,
) -> None:
    owner = _mock_windows_artifact_owner(monkeypatch, tmp_path, lambda _handle: None)
    flushes: list[int] = []
    deleted: list[int] = []

    def reject_directory_flush(handle: int) -> bool:
        flushes.append(handle)
        return False

    monkeypatch.setattr(
        windows_workspace,
        "flush_directory",
        reject_directory_flush,
    )
    monkeypatch.setattr(
        windows_workspace,
        "delete_handle",
        lambda handle: deleted.append(handle),
    )

    with pytest.raises(OSError, match="directory.*flush"):
        _exercise_mock_windows_artifact_write(owner, artifact)

    assert flushes == [202]
    assert deleted == []
    owner.close()


@pytest.mark.skipif(os.name != "nt", reason="Windows directory flush boundary")
def test_windows_failure_flush_false_retains_cleanup_until_barrier_retry(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    process = _start(tmp_path, "--lifecycle", "--ignore-shutdown")
    coordinator = process._coordinator
    owner = coordinator.owner_directory
    assert owner is not None and owner.owner_handle is not None
    real_flush = windows_workspace.flush_directory
    allow_flush = False
    attempts = 0

    def controlled_flush(handle: int) -> bool:
        nonlocal attempts
        if handle == owner.owner_handle:
            attempts += 1
            if not allow_flush:
                return False
        return real_flush(handle)

    monkeypatch.setattr(
        windows_workspace,
        "flush_directory",
        controlled_flush,
    )
    try:
        with pytest.raises(OSError, match="directory.*flush"):
            process._terminal_failure("injected_failure", time.monotonic() + 5)

        failure = process.owner_root / "failure.json"
        assert failure.is_file()
        assert coordinator.cleanup_result.evidence == "failed"
        assert coordinator.cleanup_result.ownership_pending is True
        assert coordinator.phase is lsp_process_state.LifecyclePhase.CLEANUP_PENDING
        assert coordinator.owner_directory is owner
        assert process.state is ProcessState.DEGRADED
        assert (process.owner_root / "lease.json").is_file()
        assert ("evidence", "OSError") in _cleanup_error_steps(coordinator)
        attempts_before_retry = attempts

        allow_flush = True
        process.close(time.monotonic() + 5)

        assert attempts > attempts_before_retry
        assert process.state is ProcessState.FAILED
        assert coordinator.cleanup_result.evidence == "success"
        assert not (process.owner_root / "lease.json").exists()
    finally:
        allow_flush = True
        if lsp_process_state.coordinator_has_ownership(coordinator):
            process.close(time.monotonic() + 5)


@pytest.mark.skipif(os.name != "posix", reason="POSIX directory fsync boundary")
@pytest.mark.parametrize("persistent", [False, True], ids=["one-shot", "persistent"])
def test_posix_failure_retry_repeats_directory_fsync_before_success(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    persistent: bool,
) -> None:
    owner_root = tmp_path / OWNER_NONCE
    owner = lsp_process_owner.OwnerDirectory.open(owner_root)
    owner.create(time.monotonic() + 1)
    assert owner.owner_handle is not None
    owner_handle = owner.owner_handle
    identity = lsp_process_state.FailureEvidenceIdentity(
        "injected_failure",
        OWNER_NONCE,
        "b" * 32,
        4242,
    )
    coordinator = lsp_process_state.LifecycleCoordinator(
        owner,
        startup_generation_nonce=identity.generation_nonce,
    )
    coordinator.terminal_outcome = "failure"
    coordinator.terminal_code = identity.code
    coordinator.failure_evidence_identity = identity
    coordinator.phase = lsp_process_state.LifecyclePhase.STOPPING_FAILURE
    real_fsync = os.fsync
    keep_failing = persistent
    attempts = 0

    def controlled_fsync(descriptor: int) -> None:
        nonlocal attempts
        if descriptor == owner_handle:
            attempts += 1
            if keep_failing or attempts == 1:
                raise OSError("owner directory fsync failed")
        real_fsync(descriptor)

    monkeypatch.setattr(os, "fsync", controlled_fsync)
    try:
        _expect_failure_evidence_fsync_error(coordinator, identity.code)

        assert attempts == 1
        assert coordinator.cleanup_result.evidence != "success"
        assert (owner_root / "failure.json").is_file()
        if persistent:
            _expect_failure_evidence_fsync_error(coordinator, identity.code)
            assert attempts == 2
            assert coordinator.cleanup_result.evidence != "success"
            keep_failing = False

        lsp_process_failure.ensure_failure_evidence(
            None,
            coordinator,
            identity.code,
            time.monotonic() + 1,
        )
        assert attempts == (3 if persistent else 2)
        assert coordinator.cleanup_result.evidence == "success"
        assert not list(owner_root.glob(".evidence-*.tmp"))
    finally:
        monkeypatch.setattr(os, "fsync", real_fsync)
        owner.close()
