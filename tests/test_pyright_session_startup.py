"""Startup of the pinned Pyright session: its command, deadlines, cleanup owners and interruptions."""

from __future__ import annotations

import os
import stat
import threading
import time
from pathlib import Path

import lsp_process
import pytest
from code_intelligence import PositionEncoding
from llm_wiki import (
    pyright_session_core,
    pyright_session_startup,
)
from llm_wiki.pyright_session_core import PyrightSession
from llm_wiki.pyright_session_startup import STARTUP_SECONDS
from lsp_process import LspProcess, ProcessState, StartupCleanupError
from lsp_protocol import LspProtocol
from lsp_security import RepositorySource
from pyright_profile import (
    PYRIGHT_CONFIGURATION,
    PYRIGHT_INITIALIZATION_OPTIONS,
    thaw_pyright_profile_value,
)
from repository_scope import resolve_repository_scope

from tests.code_kernel_helpers import (
    SemanticPyrightFixture,
    create_semantic_pyright_fixture,
)
from tests.pyright_session_helpers import (
    _session,
    semantic_pyright,  # noqa: F401
)
from tests.slow_machine import SHORT_TIMEOUT


def _pending_cleanup_owner_ids() -> set[int]:
    return {
        id(coordinator)
        for coordinator in lsp_process.pending_startup_cleanup_snapshot()
    }


def _unregister_cleanup_owners(errors) -> None:
    for error in errors:
        coordinator = error.coordinator
        if coordinator is not None:
            lsp_process.unregister_startup_cleanup(coordinator)


def _acyclic_exception_graph(error: BaseException) -> list[BaseException]:
    """Every exception reachable through cause and context, proving no cycle."""
    pending = [error]
    seen: set[int] = set()
    reachable: list[BaseException] = []
    while pending:
        current = pending.pop()
        assert id(current) not in seen
        seen.add(id(current))
        reachable.append(current)
        pending.extend(_linked_exceptions(current))
    return reachable


def _linked_exceptions(error: BaseException) -> list[BaseException]:
    return [item for item in (error.__cause__, error.__context__) if item is not None]


def _client_message_payloads(events) -> list[dict]:
    return [event["message"] for event in events if event["kind"] == "client-message"]


def _client_messages_once_logged(semantic_pyright, expected: int, timeout: float = 30.0):  # noqa: F811
    """Wait for the fake server to log what it has already answered.

    The server appends to its event log after replying, so a hosted macOS
    runner could observe two of the three startup messages even though the
    session had recorded all three as readiness evidence.
    """
    deadline = time.monotonic() + timeout
    messages = _client_message_payloads(semantic_pyright.events())
    while len(messages) < expected and time.monotonic() < deadline:
        time.sleep(0.02)
        messages = _client_message_payloads(semantic_pyright.events())
    return messages


def _assert_start_command(command, semantic_pyright, owner_root: Path) -> None:  # noqa: F811
    assert command == (
        str(semantic_pyright.identity.node_executable),
        str(semantic_pyright.identity.server_executable),
        "--stdio",
        f"--cancellationReceive=file:{owner_root / 'cancellation'}",
    )


def _assert_start_options(options, scope) -> None:
    assert options["cwd"] == Path(scope.checkout_root)
    assert options["deadline"] > time.monotonic()
    assert set(options["server_request_handlers"]) == {
        "client/registerCapability",
        "client/unregisterCapability",
        "window/workDoneProgress/create",
        "workspace/configuration",
    }
    assert set(options["server_notification_handlers"]) == {
        "$/progress",
        "pyright/beginProgress",
        "pyright/endProgress",
        "pyright/reportProgress",
        "textDocument/publishDiagnostics",
    }


def _assert_owner_root_is_private(owner_root: Path, state_root: Path) -> None:
    assert owner_root.parent == state_root / "run/lsp"
    assert owner_root.parent.is_dir()
    assert not owner_root.parent.is_symlink()
    if os.name == "posix":
        assert stat.S_IMODE(owner_root.parent.stat().st_mode) == 0o700


