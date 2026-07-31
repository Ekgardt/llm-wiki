"""Readiness and semantic normalization tests for the pinned Pyright session."""

from __future__ import annotations

import hashlib
import inspect
import json
import os
import shutil
import stat
import threading
import time
from dataclasses import FrozenInstanceError, fields, replace
from pathlib import Path
from types import MappingProxyType
from typing import get_type_hints

import lsp_process
import pyright_session as pyright_session_module
import pytest
from code_intelligence import PositionEncoding
from lsp_positions import LspPosition, LspRange, SourceAnchor, SourceDocument
from lsp_process import LspProcess, ProcessState, StartupCleanupError
from lsp_protocol import MAX_FRAME_BYTES, ProtocolViolation
from lsp_security import RepositorySource
from pyright_profile import (
    PYRIGHT_CONFIGURATION,
    PYRIGHT_INITIALIZATION_OPTIONS,
    PyrightIdentity,
    thaw_pyright_profile_value,
)
from pyright_session import (
    MAX_LSP_PROCESSES,
    STARTUP_SECONDS,
    LspDiagnostic,
    LspLocation,
    OpenDocument,
    ProviderCalls,
    ProviderDiagnostics,
    ProviderHover,
    ProviderLocations,
    PyrightSession,
)
from repository_scope import RepositoryScope, resolve_repository_scope

from tests.code_kernel_helpers import (
    SemanticPyrightFixture,
    create_semantic_pyright_fixture,
)


@pytest.fixture
def semantic_pyright(repository: Path) -> SemanticPyrightFixture:
    return create_semantic_pyright_fixture(repository)


def _session(
    repository: Path,
    state_root: Path,
    fixture: SemanticPyrightFixture,
) -> PyrightSession:
    return PyrightSession(
        resolve_repository_scope(repository),
        fixture.identity,  # type: ignore[arg-type]
        state_root=state_root,
    )


def _anchor(repository: Path, path: str, line: int, character: int) -> SourceAnchor:
    return SourceDocument.from_bytes(path, (repository / path).read_bytes()).validate_anchor(
        line=line,
        character=character,
    )


def _missing_identity() -> PyrightIdentity:
    return PyrightIdentity(
        status="missing",
        source=None,
        version=None,
        node_executable=None,
        node_version=None,
        node_major=None,
        server_executable=None,
        executable_sha256=None,
        package_sha256=None,
        initialization_options_sha256="a" * 64,
        configuration_sha256="b" * 64,
        qualified=False,
        degradation_codes=("pyright_missing",),
    )


def _copied_server_identity(
    repository: Path,
    fixture: SemanticPyrightFixture,
) -> tuple[Path, PyrightIdentity]:
    source = Path(fixture.identity.server_executable)  # type: ignore[union-attr]
    server = repository / "qualified-server.py"
    content = source.read_bytes()
    server.write_bytes(content)
    return server, replace(
        fixture.identity,
        server_executable=server,
        executable_sha256=hashlib.sha256(content).hexdigest(),
    )


def test_public_contract_and_initial_state(
    repository: Path,
    state_root: Path,
) -> None:
    assert STARTUP_SECONDS == 60.0
    assert MAX_LSP_PROCESSES == 4
    assert inspect.signature(PyrightSession) == inspect.Signature(
        parameters=(
            inspect.Parameter(
                "repository",
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
                annotation=RepositoryScope,
            ),
            inspect.Parameter(
                "identity",
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
                annotation=PyrightIdentity,
            ),
            inspect.Parameter(
                "state_root",
                inspect.Parameter.KEYWORD_ONLY,
                annotation=Path,
            ),
        ),
        return_annotation=None,
    )

    expected_fields = {
        OpenDocument: ("source", "content", "source_sha256", "version"),
        LspLocation: ("uri", "range"),
        ProviderLocations: ("locations", "coverage", "partial"),
        ProviderHover: ("contents", "range", "partial"),
        ProviderCalls: ("direction", "locations", "coverage", "partial"),
        LspDiagnostic: (
            "uri",
            "range",
            "severity",
            "code",
            "message",
            "related",
        ),
        ProviderDiagnostics: ("diagnostics", "document_version", "partial"),
    }
    for value_type, names in expected_fields.items():
        assert tuple(field.name for field in fields(value_type)) == names
        assert value_type.__slots__ == names

    source = RepositorySource(
        "repository:test",
        "checkout:test",
        "pkg/service.py",
        repository / "pkg/service.py",
        "file:///repository/pkg/service.py",
    )
    location = LspLocation(
        source.uri,
        LspRange(LspPosition(0, 0), LspPosition(0, 1)),
    )
    document = OpenDocument(source, b"x", "c" * 64, 1)
    diagnostic = LspDiagnostic(
        source.uri,
        location.range,
        2,
        "reportGeneralTypeIssues",
        "message",
        ((location, "related"),),
    )
    values = (
        document,
        location,
        ProviderLocations((location,), "provider_reported", False),
        ProviderHover("hover", location.range, False),
        ProviderCalls("incoming", (location,), "provider_reported", True),
        diagnostic,
        ProviderDiagnostics((diagnostic,), 1, False),
    )
    for value in values:
        with pytest.raises(FrozenInstanceError):
            setattr(value, fields(type(value))[0].name, None)

    scope = resolve_repository_scope(repository)
    before = time.monotonic()
    session = PyrightSession(scope, _missing_identity(), state_root=state_root)
    assert session.readiness == "not_ready"
    assert session.readiness_evidence == ()
    assert session.position_encoding is None
    assert session.degradation_codes == ("pyright_missing",)
    assert isinstance(session.capabilities, MappingProxyType)
    assert dict(session.capabilities) == {}
    assert session.active_operations == 0
    assert before <= session.last_used_monotonic <= time.monotonic()
    session.close(deadline=time.monotonic() + 1)

    assert get_type_hints(PyrightSession.__init__) == {
        "repository": RepositoryScope,
        "identity": PyrightIdentity,
        "state_root": Path,
        "return": type(None),
    }


def test_unqualified_identity_never_spawns_or_creates_lsp_parent(
    monkeypatch: pytest.MonkeyPatch,
    repository: Path,
    state_root: Path,
) -> None:
    def forbidden(*_args: object, **_kwargs: object) -> None:
        pytest.fail("an unqualified identity attempted to spawn Pyright")

    monkeypatch.setattr(LspProcess, "start_configured", forbidden)
    session = PyrightSession(
        resolve_repository_scope(repository),
        _missing_identity(),
        state_root=state_root,
    )

    assert session.start(deadline=time.monotonic() + 1) is None
    assert session.readiness == "not_ready"
    assert session.readiness_evidence == ()
    assert session.degradation_codes == ("pyright_missing",)
    assert not (state_root / "run/lsp").exists()


def test_server_mutated_after_qualification_never_spawns(
    monkeypatch: pytest.MonkeyPatch,
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,
) -> None:
    server, identity = _copied_server_identity(repository, semantic_pyright)
    server.write_bytes(server.read_bytes() + b"\n# changed after qualification\n")

    def forbidden(*_args: object, **_kwargs: object) -> None:
        pytest.fail("a hash-mismatched Pyright server was spawned")

    monkeypatch.setattr(
        lsp_process.ProcessTree,
        "_spawn_with_deadline",
        classmethod(forbidden),
    )
    session = PyrightSession(
        resolve_repository_scope(repository),
        identity,
        state_root=state_root,
    )

    session.start(deadline=time.monotonic() + 5)

    assert session.readiness == "not_ready"
    assert session.readiness_evidence == ()
    assert session.degradation_codes == ("pyright_executable_digest_mismatch",)


@pytest.mark.skipif(os.name != "posix", reason="POSIX verified launch descriptor")
def test_server_replaced_at_spawn_never_executes_replacement(
    monkeypatch: pytest.MonkeyPatch,
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,
) -> None:
    server, identity = _copied_server_identity(repository, semantic_pyright)
    retired = repository / "retired-server.py"
    replacement_marker = repository / "replacement-executed"
    real_spawn = lsp_process.ProcessTree._spawn_with_deadline.__func__
    replaced = False

    def replace_before_spawn(
        cls: type[lsp_process.ProcessTree],
        command: object,
        *,
        cwd: Path,
        env: object,
        deadline: float,
        pass_fds: object = (),
    ) -> lsp_process.ProcessTree:
        nonlocal replaced
        if not replaced:
            replaced = True
            server.replace(retired)
            server.write_text(
                "from pathlib import Path\n"
                f"Path({str(replacement_marker)!r}).write_bytes(b'executed')\n",
                encoding="utf-8",
            )
        return real_spawn(
            cls,
            command,
            cwd=cwd,
            env=env,
            deadline=deadline,
            pass_fds=pass_fds,
        )  # type: ignore[arg-type]

    monkeypatch.setattr(
        lsp_process.ProcessTree,
        "_spawn_with_deadline",
        classmethod(replace_before_spawn),
    )
    session = PyrightSession(
        resolve_repository_scope(repository),
        identity,
        state_root=state_root,
    )
    try:
        session.start(deadline=time.monotonic() + 10)

        assert replaced is True
        assert not replacement_marker.exists()
        assert session.readiness == "not_ready"
        assert session.degradation_codes == (
            "pyright_executable_digest_mismatch",
        )
    finally:
        session.close(deadline=time.monotonic() + 5)


@pytest.mark.skipif(os.name != "posix", reason="POSIX verified Node loader")
def test_posix_node_loader_preserves_main_module_and_original_paths(
    repository: Path,
) -> None:
    node_value = shutil.which("node")
    if node_value is None:
        pytest.skip("Node.js is unavailable")
    node = Path(node_value).resolve()
    root = repository / "loader ' quoted"
    root.mkdir()
    dependency = root / "dependency.cjs"
    dependency.write_text("module.exports = 'dependency-loaded';\n", encoding="utf-8")
    output = root / "loader-output.json"
    server = root / "server.js"
    server.write_text(
        "const fs = require('node:fs');\n"
        "const path = require('node:path');\n"
        "const dependency = require('./dependency.cjs');\n"
        "fs.writeFileSync(process.argv[2], JSON.stringify({\n"
        "  argv: process.argv,\n"
        "  dirname: __dirname,\n"
        "  filename: __filename,\n"
        "  isMain: require.main === module,\n"
        "  modulePath: module.paths[0],\n"
        "  dependency,\n"
        "}));\n",
        encoding="utf-8",
    )
    descriptor = -1
    guard = pyright_session_module._LaunchServerGuard(
        server,
        hashlib.sha256(server.read_bytes()).hexdigest(),
        command=(str(node), str(server), str(output)),
        deadline=time.monotonic() + 10,
    )

    with guard as launch:
        assert isinstance(launch, lsp_process.GenerationLaunch)
        assert len(launch.pass_fds) == 1
        descriptor = launch.pass_fds[0]
        import fcntl

        assert fcntl.fcntl(descriptor, fcntl.F_GETFL) & os.O_ACCMODE == os.O_RDONLY
        tree = lsp_process.ProcessTree.spawn_with_deadline(
            launch.command,
            cwd=repository,
            env=dict(os.environ),
            deadline=time.monotonic() + 10,
            pass_fds=launch.pass_fds,
        )
        try:
            assert tree.process.wait(timeout=10) == 0
        finally:
            tree.close()

    with pytest.raises(OSError):
        os.fstat(descriptor)
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload == {
        "argv": [str(node), str(server), str(output)],
        "dirname": str(root),
        "filename": str(server),
        "isMain": True,
        "modulePath": str(root / "node_modules"),
        "dependency": "dependency-loaded",
    }


