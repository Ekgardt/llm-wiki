"""A startup that fails before it mutates anything leaves nothing behind."""

from __future__ import annotations

import io
import json
import os
import secrets
import stat
import subprocess
import threading
import time
from pathlib import Path

import lsp_protocol
import pytest
import windows_workspace
from llm_wiki import (
    lsp_process_cleanup,
    lsp_process_failure,
    lsp_process_generation,
    lsp_process_limits,
    lsp_process_owner,
    lsp_process_recovery,
    lsp_process_startup,
    lsp_process_state,
)
from lsp_process import (
    LspProcess,
    ProcessState,
)

from tests.lsp_process_helpers import (
    _no_lsp_lifecycle_owner_leaks,  # noqa: F401
    )
from tests.lsp_process_scenario_helpers import (
    _STARTUP_CEILING_SECONDS,
    OWNER_NONCE,
    _assert_lsp_acl_is_owner_only,
    _cleanup_error_types,
    _command,
    _coordinator_wait,
    _expect_active_generation_exit,
    _FakeTree,
    _start,
    _wait,
    _windows_handle_status,
)
from tests.slow_machine import SHORT_TIMEOUT


def _optional_json(path: Path):
    if not path.exists():
        return None
    return json.loads(path.read_bytes())


def _assert_owner_state_for_stage(owner_record, failure_stage: str) -> None:
    """An owner record exists only once the process itself was running."""
    if failure_stage != "protocol":
        assert owner_record is None
        return
    assert owner_record is not None
    assert owner_record["state"] == "process_running"


def _assert_evidence_is_sanitized(owner: Path, owner_record, tmp_path: Path) -> None:
    evidence = (owner / "failure.json").read_text()
    if owner_record is not None:
        evidence += (owner / "owner.json").read_text()
    assert "repository-secret" not in evidence
    assert str(tmp_path) not in evidence


def _assert_failure_evidence_is_private(owner: Path, owner_record) -> None:
    if os.name == "posix":
        assert stat.S_IMODE((owner / "failure.json").stat().st_mode) == 0o600
    _assert_lsp_acl_is_owner_only(owner, inherited=False)
    _assert_lsp_acl_is_owner_only(owner / "cancellation", inherited=os.name == "nt")
    if owner_record is not None:
        _assert_lsp_acl_is_owner_only(owner / "owner.json", inherited=os.name == "nt")
    _assert_lsp_acl_is_owner_only(owner / "failure.json", inherited=os.name == "nt")


def _generations_with_process(coordinator) -> list:
    """Generations that still hold a process — none may survive cleanup."""
    generations = [coordinator.active, coordinator.candidate, *coordinator.retired]
    return [
        generation
        for generation in generations
        if generation is not None and generation.process is not None
    ]


def _stop_heartbeat_thread(coordinator) -> None:
    coordinator.heartbeat_stop.set()
    coordinator.heartbeat_wake.set()
    heartbeat = coordinator.heartbeat_thread
    if heartbeat is not None:
        heartbeat.join(SHORT_TIMEOUT)


def _release_owner_directory(coordinator) -> None:
    owner_directory = coordinator.owner_directory
    if owner_directory is None:
        return
    try:
        owner_directory.remove_lease()
        owner_directory.close()
    except OSError:
        pass


def _release_captured_coordinator(coordinator) -> None:
    _stop_heartbeat_thread(coordinator)
    _release_owner_directory(coordinator)
    lsp_process_state.unregister_startup_cleanup(coordinator)


@pytest.mark.parametrize(
    "name",
    ["owner", "A" * 32, "g" * 32, "a" * 31, "a" * 33],
)
def test_start_rejects_invalid_owner_identity_before_mutation(
    tmp_path: Path, name: str
) -> None:
    owner = tmp_path / name
    with pytest.raises(ValueError, match="owner_root"):
        LspProcess.start(_command(), cwd=tmp_path, owner_root=owner)
    assert not owner.exists()


def test_owner_identity_matches_caller_derived_root_not_generation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    generation = "b" * 32
    real_token_hex = secrets.token_hex
    calls = 0

    def token_hex(size: int) -> str:
        nonlocal calls
        calls += 1
        return generation if calls == 1 else real_token_hex(size)

    monkeypatch.setattr(secrets, "token_hex", token_hex)
    process = _start(tmp_path, "--sleep-seconds", "2")
    assert process.owner_nonce == OWNER_NONCE
    assert process.generation_nonce == generation
    owner_record = json.loads((process.owner_root / "owner.json").read_bytes())
    assert owner_record["owner_nonce"] == OWNER_NONCE
    _expect_active_generation_exit(process)
    _wait(process)
    process.close(time.monotonic() + 5)