def test_start_uses_exact_command_initialize_and_configuration_contract(
    monkeypatch: pytest.MonkeyPatch,
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,  # noqa: F811
) -> None:
    calls: list[tuple[tuple[str, ...], dict[str, object]]] = []
    real_start = LspProcess.start_configured.__func__

    def tracked_start(
        cls: type[LspProcess],
        command: object,
        **options: object,
    ) -> LspProcess:
        calls.append((tuple(command), dict(options)))  # type: ignore[arg-type]
        return real_start(cls, command, **options)  # type: ignore[arg-type]

    monkeypatch.setattr(LspProcess, "start_configured", classmethod(tracked_start))
    session = _session(repository, state_root, semantic_pyright)
    scope = resolve_repository_scope(repository)
    try:
        assert session.start(deadline=time.monotonic() + SHORT_TIMEOUT) is None
        assert session.readiness == "protocol_initialized"
        assert session.readiness_evidence == (
            "initialize",
            "initialized",
            "configuration",
        )
        assert session.position_encoding is PositionEncoding.UTF16
        assert dict(session.capabilities) == {
            "calls": True,
            "definition": True,
            "diagnostics": True,
            "document_symbols": True,
            "hover": True,
            "implementations": False,
            "references": True,
            "type_definition": True,
            "workspace_symbols": True,
        }

        assert len(calls) == 1
        command, options = calls[0]
        owner_root = options["owner_root"]
        assert isinstance(owner_root, Path)
        _assert_start_command(command, semantic_pyright, owner_root)
        _assert_start_options(options, scope)
        _assert_owner_root_is_private(owner_root, state_root)

        client_messages = _client_messages_once_logged(semantic_pyright, 3)
        assert [message["method"] for message in client_messages] == [
            "initialize",
            "initialized",
            "workspace/didChangeConfiguration",
        ]
        initialize = client_messages[0]["params"]
        root_uri = RepositorySource(
            scope.repository_id,
            scope.checkout_id,
            ".",
            Path(scope.checkout_root),
            Path(scope.checkout_root).as_uri(),
        ).uri
        assert initialize == {
            "processId": os.getpid(),
            "clientInfo": {"name": "llm-wiki"},
            "rootUri": root_uri,
            "workspaceFolders": [
                {"uri": root_uri, "name": Path(scope.checkout_root).name}
            ],
            "initializationOptions": thaw_pyright_profile_value(
                PYRIGHT_INITIALIZATION_OPTIONS
            ),
            "capabilities": {
                "general": {
                    "positionEncodings": ["utf-8", "utf-16", "utf-32"]
                },
                "textDocument": {
                    "callHierarchy": {"dynamicRegistration": False},
                    "definition": {
                        "dynamicRegistration": False,
                        "linkSupport": True,
                    },
                    "documentSymbol": {
                        "dynamicRegistration": False,
                        "hierarchicalDocumentSymbolSupport": True,
                    },
                    "hover": {
                        "dynamicRegistration": False,
                        "contentFormat": ["plaintext"],
                    },
                    "implementation": {
                        "dynamicRegistration": False,
                        "linkSupport": True,
                    },
                    "publishDiagnostics": {
                        "relatedInformation": True,
                        "versionSupport": True,
                    },
                    "references": {"dynamicRegistration": False},
                    "typeDefinition": {
                        "dynamicRegistration": False,
                        "linkSupport": True,
                    },
                },
                "window": {"workDoneProgress": True},
                "workspace": {
                    "configuration": True,
                    "symbol": {"dynamicRegistration": False},
                    "workspaceFolders": True,
                },
            },
        }
        settings = thaw_pyright_profile_value(PYRIGHT_CONFIGURATION)
        assert client_messages[2]["params"] == {"settings": settings}
        configuration = next(
            event
            for event in semantic_pyright.events()
            if event["kind"] == "configuration"
        )
        assert configuration["values"] == [
            settings["python"],
            settings["python"]["analysis"],
            settings["pyright"],
            None,
        ]
    finally:
        session.close(deadline=time.monotonic() + SHORT_TIMEOUT)
    assert not owner_root.exists()


@pytest.mark.parametrize(
    ("caller_seconds", "preflight_delay"),
    [(600.0, 0.0), (1.0, 0.05)],
)
def test_start_forwards_one_capped_deadline_without_shrinking_restart_budget(
    monkeypatch: pytest.MonkeyPatch,
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,  # noqa: F811
    caller_seconds: float,
    preflight_delay: float,
) -> None:
    session = _session(repository, state_root, semantic_pyright)
    captured: dict[str, float] = {}
    start_invoked: list[float] = []

    class CapturedProcess:
        state = ProcessState.PROTOCOL_INITIALIZED
        generation_nonce = "captured-generation"

        def close(self, _deadline: float) -> None:
            return None

    class CapturedGuard:
        def __init__(
            self,
            _path: Path,
            _expected_sha256: str,
            *,
            command: tuple[str, ...],
            owner_root: Path,
            deadline: float,
            degradation_prefix: str = "pyright",
        ) -> None:
            assert degradation_prefix == "pyright"
            assert command[1] == str(semantic_pyright.identity.server_executable)
            assert owner_root.parent == state_root / "run" / "lsp"
            captured["guard"] = deadline

        def __enter__(self) -> CapturedGuard:
            return self

        def verify(self) -> None:
            captured["verify"] = captured["guard"]

        def __exit__(self, error_type: object, *_error: object) -> None:
            if error_type is None:
                self.verify()

    def qualified_paths(*, deadline: float) -> tuple[Path, Path]:
        captured["paths"] = deadline
        if preflight_delay:
            time.sleep(preflight_delay)
        return (
            Path(semantic_pyright.identity.node_executable),  # type: ignore[arg-type]
            Path(semantic_pyright.identity.server_executable),  # type: ignore[arg-type]
        )

    def ensure_parent(_state_root: Path, *, deadline: float) -> Path:
        captured["parent"] = deadline
        return state_root / "run/lsp"

    def capture_start(
        cls: type[LspProcess],
        _command: object,
        *,
        deadline: float,
        bootstrap_timeout_seconds: float,
        generation_guard: object,
        **_options: object,
    ) -> CapturedProcess:
        assert cls is LspProcess
        assert callable(generation_guard)
        start_invoked.append(time.monotonic())
        captured["process"] = deadline
        captured["bootstrap_timeout"] = bootstrap_timeout_seconds
        guard = generation_guard("captured-generation", deadline)
        with guard:  # type: ignore[attr-defined]
            return CapturedProcess()

    monkeypatch.setattr(session, "_validated_qualified_paths", qualified_paths)
    monkeypatch.setattr(pyright_session_startup, "ensure_lsp_parent", ensure_parent)
    monkeypatch.setattr(pyright_session_startup, "LaunchServerGuard", CapturedGuard)
    monkeypatch.setattr(
        LspProcess,
        "start_configured",
        classmethod(capture_start),
    )
    before = time.monotonic()
    caller_deadline = before + caller_seconds
    try:
        session.start(deadline=caller_deadline)
        after = time.monotonic()
        effective = captured["process"]
        assert {
            captured["paths"],
            captured["parent"],
            captured["guard"],
            captured["verify"],
            effective,
        } == {effective}
        if caller_seconds > STARTUP_SECONDS:
            assert effective < caller_deadline
            assert before + STARTUP_SECONDS - 0.05 <= effective
            assert effective <= before + STARTUP_SECONDS + 0.05
        else:
            assert effective == caller_deadline
        expected_budget = min(caller_seconds, STARTUP_SECONDS)
        assert captured["bootstrap_timeout"] == pytest.approx(
            expected_budget,
            abs=0.05,
        )
        assert start_invoked
        invocation_remaining = effective - start_invoked[0]
        assert captured["bootstrap_timeout"] - invocation_remaining >= (
            preflight_delay - 0.02
        )
        assert effective > after
    finally:
        session.close(deadline=time.monotonic() + 1)