def test_server_identity_is_held_and_reverified_through_bootstrap(
    monkeypatch: pytest.MonkeyPatch,
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,
) -> None:
    server, identity = _copied_server_identity(repository, semantic_pyright)
    original = server.read_bytes()
    mutation_errors: list[OSError] = []
    session = PyrightSession(
        resolve_repository_scope(repository),
        identity,
        state_root=state_root,
    )
    real_bootstrap = session._bootstrap_generation

    def mutate_during_bootstrap(
        protocol: object,
        process_id: int,
        generation_nonce: str,
        deadline: float,
    ) -> ProcessState:
        state = real_bootstrap(
            protocol,  # type: ignore[arg-type]
            process_id,
            generation_nonce,
            deadline,
        )
        try:
            server.write_bytes(original + b"\n# changed during startup\n")
        except OSError as error:
            mutation_errors.append(error)
        return state

    monkeypatch.setattr(session, "_bootstrap_generation", mutate_during_bootstrap)
    try:
        session.start(deadline=time.monotonic() + 10)
        if os.name == "nt":
            assert mutation_errors
            assert session.readiness == "protocol_initialized"
        else:
            assert mutation_errors == []
            assert session.readiness == "not_ready"
            assert session.degradation_codes == (
                "pyright_executable_digest_mismatch",
            )
    finally:
        session.close(deadline=time.monotonic() + 5)


def test_start_uses_exact_command_initialize_and_configuration_contract(
    monkeypatch: pytest.MonkeyPatch,
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,
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
        assert session.start(deadline=time.monotonic() + 10) is None
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
        assert command == (
            str(semantic_pyright.identity.node_executable),
            str(semantic_pyright.identity.server_executable),
            "--stdio",
            f"--cancellationReceive=file:{owner_root / 'cancellation'}",
        )
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
        assert owner_root.parent == state_root / "run/lsp"
        assert owner_root.parent.is_dir()
        assert not owner_root.parent.is_symlink()
        if os.name == "posix":
            assert stat.S_IMODE(owner_root.parent.stat().st_mode) == 0o700

        events = semantic_pyright.events()
        client_messages = [
            event["message"]
            for event in events
            if event["kind"] == "client-message"
        ]
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
            event for event in events if event["kind"] == "configuration"
        )
        assert configuration["values"] == [
            settings["python"],
            settings["python"]["analysis"],
            settings["pyright"],
            None,
        ]
    finally:
        session.close(deadline=time.monotonic() + 5)
    assert not owner_root.exists()


@pytest.mark.parametrize(
    ("caller_seconds", "preflight_delay"),
    [(600.0, 0.0), (1.0, 0.05)],
)
def test_start_forwards_one_capped_deadline_without_shrinking_restart_budget(
    monkeypatch: pytest.MonkeyPatch,
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,
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
            deadline: float,
        ) -> None:
            assert command[1] == str(semantic_pyright.identity.server_executable)
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
    monkeypatch.setattr(pyright_session_module, "_ensure_lsp_parent", ensure_parent)
    monkeypatch.setattr(pyright_session_module, "_LaunchServerGuard", CapturedGuard)
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
    semantic_pyright: SemanticPyrightFixture,
) -> None:
    session = _session(repository, state_root, semantic_pyright)

    def forbidden(*_args: object, **_kwargs: object) -> None:
        pytest.fail("expired startup performed preflight or process work")

    monkeypatch.setattr(session, "_validated_qualified_paths", forbidden)
    monkeypatch.setattr(pyright_session_module, "_ensure_lsp_parent", forbidden)
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
    semantic_pyright: SemanticPyrightFixture,
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
            deadline: float,
        ) -> None:
            assert command[1] == str(semantic_pyright.identity.server_executable)
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
    monkeypatch.setattr(pyright_session_module, "_ensure_lsp_parent", ensure_parent)
    monkeypatch.setattr(pyright_session_module, "_LaunchServerGuard", FailingGuard)
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
    semantic_pyright: SemanticPyrightFixture,
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

    session.start(deadline=time.monotonic() + 5)
    assert len(cleanup_deadlines) == 1

    session.close(deadline=time.monotonic() + 5)

    assert len(cleanup_deadlines) == 2
    assert cleanup_deadlines[1] > time.monotonic()


