"""A generation guard admits work only for the generation that owns it."""

from __future__ import annotations

import dataclasses
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import lsp_protocol
import pytest
from llm_wiki import (
    lsp_process_generation,
    lsp_process_launch,
    lsp_process_limits,
    lsp_process_recovery,
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
    _STARTUP_BUDGET_SECONDS,
    OWNER_NONCE,
    _command,
    _initialize_generation,
    _start,
)
from tests.slow_machine import SHORT_TIMEOUT

_GUARD_STAGES = ("factory", "enter", "spawn", "bootstrap", "exit")


def _assert_guard_stages(events, deadlines, nonce: str) -> None:
    """One generation passes every stage once, under a single deadline."""
    assert [stage for stage, current in events if current == nonce] == list(
        _GUARD_STAGES
    )
    assert set(deadlines[nonce]) == set(_GUARD_STAGES)
    assert len(set(deadlines[nonce].values())) == 1


def test_generation_guard_wraps_each_autonomous_generation_without_transition_locks(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(lsp_process_limits, "HEARTBEAT_SECONDS", 60.0)
    marker = tmp_path / "query-crashed"
    coordinators: list[lsp_process_state.LifecycleCoordinator] = []
    events: list[tuple[str, str]] = []
    deadlines: dict[str, dict[str, float]] = {}
    lock_snapshots: list[tuple[str, bool, bool, bool]] = []
    active_guard: list[str] = []
    real_register = lsp_process_state.register_startup_cleanup
    real_spawn = lsp_process_state.ProcessTree._spawn_with_deadline.__func__

    def capture_coordinator(coordinator: lsp_process_state.LifecycleCoordinator) -> None:
        coordinators.append(coordinator)
        real_register(coordinator)

    def lock_is_available(lock: object) -> bool:
        acquired = lock.acquire(blocking=False)  # type: ignore[attr-defined]
        if acquired:
            lock.release()  # type: ignore[attr-defined]
        return acquired

    def record_locks(stage: str) -> None:
        coordinator = coordinators[0]
        with ThreadPoolExecutor(max_workers=3) as pool:
            driver = pool.submit(lock_is_available, coordinator.driver)
            lifecycle = pool.submit(lock_is_available, coordinator.lock)
            lease = pool.submit(lock_is_available, coordinator.lease_lock)
        lock_snapshots.append(
            (stage, driver.result(), lifecycle.result(), lease.result())
        )

    class Guard:
        def __init__(self, nonce: str, deadline: float) -> None:
            self.nonce = nonce
            self.deadline = deadline

        def __enter__(self) -> Guard:
            record_locks("enter")
            assert active_guard == []
            active_guard.append(self.nonce)
            events.append(("enter", self.nonce))
            deadlines[self.nonce]["enter"] = self.deadline
            return self

        def __exit__(self, *_error: object) -> None:
            record_locks("exit")
            coordinator = coordinators[0]
            with coordinator.condition:
                assert coordinator.active is None or coordinator.active.nonce != self.nonce
                assert coordinator.candidate is not None
                assert coordinator.candidate.nonce == self.nonce
            events.append(("exit", self.nonce))
            deadlines[self.nonce]["exit"] = self.deadline
            assert active_guard == [self.nonce]
            active_guard.clear()

    def guard_factory(generation_nonce: str, deadline: float) -> Guard:
        record_locks("factory")
        events.append(("factory", generation_nonce))
        deadlines[generation_nonce] = {"factory": deadline}
        return Guard(generation_nonce, deadline)

    def record_spawn(
        cls: type[lsp_process_state.ProcessTree],
        command: object,
        *,
        cwd: Path,
        env: object,
        deadline: float,
    ) -> lsp_process_state.ProcessTree:
        assert len(active_guard) == 1
        events.append(("spawn", active_guard[0]))
        deadlines[active_guard[0]]["spawn"] = deadline
        return real_spawn(cls, command, cwd=cwd, env=env, deadline=deadline)  # type: ignore[arg-type]

    def bootstrap(
        protocol: lsp_protocol.LspProtocol,
        pid: int,
        generation_nonce: str,
        deadline: float,
    ) -> ProcessState:
        assert active_guard == [generation_nonce]
        events.append(("bootstrap", generation_nonce))
        deadlines[generation_nonce]["bootstrap"] = deadline
        return _initialize_generation(protocol, pid, generation_nonce, deadline)

    monkeypatch.setattr(lsp_process_state, "register_startup_cleanup", capture_coordinator)
    monkeypatch.setattr(
        lsp_process_state.ProcessTree,
        "_spawn_with_deadline",
        classmethod(record_spawn),
    )
    process = LspProcess.start_configured(
        _command(
            "--lifecycle",
            "--bootstrap-handshake",
            "--query-crash-once-marker",
            str(marker),
            "--require-initialized-query",
        ),
        cwd=tmp_path,
        owner_root=tmp_path / OWNER_NONCE,
        deadline=time.monotonic() + _STARTUP_BUDGET_SECONDS,
        server_request_handlers={"workspace/configuration": lambda _params: True},
        server_notification_handlers={"$/progress": lambda _params: None},
        generation_bootstrap=bootstrap,
        generation_guard=guard_factory,
    )
    first_nonce = process.generation_nonce
    try:
        assert process.request(
            "initialized/query", {}, deadline=time.monotonic() + SHORT_TIMEOUT
        )["initialized"] is True
        second_nonce = process.generation_nonce

        assert second_nonce != first_nonce
        for nonce in (first_nonce, second_nonce):
            _assert_guard_stages(events, deadlines, nonce)
        assert lock_snapshots == [
            (stage, False, True, True)
            for stage in ("factory", "enter", "exit", "factory", "enter", "exit")
        ]
        assert process.restart_count == 1
    finally:
        process.close(time.monotonic() + 5)


@pytest.mark.skipif(os.name != "posix", reason="POSIX verified launch descriptor")
def test_generation_guard_launches_from_inherited_descriptor_after_path_replacement(
    tmp_path: Path,
) -> None:
    server = tmp_path / "server.py"
    retired = tmp_path / "retired.py"
    from tests.code_kernel_helpers import package_fake_lsp_server

    package_fake_lsp_server(server)
    descriptors: list[int] = []

    class Guard:
        def __enter__(self) -> object:
            descriptor = os.open(server, os.O_RDONLY)
            descriptors.append(descriptor)
            server.replace(retired)
            server.write_text("raise SystemExit(73)\n", encoding="utf-8")
            descriptor_path = (
                f"/proc/self/fd/{descriptor}"
                if Path("/proc/self/fd").is_dir()
                else f"/dev/fd/{descriptor}"
            )
            return lsp_process_state.GenerationLaunch(
                (
                    sys.executable,
                    descriptor_path,
                    "--lifecycle",
                    "--bootstrap-handshake",
                ),
                (descriptor,),
            )

        def __exit__(self, *_error: object) -> None:
            os.close(descriptors.pop())

    process = LspProcess.start_configured(
        (sys.executable, str(server), "--lifecycle", "--bootstrap-handshake"),
        cwd=tmp_path,
        owner_root=tmp_path / OWNER_NONCE,
        deadline=time.monotonic() + _STARTUP_BUDGET_SECONDS,
        server_request_handlers={"workspace/configuration": lambda _params: True},
        server_notification_handlers={"$/progress": lambda _params: None},
        generation_bootstrap=_initialize_generation,
        generation_guard=lambda _nonce, _deadline: Guard(),
    )
    try:
        assert process.state is ProcessState.PROTOCOL_INITIALIZED
    finally:
        process.close(time.monotonic() + 5)


def test_generation_guard_command_override_is_bounded_before_spawn(
    tmp_path: Path,
) -> None:
    command = tuple(_command("--lifecycle"))
    launch = lsp_process_state.GenerationLaunch(
        (command[0], "x" * (64 * 1024 + 1)),
    )

    with pytest.raises(ValueError, match="generation launch command"):
        lsp_process_launch.generation_launch(command, launch, cwd=tmp_path)


def test_generation_guard_enter_failure_prevents_spawn_and_cleans_owner(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    coordinators: list[lsp_process_state.LifecycleCoordinator] = []
    enters = 0
    exits = 0
    real_register = lsp_process_state.register_startup_cleanup

    def capture_coordinator(coordinator: lsp_process_state.LifecycleCoordinator) -> None:
        coordinators.append(coordinator)
        real_register(coordinator)

    class FailingGuard:
        def __enter__(self) -> FailingGuard:
            nonlocal enters
            enters += 1
            raise RuntimeError("generation guard precheck failed")

        def __exit__(self, *_error: object) -> None:
            nonlocal exits
            exits += 1

    def forbidden_spawn(*_args: object, **_kwargs: object) -> None:
        pytest.fail("generation guard failure spawned a child")

    monkeypatch.setattr(lsp_process_state, "register_startup_cleanup", capture_coordinator)
    monkeypatch.setattr(
        lsp_process_state.ProcessTree,
        "_spawn_with_deadline",
        classmethod(forbidden_spawn),
    )
    owner = tmp_path / OWNER_NONCE

    with pytest.raises(RuntimeError, match="generation guard precheck failed"):
        LspProcess.start_configured(
            _command("--lifecycle"),
            cwd=tmp_path,
            owner_root=owner,
            deadline=time.monotonic() + _STARTUP_BUDGET_SECONDS,
            server_request_handlers={},
            server_notification_handlers={},
            generation_bootstrap=lambda *_args: ProcessState.PROCESS_RUNNING,
            generation_guard=lambda _nonce, _deadline: FailingGuard(),
        )

    assert enters == 1
    assert exits == 0
    assert len(coordinators) == 1
    assert coordinators[0].candidate is None
    assert not lsp_process_state.coordinator_has_ownership(coordinators[0])
    assert json.loads((owner / "failure.json").read_bytes())["code"] == "startup_failed"
    assert not (owner / "lease.json").exists()


def test_generation_guard_rejects_non_context_result_before_spawn(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    def forbidden_spawn(*_args: object, **_kwargs: object) -> None:
        pytest.fail("an invalid generation guard spawned a child")

    monkeypatch.setattr(
        lsp_process_state.ProcessTree,
        "_spawn_with_deadline",
        classmethod(forbidden_spawn),
    )
    owner = tmp_path / OWNER_NONCE

    with pytest.raises(TypeError, match="context manager"):
        LspProcess.start_configured(
            _command("--lifecycle"),
            cwd=tmp_path,
            owner_root=owner,
            deadline=time.monotonic() + _STARTUP_BUDGET_SECONDS,
            server_request_handlers={},
            server_notification_handlers={},
            generation_bootstrap=lambda *_args: ProcessState.PROCESS_RUNNING,
            generation_guard=lambda _nonce, _deadline: object(),
        )

    assert json.loads((owner / "failure.json").read_bytes())["code"] == "startup_failed"
    assert not (owner / "lease.json").exists()


def test_generation_guard_exit_failure_cleans_replacement_without_commit(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    trees: list[lsp_process_state.ProcessTree] = []
    guard_nonces: list[str] = []
    exited_nonces: list[str] = []
    real_spawn = lsp_process_state.ProcessTree._spawn_with_deadline.__func__

    def record_spawn(
        cls: type[lsp_process_state.ProcessTree],
        command: object,
        *,
        cwd: Path,
        env: object,
        deadline: float,
    ) -> lsp_process_state.ProcessTree:
        tree = real_spawn(cls, command, cwd=cwd, env=env, deadline=deadline)  # type: ignore[arg-type]
        trees.append(tree)
        return tree

    class Guard:
        def __init__(self, nonce: str) -> None:
            self.nonce = nonce

        def __enter__(self) -> Guard:
            return self

        def __exit__(self, error_type: object, *_error: object) -> None:
            exited_nonces.append(self.nonce)
            if len(exited_nonces) == 2 and error_type is None:
                raise RuntimeError("generation guard postcheck failed")

    def guard_factory(generation_nonce: str, _deadline: float) -> Guard:
        guard_nonces.append(generation_nonce)
        return Guard(generation_nonce)

    monkeypatch.setattr(
        lsp_process_state.ProcessTree,
        "_spawn_with_deadline",
        classmethod(record_spawn),
    )
    process = LspProcess.start_configured(
        _command("--lifecycle", "--bootstrap-handshake"),
        cwd=tmp_path,
        owner_root=tmp_path / OWNER_NONCE,
        deadline=time.monotonic() + _STARTUP_BUDGET_SECONDS,
        server_request_handlers={"workspace/configuration": lambda _params: True},
        server_notification_handlers={"$/progress": lambda _params: None},
        generation_bootstrap=_initialize_generation,
        generation_guard=guard_factory,
    )
    first_nonce = process.generation_nonce

    with pytest.raises(RuntimeError, match="generation guard postcheck failed"):
        process.restart(time.monotonic() + 5)

    coordinator = process._coordinator
    assert len(trees) == 2
    assert all(tree.process.poll() is not None for tree in trees)
    assert len(guard_nonces) == len(exited_nonces) == 2
    assert guard_nonces == exited_nonces
    assert process.generation_nonce == first_nonce
    assert process.restart_count == 0
    assert process.state is ProcessState.FAILED
    assert coordinator.active is None
    assert coordinator.candidate is None
    assert not (process.owner_root / "lease.json").exists()
    assert not lsp_process_state.coordinator_has_ownership(coordinator)


def test_generation_guard_cannot_suppress_bootstrap_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    coordinators: list[lsp_process_state.LifecycleCoordinator] = []
    exit_errors: list[type[BaseException] | None] = []
    real_register = lsp_process_state.register_startup_cleanup

    def capture_coordinator(coordinator: lsp_process_state.LifecycleCoordinator) -> None:
        coordinators.append(coordinator)
        real_register(coordinator)

    class SuppressingGuard:
        def __enter__(self) -> SuppressingGuard:
            return self

        def __exit__(
            self,
            error_type: type[BaseException] | None,
            *_error: object,
        ) -> bool:
            exit_errors.append(error_type)
            return True

    def fail_bootstrap(*_args: object) -> ProcessState:
        raise RuntimeError("generation bootstrap failed")

    monkeypatch.setattr(lsp_process_state, "register_startup_cleanup", capture_coordinator)
    owner = tmp_path / OWNER_NONCE

    with pytest.raises(RuntimeError, match="generation bootstrap failed"):
        LspProcess.start_configured(
            _command("--lifecycle"),
            cwd=tmp_path,
            owner_root=owner,
            deadline=time.monotonic() + _STARTUP_BUDGET_SECONDS,
            server_request_handlers={},
            server_notification_handlers={},
            generation_bootstrap=fail_bootstrap,
            generation_guard=lambda _nonce, _deadline: SuppressingGuard(),
        )

    assert exit_errors == [RuntimeError]
    assert len(coordinators) == 1
    assert coordinators[0].active is None
    assert coordinators[0].candidate is None
    assert not lsp_process_state.coordinator_has_ownership(coordinators[0])
    assert json.loads((owner / "failure.json").read_bytes())["code"] == "startup_failed"
    assert not (owner / "lease.json").exists()


def test_generation_guard_body_interruption_outranks_exit_error() -> None:
    ordinary_error = RuntimeError("ordinary generation guard exit error")

    class Guard:
        def __enter__(self) -> Guard:
            return self

        def __exit__(self, *_error: object) -> None:
            raise ordinary_error

    configuration = dataclasses.replace(
        lsp_process_state.unconfigured_generation(),
        generation_guard=lambda _nonce, _deadline: Guard(),
    )

    def interrupt_guard_body() -> None:
        with lsp_process_generation.generation_guard_context(
            configuration,
            "a" * 32,
            time.monotonic() + 1,
        ):
            raise KeyboardInterrupt("generation guard body interrupted")

    with pytest.raises(
        KeyboardInterrupt,
        match="generation guard body interrupted",
    ) as raised:
        interrupt_guard_body()

    assert raised.value.__cause__ is ordinary_error
    traceback_names: list[str] = []
    current = raised.value.__traceback__
    while current is not None:
        traceback_names.append(current.tb_frame.f_code.co_name)
        current = current.tb_next
    assert "interrupt_guard_body" in traceback_names


def test_generation_guard_unwraps_body_interruption_without_exception_cycle() -> None:
    cleanup_error = RuntimeError("ordinary generation guard cleanup error")

    class Guard:
        def __enter__(self) -> Guard:
            return self

        def __exit__(self, *_error: object) -> None:
            raise cleanup_error

    configuration = dataclasses.replace(
        lsp_process_state.unconfigured_generation(),
        generation_guard=lambda _nonce, _deadline: Guard(),
    )

    def make_interruption() -> SystemExit:
        try:
            raise SystemExit(31)
        except SystemExit as error:
            return error

    interruption = make_interruption()
    wrapper = RuntimeError("generation guard body wrapper")
    wrapper.__cause__ = interruption

    with pytest.raises(SystemExit) as raised:
        with lsp_process_generation.generation_guard_context(
            configuration,
            "b" * 32,
            time.monotonic() + 1,
        ):
            raise wrapper

    assert raised.value is interruption
    assert raised.value.__cause__ is cleanup_error
    assert cleanup_error.__cause__ is None
    assert cleanup_error.__context__ is None
    assert wrapper.__cause__ is interruption


def test_startup_unwraps_interruption_without_revisiting_exception_graph(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    def make_interruption() -> KeyboardInterrupt:
        try:
            raise KeyboardInterrupt("wrapped process startup interruption")
        except KeyboardInterrupt as error:
            return error

    interruption = make_interruption()
    wrapper = RuntimeError("process startup interruption wrapper")
    wrapper.__cause__ = interruption

    def interrupt_lifecycle_workers(
        _instance: LspProcess,
        _deadline: float | None = None,
    ) -> None:
        raise wrapper

    monkeypatch.setattr(
        lsp_process_recovery,
        "start_lifecycle_workers",
        interrupt_lifecycle_workers,
    )
    owner_root = tmp_path / OWNER_NONCE

    with pytest.raises(
        KeyboardInterrupt,
        match="wrapped process startup interruption",
    ) as raised:
        _start(tmp_path, "--lifecycle")

    pending = [raised.value]
    seen: set[int] = set()
    reachable: list[BaseException] = []
    while pending:
        current = pending.pop()
        assert id(current) not in seen
        seen.add(id(current))
        reachable.append(current)
        if current.__cause__ is not None:
            pending.append(current.__cause__)
        if current.__context__ is not None:
            pending.append(current.__context__)

    assert raised.value is interruption
    assert wrapper in reachable
    assert wrapper.__cause__ is None
    assert wrapper.__context__ is None
    assert (owner_root / "failure.json").is_file()
    assert not (owner_root / "lease.json").exists()