def test_expired_start_deadline_marks_not_ready_without_startup_work(
    monkeypatch: pytest.MonkeyPatch,
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,  # noqa: F811
) -> None:
    session = _session(repository, state_root, semantic_pyright)

    def forbidden(*_args: object, **_kwargs: object) -> None:
        pytest.fail("expired startup performed preflight or process work")

    monkeypatch.setattr(session, "_validated_qualified_paths", forbidden)
    monkeypatch.setattr(pyright_session_startup, "ensure_lsp_parent", forbidden)
    monkeypatch.setattr(LspProcess, "start_configured", forbidden)

    assert session.start(deadline=time.monotonic() - 1) is None
    assert session.readiness == "not_ready"
    assert session.readiness_evidence == ()
    assert session.position_encoding is None
    assert dict(session.capabilities) == {}
    assert session.degradation_codes == ("pyright_startup_timeout",)
    assert session.active_operations == 0
    assert not (state_root / "run/lsp").exists()


@pytest.mark.parametrize("failure_stage", ["verification", "startup_cleanup"])
def test_startup_cleanup_uses_effective_caller_deadline(
    monkeypatch: pytest.MonkeyPatch,
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,  # noqa: F811
    failure_stage: str,
) -> None:
    session = _session(repository, state_root, semantic_pyright)
    captured: dict[str, float] = {}

    class CapturedProcess:
        state = ProcessState.PROTOCOL_INITIALIZED
        generation_nonce = "captured-generation"

        def close(self, deadline: float) -> None:
            captured["process_cleanup"] = deadline

    class CapturedCleanupError(StartupCleanupError):
        def retry_cleanup(self, deadline: float) -> None:
            captured["startup_cleanup"] = deadline

    class FailingGuard:
        def __init__(
            self,
            _path: Path,
            _expected_sha256: str,
            *,
            command: tuple[str, ...],
            owner_root: Path,
            deadline: float,
            degradation_prefix: str = "pyright",
        ) -> None:
            assert degradation_prefix == "pyright"
            assert command[1] == str(semantic_pyright.identity.server_executable)
            assert owner_root.parent == state_root / "run" / "lsp"
            captured["effective"] = deadline
            self.deadline = deadline

        def __enter__(self) -> FailingGuard:
            return self

        def verify(self) -> None:
            if failure_stage == "verification":
                raise RuntimeError("post-bootstrap verification failed")

        def __exit__(self, error_type: object, *_error: object) -> None:
            captured["guard_exit"] = self.deadline
            if error_type is None:
                self.verify()

    def qualified_paths(*, deadline: float) -> tuple[Path, Path]:
        assert deadline == captured.get("effective", deadline)
        return (
            Path(semantic_pyright.identity.node_executable),  # type: ignore[arg-type]
            Path(semantic_pyright.identity.server_executable),  # type: ignore[arg-type]
        )

    def ensure_parent(_state_root: Path, *, deadline: float) -> Path:
        captured.setdefault("effective", deadline)
        assert deadline == captured["effective"]
        return state_root / "run/lsp"

    def capture_start(
        cls: type[LspProcess],
        _command: object,
        *,
        deadline: float,
        bootstrap_timeout_seconds: float,
        generation_guard: object,
        **_options: object,
    ) -> CapturedProcess:
        assert cls is LspProcess
        assert deadline == captured["effective"]
        assert 0 < bootstrap_timeout_seconds <= STARTUP_SECONDS
        assert callable(generation_guard)
        guard = generation_guard("captured-generation", deadline)
        with guard:  # type: ignore[attr-defined]
            if failure_stage == "startup_cleanup":
                raise CapturedCleanupError("configured startup cleanup failed")
            return CapturedProcess()

    monkeypatch.setattr(session, "_validated_qualified_paths", qualified_paths)
    monkeypatch.setattr(pyright_session_startup, "ensure_lsp_parent", ensure_parent)
    monkeypatch.setattr(pyright_session_startup, "LaunchServerGuard", FailingGuard)
    monkeypatch.setattr(
        LspProcess,
        "start_configured",
        classmethod(capture_start),
    )
    caller_deadline = time.monotonic() + 0.5

    session.start(deadline=caller_deadline)

    assert captured["guard_exit"] == captured["effective"] == caller_deadline
    if failure_stage == "startup_cleanup":
        assert captured["startup_cleanup"] == caller_deadline
    else:
        assert "process_cleanup" not in captured
    assert session.readiness == "not_ready"
    assert session.degradation_codes == ("pyright_startup_failed",)