def test_start_retries_retained_cleanup_before_new_spawn(
    monkeypatch: pytest.MonkeyPatch,
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,
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

    session.start(deadline=time.monotonic() + 5)
    session.start(deadline=time.monotonic() + 5)

    assert cleanup_attempts == 2
    assert start_attempts == 2
    assert session._process is not None
    session.close(deadline=time.monotonic() + 5)


def test_session_adopts_cleanup_owner_without_exhausting_global_registry(
    monkeypatch: pytest.MonkeyPatch,
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,
) -> None:
    baseline = {
        id(coordinator)
        for coordinator in lsp_process._pending_startup_cleanup_snapshot()
    }
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
        coordinator = lsp_process._LifecycleCoordinator(None)
        lsp_process._register_startup_cleanup(coordinator)
        error = RetainedCleanupError(
            "configured startup retained ownership",
            coordinator=coordinator,
        )
        errors.append(error)
        raise error

    monkeypatch.setattr(LspProcess, "start_configured", fail_start)
    try:
        for _index in range(lsp_process._MAX_STARTUP_CLEANUP_OWNERS + 1):
            session = _session(repository, state_root, semantic_pyright)
            sessions.append(session)
            session.start(deadline=time.monotonic() + 5)

        assert len(errors) == lsp_process._MAX_STARTUP_CLEANUP_OWNERS + 1
        assert {
            id(coordinator)
            for coordinator in lsp_process._pending_startup_cleanup_snapshot()
        } == baseline
        assert [session._startup_cleanup_error for session in sessions] == errors
    finally:
        for session in sessions:
            session.close(deadline=time.monotonic() + 5)
        for error in errors:
            coordinator = error.coordinator
            if coordinator is not None:
                lsp_process._unregister_startup_cleanup(coordinator)


def test_failed_session_atexit_registration_keeps_global_cleanup_owner(
    monkeypatch: pytest.MonkeyPatch,
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,
) -> None:
    session = _session(repository, state_root, semantic_pyright)
    coordinator = lsp_process._LifecycleCoordinator(None)
    lsp_process._register_startup_cleanup(coordinator)
    error = StartupCleanupError(
        "configured startup retained ownership",
        coordinator=coordinator,
    )

    def fail_registration(_callback: object) -> None:
        raise RuntimeError("atexit registration failed")

    monkeypatch.setattr(pyright_session_module.atexit, "register", fail_registration)
    try:
        with session._lock, pytest.raises(RuntimeError, match="atexit registration"):
            session._retain_startup_cleanup_locked(error)

        assert coordinator in lsp_process._pending_startup_cleanup_snapshot()
        assert session._startup_atexit_registered is False
    finally:
        lsp_process._unregister_startup_cleanup(coordinator)
        with session._lock:
            session._startup_cleanup_error = None


def test_interrupt_during_retained_cleanup_is_rethrown_and_next_start_recovers(
    monkeypatch: pytest.MonkeyPatch,
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,
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


def test_keyboard_interrupt_resets_starting_and_notifies_waiting_start(
    monkeypatch: pytest.MonkeyPatch,
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,
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
            assert release.wait(2)
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
    assert entered.wait(1)
    second.start()
    try:
        time.sleep(0.05)
        assert second.is_alive()
        release.set()
        first.join(3)
        second.join(3)
        assert not first.is_alive()
        assert not second.is_alive()
        assert len(first_errors) == 1
        assert isinstance(first_errors[0], KeyboardInterrupt)
        assert second_errors == []
        assert session._starting is False
        assert session._process is not None
    finally:
        release.set()
        first.join(3)
        second.join(3)
        session.close(deadline=time.monotonic() + 5)


def test_concurrent_start_waits_for_process_publication(
    monkeypatch: pytest.MonkeyPatch,
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,
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
        assert publish_process.wait(5)
        return process

    def start_first() -> None:
        try:
            session.start(deadline=time.monotonic() + 10)
        except BaseException as error:
            errors.append(error)

    def start_second() -> None:
        second_entered.set()
        try:
            session.start(deadline=time.monotonic() + 10)
        except BaseException as error:
            errors.append(error)
        finally:
            second_finished.set()

    monkeypatch.setattr(LspProcess, "start_configured", delay_publication)
    first = threading.Thread(target=start_first)
    second = threading.Thread(target=start_second)
    first.start()
    assert bootstrap_finished.wait(5), errors
    assert session.readiness == "protocol_initialized"
    second.start()
    assert second_entered.wait(1)
    try:
        assert not second_finished.wait(0.1)
    finally:
        publish_process.set()
        first.join(5)
        second.join(5)
        session.close(deadline=time.monotonic() + 5)

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
        session.start(deadline=time.monotonic() + 10)
        assert session.readiness == "protocol_initialized"
        assert session.position_encoding is expected
    finally:
        session.close(deadline=time.monotonic() + 5)


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

    assert session.start(deadline=time.monotonic() + 10) is None
    assert session.readiness == "not_ready"
    assert session.position_encoding is None
    assert session.degradation_codes == ("pyright_position_encoding_unsupported",)
    owners = tuple((state_root / "run/lsp").iterdir())
    assert len(owners) == 1
    assert not (owners[0] / "lease.json").exists()


@pytest.mark.parametrize(
    ("behavior", "seconds", "code"),
    [
        ("broken", 10.0, "pyright_startup_failed"),
        ("timeout", 0.25, "pyright_startup_timeout"),
    ],
)
def test_operational_startup_failure_returns_stable_not_ready_degradation(
    repository: Path,
    state_root: Path,
    behavior: str,
    seconds: float,
    code: str,
) -> None:
    fixture = create_semantic_pyright_fixture(
        repository,
        config={"initialize_behavior": behavior},
    )
    session = _session(repository, state_root, fixture)

    assert session.start(deadline=time.monotonic() + seconds) is None
    assert session.readiness == "not_ready"
    assert session.readiness_evidence == ()
    assert session.degradation_codes == (code,)
    owners = tuple((state_root / "run/lsp").iterdir())
    assert len(owners) == 1
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
    semantic_pyright: SemanticPyrightFixture,
    deadline: object,
    error: type[Exception],
) -> None:
    session = _session(repository, state_root, semantic_pyright)
    with pytest.raises(error):
        session.start(deadline=deadline)  # type: ignore[arg-type]
    assert not (state_root / "run/lsp").exists()


def test_open_document_establishes_exact_query_readiness_and_reopens_once(
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,
) -> None:
    session = _session(repository, state_root, semantic_pyright)
    scope = resolve_repository_scope(repository)
    content = (repository / "pkg/service.py").read_bytes()
    try:
        document = session.open_document(
            "pkg/service.py",
            deadline=time.monotonic() + 10,
        )
        assert document == OpenDocument(
            RepositorySource(
                scope.repository_id,
                scope.checkout_id,
                "pkg/service.py",
                (repository / "pkg/service.py").resolve(),
                (repository / "pkg/service.py").resolve().as_uri(),
            ),
            content,
            hashlib.sha256(content).hexdigest(),
            1,
        )
        assert session.readiness == "query_ready"
        assert session.readiness_evidence == (
            "initialize",
            "initialized",
            "configuration",
            "didOpen",
            "documentSymbol",
        )

        reopened = session.open_document(
            "pkg/service.py",
            deadline=time.monotonic() + 10,
        )
        assert reopened is document
        messages = [
            event["message"]
            for event in semantic_pyright.events()
            if event["kind"] == "client-message"
        ]
        assert [message["method"] for message in messages] == [
            "initialize",
            "initialized",
            "workspace/didChangeConfiguration",
            "textDocument/didOpen",
            "textDocument/documentSymbol",
        ]
        assert messages[3]["params"] == {
            "textDocument": {
                "uri": document.source.uri,
                "languageId": "python",
                "version": 1,
                "text": content.decode("utf-8"),
            }
        }
        assert messages[4]["params"] == {
            "textDocument": {"uri": document.source.uri}
        }
    finally:
        session.close(deadline=time.monotonic() + 5)


def test_concurrent_first_open_sends_one_didopen(
    monkeypatch: pytest.MonkeyPatch,
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,
) -> None:
    session = _session(repository, state_root, semantic_pyright)
    open_document_type = OpenDocument
    construction_lock = threading.Lock()
    second_construction = threading.Event()
    start = threading.Barrier(3)
    construction_count = 0
    documents: list[OpenDocument] = []
    errors: list[BaseException] = []

    def delayed_construction(*args: object) -> OpenDocument:
        nonlocal construction_count
        with construction_lock:
            construction_count += 1
            current = construction_count
        if current == 1:
            second_construction.wait(0.5)
        else:
            second_construction.set()
        return open_document_type(*args)  # type: ignore[arg-type]

    def open_source() -> None:
        start.wait()
        try:
            documents.append(
                session.open_document(
                    "pkg/service.py",
                    deadline=time.monotonic() + 10,
                )
            )
        except BaseException as error:
            errors.append(error)

    session.start(deadline=time.monotonic() + 10)
    monkeypatch.setattr(pyright_session_module, "OpenDocument", delayed_construction)
    threads = (threading.Thread(target=open_source), threading.Thread(target=open_source))
    for thread in threads:
        thread.start()
    start.wait()
    try:
        for thread in threads:
            thread.join(5)
        assert all(not thread.is_alive() for thread in threads)
        assert errors == []
        assert len(documents) == 2
        assert documents[0] is documents[1]
        methods = [
            event["method"]
            for event in semantic_pyright.events()
            if event["kind"] == "client-message"
        ]
        assert methods.count("textDocument/didOpen") == 1
    finally:
        session.close(deadline=time.monotonic() + 5)


def test_open_racing_restart_sends_didopen_once_for_replacement_generation(
    monkeypatch: pytest.MonkeyPatch,
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,
) -> None:
    session = _session(repository, state_root, semantic_pyright)
    real_notify = LspProcess.notify
    real_notify_generation = LspProcess.notify_generation
    restarted = False

    def restart_once(process: LspProcess, method: str, deadline: float) -> None:
        nonlocal restarted
        if method == "textDocument/didOpen" and not restarted:
            restarted = True
            process.restart(deadline)

    def notify(
        process: LspProcess,
        method: str,
        params: object,
        *,
        deadline: float,
    ) -> None:
        restart_once(process, method, deadline)
        real_notify(process, method, params, deadline=deadline)

    def notify_generation(
        process: LspProcess,
        method: str,
        params: object,
        *,
        generation_nonce: str,
        deadline: float,
    ) -> bool:
        restart_once(process, method, deadline)
        return real_notify_generation(
            process,
            method,
            params,
            generation_nonce=generation_nonce,
            deadline=deadline,
        )

    try:
        session.start(deadline=time.monotonic() + 10)
        monkeypatch.setattr(LspProcess, "notify", notify)
        monkeypatch.setattr(LspProcess, "notify_generation", notify_generation)

        session.open_document("pkg/service.py", deadline=time.monotonic() + 10)

        process = session._process
        assert restarted is True
        assert process is not None
        assert process.restart_count == 1
        events = semantic_pyright.events()
        pids = tuple(
            dict.fromkeys(
                event["pid"]
                for event in events
                if event["kind"] == "client-message"
            )
        )
        assert len(pids) == 2
        replacement_pid = pids[-1]
        replacement_opens = [
            event
            for event in events
            if event["pid"] == replacement_pid
            and event.get("method") == "textDocument/didOpen"
        ]
        assert len(replacement_opens) == 1
    finally:
        session.close(deadline=time.monotonic() + 5)


def test_failed_generation_notify_records_no_didopen_and_retries_later(
    monkeypatch: pytest.MonkeyPatch,
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,
) -> None:
    session = _session(repository, state_root, semantic_pyright)
    real_notify_generation = LspProcess.notify_generation

    def reject_notify(
        _process: LspProcess,
        _method: str,
        _params: object,
        *,
        generation_nonce: str,
        deadline: float,
    ) -> bool:
        assert generation_nonce
        assert deadline > time.monotonic()
        return False

    try:
        session.start(deadline=time.monotonic() + 10)
        monkeypatch.setattr(LspProcess, "notify_generation", reject_notify)
        first = session.open_document(
            "pkg/service.py",
            deadline=time.monotonic() + 10,
        )
        assert not any(
            event.get("method") == "textDocument/didOpen"
            for event in semantic_pyright.events()
        )
        assert "didOpen" not in session.readiness_evidence

        monkeypatch.setattr(LspProcess, "notify_generation", real_notify_generation)
        second = session.open_document(
            "pkg/service.py",
            deadline=time.monotonic() + 10,
        )
        assert second is first
        assert sum(
            event.get("method") == "textDocument/didOpen"
            for event in semantic_pyright.events()
        ) == 1
    finally:
        session.close(deadline=time.monotonic() + 5)


def test_failed_readiness_probe_retries_without_duplicate_didopen(
    repository: Path,
    state_root: Path,
) -> None:
    fixture = create_semantic_pyright_fixture(
        repository,
        config={"document_symbol_failures": 1},
    )
    session = _session(repository, state_root, fixture)
    try:
        first = session.open_document(
            "pkg/service.py",
            deadline=time.monotonic() + 10,
        )
        assert session.readiness == "protocol_initialized"
        assert session.readiness_evidence == (
            "initialize",
            "initialized",
            "configuration",
            "didOpen",
        )
        second = session.open_document(
            "pkg/service.py",
            deadline=time.monotonic() + 10,
        )
        assert second is first
        assert session.readiness == "query_ready"
        methods = [
            event["method"]
            for event in fixture.events()
            if event["kind"] == "client-message"
        ]
        assert methods.count("textDocument/didOpen") == 1
        assert methods.count("textDocument/documentSymbol") == 2
    finally:
        session.close(deadline=time.monotonic() + 5)


def test_ready_second_document_cannot_authorize_failed_first_document(
    repository: Path,
    state_root: Path,
) -> None:
    fixture = create_semantic_pyright_fixture(
        repository,
        config={"document_symbol_failure_uris": ["$SERVICE_URI"]},
    )
    session = _session(repository, state_root, fixture)
    try:
        session.open_document(
            "pkg/service.py",
            deadline=time.monotonic() + 10,
        )
        session.open_document(
            "pkg/api.py",
            deadline=time.monotonic() + 10,
        )
        assert session.readiness == "query_ready"

        result = session.definition(
            _anchor(repository, "pkg/service.py", 10, 20),
            deadline=time.monotonic() + 10,
        )

        assert result == ProviderLocations((), "not_ready", True)
        methods = [
            event["method"]
            for event in fixture.events()
            if event["kind"] == "client-message"
        ]
        assert "textDocument/definition" not in methods
    finally:
        session.close(deadline=time.monotonic() + 5)


def test_restart_rebuilds_readiness_per_replayed_document(
    repository: Path,
    state_root: Path,
) -> None:
    marker = repository / ".fake-lsp-per-document-crash"
    fixture = create_semantic_pyright_fixture(
        repository,
        config={
            "crash_once_method": "textDocument/definition",
            "crash_marker": str(marker),
        },
    )
    session = _session(repository, state_root, fixture)
    try:
        session.open_document("pkg/service.py", deadline=time.monotonic() + 10)
        session.open_document("pkg/api.py", deadline=time.monotonic() + 10)
        config = json.loads(fixture.config_path.read_text(encoding="utf-8"))
        config["document_symbol_failure_uris"] = ["$SERVICE_URI"]
        fixture.config_path.write_text(
            json.dumps(config, sort_keys=True, separators=(",", ":")),
            encoding="utf-8",
        )

        result = session.definition(
            _anchor(repository, "pkg/service.py", 10, 20),
            deadline=time.monotonic() + 15,
        )

        assert result == ProviderLocations((), "not_ready", True)
        assert session.readiness == "query_ready"
        messages = [
            event
            for event in fixture.events()
            if event["kind"] == "client-message"
        ]
        pids = tuple(dict.fromkeys(event["pid"] for event in messages))
        assert len(pids) == 2
        restarted_probes = [
            event["message"]["params"]["textDocument"]["uri"]
            for event in messages
            if event["pid"] == pids[1]
            and event["method"] == "textDocument/documentSymbol"
        ]
        assert restarted_probes == [
            (repository / "pkg/service.py").resolve().as_uri(),
            (repository / "pkg/api.py").resolve().as_uri(),
        ]
    finally:
        session.close(deadline=time.monotonic() + 5)


def test_missing_document_symbol_capability_never_sends_probe_or_claims_ready(
    repository: Path,
    state_root: Path,
) -> None:
    fixture = create_semantic_pyright_fixture(
        repository,
        config={"capabilities": {"definitionProvider": True}},
    )
    session = _session(repository, state_root, fixture)
    try:
        session.open_document("pkg/service.py", deadline=time.monotonic() + 10)
        assert session.readiness == "protocol_initialized"
        assert session.readiness_evidence == (
            "initialize",
            "initialized",
            "configuration",
            "didOpen",
        )
        methods = [
            event["method"]
            for event in fixture.events()
            if event["kind"] == "client-message"
        ]
        assert "textDocument/documentSymbol" not in methods
    finally:
        session.close(deadline=time.monotonic() + 5)


def test_malformed_document_symbol_list_cannot_establish_readiness(
    repository: Path,
    state_root: Path,
) -> None:
    fixture = create_semantic_pyright_fixture(
        repository,
        config={"responses": {"textDocument/documentSymbol": [42]}},
    )
    session = _session(repository, state_root, fixture)
    try:
        session.open_document("pkg/service.py", deadline=time.monotonic() + 10)
        assert session.readiness == "protocol_initialized"
        assert session.readiness_evidence == (
            "initialize",
            "initialized",
            "configuration",
            "didOpen",
        )

        result = session.definition(
            _anchor(repository, "pkg/service.py", 10, 20),
            deadline=time.monotonic() + 10,
        )
        assert result == ProviderLocations((), "not_ready", True)
        methods = [
            event["method"]
            for event in fixture.events()
            if event["kind"] == "client-message"
        ]
        assert "textDocument/definition" not in methods
    finally:
        session.close(deadline=time.monotonic() + 5)


def test_document_symbol_normalizer_is_strict_and_accepts_empty_list(
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,
) -> None:
    session = _session(repository, state_root, semantic_pyright)
    uri = (repository / "pkg/service.py").resolve().as_uri()
    selection = {
        "start": {"line": 0, "character": 0},
        "end": {"line": 0, "character": 1},
    }

    assert session._normalize_document_symbols([], uri) == ((), False)
    assert session._normalize_document_symbols(
        [{"selectionRange": selection}],
        uri,
    ) == ((), True)


@pytest.mark.parametrize(
    ("name", "content_kind", "error"),
    [
        ("invalid.py", "invalid", UnicodeDecodeError),
        ("oversized.py", "oversized", ValueError),
    ],
)
def test_open_document_rejects_invalid_utf8_and_frame_sized_content(
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,
    name: str,
    content_kind: str,
    error: type[Exception],
) -> None:
    content = (
        b"value = '\xff'\n"
        if content_kind == "invalid"
        else b"x" * MAX_FRAME_BYTES
    )
    (repository / name).write_bytes(content)
    session = _session(repository, state_root, semantic_pyright)
    try:
        with pytest.raises(error):
            session.open_document(name, deadline=time.monotonic() + 10)
        methods = [
            event["method"]
            for event in semantic_pyright.events()
            if event["kind"] == "client-message"
        ]
        assert "textDocument/didOpen" not in methods
        assert session.readiness == "protocol_initialized"
    finally:
        session.close(deadline=time.monotonic() + 5)


def test_open_document_propagates_deadline_to_stable_source_read(
    monkeypatch: pytest.MonkeyPatch,
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,
) -> None:
    session = _session(repository, state_root, semantic_pyright)
    session.start(deadline=time.monotonic() + 10)
    deadline = time.monotonic() + 5
    observed: list[float] = []

    def expiring_read(
        _path: Path,
        _max_bytes: int,
        *,
        label: str,
        deadline: float,
    ) -> bytes:
        assert label == "Pyright source document"
        observed.append(deadline)
        raise TimeoutError("stable read deadline expired")

    monkeypatch.setattr(pyright_session_module, "read_stable_bytes", expiring_read)
    try:
        with pytest.raises(TimeoutError, match="deadline"):
            session.open_document("pkg/service.py", deadline=deadline)
        assert observed == [deadline]
        methods = [
            event["method"]
            for event in semantic_pyright.events()
            if event["kind"] == "client-message"
        ]
        assert "textDocument/didOpen" not in methods
    finally:
        session.close(deadline=time.monotonic() + 5)


def test_definition_references_type_and_implementation_are_capability_honest(
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,
) -> None:
    session = _session(repository, state_root, semantic_pyright)
    anchor = _anchor(repository, "pkg/service.py", 10, 20)
    try:
        definition = session.definition(anchor, deadline=time.monotonic() + 10)
        references = session.references(anchor, deadline=time.monotonic() + 10)
        type_definition = session.type_definition(
            anchor,
            deadline=time.monotonic() + 10,
        )
        implementations = session.implementations(
            anchor,
            deadline=time.monotonic() + 10,
        )

        assert definition == ProviderLocations(
            (
                LspLocation(
                    (repository / "pkg/api.py").resolve().as_uri(),
                    LspRange(LspPosition(1, 8), LspPosition(1, 14)),
                ),
            ),
            "provider_reported",
            False,
        )
        assert len(references.locations) == 2
        assert references.coverage == "provider_reported"
        assert references.partial is True
        assert type_definition == ProviderLocations(
            (
                LspLocation(
                    (repository / "pkg/api.py").resolve().as_uri(),
                    LspRange(LspPosition(0, 6), LspPosition(0, 15)),
                ),
            ),
            "provider_reported",
            False,
        )
        assert implementations == ProviderLocations((), "unsupported", True)
        methods = [
            event["method"]
            for event in semantic_pyright.events()
            if event["kind"] == "client-message"
        ]
        assert "textDocument/implementation" not in methods
    finally:
        session.close(deadline=time.monotonic() + 5)


@pytest.mark.parametrize("advertised", ["missing", "false"])
def test_missing_or_false_capability_sends_no_feature_request(
    repository: Path,
    state_root: Path,
    advertised: str,
) -> None:
    capabilities: dict[str, object] = {"documentSymbolProvider": True}
    if advertised == "false":
        capabilities["definitionProvider"] = False
    fixture = create_semantic_pyright_fixture(
        repository,
        config={"capabilities": capabilities},
    )
    session = _session(repository, state_root, fixture)
    try:
        result = session.definition(
            _anchor(repository, "pkg/service.py", 10, 20),
            deadline=time.monotonic() + 10,
        )
        assert result == ProviderLocations((), "unsupported", True)
        methods = [
            event["method"]
            for event in fixture.events()
            if event["kind"] == "client-message"
        ]
        assert "textDocument/definition" not in methods
    finally:
        session.close(deadline=time.monotonic() + 5)


def test_not_ready_never_returns_a_complete_provider_negative(
    repository: Path,
    state_root: Path,
) -> None:
    fixture = create_semantic_pyright_fixture(
        repository,
        config={"document_symbol_failures": 10},
    )
    session = _session(repository, state_root, fixture)
    try:
        result = session.definition(
            _anchor(repository, "pkg/service.py", 10, 20),
            deadline=time.monotonic() + 10,
        )
        assert result == ProviderLocations((), "not_ready", True)
        methods = [
            event["method"]
            for event in fixture.events()
            if event["kind"] == "client-message"
        ]
        assert "textDocument/definition" not in methods
    finally:
        session.close(deadline=time.monotonic() + 5)


def test_unsupported_document_features_return_before_source_open(
    repository: Path,
    state_root: Path,
) -> None:
    fixture = create_semantic_pyright_fixture(
        repository,
        config={"capabilities": {}},
    )
    session = _session(repository, state_root, fixture)
    anchor = SourceAnchor("missing.py", 1, 0, 0)
    try:
        assert session.definition(
            anchor,
            deadline=time.monotonic() + 10,
        ) == ProviderLocations((), "unsupported", True)
        assert session.hover(
            anchor,
            deadline=time.monotonic() + 10,
        ) == ProviderHover(None, None, True)
        assert session.incoming_calls(
            anchor,
            deadline=time.monotonic() + 10,
        ) == ProviderCalls("incoming", (), "unsupported", True)
        assert session.document_symbols(
            "missing.py",
            deadline=time.monotonic() + 10,
        ) == ProviderLocations((), "unsupported", True)
        methods = [
            event["method"]
            for event in fixture.events()
            if event["kind"] == "client-message"
        ]
        assert "textDocument/didOpen" not in methods
        assert "textDocument/documentSymbol" not in methods
    finally:
        session.close(deadline=time.monotonic() + 5)


def test_unsupported_workspace_symbols_precede_query_readiness(
    repository: Path,
    state_root: Path,
) -> None:
    fixture = create_semantic_pyright_fixture(
        repository,
        config={"capabilities": {"documentSymbolProvider": True}},
    )
    session = _session(repository, state_root, fixture)
    try:
        assert session.workspace_symbols(
            "Service",
            deadline=time.monotonic() + 10,
        ) == ProviderLocations((), "unsupported", True)
        methods = [
            event["method"]
            for event in fixture.events()
            if event["kind"] == "client-message"
        ]
        assert "workspace/symbol" not in methods
    finally:
        session.close(deadline=time.monotonic() + 5)


def test_locations_filter_external_malformed_and_duplicate_entries_in_wire_order(
    repository: Path,
    state_root: Path,
) -> None:
    valid = {
        "uri": "$API_URI",
        "range": {
            "start": {"line": 1, "character": 8},
            "end": {"line": 1, "character": 14},
        },
    }
    linked = {
        "targetUri": "$SERVICE_URI",
        "targetRange": {
            "start": {"line": 4, "character": 0},
            "end": {"line": 4, "character": 20},
        },
        "targetSelectionRange": {
            "start": {"line": 4, "character": 6},
            "end": {"line": 4, "character": 13},
        },
    }
    fixture = create_semantic_pyright_fixture(
        repository,
        config={
            "responses": {
                "textDocument/definition": [
                    valid,
                    valid,
                    {
                        "uri": "$EXTERNAL_URI",
                        "range": valid["range"],
                    },
                    {
                        "uri": "$API_URI",
                        "range": {
                            "start": {"line": True, "character": 0},
                            "end": {"line": 1, "character": 0},
                        },
                    },
                    linked,
                    {"uri": "$API_URI"},
                ]
            }
        },
    )
    session = _session(repository, state_root, fixture)
    try:
        result = session.definition(
            _anchor(repository, "pkg/service.py", 10, 20),
            deadline=time.monotonic() + 10,
        )
        assert result == ProviderLocations(
            (
                LspLocation(
                    (repository / "pkg/api.py").resolve().as_uri(),
                    LspRange(LspPosition(1, 8), LspPosition(1, 14)),
                ),
                LspLocation(
                    (repository / "pkg/service.py").resolve().as_uri(),
                    LspRange(LspPosition(4, 6), LspPosition(4, 13)),
                ),
            ),
            "provider_reported",
            True,
        )
    finally:
        session.close(deadline=time.monotonic() + 5)


def test_anchor_is_validated_against_current_document_and_negotiated_encoding(
    repository: Path,
    state_root: Path,
) -> None:
    fixture = create_semantic_pyright_fixture(
        repository,
        config={
            "capabilities": {
                "definitionProvider": True,
                "documentSymbolProvider": True,
                "positionEncoding": "utf-16",
            }
        },
    )
    session = _session(repository, state_root, fixture)
    anchor = _anchor(repository, "pkg/unicode_api.py", 1, len('VALUE = "a😀'.encode()))
    try:
        session.definition(anchor, deadline=time.monotonic() + 10)
        definition = next(
            event["message"]
            for event in fixture.events()
            if event.get("method") == "textDocument/definition"
        )
        assert definition["params"]["position"] == {"line": 0, "character": 12}

        invalid = SourceAnchor(
            "pkg/unicode_api.py",
            anchor.line,
            anchor.utf8_character,
            0,
        )
        with pytest.raises(ValueError, match="byte_offset"):
            session.definition(invalid, deadline=time.monotonic() + 10)
    finally:
        session.close(deadline=time.monotonic() + 5)


def test_document_and_workspace_symbols_normalize_selection_ranges(
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,
) -> None:
    session = _session(repository, state_root, semantic_pyright)
    try:
        document = session.document_symbols(
            "pkg/service.py",
            deadline=time.monotonic() + 10,
        )
        workspace = session.workspace_symbols(
            "Service",
            deadline=time.monotonic() + 10,
        )
        service_uri = (repository / "pkg/service.py").resolve().as_uri()
        assert document == ProviderLocations(
            (
                LspLocation(
                    service_uri,
                    LspRange(LspPosition(4, 6), LspPosition(4, 13)),
                ),
                LspLocation(
                    service_uri,
                    LspRange(LspPosition(8, 8), LspPosition(8, 15)),
                ),
            ),
            "provider_reported",
            False,
        )
        assert workspace == ProviderLocations(
            (
                LspLocation(
                    service_uri,
                    LspRange(LspPosition(4, 6), LspPosition(4, 13)),
                ),
                LspLocation(
                    (repository / "pkg/api.py").resolve().as_uri(),
                    LspRange(LspPosition(0, 6), LspPosition(0, 15)),
                ),
            ),
            "provider_reported",
            False,
        )
    finally:
        session.close(deadline=time.monotonic() + 5)


@pytest.mark.parametrize(
    ("contents", "expected"),
    [
        ("plain hover", "plain hover"),
        ({"kind": "markdown", "value": "**hover**"}, "**hover**"),
        ({"language": "python", "value": "def hover(): ..."}, "def hover(): ..."),
        (
            [
                "first",
                {"kind": "plaintext", "value": "second"},
                {"language": "python", "value": "third"},
            ],
            "first\n\nsecond\n\nthird",
        ),
    ],
)
def test_hover_normalizes_string_markup_and_marked_string_forms(
    repository: Path,
    state_root: Path,
    contents: object,
    expected: str,
) -> None:
    fixture = create_semantic_pyright_fixture(
        repository,
        config={
            "responses": {
                "textDocument/hover": {
                    "contents": contents,
                    "range": {
                        "start": {"line": 8, "character": 8},
                        "end": {"line": 8, "character": 15},
                    },
                }
            }
        },
    )
    session = _session(repository, state_root, fixture)
    try:
        hover = session.hover(
            _anchor(repository, "pkg/service.py", 10, 20),
            deadline=time.monotonic() + 10,
        )
        assert hover == ProviderHover(
            expected,
            LspRange(LspPosition(8, 8), LspPosition(8, 15)),
            False,
        )
    finally:
        session.close(deadline=time.monotonic() + 5)


def test_malformed_hover_is_bounded_partial_instead_of_complete_negative(
    repository: Path,
    state_root: Path,
) -> None:
    fixture = create_semantic_pyright_fixture(
        repository,
        config={
            "responses": {
                "textDocument/hover": {
                    "contents": [{"kind": "plaintext", "value": 3}],
                    "range": {
                        "start": {"line": -1, "character": 0},
                        "end": {"line": 0, "character": 0},
                    },
                }
            }
        },
    )
    session = _session(repository, state_root, fixture)
    try:
        assert session.hover(
            _anchor(repository, "pkg/service.py", 10, 20),
            deadline=time.monotonic() + 10,
        ) == ProviderHover(None, None, True)
    finally:
        session.close(deadline=time.monotonic() + 5)


def test_incoming_and_outgoing_calls_use_call_hierarchy_not_references(
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,
) -> None:
    session = _session(repository, state_root, semantic_pyright)
    anchor = _anchor(repository, "pkg/service.py", 10, 20)
    try:
        incoming = session.incoming_calls(anchor, deadline=time.monotonic() + 10)
        outgoing = session.outgoing_calls(anchor, deadline=time.monotonic() + 10)
        assert incoming == ProviderCalls(
            "incoming",
            (
                LspLocation(
                    (repository / "pkg/service.py").resolve().as_uri(),
                    LspRange(LspPosition(12, 4), LspPosition(12, 11)),
                ),
            ),
            "provider_reported",
            True,
        )
        assert outgoing == ProviderCalls(
            "outgoing",
            (
                LspLocation(
                    (repository / "pkg/api.py").resolve().as_uri(),
                    LspRange(LspPosition(1, 4), LspPosition(1, 11)),
                ),
            ),
            "provider_reported",
            True,
        )
        methods = [
            event["method"]
            for event in semantic_pyright.events()
            if event["kind"] == "client-message"
        ]
        assert methods.count("textDocument/prepareCallHierarchy") == 2
        assert methods.count("callHierarchy/incomingCalls") == 1
        assert methods.count("callHierarchy/outgoingCalls") == 1
        assert "textDocument/references" not in methods
    finally:
        session.close(deadline=time.monotonic() + 5)


def test_every_prepared_call_item_is_queried_and_results_are_deduplicated(
    repository: Path,
    state_root: Path,
) -> None:
    item = {
        "name": "execute",
        "kind": 12,
        "uri": "$SERVICE_URI",
        "range": {
            "start": {"line": 8, "character": 4},
            "end": {"line": 9, "character": 37},
        },
        "selectionRange": {
            "start": {"line": 8, "character": 8},
            "end": {"line": 8, "character": 15},
        },
    }
    fixture = create_semantic_pyright_fixture(
        repository,
        config={
            "responses": {
                "textDocument/prepareCallHierarchy": [item for _ in range(257)],
                "callHierarchy/incomingCalls": [
                    {
                        "from": {
                            **item,
                            "name": "format_value",
                            "selectionRange": {
                                "start": {"line": 12, "character": 4},
                                "end": {"line": 12, "character": 16},
                            },
                        },
                        "fromRanges": [],
                    }
                ],
            }
        },
    )
    session = _session(repository, state_root, fixture)
    try:
        assert session._sanitize_call_item(
            {
                **item,
                "name": "\ud800",
                "uri": (repository / "pkg/service.py").resolve().as_uri(),
            }
        ) is None
        calls = session.incoming_calls(
            _anchor(repository, "pkg/service.py", 10, 20),
            deadline=time.monotonic() + 10,
        )
        assert len(calls.locations) == 1
        methods = [
            event["method"]
            for event in fixture.events()
            if event["kind"] == "client-message"
        ]
        assert methods.count("callHierarchy/incomingCalls") == 257
    finally:
        session.close(deadline=time.monotonic() + 5)


def test_unsupported_calls_never_fall_back_to_references(
    repository: Path,
    state_root: Path,
) -> None:
    fixture = create_semantic_pyright_fixture(
        repository,
        config={
            "capabilities": {
                "documentSymbolProvider": True,
                "referencesProvider": True,
            }
        },
    )
    session = _session(repository, state_root, fixture)
    try:
        calls = session.outgoing_calls(
            _anchor(repository, "pkg/service.py", 10, 20),
            deadline=time.monotonic() + 10,
        )
        assert calls == ProviderCalls("outgoing", (), "unsupported", True)
        methods = [
            event["method"]
            for event in fixture.events()
            if event["kind"] == "client-message"
        ]
        assert "textDocument/prepareCallHierarchy" not in methods
        assert "textDocument/references" not in methods
    finally:
        session.close(deadline=time.monotonic() + 5)


def test_push_diagnostics_are_current_normalized_and_related(
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,
) -> None:
    session = _session(repository, state_root, semantic_pyright)
    service_uri = (repository / "pkg/service.py").resolve().as_uri()
    api_uri = (repository / "pkg/api.py").resolve().as_uri()
    try:
        result = session.diagnostics(
            "pkg/service.py",
            deadline=time.monotonic() + 10,
        )
        assert result == ProviderDiagnostics(
            (
                LspDiagnostic(
                    service_uri,
                    LspRange(LspPosition(9, 15), LspPosition(9, 25)),
                    2,
                    "reportUnknownMemberType",
                    "Member type is unknown",
                    (
                        (
                            LspLocation(
                                api_uri,
                                LspRange(
                                    LspPosition(1, 8),
                                    LspPosition(1, 14),
                                ),
                            ),
                            "Declared here",
                        ),
                    ),
                ),
            ),
            1,
            False,
        )
        methods = [
            event["method"]
            for event in semantic_pyright.events()
            if event["kind"] == "client-message"
        ]
        assert "textDocument/diagnostic" not in methods
    finally:
        session.close(deadline=time.monotonic() + 5)


def test_stale_lower_diagnostic_version_cannot_replace_current_snapshot(
    repository: Path,
    state_root: Path,
) -> None:
    diagnostic = {
        "range": {
            "start": {"line": 0, "character": 0},
            "end": {"line": 0, "character": 5},
        },
        "severity": 1,
        "code": 1001,
        "message": "current",
    }
    fixture = create_semantic_pyright_fixture(
        repository,
        config={
            "diagnostic_notifications": [
                {
                    "uri": "$REQUEST_URI",
                    "version": 1,
                    "diagnostics": [diagnostic],
                },
                {
                    "uri": "$REQUEST_URI",
                    "version": 0,
                    "diagnostics": [{**diagnostic, "message": "stale"}],
                },
            ]
        },
    )
    session = _session(repository, state_root, fixture)
    try:
        result = session.diagnostics(
            "pkg/service.py",
            deadline=time.monotonic() + 10,
        )
        assert result.document_version == 1
        assert [item.message for item in result.diagnostics] == ["current"]
        assert result.diagnostics[0].code == "1001"
        assert result.partial is False
    finally:
        session.close(deadline=time.monotonic() + 5)


def test_diagnostic_filtering_marks_snapshot_partial_and_drops_external_related(
    repository: Path,
    state_root: Path,
) -> None:
    fixture = create_semantic_pyright_fixture(
        repository,
        config={
            "diagnostic_notifications": [
                {
                    "uri": "$REQUEST_URI",
                    "version": 1,
                    "diagnostics": [
                        {
                            "range": {
                                "start": {"line": 0, "character": 0},
                                "end": {"line": 0, "character": 5},
                            },
                            "severity": 2,
                            "message": "kept",
                            "relatedInformation": [
                                {
                                    "location": {
                                        "uri": "$EXTERNAL_URI",
                                        "range": {
                                            "start": {"line": 0, "character": 0},
                                            "end": {"line": 0, "character": 1},
                                        },
                                    },
                                    "message": "external",
                                }
                            ],
                        },
                        {
                            "range": {
                                "start": {"line": -1, "character": 0},
                                "end": {"line": 0, "character": 0},
                            },
                            "severity": 2,
                            "message": "malformed",
                        },
                    ],
                }
            ]
        },
    )
    session = _session(repository, state_root, fixture)
    try:
        result = session.diagnostics(
            "pkg/service.py",
            deadline=time.monotonic() + 10,
        )
        assert len(result.diagnostics) == 1
        assert result.diagnostics[0].message == "kept"
        assert result.diagnostics[0].related == ()
        assert result.partial is True
    finally:
        session.close(deadline=time.monotonic() + 5)


def test_diagnostics_wait_for_current_notification_and_timeout_partial(
    repository: Path,
    state_root: Path,
) -> None:
    delayed = create_semantic_pyright_fixture(
        repository,
        config={"diagnostics_delay_seconds": 0.1},
    )
    session = _session(repository, state_root, delayed)
    started = time.monotonic()
    try:
        result = session.diagnostics(
            "pkg/service.py",
            deadline=time.monotonic() + 5,
        )
        assert time.monotonic() - started >= 0.05
        assert result.document_version == 1
        assert result.partial is False
    finally:
        session.close(deadline=time.monotonic() + 5)

    silent = create_semantic_pyright_fixture(
        repository,
        config={"push_diagnostics": False},
    )
    silent_session = _session(repository, state_root, silent)
    try:
        silent_session.open_document(
            "pkg/service.py",
            deadline=time.monotonic() + 10,
        )
        result = silent_session.diagnostics(
            "pkg/service.py",
            deadline=time.monotonic() + 0.25,
        )
        assert result == ProviderDiagnostics((), None, True)
    finally:
        silent_session.close(deadline=time.monotonic() + 5)


def test_not_ready_diagnostics_are_partial_even_when_push_arrived(
    repository: Path,
    state_root: Path,
) -> None:
    fixture = create_semantic_pyright_fixture(
        repository,
        config={"document_symbol_failures": 10},
    )
    session = _session(repository, state_root, fixture)
    try:
        assert session.diagnostics(
            "pkg/service.py",
            deadline=time.monotonic() + 10,
        ) == ProviderDiagnostics((), None, True)
    finally:
        session.close(deadline=time.monotonic() + 5)


def test_diagnostics_ignore_unopened_uri(
    repository: Path,
    state_root: Path,
) -> None:
    fixture = create_semantic_pyright_fixture(
        repository,
        config={"push_diagnostics": False},
    )
    session = _session(repository, state_root, fixture)
    api_uri = (repository / "pkg/api.py").resolve().as_uri()
    diagnostic = {
        "range": {
            "start": {"line": 0, "character": 0},
            "end": {"line": 0, "character": 1},
        },
        "message": "must not be retained",
    }
    try:
        session.open_document("pkg/service.py", deadline=time.monotonic() + 10)
        session._publish_diagnostics(
            {"uri": api_uri, "version": 1, "diagnostics": [diagnostic]}
        )

        assert api_uri not in session._diagnostics
    finally:
        session.close(deadline=time.monotonic() + 5)


def test_diagnostic_aggregate_rejection_preserves_previous_snapshot(
    monkeypatch: pytest.MonkeyPatch,
    repository: Path,
    state_root: Path,
) -> None:
    fixture = create_semantic_pyright_fixture(
        repository,
        config={"push_diagnostics": False},
    )
    session = _session(repository, state_root, fixture)
    uri = (repository / "pkg/service.py").resolve().as_uri()
    base = {
        "range": {
            "start": {"line": 0, "character": 0},
            "end": {"line": 0, "character": 1},
        },
    }
    monkeypatch.setattr(
        pyright_session_module,
        "_MAX_DIAGNOSTIC_BYTES",
        512,
        raising=False,
    )
    try:
        session.open_document("pkg/service.py", deadline=time.monotonic() + 10)
        session._publish_diagnostics(
            {
                "uri": uri,
                "version": 1,
                "diagnostics": [{**base, "message": "kept"}],
            }
        )
        previous = session._diagnostics[uri]
        session._publish_diagnostics(
            {
                "uri": uri,
                "version": 1,
                "diagnostics": [{**base, "message": "x" * 1024}],
            }
        )

        assert session._diagnostics[uri] is previous
        assert session._diagnostic_bytes == previous.retained_bytes
    finally:
        session.close(deadline=time.monotonic() + 5)


def test_diagnostic_replacement_and_stale_accounting_are_exact(
    monkeypatch: pytest.MonkeyPatch,
    repository: Path,
    state_root: Path,
) -> None:
    fixture = create_semantic_pyright_fixture(
        repository,
        config={"push_diagnostics": False},
    )
    session = _session(repository, state_root, fixture)
    service_uri = (repository / "pkg/service.py").resolve().as_uri()
    api_uri = (repository / "pkg/api.py").resolve().as_uri()
    base = {
        "range": {
            "start": {"line": 0, "character": 0},
            "end": {"line": 0, "character": 1},
        },
    }
    monkeypatch.setattr(
        pyright_session_module,
        "_MAX_DIAGNOSTIC_BYTES",
        1400,
        raising=False,
    )
    try:
        session.open_document("pkg/service.py", deadline=time.monotonic() + 10)
        session.open_document("pkg/api.py", deadline=time.monotonic() + 10)
        session._publish_diagnostics(
            {
                "uri": service_uri,
                "version": 1,
                "diagnostics": [{**base, "message": "a" * 600}],
            }
        )
        first_size = session._diagnostic_bytes
        session._publish_diagnostics(
            {
                "uri": service_uri,
                "version": 1,
                "diagnostics": [{**base, "message": "small"}],
            }
        )
        replacement_size = session._diagnostic_bytes
        assert replacement_size < first_size

        session._publish_diagnostics(
            {
                "uri": api_uri,
                "version": 1,
                "diagnostics": [{**base, "message": "b" * 600}],
            }
        )
        assert set(session._diagnostics) == {service_uri, api_uri}
        assert session._diagnostic_bytes == sum(
            snapshot.retained_bytes for snapshot in session._diagnostics.values()
        )
        assert session._diagnostic_bytes <= 1400

        retained_bytes = session._diagnostic_bytes
        retained = session._diagnostics[service_uri]
        session._publish_diagnostics(
            {
                "uri": service_uri,
                "version": 0,
                "diagnostics": [{**base, "message": "stale" * 200}],
            }
        )
        assert session._diagnostics[service_uri] is retained
        assert session._diagnostic_bytes == retained_bytes
    finally:
        session.close(deadline=time.monotonic() + 5)


def test_benign_server_requests_and_progress_are_bounded_in_memory_only(
    repository: Path,
    state_root: Path,
) -> None:
    fixture = create_semantic_pyright_fixture(
        repository,
        config={"benign_requests": True, "push_progress": True},
    )
    session = _session(repository, state_root, fixture)
    try:
        session.start(deadline=time.monotonic() + 10)
        assert session.progress_events == (
            ("$/progress", "semantic-progress", "begin", "Analyzing"),
            ("pyright/beginProgress",),
            ("pyright/reportProgress", "Analyzing files"),
            ("pyright/endProgress",),
        )
        responses = [
            event["response"]
            for event in fixture.events()
            if event["kind"] == "client-response"
            and event["request_id"] != "semantic-configuration"
        ]
        assert [response["result"] for response in responses] == [None, None, None]
        assert session.capabilities["implementations"] is False
        runtime_files = tuple(
            path.relative_to(state_root).as_posix()
            for path in state_root.rglob("*")
            if path.is_file()
        )
        assert all("progress" not in path and "diagnostic" not in path for path in runtime_files)
    finally:
        session.close(deadline=time.monotonic() + 5)


def test_progress_retention_has_count_and_aggregate_byte_bounds(
    repository: Path,
    state_root: Path,
) -> None:
    session = PyrightSession(
        resolve_repository_scope(repository),
        _missing_identity(),
        state_root=state_root,
    )
    for index in range(300):
        session._progress(
            "$/progress",
            {
                "token": f"{index:03d}" + "t" * 253,
                "value": {"kind": "report", "message": "m" * 4096},
            },
        )

    events = session.progress_events
    assert len(events) <= 256
    assert sum(
        len(value.encode("utf-8"))
        for event in events
        for value in event
        if isinstance(value, str)
    ) <= 1024 * 1024


def test_transparent_restart_reinitializes_configures_reopens_and_reprobes(
    repository: Path,
    state_root: Path,
) -> None:
    marker = repository / ".fake-lsp-crashed"
    fixture = create_semantic_pyright_fixture(
        repository,
        config={
            "crash_once_method": "textDocument/definition",
            "crash_marker": str(marker),
        },
    )
    session = _session(repository, state_root, fixture)
    content = (repository / "pkg/service.py").read_text(encoding="utf-8")
    try:
        initial_diagnostics = session.diagnostics(
            "pkg/service.py",
            deadline=time.monotonic() + 10,
        )
        assert len(initial_diagnostics.diagnostics) == 1
        config = json.loads(fixture.config_path.read_text(encoding="utf-8"))
        config["push_diagnostics"] = False
        fixture.config_path.write_text(
            json.dumps(config, sort_keys=True, separators=(",", ":")),
            encoding="utf-8",
        )
        result = session.definition(
            _anchor(repository, "pkg/service.py", 10, 20),
            deadline=time.monotonic() + 15,
        )
        assert result.coverage == "provider_reported"
        assert len(result.locations) == 1
        assert session.readiness == "query_ready"
        assert session.readiness_evidence == (
            "initialize",
            "initialized",
            "configuration",
            "didOpen",
            "documentSymbol",
        )
        messages = [
            event
            for event in fixture.events()
            if event["kind"] == "client-message"
        ]
        pids = tuple(dict.fromkeys(event["pid"] for event in messages))
        assert len(pids) == 2
        for pid in pids:
            generation = [event["method"] for event in messages if event["pid"] == pid]
            assert generation[:5] == [
                "initialize",
                "initialized",
                "workspace/didChangeConfiguration",
                "textDocument/didOpen",
                "textDocument/documentSymbol",
            ]
            opened = next(
                event["message"]["params"]["textDocument"]
                for event in messages
                if event["pid"] == pid
                and event["method"] == "textDocument/didOpen"
            )
            assert opened["version"] == 1
            assert opened["text"] == content
        assert sum(event["method"] == "textDocument/definition" for event in messages) == 2
        assert session.diagnostics(
            "pkg/service.py",
            deadline=time.monotonic() + 0.1,
        ) == ProviderDiagnostics((), None, True)
    finally:
        session.close(deadline=time.monotonic() + 5)
        session.close(deadline=time.monotonic() + 5)
    assert tuple((state_root / "run/lsp").iterdir()) == ()


def test_server_mutated_before_transparent_restart_never_spawns_replacement(
    monkeypatch: pytest.MonkeyPatch,
    repository: Path,
    state_root: Path,
) -> None:
    marker = repository / ".fake-lsp-crashed"
    fixture = create_semantic_pyright_fixture(
        repository,
        config={
            "crash_once_method": "textDocument/definition",
            "crash_marker": str(marker),
        },
    )
    server, identity = _copied_server_identity(repository, fixture)
    session = PyrightSession(
        resolve_repository_scope(repository),
        identity,
        state_root=state_root,
    )
    trees: list[lsp_process.ProcessTree] = []
    real_spawn = lsp_process.ProcessTree._spawn_with_deadline.__func__

    def record_spawn(
        cls: type[lsp_process.ProcessTree],
        command: object,
        *,
        cwd: Path,
        env: object,
        deadline: float,
        pass_fds: object = (),
    ) -> lsp_process.ProcessTree:
        tree = real_spawn(
            cls,
            command,
            cwd=cwd,
            env=env,
            deadline=deadline,
            pass_fds=pass_fds,
        )  # type: ignore[arg-type]
        trees.append(tree)
        return tree

    monkeypatch.setattr(
        lsp_process.ProcessTree,
        "_spawn_with_deadline",
        classmethod(record_spawn),
    )
    try:
        session.open_document("pkg/service.py", deadline=time.monotonic() + 10)
        assert session.readiness == "query_ready"
        assert len(trees) == 1
        server.write_bytes(server.read_bytes() + b"\n# changed before restart\n")

        with pytest.raises(ProtocolViolation):
            result = session.definition(
                _anchor(repository, "pkg/service.py", 10, 20),
                deadline=time.monotonic() + 15,
            )
            assert result.coverage != "provider_reported"

        process = session._process
        assert process is not None
        coordinator = process._coordinator
        assert len(trees) == 1
        assert process.restart_count == 0
        assert process.state is ProcessState.FAILED
        assert coordinator.active is None
        assert coordinator.candidate is None
        assert session.readiness == "not_ready"
        assert not (process.owner_root / "lease.json").exists()
        assert not lsp_process._coordinator_has_ownership(coordinator)
    finally:
        session.close(deadline=time.monotonic() + 5)


def test_server_mutated_during_replacement_bootstrap_never_commits_candidate(
    monkeypatch: pytest.MonkeyPatch,
    repository: Path,
    state_root: Path,
) -> None:
    marker = repository / ".fake-lsp-crashed"
    fixture = create_semantic_pyright_fixture(
        repository,
        config={
            "crash_once_method": "textDocument/definition",
            "crash_marker": str(marker),
        },
    )
    server, identity = _copied_server_identity(repository, fixture)
    session = PyrightSession(
        resolve_repository_scope(repository),
        identity,
        state_root=state_root,
    )
    original = server.read_bytes()
    bootstrap_nonces: list[str] = []
    mutation_errors: list[OSError] = []
    trees: list[lsp_process.ProcessTree] = []
    real_bootstrap = session._bootstrap_generation
    real_spawn = lsp_process.ProcessTree._spawn_with_deadline.__func__

    def mutate_during_bootstrap(
        protocol: object,
        process_id: int,
        generation_nonce: str,
        deadline: float,
    ) -> ProcessState:
        state = real_bootstrap(
            protocol,  # type: ignore[arg-type]
            process_id,
            generation_nonce,
            deadline,
        )
        bootstrap_nonces.append(generation_nonce)
        if len(bootstrap_nonces) == 2:
            try:
                server.write_bytes(original + b"\n# changed during restart bootstrap\n")
            except OSError as error:
                mutation_errors.append(error)
                raise
        return state

    def record_spawn(
        cls: type[lsp_process.ProcessTree],
        command: object,
        *,
        cwd: Path,
        env: object,
        deadline: float,
        pass_fds: object = (),
    ) -> lsp_process.ProcessTree:
        tree = real_spawn(
            cls,
            command,
            cwd=cwd,
            env=env,
            deadline=deadline,
            pass_fds=pass_fds,
        )  # type: ignore[arg-type]
        trees.append(tree)
        return tree

    monkeypatch.setattr(session, "_bootstrap_generation", mutate_during_bootstrap)
    monkeypatch.setattr(
        lsp_process.ProcessTree,
        "_spawn_with_deadline",
        classmethod(record_spawn),
    )
    try:
        session.open_document("pkg/service.py", deadline=time.monotonic() + 10)
        first_nonce = session._generation_nonce
        assert first_nonce is not None
        assert session.readiness == "query_ready"

        with pytest.raises(ProtocolViolation):
            result = session.definition(
                _anchor(repository, "pkg/service.py", 10, 20),
                deadline=time.monotonic() + 15,
            )
            assert result.coverage != "provider_reported"

        process = session._process
        assert process is not None
        coordinator = process._coordinator
        assert len(bootstrap_nonces) == 2
        assert bootstrap_nonces[1] != first_nonce
        assert len(trees) == 2
        assert all(tree.process.poll() is not None for tree in trees)
        assert mutation_errors if os.name == "nt" else mutation_errors == []
        assert process.generation_nonce == first_nonce
        assert process.restart_count == 0
        assert process.state is ProcessState.FAILED
        assert coordinator.active is None
        assert coordinator.candidate is None
        assert session.readiness == "not_ready"
        assert not (process.owner_root / "lease.json").exists()
        assert not lsp_process._coordinator_has_ownership(coordinator)
    finally:
        session.close(deadline=time.monotonic() + 5)


def test_successful_close_releases_all_protocol_derived_session_state(
    repository: Path,
    state_root: Path,
) -> None:
    fixture = create_semantic_pyright_fixture(
        repository,
        config={"push_progress": True},
    )
    session = _session(repository, state_root, fixture)
    session.open_document("pkg/service.py", deadline=time.monotonic() + 10)
    assert session.readiness == "query_ready"
    assert session._documents
    assert session._ready_uri_generations
    assert session.progress_events

    session.close(deadline=time.monotonic() + 5)

    assert session.readiness == "not_ready"
    assert session.readiness_evidence == ()
    assert session.position_encoding is None
    assert dict(session.capabilities) == {}
    assert session.progress_events == ()
    assert session._documents == {}
    assert session._readiness_target_uri is None
    assert session._generation_nonce is None
    assert session._ready_uri_generations == {}
    assert session._diagnostics == {}


def test_post_start_probe_promotes_owned_process_state(
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,
) -> None:
    session = _session(repository, state_root, semantic_pyright)
    try:
        session.open_document("pkg/service.py", deadline=time.monotonic() + 10)

        assert session._process is not None
        assert session._process.state is ProcessState.WORKSPACE_READY
    finally:
        session.close(deadline=time.monotonic() + 5)


def test_rejected_lifecycle_promotion_cannot_claim_query_readiness(
    monkeypatch: pytest.MonkeyPatch,
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,
) -> None:
    session = _session(repository, state_root, semantic_pyright)
    transitions: list[str] = []

    def reject_transition(
        self: LspProcess,
        *,
        generation_nonce: str,
        deadline: float,
    ) -> bool:
        assert self is session._process
        assert deadline > time.monotonic()
        transitions.append(generation_nonce)
        return False

    try:
        session.start(deadline=time.monotonic() + 10)
        process = session._process
        assert process is not None
        monkeypatch.setattr(LspProcess, "promote_workspace_ready", reject_transition)

        session.open_document("pkg/service.py", deadline=time.monotonic() + 10)

        assert transitions == [process.generation_nonce]
        assert session.readiness == "protocol_initialized"
        assert process.state is ProcessState.PROTOCOL_INITIALIZED
    finally:
        session.close(deadline=time.monotonic() + 5)


def test_timed_out_lifecycle_promotion_cannot_leave_query_readiness(
    monkeypatch: pytest.MonkeyPatch,
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,
) -> None:
    session = _session(repository, state_root, semantic_pyright)

    def timeout_transition(
        self: LspProcess,
        *,
        generation_nonce: str,
        deadline: float,
    ) -> bool:
        assert self is session._process
        assert generation_nonce == self.generation_nonce
        assert deadline > 0
        raise TimeoutError("lifecycle promotion deadline expired")

    try:
        session.start(deadline=time.monotonic() + 10)
        process = session._process
        assert process is not None
        monkeypatch.setattr(
            LspProcess,
            "promote_workspace_ready",
            timeout_transition,
        )

        with pytest.raises(TimeoutError, match="promotion deadline"):
            session.open_document(
                "pkg/service.py",
                deadline=time.monotonic() + 10,
            )

        assert session.readiness == "protocol_initialized"
        assert session.readiness_evidence == (
            "initialize",
            "initialized",
            "configuration",
            "didOpen",
        )
        assert not session._document_ready_locked(
            (repository / "pkg/service.py").resolve().as_uri()
        )
        assert process.state is ProcessState.PROTOCOL_INITIALIZED
    finally:
        session.close(deadline=time.monotonic() + 5)


@pytest.mark.parametrize(
    "terminal_state",
    [ProcessState.DEGRADED, ProcessState.FAILED],
)
def test_terminal_process_state_revokes_session_readiness_before_semantic_use(
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,
    terminal_state: ProcessState,
) -> None:
    session = _session(repository, state_root, semantic_pyright)
    process: LspProcess | None = None
    original_state: ProcessState | None = None
    try:
        session.open_document("pkg/service.py", deadline=time.monotonic() + 10)
        process = session._process
        assert process is not None
        original_state = process.state
        process.state = terminal_state
        definitions_before = sum(
            event.get("method") == "textDocument/definition"
            for event in semantic_pyright.events()
        )

        assert session.readiness == "not_ready"
        assert session.readiness_evidence == ()
        assert session.position_encoding is None
        assert dict(session.capabilities) == {}
        assert session.definition(
            _anchor(repository, "pkg/service.py", 10, 20),
            deadline=time.monotonic() + 10,
        ) == ProviderLocations((), "not_ready", True)
        assert sum(
            event.get("method") == "textDocument/definition"
            for event in semantic_pyright.events()
        ) == definitions_before
    finally:
        if process is not None and original_state is not None:
            process.state = original_state
        session.close(deadline=time.monotonic() + 5)


def test_active_operation_and_lru_state_are_released_after_blocking_request(
    repository: Path,
    state_root: Path,
) -> None:
    fixture = create_semantic_pyright_fixture(
        repository,
        config={"response_delays": {"textDocument/definition": 0.3}},
    )
    session = _session(repository, state_root, fixture)
    anchor = _anchor(repository, "pkg/service.py", 10, 20)
    results: list[ProviderLocations] = []
    errors: list[BaseException] = []

    def query() -> None:
        try:
            results.append(
                session.definition(anchor, deadline=time.monotonic() + 10)
            )
        except BaseException as error:
            errors.append(error)

    before = session.last_used_monotonic
    thread = threading.Thread(target=query)
    thread.start()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if any(
            event.get("method") == "textDocument/definition"
            for event in fixture.events()
        ):
            break
        time.sleep(0.01)
    try:
        assert thread.is_alive()
        assert session.active_operations == 1
        observed = time.monotonic()
        assert session.readiness == "query_ready"
        assert time.monotonic() - observed < 0.1
        assert session.last_used_monotonic >= before
        thread.join(5)
        assert not thread.is_alive()
        assert errors == []
        assert len(results) == 1
        assert session.active_operations == 0
        assert session.last_used_monotonic >= before

        invalid = SourceAnchor(anchor.path, anchor.line, anchor.utf8_character, 0)
        with pytest.raises(ValueError):
            session.definition(invalid, deadline=time.monotonic() + 5)
        assert session.active_operations == 0
    finally:
        thread.join(5)
        session.close(deadline=time.monotonic() + 5)


def test_configuration_request_item_bound_returns_empty_snapshot(
    repository: Path,
    state_root: Path,
) -> None:
    fixture = create_semantic_pyright_fixture(
        repository,
        config={
            "configuration_items": [
                {"section": "python"} for _ in range(65)
            ]
        },
    )
    session = _session(repository, state_root, fixture)
    try:
        session.start(deadline=time.monotonic() + 10)
        configuration = next(
            event for event in fixture.events() if event["kind"] == "configuration"
        )
        assert configuration["values"] == []
        assert session.readiness == "protocol_initialized"
    finally:
        session.close(deadline=time.monotonic() + 5)


def test_invalid_server_capability_shape_degrades_without_claiming_support(
    repository: Path,
    state_root: Path,
) -> None:
    fixture = create_semantic_pyright_fixture(
        repository,
        config={"capabilities": {"definitionProvider": "yes"}},
    )
    session = _session(repository, state_root, fixture)

    session.start(deadline=time.monotonic() + 10)
    assert session.readiness == "not_ready"
    assert session.capabilities == {}
    assert session.degradation_codes == ("pyright_definition_capability_invalid",)


@pytest.mark.parametrize(
    ("repository_value", "identity_value", "state_value", "error"),
    [
        (object(), _missing_identity(), Path("state"), TypeError),
        (None, object(), Path("state"), TypeError),
        (None, _missing_identity(), "state", TypeError),
    ],
)
def test_constructor_requires_exact_contract_types(
    repository: Path,
    repository_value: object,
    identity_value: object,
    state_value: object,
    error: type[Exception],
) -> None:
    scope = resolve_repository_scope(repository)
    if repository_value is None:
        repository_value = scope
    with pytest.raises(error):
        PyrightSession(
            repository_value,  # type: ignore[arg-type]
            identity_value,  # type: ignore[arg-type]
            state_root=state_value,  # type: ignore[arg-type]
        )


def test_qualified_identity_validation_unwinds_start_state_for_retry_and_close(
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,
) -> None:
    invalid = replace(semantic_pyright.identity, node_executable=None)
    session = PyrightSession(
        resolve_repository_scope(repository),
        invalid,  # type: ignore[arg-type]
        state_root=state_root,
    )

    with pytest.raises(TypeError, match="node_executable"):
        session.start(deadline=time.monotonic() + 1)
    assert session.active_operations == 0
    assert session.readiness == "not_ready"
    with pytest.raises(TypeError, match="node_executable"):
        session.start(deadline=time.monotonic() + 1)
    session.close(deadline=time.monotonic() + 1)


def test_symlinked_lsp_owner_parent_fails_closed_without_mutating_target(
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,
) -> None:
    outside = state_root.parent / "outside-lsp"
    outside.mkdir()
    lsp_parent = state_root / "run/lsp"
    lsp_parent.parent.mkdir()
    try:
        lsp_parent.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks are unavailable")
    session = _session(repository, state_root, semantic_pyright)

    session.start(deadline=time.monotonic() + 5)
    assert session.readiness == "not_ready"
    assert session.degradation_codes == ("pyright_startup_failed",)
    assert tuple(outside.iterdir()) == ()

    outside_state_parent = state_root.parent / "outside-state-parent"
    linked_state = outside_state_parent / "state"
    linked_state.mkdir(parents=True)
    state_parent_link = state_root.parent / "state-parent-link"
    state_parent_link.symlink_to(outside_state_parent, target_is_directory=True)
    linked_session = _session(
        repository,
        state_parent_link / "state",
        semantic_pyright,
    )
    try:
        linked_session.start(deadline=time.monotonic() + 5)
        assert linked_session.readiness == "not_ready"
        assert linked_session.degradation_codes == ("pyright_startup_failed",)
        assert tuple(linked_state.iterdir()) == ()
    finally:
        linked_session.close(deadline=time.monotonic() + 5)


def test_exact_public_method_annotations() -> None:
    assert get_type_hints(PyrightSession.start) == {
        "deadline": float,
        "return": type(None),
    }
    assert get_type_hints(PyrightSession.open_document) == {
        "path": str,
        "deadline": float,
        "return": OpenDocument,
    }
    for name in ("definition", "references", "implementations", "type_definition"):
        assert get_type_hints(getattr(PyrightSession, name)) == {
            "anchor": SourceAnchor,
            "deadline": float,
            "return": ProviderLocations,
        }
    assert get_type_hints(PyrightSession.hover) == {
        "anchor": SourceAnchor,
        "deadline": float,
        "return": ProviderHover,
    }
    for name in ("incoming_calls", "outgoing_calls"):
        assert get_type_hints(getattr(PyrightSession, name)) == {
            "anchor": SourceAnchor,
            "deadline": float,
            "return": ProviderCalls,
        }
    assert get_type_hints(PyrightSession.document_symbols) == {
        "path": str,
        "deadline": float,
        "return": ProviderLocations,
    }
    assert get_type_hints(PyrightSession.workspace_symbols) == {
        "query": str,
        "deadline": float,
        "return": ProviderLocations,
    }
    assert get_type_hints(PyrightSession.diagnostics) == {
        "path": str,
        "deadline": float,
        "return": ProviderDiagnostics,
    }
    assert get_type_hints(PyrightSession.close) == {
        "deadline": float,
        "return": type(None),
    }


def test_unqualified_semantic_features_return_not_ready_without_opening_or_wiring(
    repository: Path,
    state_root: Path,
) -> None:
    session = PyrightSession(
        resolve_repository_scope(repository),
        _missing_identity(),
        state_root=state_root,
    )
    anchor = _anchor(repository, "pkg/service.py", 10, 20)

    assert session.definition(
        anchor,
        deadline=time.monotonic() + 1,
    ) == ProviderLocations((), "not_ready", True)
    assert session.hover(
        anchor,
        deadline=time.monotonic() + 1,
    ) == ProviderHover(None, None, True)
    assert session.incoming_calls(
        anchor,
        deadline=time.monotonic() + 1,
    ) == ProviderCalls("incoming", (), "not_ready", True)
    assert session.document_symbols(
        "pkg/service.py",
        deadline=time.monotonic() + 1,
    ) == ProviderLocations((), "not_ready", True)
    assert session.workspace_symbols(
        "Service",
        deadline=time.monotonic() + 1,
    ) == ProviderLocations((), "not_ready", True)
    assert session.diagnostics(
        "pkg/service.py",
        deadline=time.monotonic() + 1,
    ) == ProviderDiagnostics((), None, True)
    assert not (state_root / "run/lsp").exists()


def test_multimegabyte_document_below_frame_limit_can_be_opened(
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,
) -> None:
    content = b"# " + b"x" * (2 * 1024 * 1024) + b"\n"
    (repository / "large.py").write_bytes(content)
    session = _session(repository, state_root, semantic_pyright)
    try:
        document = session.open_document(
            "large.py",
            deadline=time.monotonic() + 15,
        )
        assert document.content == content
        assert session.readiness == "query_ready"
    finally:
        session.close(deadline=time.monotonic() + 5)


def test_document_count_limit_rejects_without_partial_session_state(
    monkeypatch: pytest.MonkeyPatch,
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,
) -> None:
    first_path = repository / "first.py"
    second_path = repository / "second.py"
    first_path.write_text("first = 1\n", encoding="utf-8")
    second_path.write_text("second = 2\n", encoding="utf-8")
    monkeypatch.setattr(
        pyright_session_module,
        "_MAX_OPEN_DOCUMENTS",
        1,
        raising=False,
    )
    session = _session(repository, state_root, semantic_pyright)
    try:
        first = session.open_document("first.py", deadline=time.monotonic() + 10)
        target = session._readiness_target_uri
        with pytest.raises(RuntimeError, match="document count"):
            session.open_document("second.py", deadline=time.monotonic() + 10)

        assert session._documents == {first.source.uri: first}
        assert session._readiness_target_uri == target
        assert session._document_bytes == len(first.content)
        assert sum(
            event.get("method") == "textDocument/didOpen"
            for event in semantic_pyright.events()
        ) == 1
    finally:
        session.close(deadline=time.monotonic() + 5)


def test_document_byte_limit_rejects_without_partial_session_state(
    monkeypatch: pytest.MonkeyPatch,
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,
) -> None:
    first_path = repository / "first.py"
    second_path = repository / "second.py"
    first_path.write_text("first = 1\n", encoding="utf-8")
    second_path.write_text("second = 2\n", encoding="utf-8")
    first_size = first_path.stat().st_size
    monkeypatch.setattr(
        pyright_session_module,
        "_MAX_OPEN_DOCUMENT_BYTES",
        first_size,
        raising=False,
    )
    session = _session(repository, state_root, semantic_pyright)
    try:
        first = session.open_document("first.py", deadline=time.monotonic() + 10)
        target = session._readiness_target_uri
        with pytest.raises(RuntimeError, match="document source bytes"):
            session.open_document("second.py", deadline=time.monotonic() + 10)

        assert session._documents == {first.source.uri: first}
        assert session._readiness_target_uri == target
        assert session._document_bytes == len(first.content)
    finally:
        session.close(deadline=time.monotonic() + 5)


def test_non_string_position_encoding_shape_is_operational_degradation(
    repository: Path,
    state_root: Path,
) -> None:
    fixture = create_semantic_pyright_fixture(
        repository,
        config={"capabilities": {"positionEncoding": ["utf-8"]}},
    )
    session = _session(repository, state_root, fixture)

    session.start(deadline=time.monotonic() + 10)
    assert session.readiness == "not_ready"
    assert session.degradation_codes == ("pyright_position_encoding_unsupported",)


def test_supported_empty_locations_are_complete_only_after_query_readiness(
    repository: Path,
    state_root: Path,
) -> None:
    fixture = create_semantic_pyright_fixture(
        repository,
        config={
            "responses": {
                "textDocument/definition": None,
                "textDocument/references": None,
            }
        },
    )
    session = _session(repository, state_root, fixture)
    anchor = _anchor(repository, "pkg/service.py", 10, 20)
    try:
        assert session.definition(
            anchor,
            deadline=time.monotonic() + 10,
        ) == ProviderLocations((), "provider_reported", False)
        assert session.references(
            anchor,
            deadline=time.monotonic() + 10,
        ) == ProviderLocations((), "provider_reported", True)
    finally:
        session.close(deadline=time.monotonic() + 5)