@pytest.mark.parametrize("generated", [RuntimeError("generation nonce failed"), "invalid"])
def test_generation_nonce_failure_occurs_before_filesystem_mutation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, generated: object
) -> None:
    owner = tmp_path / OWNER_NONCE

    def fail_nonce(_size: int) -> str:
        if isinstance(generated, BaseException):
            raise generated
        assert isinstance(generated, str)
        return generated

    monkeypatch.setattr(secrets, "token_hex", fail_nonce)
    with pytest.raises((RuntimeError, ValueError), match="generation nonce"):
        LspProcess.start(_command(), cwd=tmp_path, owner_root=owner)
    assert not owner.exists()


def test_timestamp_failure_occurs_before_filesystem_mutation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    owner = tmp_path / OWNER_NONCE

    class BrokenDateTime:
        @staticmethod
        def now(_timezone: object) -> object:
            raise RuntimeError("timestamp failed")

    monkeypatch.setattr(lsp_process_startup, "datetime", BrokenDateTime)
    with pytest.raises(RuntimeError, match="timestamp failed"):
        LspProcess.start(_command(), cwd=tmp_path, owner_root=owner)
    assert not owner.exists()


def test_popen_failure_retains_owner_root_and_omits_unknown_pid(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    owner = tmp_path / OWNER_NONCE
    sibling = tmp_path / "preexisting.txt"
    sibling.write_text("preserve", encoding="utf-8")

    def fail_popen(*_args: object, **_kwargs: object) -> object:
        raise OSError("popen failed")

    monkeypatch.setattr(lsp_process_state.subprocess, "Popen", fail_popen)
    with pytest.raises(OSError, match="popen failed"):
        LspProcess.start(_command(), cwd=tmp_path, owner_root=owner)
    assert set(path.name for path in owner.iterdir()) == {"cancellation", "failure.json"}
    failure = json.loads((owner / "failure.json").read_bytes())
    assert set(failure) == {
        "code",
        "generation_nonce",
        "owner_nonce",
        "timestamp",
    }
    assert failure["code"] == "startup_failed"
    assert sibling.read_text(encoding="utf-8") == "preserve"


def test_cancellation_directory_failure_retains_original_owner_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    owner = tmp_path / OWNER_NONCE
    if os.name == "nt":
        real_create = windows_workspace.create_directory

        def fail_cancellation(parent: int, name: str) -> int:
            if name == "cancellation":
                raise OSError("cancellation directory failed")
            return real_create(parent, name)

        monkeypatch.setattr(
            windows_workspace, "create_directory", fail_cancellation
        )
    else:
        real_mkdir = os.mkdir

        def fail_cancellation(
            path: object, mode: int = 0o777, *, dir_fd: int | None = None
        ) -> None:
            if path == "cancellation":
                raise OSError("cancellation directory failed")
            real_mkdir(path, mode, dir_fd=dir_fd)

        monkeypatch.setattr(os, "mkdir", fail_cancellation)
    with pytest.raises(OSError, match="cancellation directory failed"):
        LspProcess.start(_command(), cwd=tmp_path, owner_root=owner)
    assert owner.is_dir()
    assert set(path.name for path in owner.iterdir()) == {"failure.json"}


def test_owner_json_failure_terminates_child_and_retains_failure_only(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    children: list[subprocess.Popen[bytes]] = []
    real_popen = subprocess.Popen

    def popen_spy(*args: object, **kwargs: object) -> subprocess.Popen[bytes]:
        child = real_popen(*args, **kwargs)
        children.append(child)
        return child

    def fail_owner_json(*_args: object, **_kwargs: object) -> None:
        raise OSError("owner JSON failed")

    monkeypatch.setattr(lsp_process_state.subprocess, "Popen", popen_spy)
    monkeypatch.setattr(lsp_process_state, "write_owner_record", fail_owner_json)
    owner = tmp_path / OWNER_NONCE
    with pytest.raises(OSError, match="owner JSON failed"):
        LspProcess.start(
            _command("--exit-while-pending"), cwd=tmp_path, owner_root=owner
        )
    assert len(children) == 1
    children[0].wait(timeout=SHORT_TIMEOUT)
    assert children[0].poll() is not None
    assert owner.is_dir()
    assert not (owner / "owner.json").exists()
    assert json.loads((owner / "failure.json").read_bytes())["code"] == "startup_failed"


def test_exit_monitor_thread_start_failure_retains_process_owner_evidence(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    children: list[subprocess.Popen[bytes]] = []
    real_popen = subprocess.Popen
    real_start = threading.Thread.start

    def popen_spy(*args: object, **kwargs: object) -> subprocess.Popen[bytes]:
        child = real_popen(*args, **kwargs)
        children.append(child)
        return child

    def fail_exit_start(thread: threading.Thread) -> None:
        if thread.name.startswith("lsp-exit-"):
            raise RuntimeError("exit thread start failed")
        real_start(thread)

    monkeypatch.setattr(lsp_process_state.subprocess, "Popen", popen_spy)
    monkeypatch.setattr(threading.Thread, "start", fail_exit_start)
    owner = tmp_path / OWNER_NONCE
    with pytest.raises(RuntimeError, match="exit thread start failed"):
        LspProcess.start(
            _command("--exit-while-pending"), cwd=tmp_path, owner_root=owner
        )
    assert len(children) == 1
    children[0].wait(timeout=SHORT_TIMEOUT)
    assert children[0].poll() is not None
    assert owner.is_dir()
    assert json.loads((owner / "owner.json").read_bytes())["state"] == "process_running"
    assert json.loads((owner / "failure.json").read_bytes())["code"] == "startup_failed"


def test_heartbeat_thread_start_failure_removes_live_lease_and_owned_threads(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    baseline = {
        thread.name for thread in threading.enumerate() if thread.name.startswith("lsp-")
    }
    real_start = threading.Thread.start

    def fail_heartbeat_start(thread: threading.Thread) -> None:
        if thread.name.startswith("lsp-heartbeat-"):
            raise RuntimeError("heartbeat thread start failed")
        real_start(thread)

    monkeypatch.setattr(threading.Thread, "start", fail_heartbeat_start)
    owner = tmp_path / OWNER_NONCE

    with pytest.raises(RuntimeError, match="heartbeat thread start failed"):
        LspProcess.start(
            _command("--sleep-seconds", "30"), cwd=tmp_path, owner_root=owner
        )

    assert json.loads((owner / "failure.json").read_bytes())["code"] == "startup_failed"
    assert not (owner / "lease.json").exists()
    assert {
        thread.name for thread in threading.enumerate() if thread.name.startswith("lsp-")
    } == baseline


def test_owner_create_failure_uses_allocated_startup_generation_nonce(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    generation_nonce = "b" * 32
    owner_root = tmp_path / OWNER_NONCE
    real_create = lsp_process_owner.OwnerDirectory.create

    def fail_after_create(
        owner: lsp_process_owner.OwnerDirectory, deadline: float
    ) -> None:
        real_create(owner, deadline)
        raise RuntimeError("owner create failed after allocation")

    monkeypatch.setattr(lsp_process_owner, "new_generation_nonce", lambda: generation_nonce)
    monkeypatch.setattr(lsp_process_owner.OwnerDirectory, "create", fail_after_create)

    with pytest.raises(RuntimeError, match="owner create failed after allocation"):
        LspProcess.start(_command(), cwd=tmp_path, owner_root=owner_root)

    failure = json.loads((owner_root / "failure.json").read_bytes())
    assert failure["generation_nonce"] == generation_nonce
    assert failure["owner_nonce"] == OWNER_NONCE
    assert failure["code"] == "startup_failed"


def test_recovery_cannot_run_before_startup_final_fence_completes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    real_start_workers = lsp_process_recovery.start_lifecycle_workers
    workers_started = threading.Event()
    release_startup = threading.Event()
    captured: list[LspProcess] = []
    start_errors: list[BaseException] = []

    def inject_fatal_before_final_fence(
        instance: LspProcess,
        deadline: float | None = None,
    ) -> None:
        real_start_workers(instance, deadline)
        captured.append(instance)
        instance.protocol._become_fatal("fatal during startup final fence")
        workers_started.set()
        assert release_startup.wait(SHORT_TIMEOUT)

    def start() -> None:
        try:
            _start(tmp_path, "--lifecycle")
        except BaseException as error:
            start_errors.append(error)

    monkeypatch.setattr(
        lsp_process_recovery,
        "start_lifecycle_workers",
        inject_fatal_before_final_fence,
    )
    start_thread = threading.Thread(target=start)
    start_thread.start()
    assert workers_started.wait(SHORT_TIMEOUT)
    instance = captured[0]
    coordinator = instance._coordinator
    recovery_ran_before_final_fence = _coordinator_wait(
        instance,
        lambda: instance.restart_count != 0
        or coordinator.phase is not lsp_process_state.LifecyclePhase.RUNNING,
        timeout=0.2,
    )
    release_startup.set()
    start_thread.join(SHORT_TIMEOUT)

    assert recovery_ran_before_final_fence is False
    assert not start_thread.is_alive()
    assert len(start_errors) == 1
    assert isinstance(start_errors[0], RuntimeError)
    assert json.loads((instance.owner_root / "failure.json").read_bytes())["code"] == (
        "startup_failed"
    )


def test_start_passes_one_absolute_deadline_to_owner_process_tree_and_protocol(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    deadlines: dict[str, float | None] = {}
    real_spawn = lsp_process_state.ProcessTree._spawn_with_deadline.__func__
    real_protocol = lsp_process_generation.LspProtocol
    real_create = lsp_process_owner.OwnerDirectory.create

    def record_owner_create(
        owner: lsp_process_owner.OwnerDirectory,
        deadline: float,
    ) -> None:
        deadlines["owner"] = deadline
        real_create(owner, deadline)

    def record_spawn(
        cls: type[lsp_process_state.ProcessTree],
        command: object,
        *,
        cwd: Path,
        env: object,
        deadline: float,
    ) -> lsp_process_state.ProcessTree:
        deadlines["spawn"] = deadline
        return real_spawn(cls, command, cwd=cwd, env=env, deadline=deadline)  # type: ignore[arg-type]

    def record_protocol(*args: object, **kwargs: object) -> lsp_protocol.LspProtocol:
        deadlines["protocol"] = kwargs.get("_startup_deadline")  # type: ignore[assignment]
        return real_protocol(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(
        lsp_process_state.ProcessTree,
        "_spawn_with_deadline",
        classmethod(record_spawn),
    )
    monkeypatch.setattr(lsp_process_owner.OwnerDirectory, "create", record_owner_create)
    monkeypatch.setattr(lsp_process_generation, "LspProtocol", record_protocol)
    process = _start(tmp_path, "--lifecycle")
    try:
        assert deadlines["owner"] == deadlines["spawn"]
        assert deadlines["protocol"] == deadlines["spawn"]
        assert deadlines["owner"] is not None
        assert deadlines["spawn"] is not None
    finally:
        process.close(time.monotonic() + 5)


def test_default_startup_deadline_begins_after_nonmutating_owner_open(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    clock = [100.0]
    deadlines: dict[str, float] = {}
    real_open = lsp_process_owner.OwnerDirectory.open.__func__
    real_create = lsp_process_owner.OwnerDirectory.create

    def delayed_open(
        cls: type[lsp_process_owner.OwnerDirectory],
        owner_root: Path,
    ) -> lsp_process_owner.OwnerDirectory:
        owner = real_open(cls, owner_root)
        clock[0] += 10.0
        return owner

    def record_create(
        owner: lsp_process_owner.OwnerDirectory,
        deadline: float,
    ) -> None:
        deadlines["create"] = deadline
        real_create(owner, deadline)

    def stop_after_mutation_budget_capture(
        _cls: type[lsp_process_state.ProcessTree],
        _command: object,
        *,
        cwd: Path,
        env: object,
        deadline: float,
    ) -> lsp_process_state.ProcessTree:
        assert cwd == tmp_path.resolve()
        assert env
        deadlines["spawn"] = deadline
        raise RuntimeError("stopped after startup deadline capture")

    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(
        lsp_process_owner.OwnerDirectory,
        "open",
        classmethod(delayed_open),
    )
    monkeypatch.setattr(lsp_process_owner.OwnerDirectory, "create", record_create)
    monkeypatch.setattr(
        lsp_process_state.ProcessTree,
        "_spawn_with_deadline",
        classmethod(stop_after_mutation_budget_capture),
    )

    with pytest.raises(RuntimeError, match="stopped after startup deadline capture"):
        LspProcess.start(
            _command("--lifecycle"),
            cwd=tmp_path,
            owner_root=tmp_path / OWNER_NONCE,
        )

    expected = 110.0 + lsp_process_limits.STARTUP_WAIT_SECONDS
    assert deadlines == {"create": expected, "spawn": expected}


def test_startup_heartbeat_stop_timeout_still_runs_all_other_cleanup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    captured: list[lsp_process_state.LifecycleCoordinator] = []
    real_verify = lsp_process_owner.OwnerDirectory.verify_lexical_identity
    checks = 0

    def fail_after_heartbeat(owner: object) -> None:
        nonlocal checks
        checks += 1
        if checks == 4:
            raise RuntimeError("final startup fence failed")
        real_verify(owner)

    def fail_heartbeat_stop(
        coordinator: lsp_process_state.LifecycleCoordinator,
        _deadline: float,
        *,
        allow_expired: bool = False,
    ) -> None:
        del allow_expired
        captured.append(coordinator)
        raise TimeoutError("heartbeat stop blocked")

    monkeypatch.setattr(
        lsp_process_owner.OwnerDirectory,
        "verify_lexical_identity",
        fail_after_heartbeat,
    )
    monkeypatch.setattr(lsp_process_failure, "stop_heartbeat_owner", fail_heartbeat_stop)
    owner = tmp_path / OWNER_NONCE
    started = time.monotonic()

    try:
        with pytest.raises(lsp_process_cleanup.StartupCleanupError) as raised:
            LspProcess.start(
                _command("--sleep-seconds", "30"), cwd=tmp_path, owner_root=owner
            )

        assert time.monotonic() - started <= _STARTUP_CEILING_SECONDS
        assert len(captured) == 1
        assert isinstance(raised.value.__cause__, RuntimeError)
        coordinator = captured[0]
        assert _cleanup_error_types(coordinator) & {"TimeoutError"}
        assert not _generations_with_process(coordinator)
        assert coordinator.owner_directory is not None
        assert coordinator.owner_directory._closed is False
        assert (owner / "failure.json").is_file()
        assert (owner / "lease.json").is_file()
    finally:
        for coordinator in captured:
            _release_captured_coordinator(coordinator)


def test_restart_thread_start_failure_cleans_new_tree_and_becomes_terminal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    process = _start(tmp_path, "--lifecycle")
    spawned: list[object] = []
    real_spawn = lsp_process_state.ProcessTree._spawn_with_deadline.__func__
    real_start = threading.Thread.start

    def spawn(cls: object, *args: object, **kwargs: object) -> object:
        tree = real_spawn(cls, *args, **kwargs)
        spawned.append(tree)
        return tree

    def fail_restart_stderr(thread: threading.Thread) -> None:
        if thread.name.startswith("lsp-stderr-"):
            raise RuntimeError("restart stderr thread start failed")
        real_start(thread)

    monkeypatch.setattr(
        lsp_process_state.ProcessTree, "_spawn_with_deadline", classmethod(spawn)
    )
    monkeypatch.setattr(threading.Thread, "start", fail_restart_stderr)

    with pytest.raises(RuntimeError, match="restart stderr thread start failed"):
        process.restart(time.monotonic() + 5)

    assert len(spawned) == 1
    assert spawned[0].process.poll() is not None
    if os.name == "nt":
        assert _windows_handle_status(int(spawned[0].process._handle)) == (False, 6)
    assert process.restart_count == 0
    assert process.state is ProcessState.FAILED
    assert json.loads((process.owner_root / "failure.json").read_bytes())["code"] == (
        "restart_failed"
    )
    assert not (process.owner_root / "lease.json").exists()


def test_restart_spawn_failure_becomes_terminal_without_live_lease(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    process = _start(tmp_path, "--lifecycle")
    spawn_with_deadline = lsp_process_state.ProcessTree._spawn_with_deadline.__func__
    monkeypatch.setattr(
        lsp_process_state.ProcessTree,
        "_spawn_with_deadline",
        classmethod(
            lambda _cls, *_args, **_kwargs: (_ for _ in ()).throw(
                OSError("restart spawn failed")
            )
        ),
    )

    with pytest.raises(OSError, match="restart spawn failed"):
        process.restart(time.monotonic() + 5)

    assert process.restart_count == 0
    assert process.state is ProcessState.FAILED
    failure = json.loads((process.owner_root / "failure.json").read_bytes())
    assert failure["code"] == "restart_failed"
    assert not (process.owner_root / "lease.json").exists()
    monkeypatch.setattr(
        lsp_process_state.ProcessTree,
        "_spawn_with_deadline",
        classmethod(spawn_with_deadline),
    )
    _exercise_explicit_restart_candidate_cleanup_replaces_stopped_recovery_owner(
        monkeypatch, tmp_path
    )


def test_terminal_lease_removal_failure_does_not_skip_tree_or_protocol_cleanup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    process = _start(
        tmp_path,
        "--lifecycle",
        "--spawn-descendant",
        "--descendant-pid-file",
        str(tmp_path / "terminal-descendant.pid"),
    )
    owner = process._owner_directory
    assert owner is not None
    real_remove = lsp_process_owner.OwnerDirectory.remove_lease

    def remove_lease(current: object) -> None:
        if current is owner:
            raise OSError("lease removal failed")
        real_remove(current)

    monkeypatch.setattr(
        lsp_process_owner.OwnerDirectory,
        "remove_lease",
        remove_lease,
    )

    with pytest.raises(OSError, match="lease removal failed"):
        process._terminal_failure("injected_failure", time.monotonic() + 5)

    assert process.process.poll() is not None
    assert process._tree is None
    assert not process.protocol.reader_thread.is_alive()
    assert not process.protocol.writer_thread.is_alive()
    assert process.state is ProcessState.FAILED
    monkeypatch.setattr(lsp_process_owner.OwnerDirectory, "remove_lease", real_remove)
    process.close(time.monotonic() + 5)


def test_terminal_failure_retains_lease_until_protocol_owners_are_proven_stopped(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    process = _start(tmp_path, "--lifecycle")
    lease = process.owner_root / "lease.json"
    close = process.protocol.close
    monkeypatch.setattr(
        process.protocol,
        "close",
        lambda _deadline: (_ for _ in ()).throw(TimeoutError("protocol owner blocked")),
    )

    with pytest.raises(TimeoutError, match="protocol owner blocked"):
        process._terminal_failure("injected_failure", time.monotonic() + 5)

    assert lease.is_file()
    monkeypatch.setattr(process.protocol, "close", close)
    process.protocol.close(time.monotonic() + 1)
    process.close(time.monotonic() + 5)


def test_heartbeat_stop_reports_deadline_with_live_owner_thread(
    tmp_path: Path,
) -> None:
    process = _start(tmp_path, "--lifecycle")

    with pytest.raises(TimeoutError, match="heartbeat"):
        process._stop_heartbeat(time.monotonic() - 1)

    process.close(time.monotonic() + 5)


def test_shutdown_retains_lease_and_scratch_until_protocol_owners_are_proven_stopped(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    process = _start(tmp_path, "--lifecycle")
    owner = process.owner_root
    lease = owner / "lease.json"
    close = process.protocol.close
    monkeypatch.setattr(
        process.protocol,
        "close",
        lambda _deadline: (_ for _ in ()).throw(TimeoutError("protocol owner blocked")),
    )

    with pytest.raises(TimeoutError, match="protocol owner blocked"):
        process.shutdown(time.monotonic() + 5)

    assert owner.is_dir()
    assert lease.is_file()
    monkeypatch.setattr(process.protocol, "close", close)
    process.shutdown(time.monotonic() + 5)
    assert not owner.exists()


@pytest.mark.parametrize("failure_stage", ["owner-json", "protocol"])
def test_stubborn_child_preserves_restricted_failure_evidence(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, failure_stage: str
) -> None:
    class StubbornProcess:
        def __init__(self) -> None:
            self.args = _command("--ignored-secret", "repository-secret")
            self.pid = 424242
            self.stdin = io.BytesIO()
            self.stdout = io.BytesIO()
            self.stderr = io.BytesIO()
            self.terminate_calls = 0
            self.kill_calls = 0
            self.cleanup_allowed = False
            self.alive = True

        def poll(self) -> int | None:
            return None if self.alive else 1

        def wait(self, timeout: float | None = None) -> int:
            if not self.alive:
                return 1
            raise subprocess.TimeoutExpired(self.args, timeout)

        def terminate(self) -> None:
            self.terminate_calls += 1

        def kill(self) -> None:
            self.kill_calls += 1
            if self.cleanup_allowed:
                self.alive = False

    class BrokenProtocol:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            raise RuntimeError("protocol startup failed")

    child = StubbornProcess()
    monkeypatch.setattr(
        lsp_process_state.ProcessTree,
        "_spawn_with_deadline",
        classmethod(lambda _cls, *_args, **_kwargs: _FakeTree(child)),
    )
    if failure_stage == "owner-json":
        real_owner_write = lsp_process_state.write_owner_record
        writes = 0

        def fail_initial_owner_write(*args: object, **kwargs: object):
            nonlocal writes
            writes += 1
            if writes == 1:
                raise OSError("owner JSON failed")
            return real_owner_write(*args, **kwargs)

        monkeypatch.setattr(
            lsp_process_state,
            "write_owner_record",
            fail_initial_owner_write,
        )
    else:
        monkeypatch.setattr(lsp_process_generation, "LspProtocol", BrokenProtocol)
    owner = tmp_path / OWNER_NONCE
    with pytest.raises(lsp_process_cleanup.StartupCleanupError, match="retains retryable ownership") as raised:
        LspProcess.start(_command(), cwd=tmp_path, owner_root=owner)

    expected_cause = OSError if failure_stage == "owner-json" else RuntimeError
    assert isinstance(raised.value.__cause__, expected_cause)
    assert child.terminate_calls == 1
    assert child.kill_calls == 1
    assert owner.is_dir()
    owner_record = _optional_json(owner / "owner.json")
    failure_record = json.loads((owner / "failure.json").read_bytes())
    _assert_owner_state_for_stage(owner_record, failure_stage)
    assert failure_record["code"] == "startup_failed"
    assert failure_record["server_pid"] == child.pid
    assert failure_record["owner_nonce"] == OWNER_NONCE
    assert failure_record["timestamp"].endswith("Z")
    _assert_evidence_is_sanitized(owner, owner_record, tmp_path)
    _assert_failure_evidence_is_private(owner, owner_record)
    child.cleanup_allowed = True
    raised.value.retry_cleanup(time.monotonic() + 5)


def test_owner_permission_failure_rolls_back_before_spawn(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    spawned = False

    if os.name == "nt":
        def fail_permissions(_path: Path, _deadline: float) -> None:
            raise PermissionError("ACL unavailable")

        monkeypatch.setattr(lsp_process_owner, "secure_windows_owner_root", fail_permissions)
    else:
        def fail_permissions(_descriptor: int, _mode: int) -> None:
            raise PermissionError("mode unavailable")

        monkeypatch.setattr(os, "fchmod", fail_permissions)

    def unexpected_spawn(*_args: object, **_kwargs: object) -> object:
        nonlocal spawned
        spawned = True
        raise AssertionError("spawn must not run")

    monkeypatch.setattr(lsp_process_state.subprocess, "Popen", unexpected_spawn)
    owner = tmp_path / OWNER_NONCE
    with pytest.raises(PermissionError):
        LspProcess.start(_command(), cwd=tmp_path, owner_root=owner)
    assert spawned is False
    assert owner.is_dir()
    assert not any(owner.iterdir())


def test_immediate_parent_symlink_is_rejected_before_mutation(tmp_path: Path) -> None:
    real_parent = tmp_path / "real-parent"
    real_parent.mkdir()
    linked_parent = tmp_path / "linked-parent"
    try:
        linked_parent.symlink_to(real_parent, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks unavailable")
    owner = linked_parent / OWNER_NONCE
    # POSIX refuses the linked parent by name; the Windows workspace refuses the
    # reparse point first. Both are the refusal this test exists for, and
    # neither may leave the owner directory behind.
    with pytest.raises((ValueError, PermissionError), match="parent|reparse point"):
        LspProcess.start(_command(), cwd=tmp_path, owner_root=owner)
    assert not owner.exists()


def test_parent_handle_open_failure_occurs_before_spawn(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    spawned = False

    monkeypatch.setattr(
        lsp_process_owner.OwnerDirectory,
        "open",
        lambda _owner: (_ for _ in ()).throw(RuntimeError("parent handle failed")),
    )

    def unexpected_spawn(*_args: object, **_kwargs: object) -> object:
        nonlocal spawned
        spawned = True
        raise AssertionError("spawn must not run")

    monkeypatch.setattr(lsp_process_state.subprocess, "Popen", unexpected_spawn)
    owner = tmp_path / OWNER_NONCE
    with pytest.raises(RuntimeError, match="parent handle failed"):
        LspProcess.start(_command(), cwd=tmp_path, owner_root=owner)
    assert spawned is False
    assert not owner.exists()


def test_parent_identity_change_after_spawn_terminates_child_and_rolls_back(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    children: list[subprocess.Popen[bytes]] = []
    real_popen = subprocess.Popen
    real_verify = lsp_process_owner.OwnerDirectory.verify_lexical_identity
    checks = 0

    def fail_first_fence(owner: object) -> None:
        nonlocal checks
        checks += 1
        if checks == 1:
            raise RuntimeError("parent identity changed during startup")
        real_verify(owner)

    monkeypatch.setattr(
        lsp_process_owner.OwnerDirectory, "verify_lexical_identity", fail_first_fence
    )

    def popen_spy(*args: object, **kwargs: object) -> subprocess.Popen[bytes]:
        child = real_popen(*args, **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(lsp_process_state.subprocess, "Popen", popen_spy)
    owner = tmp_path / OWNER_NONCE
    with pytest.raises(RuntimeError, match="parent identity changed"):
        LspProcess.start(
            _command("--exit-while-pending"), cwd=tmp_path, owner_root=owner
        )
    assert len(children) == 1
    children[0].wait(timeout=SHORT_TIMEOUT)
    assert children[0].poll() is not None
    assert owner.is_dir()


@pytest.mark.skipif(os.name != "nt", reason="Windows reparse contract")
def test_windows_reparse_parent_is_rejected_before_mutation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    parent = tmp_path / "reparse-parent"
    parent.mkdir()
    real_open = windows_workspace.open_directory_path

    def reject_reparse(path: Path) -> int:
        if path == parent:
            raise PermissionError("parent is a reparse point")
        return real_open(path)

    monkeypatch.setattr(
        windows_workspace, "open_directory_path", reject_reparse
    )
    owner = parent / OWNER_NONCE
    with pytest.raises(PermissionError, match="reparse"):
        LspProcess.start(_command(), cwd=tmp_path, owner_root=owner)
    assert not owner.exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows command contract")
@pytest.mark.parametrize("suffix", [".cmd", ".BAT"])
def test_windows_shell_scripts_are_rejected_before_mutation(
    tmp_path: Path, suffix: str
) -> None:
    executable = tmp_path / f"server{suffix}"
    executable.write_text("@echo off", encoding="utf-8")
    owner = tmp_path / OWNER_NONCE
    with pytest.raises(ValueError, match="shell script"):
        LspProcess.start([str(executable)], cwd=tmp_path, owner_root=owner)
    assert not owner.exists()


@pytest.mark.skipif(os.name != "posix", reason="POSIX executable contract")
def test_posix_non_executable_file_is_rejected_before_mutation(tmp_path: Path) -> None:
    executable = tmp_path / "server"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o600)
    owner = tmp_path / OWNER_NONCE
    with pytest.raises(ValueError, match="executable"):
        LspProcess.start([str(executable)], cwd=tmp_path, owner_root=owner)
    assert not owner.exists()


def _exercise_explicit_restart_candidate_cleanup_replaces_stopped_recovery_owner(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    process = LspProcess.start(
        _command("--lifecycle", "--sleep-seconds", "30"),
        cwd=tmp_path,
        owner_root=tmp_path / ("b" * 32),
    )
    coordinator = process._coordinator
    original_recovery = coordinator.recovery_thread
    owner = coordinator.owner_directory
    assert original_recovery is not None and owner is not None
    remove_lease = lsp_process_owner.OwnerDirectory.remove_lease
    retry_started = threading.Event()
    allow_retry = threading.Event()
    retry_threads: list[threading.Thread] = []
    remove_calls = 0

    def fail_restart_spawn(
        _cls: type[lsp_process_state.ProcessTree],
        *_args: object,
        **_kwargs: object,
    ) -> lsp_process_state.ProcessTree:
        raise OSError("restart candidate spawn failed")

    def transient_remove_lease(current: lsp_process_owner.OwnerDirectory) -> None:
        nonlocal remove_calls
        if current is owner:
            remove_calls += 1
            if remove_calls == 1:
                raise OSError("restart cleanup lease removal failed")
            retry_threads.append(threading.current_thread())
            retry_started.set()
            assert allow_retry.wait(SHORT_TIMEOUT)
        remove_lease(current)

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
        with pytest.raises(OSError, match="restart candidate spawn failed"):
            process.restart(time.monotonic() + 5)

        assert coordinator.recovery_request_pending.is_set()
        assert retry_started.wait(SHORT_TIMEOUT)
        assert len(retry_threads) == 1
        replacement = retry_threads[0]
        assert replacement is not original_recovery
        assert replacement.is_alive()

        allow_retry.set()
        assert _coordinator_wait(
            process,
            lambda: coordinator.phase is lsp_process_state.LifecyclePhase.STOPPED_FAILURE,
            timeout=5,
        )
        replacement.join(SHORT_TIMEOUT)

        assert remove_calls == 2
        assert not replacement.is_alive()
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