def test_close_retries_startup_cleanup_retained_by_session(
    monkeypatch: pytest.MonkeyPatch,
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,  # noqa: F811
) -> None:
    session = _session(repository, state_root, semantic_pyright)
    cleanup_deadlines: list[float] = []

    class RetainedCleanupError(StartupCleanupError):
        def retry_cleanup(self, deadline: float) -> None:
            cleanup_deadlines.append(deadline)
            if len(cleanup_deadlines) == 1:
                raise TimeoutError("startup owner remains live")

    def fail_start(*_args: object, **_kwargs: object) -> None:
        raise RetainedCleanupError("configured startup retained ownership")

    monkeypatch.setattr(LspProcess, "start_configured", fail_start)

    session.start(deadline=time.monotonic() + SHORT_TIMEOUT)
    assert len(cleanup_deadlines) == 1

    session.close(deadline=time.monotonic() + SHORT_TIMEOUT)

    assert len(cleanup_deadlines) == 2
    assert cleanup_deadlines[1] > time.monotonic()


def test_close_unwraps_retained_cleanup_interruption_and_preserves_owner(
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,  # noqa: F811
) -> None:
    session = _session(repository, state_root, semantic_pyright)
    attempts = 0

    def make_interruption() -> KeyboardInterrupt:
        try:
            raise KeyboardInterrupt("direct close cleanup interruption")
        except KeyboardInterrupt as error:
            return error

    interruption = make_interruption()
    wrapper = RuntimeError("direct close cleanup wrapper")
    wrapper.__cause__ = interruption

    class RetainedCleanupError(StartupCleanupError):
        def retry_cleanup(self, _deadline: float) -> None:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise wrapper

    retained = RetainedCleanupError("startup cleanup ownership retained")
    with session._lock:
        session._startup_cleanup_error = retained

    try:
        with pytest.raises(
            KeyboardInterrupt,
            match="direct close cleanup interruption",
        ) as raised:
            session.close(deadline=time.monotonic() + 5)

        reachable = _acyclic_exception_graph(raised.value)

        assert raised.value is interruption
        assert wrapper in reachable
        assert wrapper.__cause__ is None
        assert wrapper.__context__ is None
        assert session._startup_cleanup_error is retained
        assert session._closed is False
        assert attempts == 1
    finally:
        session.close(deadline=time.monotonic() + 5)

    assert attempts == 2


def test_close_unwraps_retained_process_interruption_and_preserves_owner(
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,  # noqa: F811
) -> None:
    session = _session(repository, state_root, semantic_pyright)
    attempts = 0
    interruption = SystemExit(79)
    wrapper: RuntimeError | None = None

    class RetainedProcess:
        state = ProcessState.PROTOCOL_INITIALIZED
        generation_nonce = "direct-close-retained-generation"

        @staticmethod
        def close(_deadline: float) -> None:
            nonlocal attempts, wrapper
            attempts += 1
            if attempts == 1:
                try:
                    raise interruption
                except SystemExit:
                    wrapper = RuntimeError("direct close process wrapper")
                    raise wrapper

    retained = RetainedProcess()
    with session._lock:
        session._startup_process = retained  # type: ignore[assignment]

    try:
        with pytest.raises(SystemExit) as raised:
            session.close(deadline=time.monotonic() + 5)

        reachable = _acyclic_exception_graph(raised.value)

        assert raised.value is interruption
        assert wrapper is not None and wrapper in reachable
        assert wrapper.__cause__ is None
        assert wrapper.__context__ is None
        assert session._startup_process is retained
        assert session._closed is False
        assert attempts == 1
    finally:
        session.close(deadline=time.monotonic() + 5)

    assert attempts == 2


def test_start_retries_retained_cleanup_before_new_spawn(
    monkeypatch: pytest.MonkeyPatch,
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,  # noqa: F811
) -> None:
    session = _session(repository, state_root, semantic_pyright)
    cleanup_attempts = 0
    start_attempts = 0

    class RetainedCleanupError(StartupCleanupError):
        def retry_cleanup(self, _deadline: float) -> None:
            nonlocal cleanup_attempts
            cleanup_attempts += 1
            if cleanup_attempts == 1:
                raise TimeoutError("startup owner remains live")

    class CapturedProcess:
        state = ProcessState.PROTOCOL_INITIALIZED
        generation_nonce = "captured-generation"

        @staticmethod
        def close(_deadline: float) -> None:
            return None

    def start(*_args: object, **_kwargs: object) -> CapturedProcess:
        nonlocal start_attempts
        start_attempts += 1
        if start_attempts == 1:
            raise RetainedCleanupError("configured startup retained ownership")
        return CapturedProcess()

    monkeypatch.setattr(LspProcess, "start_configured", start)

    session.start(deadline=time.monotonic() + SHORT_TIMEOUT)
    session.start(deadline=time.monotonic() + SHORT_TIMEOUT)

    assert cleanup_attempts == 2
    assert start_attempts == 2
    assert session._process is not None
    session.close(deadline=time.monotonic() + SHORT_TIMEOUT)


