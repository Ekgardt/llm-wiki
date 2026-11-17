from __future__ import annotations

import subprocess
import threading
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from types import MappingProxyType
from typing import cast

import pytest
from llm_wiki import lsp_process_generation
from llm_wiki.lsp_process_owner import OwnerDirectory
from llm_wiki.lsp_process_state import (
    Generation,
    GenerationConfiguration,
    LifecycleCoordinator,
    LifecyclePhase,
    LspProcessHandle,
    ProcessState,
    ProcessTree,
)
from lsp_process_tree import ProcessTreeSpawnError
from lsp_protocol import LspProtocol

OWNER_NONCE = "a" * 32
GENERATION_NONCE = "b" * 32
OTHER_NONCE = "c" * 32


def _later() -> float:
    return time.monotonic() + 5.0


def _owner(tmp_path: Path) -> OwnerDirectory:
    return OwnerDirectory(tmp_path / OWNER_NONCE, parent_handle=0, parent_identity=None)


class _ExitedProcess:
    pid = 4242
    stdin = None
    stdout = None
    stderr = None

    def __init__(self, wait_error: BaseException | None = None) -> None:
        self.wait_error = wait_error

    def wait(self) -> int:
        if self.wait_error is not None:
            raise self.wait_error
        return 0


class _SpawnedTree:
    windows_job = None

    def __init__(self, process: _ExitedProcess) -> None:
        self.process = process


class _ProcessView(LspProcessHandle):
    def __init__(self, coordinator: LifecycleCoordinator) -> None:
        self._coordinator = coordinator
        self.owner_nonce = OWNER_NONCE
        self.state = ProcessState.PROCESS_RUNNING


def _as_tree(tree: _SpawnedTree) -> ProcessTree:
    return cast(ProcessTree, tree)


def _as_popen(process: _ExitedProcess) -> subprocess.Popen[bytes]:
    return cast("subprocess.Popen[bytes]", process)


def _spawn_that_raises(error: BaseException) -> object:
    def spawn(command: Sequence[str], **options: object) -> ProcessTree:
        raise error

    return spawn


def _spawn_that_returns(tree: _SpawnedTree) -> object:
    def spawn(command: Sequence[str], **options: object) -> ProcessTree:
        return _as_tree(tree)

    return spawn


def _prepare(coordinator: LifecycleCoordinator, tmp_path: Path) -> Generation:
    environment: Mapping[str, str] = {}
    return lsp_process_generation.prepare_generation(
        coordinator,
        ["server"],
        cwd=tmp_path,
        environment=environment,
        deadline=_later(),
        generation_nonce=GENERATION_NONCE,
    )


def _bootstrap_configuration() -> GenerationConfiguration:
    def bootstrap(protocol: object, pid: int, nonce: str, deadline: float) -> ProcessState:
        return ProcessState.WORKSPACE_READY

    return GenerationConfiguration(MappingProxyType({}), MappingProxyType({}), bootstrap, 5.0, None)


def test_a_failed_spawn_leaves_its_windows_job_with_the_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    coordinator = LifecycleCoordinator(_owner(tmp_path))
    failure = ProcessTreeSpawnError(None, (OSError("denied"),), windows_job=77)
    monkeypatch.setattr(ProcessTree, "_spawn_with_deadline", _spawn_that_raises(failure))

    with pytest.raises(ProcessTreeSpawnError):
        _prepare(coordinator, tmp_path)

    candidate = coordinator.candidate
    assert candidate is not None
    assert (candidate.tree, candidate.windows_job, candidate.process) == (None, 77, None)


def test_a_failed_spawn_leaves_its_tree_and_process_with_the_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    coordinator = LifecycleCoordinator(_owner(tmp_path))
    tree = _SpawnedTree(_ExitedProcess())
    failure = ProcessTreeSpawnError(_as_tree(tree), (OSError("denied"),))
    monkeypatch.setattr(ProcessTree, "_spawn_with_deadline", _spawn_that_raises(failure))

    with pytest.raises(ProcessTreeSpawnError):
        _prepare(coordinator, tmp_path)

    candidate = coordinator.candidate
    assert candidate is not None
    assert candidate.tree is cast(object, tree)
    assert candidate.process is cast(object, tree.process)


def test_a_spawned_tree_without_its_pipes_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    coordinator = LifecycleCoordinator(_owner(tmp_path))
    tree = _SpawnedTree(_ExitedProcess())
    monkeypatch.setattr(ProcessTree, "_spawn_with_deadline", _spawn_that_returns(tree))

    with pytest.raises(RuntimeError, match="pipes were not created"):
        _prepare(coordinator, tmp_path)

    candidate = coordinator.candidate
    assert candidate is not None
    assert candidate.server_pid == 4242