def test_session_adopts_cleanup_owner_without_exhausting_global_registry(
    monkeypatch: pytest.MonkeyPatch,
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,  # noqa: F811
) -> None:
    baseline = _pending_cleanup_owner_ids()
    sessions: list[PyrightSession] = []
    errors: list[StartupCleanupError] = []
    cleanup_attempts: dict[int, int] = {}

    class RetainedCleanupError(StartupCleanupError):
        def retry_cleanup(self, _deadline: float) -> None:
            coordinator = self.coordinator
            assert coordinator is not None
            key = id(coordinator)
            cleanup_attempts[key] = cleanup_attempts.get(key, 0) + 1
            if cleanup_attempts[key] == 1:
                raise TimeoutError("startup owner remains live")

    def fail_start(*_args: object, **_kwargs: object) -> None:
        coordinator = lsp_process.LifecycleCoordinator(None)
        lsp_process.register_startup_cleanup(coordinator)
        error = RetainedCleanupError(
            "configured startup retained ownership",
            coordinator=coordinator,
        )
        errors.append(error)
        raise error

    monkeypatch.setattr(LspProcess, "start_configured", fail_start)
    try:
        for _index in range(lsp_process.MAX_STARTUP_CLEANUP_OWNERS + 1):
            session = _session(repository, state_root, semantic_pyright)
            sessions.append(session)
            session.start(deadline=time.monotonic() + SHORT_TIMEOUT)

        assert len(errors) == lsp_process.MAX_STARTUP_CLEANUP_OWNERS + 1
        assert _pending_cleanup_owner_ids() == baseline
        assert [session._startup_cleanup_error for session in sessions] == errors
    finally:
        for session in sessions:
            session.close(deadline=time.monotonic() + SHORT_TIMEOUT)
        _unregister_cleanup_owners(errors)


def test_failed_session_atexit_registration_keeps_global_cleanup_owner(
    monkeypatch: pytest.MonkeyPatch,
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,  # noqa: F811
) -> None:
    session = _session(repository, state_root, semantic_pyright)
    coordinator = lsp_process.LifecycleCoordinator(None)
    lsp_process.register_startup_cleanup(coordinator)
    error = StartupCleanupError(
        "configured startup retained ownership",
        coordinator=coordinator,
    )

    def fail_registration(_callback: object) -> None:
        raise RuntimeError("atexit registration failed")

    monkeypatch.setattr(pyright_session_core.atexit, "register", fail_registration)
    try:
        with session._lock, pytest.raises(RuntimeError, match="atexit registration"):
            session._retain_startup_cleanup_locked(error)

        assert coordinator in lsp_process.pending_startup_cleanup_snapshot()
        assert session._startup_atexit_registered is False
    finally:
        lsp_process.unregister_startup_cleanup(coordinator)
        with session._lock:
            session._startup_cleanup_error = None


def test_interrupt_during_retained_cleanup_is_rethrown_and_next_start_recovers(
    monkeypatch: pytest.MonkeyPatch,
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,  # noqa: F811
) -> None:
    session = _session(repository, state_root, semantic_pyright)
    cleanup_attempts = 0
    start_attempts = 0

    class InterruptingCleanupError(StartupCleanupError):
        def retry_cleanup(self, _deadline: float) -> None:
            nonlocal cleanup_attempts
            cleanup_attempts += 1
            if cleanup_attempts == 1:
                raise SystemExit(17)

    retained = InterruptingCleanupError("startup owner remains live")

    class CapturedProcess:
        state = ProcessState.PROTOCOL_INITIALIZED
        generation_nonce = "captured-generation"

        @staticmethod
        def close(_deadline: float) -> None:
            return None

    def start(*_args: object, **_kwargs: object) -> CapturedProcess:
        nonlocal start_attempts
        start_attempts += 1
        if start_attempts == 1:
            raise retained
        return CapturedProcess()

    monkeypatch.setattr(LspProcess, "start_configured", start)

    with pytest.raises(SystemExit) as raised:
        session.start(deadline=time.monotonic() + 5)
    assert raised.value.code == 17
    assert session._startup_cleanup_error is retained
    assert session._starting is False

    session.start(deadline=time.monotonic() + 5)
    assert cleanup_attempts == 2
    assert start_attempts == 2
    assert session._process is not None
    session.close(deadline=time.monotonic() + 5)


def test_wrapped_interruption_during_retained_cleanup_is_rethrown_with_owner(
    monkeypatch: pytest.MonkeyPatch,
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,  # noqa: F811
) -> None:
    session = _session(repository, state_root, semantic_pyright)
    attempts = 0

    def make_interruption() -> KeyboardInterrupt:
        try:
            raise KeyboardInterrupt("wrapped retained cleanup interruption")
        except KeyboardInterrupt as error:
            return error

    interruption = make_interruption()
    wrapper = RuntimeError("retained cleanup interruption wrapper")
    wrapper.__cause__ = interruption

    class RetainedCleanupError(StartupCleanupError):
        def retry_cleanup(self, _deadline: float) -> None:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise wrapper

    retained = RetainedCleanupError("startup cleanup ownership retained")
    with session._lock:
        session._startup_cleanup_error = retained
        session._startup_attempted = True

    def forbidden_spawn(*_args: object, **_kwargs: object) -> None:
        pytest.fail("wrapped cleanup interruption allowed a new spawn")

    monkeypatch.setattr(LspProcess, "start_configured", forbidden_spawn)
    try:
        with pytest.raises(
            KeyboardInterrupt,
            match="wrapped retained cleanup interruption",
        ) as raised:
            session.start(deadline=time.monotonic() + 5)

        reachable = _acyclic_exception_graph(raised.value)

        assert raised.value is interruption
        assert wrapper in reachable
        assert wrapper.__cause__ is None
        assert wrapper.__context__ is None
        assert session._startup_cleanup_error is retained
        assert session._starting is False
        assert attempts == 1
    finally:
        session.close(deadline=time.monotonic() + 5)


def test_wrapped_interruption_during_retained_process_close_is_rethrown_with_owner(
    monkeypatch: pytest.MonkeyPatch,
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,  # noqa: F811
) -> None:
    session = _session(repository, state_root, semantic_pyright)
    attempts = 0
    interruption = SystemExit(73)
    wrapper: RuntimeError | None = None

    class RetainedProcess:
        state = ProcessState.PROTOCOL_INITIALIZED
        generation_nonce = "retained-process-generation"

        @staticmethod
        def close(_deadline: float) -> None:
            nonlocal attempts, wrapper
            attempts += 1
            if attempts == 1:
                try:
                    raise interruption
                except SystemExit:
                    wrapper = RuntimeError("retained process close interruption wrapper")
                    raise wrapper

    retained = RetainedProcess()
    with session._lock:
        session._startup_process = retained  # type: ignore[assignment]
        session._startup_attempted = True

    def forbidden_spawn(*_args: object, **_kwargs: object) -> None:
        pytest.fail("wrapped process interruption allowed a new spawn")

    monkeypatch.setattr(LspProcess, "start_configured", forbidden_spawn)
    try:
        with pytest.raises(SystemExit) as raised:
            session.start(deadline=time.monotonic() + 5)

        reachable = _acyclic_exception_graph(raised.value)

        assert raised.value is interruption
        assert wrapper is not None and wrapper in reachable
        assert wrapper.__cause__ is None
        assert wrapper.__context__ is None
        assert session._startup_process is retained
        assert session._starting is False
        assert attempts == 1
    finally:
        session.close(deadline=time.monotonic() + 5)


def test_startup_interruption_traverses_implicit_context() -> None:
    interruption = SystemExit(23)
    try:
        try:
            raise interruption
        except SystemExit:
            raise RuntimeError("startup wrapper")
    except RuntimeError as wrapper:
        assert pyright_session_startup.startup_interruption(wrapper) is interruption


def test_startup_rollback_rethrows_interruption_and_retains_process_owner(
    monkeypatch: pytest.MonkeyPatch,
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,  # noqa: F811
) -> None:
    session = _session(repository, state_root, semantic_pyright)
    close_calls = 0
    sync_calls = 0

    class CapturedProcess:
        state = ProcessState.PROTOCOL_INITIALIZED
        generation_nonce = "captured-generation"

        @staticmethod
        def close(_deadline: float) -> None:
            nonlocal close_calls
            close_calls += 1
            if close_calls == 1:
                raise KeyboardInterrupt("rollback interrupted")

    process = CapturedProcess()

    def fail_publication_once() -> None:
        nonlocal sync_calls
        sync_calls += 1
        if sync_calls == 1:
            raise RuntimeError("publication failed")

    monkeypatch.setattr(
        LspProcess,
        "start_configured",
        lambda *_args, **_kwargs: process,
    )
    monkeypatch.setattr(session, "_sync_startup_atexit_locked", fail_publication_once)

    with pytest.raises(KeyboardInterrupt, match="rollback interrupted"):
        session.start(deadline=time.monotonic() + 5)

    assert session._process is None
    assert session._startup_process is process
    assert session._starting is False
    session.close(deadline=time.monotonic() + 5)
    assert close_calls == 2


def test_startup_original_interruption_outranks_ordinary_rollback_error(
    monkeypatch: pytest.MonkeyPatch,
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,  # noqa: F811
) -> None:
    session = _session(repository, state_root, semantic_pyright)
    close_calls = 0
    sync_calls = 0

    class CapturedProcess:
        state = ProcessState.PROTOCOL_INITIALIZED
        generation_nonce = "captured-generation"

        @staticmethod
        def close(_deadline: float) -> None:
            nonlocal close_calls
            close_calls += 1
            if close_calls == 1:
                raise RuntimeError("ordinary startup rollback error")

    process = CapturedProcess()

    def interrupt_publication_once() -> None:
        nonlocal sync_calls
        sync_calls += 1
        if sync_calls == 1:
            raise KeyboardInterrupt("startup publication interrupted")

    monkeypatch.setattr(
        LspProcess,
        "start_configured",
        lambda *_args, **_kwargs: process,
    )
    monkeypatch.setattr(
        session,
        "_sync_startup_atexit_locked",
        interrupt_publication_once,
    )

    with pytest.raises(
        KeyboardInterrupt,
        match="startup publication interrupted",
    ) as raised:
        session.start(deadline=time.monotonic() + 5)

    assert isinstance(raised.value.__cause__, RuntimeError)
    assert str(raised.value.__cause__) == "ordinary startup rollback error"
    assert session._process is None
    assert session._startup_process is process
    assert session._starting is False
    session.close(deadline=time.monotonic() + 5)
    assert close_calls == 2