def test_a_generation_needs_an_owner_directory() -> None:
    with pytest.raises(RuntimeError, match="owner directory is unavailable"):
        lsp_process_generation._require_owner_directory(LifecycleCoordinator(None))


def test_an_exit_monitor_without_a_process_has_nothing_to_watch() -> None:
    coordinator = LifecycleCoordinator(None)

    lsp_process_generation._monitor_generation_exit(
        coordinator, Generation(GENERATION_NONCE, None, None)
    )

    assert coordinator.pending_failure_intents == 0


def test_an_exit_monitor_whose_wait_fails_queues_the_generation_failure() -> None:
    coordinator = LifecycleCoordinator(None)
    generation = Generation(
        GENERATION_NONCE, None, _as_popen(_ExitedProcess(OSError("wait broke")))
    )

    lsp_process_generation._monitor_generation_exit(coordinator, generation)

    intent = coordinator.failure_queue.get_nowait()
    assert (intent.generation_nonce, intent.reason) == (GENERATION_NONCE, "wait broke")
    assert generation.failure_queued


def test_a_lease_needs_a_server_process(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="generation process is unavailable"):
        lsp_process_generation.write_generation_lease(
            _owner(tmp_path),
            Generation(GENERATION_NONCE, None, None),
            OWNER_NONCE,
            _later(),
            threading.Event(),
        )


def test_a_lease_needs_an_owner_directory() -> None:
    view = _ProcessView(LifecycleCoordinator(None))

    with pytest.raises(RuntimeError, match="owner directory is unavailable"):
        lsp_process_generation.publish_generation_lease(
            view, Generation(GENERATION_NONCE, None, None), _later()
        )


def test_a_lease_is_not_published_for_a_generation_that_lost_its_candidacy(
    tmp_path: Path,
) -> None:
    coordinator = LifecycleCoordinator(_owner(tmp_path))
    coordinator.candidate = Generation(OTHER_NONCE, None, None)

    with pytest.raises(RuntimeError, match="lost lifecycle ownership"):
        lsp_process_generation.publish_generation_lease(
            _ProcessView(coordinator), Generation(GENERATION_NONCE, None, None), _later()
        )

    assert coordinator.lease_generation is None
    assert not coordinator.lease_lock.locked()


def test_a_bootstrap_needs_the_process_and_protocol_it_drives() -> None:
    generation = Generation(GENERATION_NONCE, None, None)

    with pytest.raises(RuntimeError, match="bootstrap owners are unavailable"):
        lsp_process_generation.bootstrapped_state(_bootstrap_configuration(), generation, _later())


def test_a_bootstrap_reports_the_state_it_reached() -> None:
    generation = Generation(GENERATION_NONCE, None, None)
    generation.process = _as_popen(_ExitedProcess())
    generation.protocol = cast(LspProtocol, object())

    state = lsp_process_generation.bootstrapped_state(
        _bootstrap_configuration(), generation, _later()
    )

    assert state is ProcessState.WORKSPACE_READY


def test_a_heartbeat_failure_after_success_changes_nothing() -> None:
    coordinator = LifecycleCoordinator(None)
    coordinator.success_committed = True
    view = _ProcessView(coordinator)

    lsp_process_generation._record_heartbeat_failure(
        view, coordinator, OSError("disk full"), _later()
    )
    lsp_process_generation._commit_heartbeat_failure_locked(view, coordinator)

    assert coordinator.pending_failure_intents == 0
    assert coordinator.phase is LifecyclePhase.STARTING
    assert view.state is ProcessState.PROCESS_RUNNING


@pytest.mark.parametrize("phase", [LifecyclePhase.STOPPED_SUCCESS, LifecyclePhase.STOPPED_FAILURE])
def test_a_stopped_lifecycle_writes_no_lease(tmp_path: Path, phase: LifecyclePhase) -> None:
    coordinator = LifecycleCoordinator(_owner(tmp_path))
    coordinator.lease_generation = Generation(GENERATION_NONCE, None, None)
    coordinator.phase = phase

    lsp_process_generation.write_current_lease(_ProcessView(coordinator), _later())

    assert not (tmp_path / OWNER_NONCE).exists()
    assert not coordinator.lease_lock.locked()


def test_a_stopped_heartbeat_writes_no_lease(tmp_path: Path) -> None:
    coordinator = LifecycleCoordinator(_owner(tmp_path))
    coordinator.lease_generation = Generation(GENERATION_NONCE, None, None)
    coordinator.heartbeat_stop.set()

    lsp_process_generation.write_current_lease(_ProcessView(coordinator), _later())

    assert not (tmp_path / OWNER_NONCE).exists()