def test_startup_process_wrapper_is_reachable_without_exception_graph_revisit(
    monkeypatch: pytest.MonkeyPatch,
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,  # noqa: F811
) -> None:
    session = _session(repository, state_root, semantic_pyright)

    def make_interruption() -> KeyboardInterrupt:
        try:
            raise KeyboardInterrupt("wrapped session process interruption")
        except KeyboardInterrupt as error:
            return error

    interruption = make_interruption()
    wrapper = RuntimeError("session process startup wrapper")
    wrapper.__cause__ = interruption

    def interrupt_process_start(*_args: object, **_kwargs: object) -> LspProcess:
        raise wrapper

    monkeypatch.setattr(LspProcess, "start_configured", interrupt_process_start)

    with pytest.raises(
        KeyboardInterrupt,
        match="wrapped session process interruption",
    ) as raised:
        session.start(deadline=time.monotonic() + SHORT_TIMEOUT)

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
    assert session._process is None
    assert session._startup_process is None
    assert session._starting is False


def test_startup_rollback_unwraps_interruption_without_exception_cycle(
    monkeypatch: pytest.MonkeyPatch,
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,  # noqa: F811
) -> None:
    session = _session(repository, state_root, semantic_pyright)
    close_calls = 0
    sync_calls = 0
    cleanup_error = RuntimeError("ordinary wrapped startup rollback error")

    class CapturedProcess:
        state = ProcessState.PROTOCOL_INITIALIZED
        generation_nonce = "captured-generation"

        @staticmethod
        def close(_deadline: float) -> None:
            nonlocal close_calls
            close_calls += 1
            if close_calls == 1:
                raise cleanup_error

    process = CapturedProcess()

    def make_interruption() -> KeyboardInterrupt:
        try:
            raise KeyboardInterrupt("wrapped startup publication interruption")
        except KeyboardInterrupt as error:
            return error

    interruption = make_interruption()
    wrapper = RuntimeError("startup publication wrapper")
    wrapper.__cause__ = interruption

    def interrupt_publication_once() -> None:
        nonlocal sync_calls
        sync_calls += 1
        if sync_calls == 1:
            raise wrapper

    monkeypatch.setattr(
        LspProcess,
        "start_configured",
        lambda *_args, **_kwargs: process,
    )
    monkeypatch.setattr(
        session,
        "_sync_startup_atexit_locked",
        interrupt_publication_once,
    )

    with pytest.raises(
        KeyboardInterrupt,
        match="wrapped startup publication interruption",
    ) as raised:
        session.start(deadline=time.monotonic() + 5)

    assert raised.value is interruption
    assert raised.value.__cause__ is cleanup_error
    assert raised.value.__context__ is None
    assert cleanup_error.__context__ is None
    assert wrapper.__cause__ is interruption
    assert session._process is None
    assert session._startup_process is process
    assert session._starting is False
    session.close(deadline=time.monotonic() + 5)
    assert close_calls == 2


def test_keyboard_interrupt_resets_starting_and_notifies_waiting_start(
    monkeypatch: pytest.MonkeyPatch,
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,  # noqa: F811
) -> None:
    session = _session(repository, state_root, semantic_pyright)
    entered = threading.Event()
    release = threading.Event()
    calls = 0
    first_errors: list[BaseException] = []
    second_errors: list[BaseException] = []

    class CapturedProcess:
        state = ProcessState.PROTOCOL_INITIALIZED
        generation_nonce = "captured-generation"

        @staticmethod
        def close(_deadline: float) -> None:
            return None

    def start(*_args: object, **_kwargs: object) -> CapturedProcess:
        nonlocal calls
        calls += 1
        if calls == 1:
            entered.set()
            assert release.wait(SHORT_TIMEOUT)
            raise KeyboardInterrupt("interrupted startup")
        return CapturedProcess()

    def first_start() -> None:
        try:
            session.start(deadline=time.monotonic() + 2)
        except BaseException as error:
            first_errors.append(error)

    def second_start() -> None:
        try:
            session.start(deadline=time.monotonic() + 2)
        except BaseException as error:
            second_errors.append(error)

    monkeypatch.setattr(LspProcess, "start_configured", start)
    first = threading.Thread(target=first_start)
    second = threading.Thread(target=second_start)
    first.start()
    assert entered.wait(SHORT_TIMEOUT)
    second.start()
    try:
        time.sleep(0.05)
        assert second.is_alive()
        release.set()
        first.join(SHORT_TIMEOUT)
        second.join(SHORT_TIMEOUT)
        assert not first.is_alive()
        assert not second.is_alive()
        assert len(first_errors) == 1
        assert isinstance(first_errors[0], KeyboardInterrupt)
        assert second_errors == []
        assert session._starting is False
        assert session._process is not None
    finally:
        release.set()
        first.join(SHORT_TIMEOUT)
        second.join(SHORT_TIMEOUT)
        session.close(deadline=time.monotonic() + 5)


def test_concurrent_start_waits_for_process_publication(
    monkeypatch: pytest.MonkeyPatch,
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,  # noqa: F811
) -> None:
    session = _session(repository, state_root, semantic_pyright)
    start_configured = LspProcess.start_configured
    bootstrap_finished = threading.Event()
    publish_process = threading.Event()
    second_entered = threading.Event()
    second_finished = threading.Event()
    errors: list[BaseException] = []

    def delay_publication(*args: object, **kwargs: object) -> LspProcess:
        process = start_configured(*args, **kwargs)  # type: ignore[arg-type]
        bootstrap_finished.set()
        assert publish_process.wait(SHORT_TIMEOUT)
        return process

    def start_first() -> None:
        try:
            session.start(deadline=time.monotonic() + SHORT_TIMEOUT)
        except BaseException as error:
            errors.append(error)

    def start_second() -> None:
        second_entered.set()
        try:
            session.start(deadline=time.monotonic() + SHORT_TIMEOUT)
        except BaseException as error:
            errors.append(error)
        finally:
            second_finished.set()

    monkeypatch.setattr(LspProcess, "start_configured", delay_publication)
    first = threading.Thread(target=start_first)
    second = threading.Thread(target=start_second)
    first.start()
    assert bootstrap_finished.wait(SHORT_TIMEOUT), errors
    assert session.readiness == "protocol_initialized"
    second.start()
    assert second_entered.wait(SHORT_TIMEOUT)
    try:
        assert not second_finished.wait(0.1)
    finally:
        publish_process.set()
        first.join(SHORT_TIMEOUT)
        second.join(SHORT_TIMEOUT)
        session.close(deadline=time.monotonic() + SHORT_TIMEOUT)

    assert not first.is_alive()
    assert not second.is_alive()
    assert errors == []


@pytest.mark.parametrize(
    ("encoding", "expected"),
    [
        (None, PositionEncoding.UTF16),
        ("utf-8", PositionEncoding.UTF8),
        ("utf-16", PositionEncoding.UTF16),
        ("utf-32", PositionEncoding.UTF32),
    ],
)
def test_position_encoding_is_negotiated_or_defaults_to_utf16(
    repository: Path,
    state_root: Path,
    encoding: str | None,
    expected: PositionEncoding,
) -> None:
    capabilities: dict[str, object] = {"documentSymbolProvider": True}
    if encoding is not None:
        capabilities["positionEncoding"] = encoding
    fixture = create_semantic_pyright_fixture(
        repository,
        config={"capabilities": capabilities},
    )
    session = _session(repository, state_root, fixture)
    try:
        session.start(deadline=time.monotonic() + SHORT_TIMEOUT)
        assert session.readiness == "protocol_initialized"
        assert session.position_encoding is expected
    finally:
        session.close(deadline=time.monotonic() + SHORT_TIMEOUT)


def test_unsupported_server_position_encoding_fails_closed(
    repository: Path,
    state_root: Path,
) -> None:
    fixture = create_semantic_pyright_fixture(
        repository,
        config={
            "capabilities": {
                "documentSymbolProvider": True,
                "positionEncoding": "utf-7",
            }
        },
    )
    session = _session(repository, state_root, fixture)

    assert session.start(deadline=time.monotonic() + SHORT_TIMEOUT) is None
    assert session.readiness == "not_ready"
    assert session.position_encoding is None
    assert session.degradation_codes == ("pyright_position_encoding_unsupported",)
    owners = tuple((state_root / "run/lsp").iterdir())
    assert len(owners) == 1
    assert not (owners[0] / "lease.json").exists()


def test_operational_startup_failure_returns_stable_not_ready_degradation(
    repository: Path,
    state_root: Path,
) -> None:
    fixture = create_semantic_pyright_fixture(
        repository,
        config={"initialize_behavior": "broken"},
    )
    session = _session(repository, state_root, fixture)

    assert session.start(deadline=time.monotonic() + SHORT_TIMEOUT) is None
    assert session.readiness == "not_ready"
    assert session.readiness_evidence == ()
    assert session.degradation_codes == ("pyright_startup_failed",)
    owners = tuple((state_root / "run/lsp").iterdir())
    assert len(owners) == 1
    assert not (owners[0] / "lease.json").exists()


def test_operational_startup_timeout_after_owner_evidence_is_deterministic(
    monkeypatch: pytest.MonkeyPatch,
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,  # noqa: F811
) -> None:
    session = _session(repository, state_root, semantic_pyright)
    request = LspProtocol.request
    injected_owners: list[Path] = []

    def timeout_initialize(
        protocol: LspProtocol,
        method: str,
        params: object,
        *,
        deadline: float,
        cancellation: object = None,
    ) -> object:
        if method == "initialize":
            owners = tuple((state_root / "run/lsp").iterdir())
            assert len(owners) == 1
            assert (owners[0] / "owner.json").is_file()
            assert (owners[0] / "lease.json").is_file()
            assert time.monotonic() < deadline
            injected_owners.extend(owners)
            raise TimeoutError("injected initialize deadline expired")
        return request(
            protocol,
            method,
            params,
            deadline=deadline,
            cancellation=cancellation,  # type: ignore[arg-type]
        )

    monkeypatch.setattr(
        LspProtocol,
        "request",
        timeout_initialize,
    )

    assert session.start(deadline=time.monotonic() + 10) is None
    assert len(injected_owners) == 1
    assert session.readiness == "not_ready"
    assert session.readiness_evidence == ()
    assert session.degradation_codes == ("pyright_startup_timeout",)
    owners = tuple((state_root / "run/lsp").iterdir())
    assert owners == tuple(injected_owners)
    assert not (owners[0] / "lease.json").exists()


@pytest.mark.parametrize(
    ("deadline", "error"),
    [
        (True, TypeError),
        (float("nan"), ValueError),
        (float("inf"), ValueError),
    ],
)
def test_start_programmer_validation_errors_propagate(
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,  # noqa: F811
    deadline: object,
    error: type[Exception],
) -> None:
    session = _session(repository, state_root, semantic_pyright)
    with pytest.raises(error):
        session.start(deadline=deadline)  # type: ignore[arg-type]
    assert not (state_root / "run/lsp").exists()
