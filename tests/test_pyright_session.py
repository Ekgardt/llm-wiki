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
from collections.abc import Mapping
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
    PyrightSessionManager,
)
from repository_scope import RepositoryScope, resolve_repository_scope
from workspace_revision import (
    WorkspaceDelta,
    compute_workspace_revision,
)

from tests.code_kernel_helpers import (
    SemanticPyrightFixture,
    create_python_repository,
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


def test_session_exposes_immutable_qualified_identity(
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,
) -> None:
    session = _session(repository, state_root, semantic_pyright)
    try:
        assert session.identity is semantic_pyright.identity
        with pytest.raises(AttributeError):
            session.identity = _missing_identity()  # type: ignore[misc]
    finally:
        session.close(deadline=time.monotonic() + 5)


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
    owner = repository / "run" / "lsp" / ("a" * 32)
    owner.mkdir(parents=True)
    descriptor = -1
    guard = pyright_session_module._LaunchServerGuard(
        server,
        hashlib.sha256(server.read_bytes()).hexdigest(),
        command=(str(node), str(server), str(output)),
        owner_root=owner,
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


def test_posix_launch_snapshot_is_scoped_to_owner_and_removed_on_all_branches(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    owner = tmp_path / "run" / "lsp" / ("a" * 32)
    owner.mkdir(parents=True)
    server = tmp_path / "server.py"
    server.write_bytes(b"print('server')\n")
    snapshots: list[Path] = []
    unlinked: list[Path] = []
    real_mkstemp = pyright_session_module.tempfile.mkstemp

    class PosixOsProxy:
        name = "posix"

        def __getattr__(self, name: str) -> object:
            return getattr(os, name)

        @staticmethod
        def unlink(path: object, *args: object, **kwargs: object) -> None:
            assert args == () and kwargs == {}
            unlinked.append(Path(path))

    def record_mkstemp(*args: object, **kwargs: object) -> tuple[int, str]:
        descriptor, name = real_mkstemp(*args, **kwargs)
        snapshots.append(Path(name))
        return descriptor, name

    monkeypatch.setattr(pyright_session_module, "os", PosixOsProxy())
    monkeypatch.setattr(pyright_session_module.tempfile, "mkstemp", record_mkstemp)
    monkeypatch.setattr(
        pyright_session_module._LaunchServerGuard,
        "_posix_launch_command",
        lambda _self, descriptor: ("server", str(descriptor)),
    )

    guard = pyright_session_module._LaunchServerGuard(
        server,
        hashlib.sha256(server.read_bytes()).hexdigest(),
        command=("python", str(server)),
        owner_root=owner,
        deadline=time.monotonic() + 5,
    )
    with guard as launch:
        assert isinstance(launch, lsp_process.GenerationLaunch)
        assert snapshots[-1].parent == owner
        assert unlinked[-1] == snapshots[-1]
    snapshots[-1].unlink()
    assert list(owner.iterdir()) == []

    failing = pyright_session_module._LaunchServerGuard(
        server,
        hashlib.sha256(server.read_bytes()).hexdigest(),
        command=("python", str(server)),
        owner_root=owner,
        deadline=time.monotonic() + 5,
    )
    monkeypatch.setattr(
        failing,
        "_copy_snapshot",
        lambda _snapshot: (_ for _ in ()).throw(RuntimeError("copy failed")),
    )
    with pytest.raises(RuntimeError, match="copy failed"):
        failing.__enter__()
    assert snapshots[-1].parent == owner
    assert unlinked[-1] == snapshots[-1]
    snapshots[-1].unlink()
    assert list(owner.iterdir()) == []


def test_launch_guard_close_prioritizes_later_interruption(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    server = tmp_path / "server.py"
    server.write_bytes(b"print('server')\n")
    guard = pyright_session_module._LaunchServerGuard(
        server,
        hashlib.sha256(server.read_bytes()).hexdigest(),
        command=("python", str(server)),
        owner_root=tmp_path,
        deadline=time.monotonic() + 1,
    )
    ordinary_error = RuntimeError("first ordinary launch guard close error")

    class Snapshot:
        @staticmethod
        def close() -> None:
            raise ordinary_error

    def interrupt_unlink(_path: object) -> None:
        raise KeyboardInterrupt("later launch guard close interruption")

    guard._snapshot = Snapshot()  # type: ignore[assignment]
    guard._snapshot_path = tmp_path / "snapshot.py"
    monkeypatch.setattr(pyright_session_module.os, "unlink", interrupt_unlink)

    with pytest.raises(
        KeyboardInterrupt,
        match="later launch guard close interruption",
    ) as raised:
        guard.close()

    assert raised.value.__cause__ is ordinary_error


def test_launch_guard_body_interruption_outranks_close_error(tmp_path: Path) -> None:
    server = tmp_path / "server.py"
    server.write_bytes(b"print('server')\n")

    class EnteredGuard(pyright_session_module._LaunchServerGuard):
        def __enter__(self) -> EnteredGuard:
            return self

    guard = EnteredGuard(
        server,
        hashlib.sha256(server.read_bytes()).hexdigest(),
        command=("python", str(server)),
        owner_root=tmp_path,
        deadline=time.monotonic() + 1,
    )
    ordinary_error = RuntimeError("ordinary launch guard close error")

    class Snapshot:
        @staticmethod
        def close() -> None:
            raise ordinary_error

    guard._snapshot = Snapshot()  # type: ignore[assignment]

    def interrupt_guard_body() -> KeyboardInterrupt:
        try:
            raise KeyboardInterrupt("launch guard body interrupted")
        except KeyboardInterrupt as error:
            return error

    interruption = interrupt_guard_body()
    with pytest.raises(
        KeyboardInterrupt,
        match="launch guard body interrupted",
    ) as raised:
        with guard:
            raise interruption.with_traceback(interruption.__traceback__)

    assert raised.value is interruption
    assert raised.value.__cause__ is ordinary_error
    traceback_names: list[str] = []
    current = raised.value.__traceback__
    while current is not None:
        traceback_names.append(current.tb_frame.f_code.co_name)
        current = current.tb_next
    assert "interrupt_guard_body" in traceback_names


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
            owner_root: Path,
            deadline: float,
        ) -> None:
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
            owner_root: Path,
            deadline: float,
        ) -> None:
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


def test_close_unwraps_retained_cleanup_interruption_and_preserves_owner(
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,
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
        assert session._startup_cleanup_error is retained
        assert session._closed is False
        assert attempts == 1
    finally:
        session.close(deadline=time.monotonic() + 5)

    assert attempts == 2


def test_close_unwraps_retained_process_interruption_and_preserves_owner(
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,
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


def test_wrapped_interruption_during_retained_cleanup_is_rethrown_with_owner(
    monkeypatch: pytest.MonkeyPatch,
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,
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
        assert session._startup_cleanup_error is retained
        assert session._starting is False
        assert attempts == 1
    finally:
        session.close(deadline=time.monotonic() + 5)


def test_wrapped_interruption_during_retained_process_close_is_rethrown_with_owner(
    monkeypatch: pytest.MonkeyPatch,
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,
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
        assert pyright_session_module._startup_interruption(wrapper) is interruption


def test_startup_rollback_rethrows_interruption_and_retains_process_owner(
    monkeypatch: pytest.MonkeyPatch,
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,
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
    semantic_pyright: SemanticPyrightFixture,
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
    semantic_pyright: SemanticPyrightFixture,
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
        session.start(deadline=time.monotonic() + 5)

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
    semantic_pyright: SemanticPyrightFixture,
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


def test_operational_startup_failure_returns_stable_not_ready_degradation(
    repository: Path,
    state_root: Path,
) -> None:
    fixture = create_semantic_pyright_fixture(
        repository,
        config={"initialize_behavior": "broken"},
    )
    session = _session(repository, state_root, fixture)

    assert session.start(deadline=time.monotonic() + 10) is None
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
    semantic_pyright: SemanticPyrightFixture,
) -> None:
    session = _session(repository, state_root, semantic_pyright)
    request = pyright_session_module.LspProtocol.request
    injected_owners: list[Path] = []

    def timeout_initialize(
        protocol: pyright_session_module.LspProtocol,
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
        pyright_session_module.LspProtocol,
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


def _hold_document_lock_for_contention(
    session: PyrightSession,
    held: threading.Event,
    released: threading.Event,
) -> None:
    with session._document_lock:
        held.set()
        time.sleep(0.25)
    released.set()


def test_open_document_lock_contention_obeys_deadline_without_mutation(
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,
) -> None:
    session = _session(repository, state_root, semantic_pyright)
    held = threading.Event()
    released = threading.Event()
    holder = threading.Thread(
        target=_hold_document_lock_for_contention,
        args=(session, held, released),
    )
    holder.start()
    assert held.wait(1)
    before_state = (
        session._readiness,
        session._readiness_evidence,
        session._degradation_codes,
        session._active_operations,
        session._last_used_monotonic,
        session._process,
        session._startup_attempted,
        session._starting,
        dict(session._documents),
        session._document_bytes,
        session._workspace_revision,
        session._synchronize_epoch,
        session._generation_nonce,
    )
    before_events = tuple(semantic_pyright.events())
    started = time.monotonic()
    try:
        with pytest.raises(TimeoutError, match="document lock"):
            session.open_document(
                "pkg/service.py",
                deadline=started + 0.05,
            )
        assert time.monotonic() - started < 0.2
        assert released.is_set() is False
        assert (
            session._readiness,
            session._readiness_evidence,
            session._degradation_codes,
            session._active_operations,
            session._last_used_monotonic,
            session._process,
            session._startup_attempted,
            session._starting,
            dict(session._documents),
            session._document_bytes,
            session._workspace_revision,
            session._synchronize_epoch,
            session._generation_nonce,
        ) == before_state
        assert tuple(semantic_pyright.events()) == before_events
        assert not (state_root / "run/lsp").exists()
    finally:
        holder.join(1)
        session.close(deadline=time.monotonic() + 1)
    assert not holder.is_alive()


def test_synchronize_lock_contention_obeys_deadline_without_mutation(
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,
) -> None:
    session = _session(repository, state_root, semantic_pyright)
    revision = compute_workspace_revision(
        resolve_repository_scope(repository),
        deadline=time.monotonic() + 5,
    )
    held = threading.Event()
    released = threading.Event()
    holder = threading.Thread(
        target=_hold_document_lock_for_contention,
        args=(session, held, released),
    )
    holder.start()
    assert held.wait(1)
    before_state = (
        session._readiness,
        session._readiness_evidence,
        session._degradation_codes,
        session._active_operations,
        session._last_used_monotonic,
        session._process,
        session._startup_attempted,
        session._starting,
        dict(session._documents),
        session._document_bytes,
        session._workspace_revision,
        session._synchronize_epoch,
        session._generation_nonce,
    )
    before_events = tuple(semantic_pyright.events())
    started = time.monotonic()
    try:
        with pytest.raises(TimeoutError, match="document lock"):
            session.synchronize(revision, deadline=started + 0.05)
        assert time.monotonic() - started < 0.2
        assert released.is_set() is False
        assert (
            session._readiness,
            session._readiness_evidence,
            session._degradation_codes,
            session._active_operations,
            session._last_used_monotonic,
            session._process,
            session._startup_attempted,
            session._starting,
            dict(session._documents),
            session._document_bytes,
            session._workspace_revision,
            session._synchronize_epoch,
            session._generation_nonce,
        ) == before_state
        assert tuple(semantic_pyright.events()) == before_events
        assert not (state_root / "run/lsp").exists()
    finally:
        holder.join(1)
        session.close(deadline=time.monotonic() + 1)
    assert not holder.is_alive()


def test_document_lock_post_acquire_interruption_releases_and_remains_usable(
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,
) -> None:
    session = _session(repository, state_root, semantic_pyright)
    underlying = session._document_lock

    class InterruptAfterAcquire:
        def __init__(self) -> None:
            self.interrupt = True

        def acquire(self, *, timeout: float) -> bool:
            acquired = underlying.acquire(timeout=timeout)
            if acquired and self.interrupt:
                self.interrupt = False
                raise KeyboardInterrupt("document lock interrupted after acquire")
            return acquired

        def release(self) -> None:
            underlying.release()

        def cleanup_failed_test(self) -> None:
            try:
                underlying.release()
            except RuntimeError:
                pass

    interrupting_lock = InterruptAfterAcquire()
    session._document_lock = interrupting_lock  # type: ignore[assignment]
    try:
        with pytest.raises(
            KeyboardInterrupt,
            match="document lock interrupted after acquire",
        ):
            session.open_document(
                "pkg/service.py",
                deadline=time.monotonic() + 5,
            )

        available = underlying.acquire(blocking=False)
        if available:
            underlying.release()
        assert available

        document = session.open_document(
            "pkg/service.py",
            deadline=time.monotonic() + 10,
        )
        revision = compute_workspace_revision(
            resolve_repository_scope(repository),
            deadline=time.monotonic() + 10,
        )
        assert session.synchronize(
            revision,
            deadline=time.monotonic() + 10,
        ) == WorkspaceDelta((), (), (), (), False)
        assert session._documents[document.source.uri] is document
    finally:
        interrupting_lock.cleanup_failed_test()
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


def test_transparent_restart_requires_fresh_query_after_reopen_and_reprobe(
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
        assert result == ProviderLocations((), "not_ready", True)
        fresh = session.definition(
            _anchor(repository, "pkg/service.py", 10, 20),
            deadline=time.monotonic() + 10,
        )
        assert fresh.coverage == "provider_reported"
        assert len(fresh.locations) == 1
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
        assert sum(event["method"] == "textDocument/definition" for event in messages) == 3
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
        with coordinator.condition:
            assert coordinator.condition.wait_for(
                lambda: coordinator.phase
                is lsp_process._LifecyclePhase.STOPPED_FAILURE,
                timeout=5,
            )
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
        with coordinator.condition:
            assert coordinator.condition.wait_for(
                lambda: coordinator.phase
                is lsp_process._LifecyclePhase.STOPPED_FAILURE,
                timeout=5,
            )
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


def test_definition_drops_response_after_same_generation_document_change(
    monkeypatch: pytest.MonkeyPatch,
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,
) -> None:
    session = _session(repository, state_root, semantic_pyright)
    response_ready = threading.Event()
    release_response = threading.Event()
    results: list[ProviderLocations] = []
    errors: list[BaseException] = []
    worker: threading.Thread | None = None
    try:
        document = session.open_document(
            "pkg/service.py",
            deadline=time.monotonic() + 10,
        )
        process = session._process
        generation = session._generation_nonce
        assert process is not None and generation is not None
        request = LspProcess.request

        def block_completed_definition(
            current: LspProcess,
            method: str,
            params: object,
            *,
            deadline: float,
            cancellation: object = None,
        ) -> object:
            result = request(
                current,
                method,
                params,
                deadline=deadline,
                cancellation=cancellation,  # type: ignore[arg-type]
            )
            if current is process and method == "textDocument/definition":
                response_ready.set()
                if not release_response.wait(max(0.0, deadline - time.monotonic())):
                    raise TimeoutError("definition publication barrier expired")
            return result

        monkeypatch.setattr(LspProcess, "request", block_completed_definition)
        anchor = _anchor(repository, "pkg/service.py", 10, 20)

        def query() -> None:
            try:
                results.append(
                    session.definition(anchor, deadline=time.monotonic() + 10)
                )
            except BaseException as error:
                errors.append(error)

        worker = threading.Thread(target=query)
        worker.start()
        assert response_ready.wait(5), errors

        (repository / "pkg/service.py").write_bytes(
            document.content + b"\nchanged = True\n"
        )
        revision = compute_workspace_revision(
            resolve_repository_scope(repository),
            deadline=time.monotonic() + 10,
        )
        delta = session.synchronize(revision, deadline=time.monotonic() + 10)
        replacement = session._documents[document.source.uri]
        assert delta.changed == ("pkg/service.py",)
        assert replacement is not document
        assert replacement.version == 2
        assert session._generation_nonce == generation

        release_response.set()
        worker.join(5)

        assert not worker.is_alive()
        assert errors == []
        assert results == [ProviderLocations((), "not_ready", True)]
    finally:
        release_response.set()
        if worker is not None:
            worker.join(5)
        session.close(deadline=time.monotonic() + 5)


_SYNCHRONIZE_FENCE_CASES = (
    ("definition", "textDocument/definition"),
    ("references", "textDocument/references"),
    ("implementations", "textDocument/implementation"),
    ("type_definition", "textDocument/typeDefinition"),
    ("document_symbols", "textDocument/documentSymbol"),
    ("hover", "textDocument/hover"),
    ("incoming_calls", "textDocument/prepareCallHierarchy"),
    ("workspace_symbols", "workspace/symbol"),
)


def _synchronize_fence_query(
    session: PyrightSession,
    repository: Path,
    feature: str,
    *,
    deadline: float,
) -> object:
    if feature == "document_symbols":
        return session.document_symbols("pkg/service.py", deadline=deadline)
    if feature == "workspace_symbols":
        return session.workspace_symbols("Service", deadline=deadline)
    anchor = _anchor(repository, "pkg/service.py", 10, 20)
    return getattr(session, feature)(anchor, deadline=deadline)


def _synchronize_fence_not_ready(feature: str) -> object:
    if feature == "hover":
        return ProviderHover(None, None, True)
    if feature == "incoming_calls":
        return ProviderCalls("incoming", (), "not_ready", True)
    return ProviderLocations((), "not_ready", True)


@pytest.mark.parametrize(
    ("feature", "request_method"),
    _SYNCHRONIZE_FENCE_CASES,
)
def test_synchronize_fence_rejects_queries_started_before_and_during_wire_commit(
    monkeypatch: pytest.MonkeyPatch,
    repository: Path,
    state_root: Path,
    feature: str,
    request_method: str,
) -> None:
    fixture = create_semantic_pyright_fixture(
        repository,
        config={
            "capabilities": {
                "callHierarchyProvider": True,
                "definitionProvider": True,
                "documentSymbolProvider": True,
                "hoverProvider": True,
                "implementationProvider": True,
                "referencesProvider": True,
                "textDocumentSync": 2,
                "typeDefinitionProvider": True,
                "workspaceSymbolProvider": True,
            }
        },
    )
    session = _session(repository, state_root, fixture)
    first_response_ready = threading.Event()
    release_first_response = threading.Event()
    wire_delivered = threading.Event()
    release_commit = threading.Event()
    first_done = threading.Event()
    during_done = threading.Event()
    first_results: list[object] = []
    during_results: list[object] = []
    sync_results: list[WorkspaceDelta] = []
    errors: list[BaseException] = []
    semantic_requests = 0
    first_worker: threading.Thread | None = None
    during_worker: threading.Thread | None = None
    sync_worker: threading.Thread | None = None
    try:
        document = session.open_document(
            "pkg/service.py",
            deadline=time.monotonic() + 10,
        )
        scope = resolve_repository_scope(repository)
        prior = compute_workspace_revision(scope, deadline=time.monotonic() + 10)
        session.synchronize(prior, deadline=time.monotonic() + 10)
        process = session._process
        assert process is not None
        request = LspProcess.request
        notify_generation = LspProcess.notify_generation
        start = PyrightSession.start

        def reject_start_inside_active_fence(
            current: PyrightSession,
            *,
            deadline: float,
        ) -> None:
            if current is session and threading.current_thread().name == (
                f"during-synchronize-{feature}"
            ):
                raise AssertionError("semantic fence was checked after startup work")
            start(current, deadline=deadline)

        def block_first_completed_response(
            current: LspProcess,
            method: str,
            params: object,
            *,
            deadline: float,
            cancellation: object = None,
        ) -> object:
            nonlocal semantic_requests
            result = request(
                current,
                method,
                params,
                deadline=deadline,
                cancellation=cancellation,  # type: ignore[arg-type]
            )
            if current is process and method == request_method:
                semantic_requests += 1
                if semantic_requests == 1:
                    first_response_ready.set()
                    if not release_first_response.wait(
                        max(0.0, deadline - time.monotonic())
                    ):
                        raise TimeoutError("semantic response release expired")
            return result

        def hold_after_delivered_change(
            current: LspProcess,
            method: str,
            params: object,
            *,
            generation_nonce: str,
            deadline: float,
        ) -> bool:
            delivered = notify_generation(
                current,
                method,
                params,
                generation_nonce=generation_nonce,
                deadline=deadline,
            )
            if current is process and method == "textDocument/didChange":
                assert delivered is True
                wire_delivered.set()
                if not release_commit.wait(max(0.0, deadline - time.monotonic())):
                    raise TimeoutError("synchronize commit release expired")
            return delivered

        monkeypatch.setattr(LspProcess, "request", block_first_completed_response)
        monkeypatch.setattr(LspProcess, "notify_generation", hold_after_delivered_change)
        monkeypatch.setattr(PyrightSession, "start", reject_start_inside_active_fence)

        def query(results: list[object], done: threading.Event) -> None:
            try:
                results.append(
                    _synchronize_fence_query(
                        session,
                        repository,
                        feature,
                        deadline=time.monotonic() + 15,
                    )
                )
            except BaseException as error:
                errors.append(error)
            finally:
                done.set()

        first_worker = threading.Thread(
            target=query,
            args=(first_results, first_done),
            name=f"pre-synchronize-{feature}",
        )
        first_worker.start()
        assert first_response_ready.wait(5), errors

        (repository / "pkg/service.py").write_bytes(
            document.content + b"\nchanged = True\n"
        )
        revision = compute_workspace_revision(scope, deadline=time.monotonic() + 10)

        def synchronize() -> None:
            try:
                sync_results.append(
                    session.synchronize(revision, deadline=time.monotonic() + 15)
                )
            except BaseException as error:
                errors.append(error)

        sync_worker = threading.Thread(
            target=synchronize,
            name=f"synchronize-{feature}",
        )
        sync_worker.start()
        assert wire_delivered.wait(5), errors
        assert session._documents[document.source.uri] is document

        during_worker = threading.Thread(
            target=query,
            args=(during_results, during_done),
            name=f"during-synchronize-{feature}",
        )
        during_worker.start()
        assert during_done.wait(1), "semantic query waited for synchronize commit"
        assert errors == []
        not_ready = _synchronize_fence_not_ready(feature)
        assert during_results == [not_ready]
        assert semantic_requests == 1

        release_first_response.set()
        assert first_done.wait(5), errors
        assert first_results == [not_ready]
        assert semantic_requests == 1

        release_commit.set()
        sync_worker.join(5)
        assert not sync_worker.is_alive()
        assert errors == []
        assert len(sync_results) == 1
        assert session._documents[document.source.uri] is not document

        fresh = _synchronize_fence_query(
            session,
            repository,
            feature,
            deadline=time.monotonic() + 10,
        )
        assert fresh != not_ready
        assert semantic_requests >= 2
    finally:
        release_first_response.set()
        release_commit.set()
        for worker in (first_worker, during_worker, sync_worker):
            if worker is not None:
                worker.join(5)
        session.close(deadline=time.monotonic() + 5)


def test_call_hierarchy_stops_after_prepare_when_document_version_changes(
    monkeypatch: pytest.MonkeyPatch,
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,
) -> None:
    session = _session(repository, state_root, semantic_pyright)
    prepare_ready = threading.Event()
    release_prepare = threading.Event()
    results: list[ProviderCalls] = []
    errors: list[BaseException] = []
    second_stage_calls = 0
    worker: threading.Thread | None = None
    try:
        document = session.open_document(
            "pkg/service.py",
            deadline=time.monotonic() + 10,
        )
        process = session._process
        assert process is not None
        request = LspProcess.request

        def block_completed_prepare(
            current: LspProcess,
            method: str,
            params: object,
            *,
            deadline: float,
            cancellation: object = None,
        ) -> object:
            nonlocal second_stage_calls
            result = request(
                current,
                method,
                params,
                deadline=deadline,
                cancellation=cancellation,  # type: ignore[arg-type]
            )
            if current is process and method == "textDocument/prepareCallHierarchy":
                prepare_ready.set()
                if not release_prepare.wait(max(0.0, deadline - time.monotonic())):
                    raise TimeoutError("call prepare publication barrier expired")
            elif current is process and method == "callHierarchy/incomingCalls":
                second_stage_calls += 1
            return result

        monkeypatch.setattr(LspProcess, "request", block_completed_prepare)
        anchor = _anchor(repository, "pkg/service.py", 10, 20)

        def query() -> None:
            try:
                results.append(
                    session.incoming_calls(anchor, deadline=time.monotonic() + 10)
                )
            except BaseException as error:
                errors.append(error)

        worker = threading.Thread(target=query)
        worker.start()
        assert prepare_ready.wait(5), errors

        (repository / "pkg/service.py").write_bytes(
            document.content + b"\nchanged = True\n"
        )
        revision = compute_workspace_revision(
            resolve_repository_scope(repository),
            deadline=time.monotonic() + 10,
        )
        session.synchronize(revision, deadline=time.monotonic() + 10)
        release_prepare.set()
        worker.join(5)

        assert not worker.is_alive()
        assert errors == []
        assert results == [ProviderCalls("incoming", (), "not_ready", True)]
        assert second_stage_calls == 0
    finally:
        release_prepare.set()
        if worker is not None:
            worker.join(5)
        session.close(deadline=time.monotonic() + 5)


def test_workspace_symbols_drop_response_after_generation_replacement(
    monkeypatch: pytest.MonkeyPatch,
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,
) -> None:
    session = _session(repository, state_root, semantic_pyright)
    response_ready = threading.Event()
    release_response = threading.Event()
    results: list[ProviderLocations] = []
    errors: list[BaseException] = []
    worker: threading.Thread | None = None
    try:
        session.open_document("pkg/service.py", deadline=time.monotonic() + 10)
        process = session._process
        generation = session._generation_nonce
        assert process is not None and generation is not None
        assert session.readiness == "query_ready"
        request = LspProcess.request

        def block_completed_workspace_symbols(
            current: LspProcess,
            method: str,
            params: object,
            *,
            deadline: float,
            cancellation: object = None,
        ) -> object:
            result = request(
                current,
                method,
                params,
                deadline=deadline,
                cancellation=cancellation,  # type: ignore[arg-type]
            )
            if current is process and method == "workspace/symbol":
                response_ready.set()
                if not release_response.wait(max(0.0, deadline - time.monotonic())):
                    raise TimeoutError("workspace symbol publication barrier expired")
            return result

        monkeypatch.setattr(LspProcess, "request", block_completed_workspace_symbols)

        def query() -> None:
            try:
                results.append(
                    session.workspace_symbols("Service", deadline=time.monotonic() + 15)
                )
            except BaseException as error:
                errors.append(error)

        worker = threading.Thread(target=query)
        worker.start()
        assert response_ready.wait(5), errors

        process.restart(time.monotonic() + 10)
        assert session._process is process
        assert session._generation_nonce == process.generation_nonce
        assert session._generation_nonce != generation
        assert session.readiness == "query_ready"

        release_response.set()
        worker.join(5)

        assert not worker.is_alive()
        assert errors == []
        assert results == [ProviderLocations((), "not_ready", True)]
    finally:
        release_response.set()
        if worker is not None:
            worker.join(5)
        session.close(deadline=time.monotonic() + 5)


def test_diagnostics_wait_stops_when_exact_open_document_is_replaced(
    monkeypatch: pytest.MonkeyPatch,
    repository: Path,
    state_root: Path,
) -> None:
    fixture = create_semantic_pyright_fixture(
        repository,
        config={"push_diagnostics": False},
    )
    session = _session(repository, state_root, fixture)
    waiting = threading.Event()
    done = threading.Event()
    results: list[ProviderDiagnostics] = []
    errors: list[BaseException] = []
    worker: threading.Thread | None = None
    real_wait = threading.Condition.wait

    def observe_diagnostics_wait(
        condition: threading.Condition,
        timeout: float | None = None,
    ) -> bool:
        if condition is session._condition and threading.current_thread().name == (
            "stale-diagnostics-wait"
        ):
            waiting.set()
        return real_wait(condition, timeout)

    monkeypatch.setattr(threading.Condition, "wait", observe_diagnostics_wait)
    try:
        document = session.open_document(
            "pkg/service.py",
            deadline=time.monotonic() + 10,
        )

        def query() -> None:
            try:
                results.append(
                    session.diagnostics(
                        "pkg/service.py",
                        deadline=time.monotonic() + 1,
                    )
                )
            except BaseException as error:
                errors.append(error)
            finally:
                done.set()

        worker = threading.Thread(target=query, name="stale-diagnostics-wait")
        worker.start()
        assert waiting.wait(1), errors

        (repository / "pkg/service.py").write_bytes(
            document.content + b"\nchanged = True\n"
        )
        revision = compute_workspace_revision(
            resolve_repository_scope(repository),
            deadline=time.monotonic() + 10,
        )
        session.synchronize(revision, deadline=time.monotonic() + 10)

        assert done.wait(0.25), "diagnostics wait retained a replaced document"
        assert errors == []
        assert results == [ProviderDiagnostics((), None, True)]
    finally:
        if worker is not None:
            worker.join(2)
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


def _client_messages(fixture: SemanticPyrightFixture, method: str) -> tuple[dict, ...]:
    return tuple(
        event["message"]
        for event in fixture.events()
        if event.get("kind") == "client-message" and event.get("method") == method
    )


def _wait_client_messages(
    fixture: SemanticPyrightFixture, method: str, *, count: int = 1
) -> tuple[dict, ...]:
    deadline = time.monotonic() + 5
    while True:
        messages = _client_messages(fixture, method)
        if len(messages) >= count or time.monotonic() >= deadline:
            return messages
        time.sleep(0.02)


def test_first_synchronize_establishes_retained_workspace_revision(
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,
) -> None:
    session = _session(repository, state_root, semantic_pyright)
    try:
        session.open_document("pkg/service.py", deadline=time.monotonic() + 10)
        scope = resolve_repository_scope(repository)
        revision = compute_workspace_revision(scope, deadline=time.monotonic() + 10)
        delta = session.synchronize(revision, deadline=time.monotonic() + 10)
        assert delta == WorkspaceDelta((), (), (), (), False)
        assert session._workspace_revision is revision
    finally:
        session.close(deadline=time.monotonic() + 5)


def test_first_synchronize_reverifies_unchanged_open_document_after_revision(
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,
) -> None:
    session = _session(repository, state_root, semantic_pyright)
    try:
        document = session.open_document(
            "pkg/service.py",
            deadline=time.monotonic() + 10,
        )
        revision = compute_workspace_revision(
            resolve_repository_scope(repository),
            deadline=time.monotonic() + 10,
        )
        (repository / "pkg/service.py").write_bytes(b"mutated_after_revision = True\n")
        before_events = tuple(semantic_pyright.events())

        with pytest.raises(RuntimeError, match="hash differs from the revision"):
            session.synchronize(revision, deadline=time.monotonic() + 10)

        assert tuple(semantic_pyright.events()) == before_events
        assert session._documents[document.source.uri] is document
        assert session._workspace_revision is None
    finally:
        session.close(deadline=time.monotonic() + 5)


def test_every_synchronize_reverifies_unchanged_retained_document_before_wire(
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,
) -> None:
    session = _session(repository, state_root, semantic_pyright)
    try:
        document = session.open_document(
            "pkg/service.py",
            deadline=time.monotonic() + 10,
        )
        scope = resolve_repository_scope(repository)
        prior = compute_workspace_revision(scope, deadline=time.monotonic() + 10)
        session.synchronize(prior, deadline=time.monotonic() + 10)
        unchanged = compute_workspace_revision(scope, deadline=time.monotonic() + 10)
        before_events = tuple(semantic_pyright.events())
        before_document_bytes = session._document_bytes
        (repository / "pkg/service.py").write_bytes(b"mutated_after_snapshot = True\n")

        with pytest.raises(RuntimeError, match="hash differs from the revision"):
            session.synchronize(unchanged, deadline=time.monotonic() + 10)

        assert tuple(semantic_pyright.events()) == before_events
        assert session._documents[document.source.uri] is document
        assert session._document_bytes == before_document_bytes
        assert session._workspace_revision is prior
    finally:
        session.close(deadline=time.monotonic() + 5)


def test_first_synchronize_reconciles_changed_and_absent_open_documents(
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,
) -> None:
    absent_path = repository / "pkg/absent.py"
    absent_path.write_bytes(b"absent = True\n")
    session = _session(repository, state_root, semantic_pyright)
    try:
        changed = session.open_document(
            "pkg/service.py",
            deadline=time.monotonic() + 10,
        )
        absent = session.open_document(
            "pkg/absent.py",
            deadline=time.monotonic() + 10,
        )
        (repository / "pkg/service.py").write_bytes(b"changed = True\n")
        absent_path.unlink()
        revision = compute_workspace_revision(
            resolve_repository_scope(repository),
            deadline=time.monotonic() + 10,
        )

        delta = session.synchronize(revision, deadline=time.monotonic() + 10)

        assert delta.changed == ("pkg/service.py",)
        assert delta.deleted == ("pkg/absent.py",)
        changes = _wait_client_messages(semantic_pyright, "textDocument/didChange")
        closes = _wait_client_messages(semantic_pyright, "textDocument/didClose")
        assert len(changes) == 1
        assert len(closes) == 1
        assert session._documents[changed.source.uri].content == b"changed = True\n"
        assert absent.source.uri not in session._documents
        assert session._workspace_revision is revision
    finally:
        session.close(deadline=time.monotonic() + 5)


def test_first_synchronize_rejects_stale_changed_document_revision(
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,
) -> None:
    session = _session(repository, state_root, semantic_pyright)
    try:
        document = session.open_document(
            "pkg/service.py",
            deadline=time.monotonic() + 10,
        )
        (repository / "pkg/service.py").write_bytes(b"first = True\n")
        revision = compute_workspace_revision(
            resolve_repository_scope(repository),
            deadline=time.monotonic() + 10,
        )
        (repository / "pkg/service.py").write_bytes(b"second = True\n")

        with pytest.raises(RuntimeError, match="hash differs from the revision"):
            session.synchronize(revision, deadline=time.monotonic() + 10)

        assert session._documents[document.source.uri] is document
        assert session._workspace_revision is None
        assert _client_messages(semantic_pyright, "textDocument/didChange") == ()
    finally:
        session.close(deadline=time.monotonic() + 5)


@pytest.mark.parametrize("bound", ["document", "aggregate"])
def test_synchronize_rejects_projected_source_bounds_before_reads_or_notifications(
    monkeypatch: pytest.MonkeyPatch,
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,
    bound: str,
) -> None:
    session = _session(repository, state_root, semantic_pyright)
    try:
        document = session.open_document(
            "pkg/service.py",
            deadline=time.monotonic() + 10,
        )
        scope = resolve_repository_scope(repository)
        prior = compute_workspace_revision(scope, deadline=time.monotonic() + 10)
        session.synchronize(prior, deadline=time.monotonic() + 10)
        (repository / "pkg/service.py").write_bytes(b"projected = True\n")
        revision = compute_workspace_revision(scope, deadline=time.monotonic() + 10)
        if bound == "document":
            revision = replace(
                revision,
                entries=tuple(
                    replace(
                        entry,
                        size=pyright_session_module._MAX_DOCUMENT_BYTES + 1,
                    )
                    if entry.path == "pkg/service.py"
                    else entry
                    for entry in revision.entries
                ),
            )
            expected = "source document byte limit"
        else:
            entry = next(
                item for item in revision.entries if item.path == "pkg/service.py"
            )
            monkeypatch.setattr(
                pyright_session_module,
                "_MAX_OPEN_DOCUMENT_BYTES",
                entry.size - 1,
            )
            expected = "open document source bytes limit"

        reads = 0

        def reject_read(*_args: object, **_kwargs: object) -> bytes:
            nonlocal reads
            reads += 1
            raise AssertionError("changed document was read before projected bounds")

        monkeypatch.setattr(pyright_session_module, "read_stable_bytes", reject_read)
        before_events = tuple(semantic_pyright.events())

        with pytest.raises(RuntimeError, match=expected):
            session.synchronize(revision, deadline=time.monotonic() + 10)

        assert reads == 0
        assert tuple(semantic_pyright.events()) == before_events
        assert session._documents[document.source.uri] is document
        assert session._workspace_revision is prior
    finally:
        session.close(deadline=time.monotonic() + 5)


def test_changed_open_document_sends_full_did_change_and_increments_version_once(
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,
) -> None:
    session = _session(repository, state_root, semantic_pyright)
    try:
        document = session.open_document(
            "pkg/service.py", deadline=time.monotonic() + 10
        )
        assert document.version == 1
        scope = resolve_repository_scope(repository)
        session.synchronize(
            compute_workspace_revision(scope, deadline=time.monotonic() + 10),
            deadline=time.monotonic() + 10,
        )
        (repository / "pkg/service.py").write_bytes(
            b"class Updated:\n    pass\n"
        )
        delta = session.synchronize(
            compute_workspace_revision(scope, deadline=time.monotonic() + 10),
            deadline=time.monotonic() + 10,
        )
        assert delta.changed == ("pkg/service.py",)
        changes = _wait_client_messages(semantic_pyright, "textDocument/didChange")
        assert len(changes) == 1
        body = changes[0]["params"]
        assert body["textDocument"]["version"] == 2
        assert body["contentChanges"] == [{"text": "class Updated:\n    pass\n"}]
        assert session._documents[document.source.uri].version == 2
    finally:
        session.close(deadline=time.monotonic() + 5)


def test_synchronize_proves_retained_document_open_before_did_change(
    monkeypatch: pytest.MonkeyPatch,
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,
) -> None:
    session = _session(repository, state_root, semantic_pyright)
    sent_methods: list[str] = []
    recoveries: list[tuple[LspProcess, str]] = []
    try:
        session.start(deadline=time.monotonic() + 10)
        process = session._process
        assert process is not None
        notify_generation = LspProcess.notify_generation

        def reject_did_open(
            current: LspProcess,
            method: str,
            params: object,
            *,
            generation_nonce: str,
            deadline: float,
        ) -> bool:
            if current is process:
                sent_methods.append(method)
                if method == "textDocument/didOpen":
                    return False
            return notify_generation(
                current,
                method,
                params,
                generation_nonce=generation_nonce,
                deadline=deadline,
            )

        monkeypatch.setattr(LspProcess, "notify_generation", reject_did_open)
        document = session.open_document(
            "pkg/service.py",
            deadline=time.monotonic() + 10,
        )
        generation = session._generation_nonce
        assert generation is not None
        assert not session._wire_document_opened(document, generation)
        sent_methods.clear()

        def record_recovery(
            current: LspProcess,
            failed_generation: str,
            *,
            deadline: float,
        ) -> None:
            assert current is process
            assert failed_generation == generation
            assert time.monotonic() < deadline
            recoveries.append((current, failed_generation))

        monkeypatch.setattr(
            session,
            "_recover_synchronize_snapshot",
            record_recovery,
        )
        (repository / "pkg/service.py").write_bytes(b"changed = True\n")
        revision = compute_workspace_revision(
            resolve_repository_scope(repository),
            deadline=time.monotonic() + 10,
        )

        with pytest.raises(RuntimeError, match="didOpen"):
            session.synchronize(revision, deadline=time.monotonic() + 10)

        assert sent_methods == ["textDocument/didOpen"]
        assert recoveries == [(process, generation)]
        assert session._documents[document.source.uri] is document
        assert session._workspace_revision is None
        assert not session._wire_document_opened(document, generation)
        with session._lock:
            assert not session._document_ready_locked(document.source.uri)
    finally:
        session.close(deadline=time.monotonic() + 5)


def test_synchronize_commit_preserves_diagnostics_published_during_wire_calls(
    monkeypatch: pytest.MonkeyPatch,
    repository: Path,
    state_root: Path,
) -> None:
    fixture = create_semantic_pyright_fixture(
        repository,
        config={"push_diagnostics": False},
    )
    session = _session(repository, state_root, fixture)
    wire_entered = threading.Event()
    release_wire = threading.Event()
    sync_errors: list[BaseException] = []
    sync_results: list[WorkspaceDelta] = []
    worker: threading.Thread | None = None
    base = {
        "range": {
            "start": {"line": 0, "character": 0},
            "end": {"line": 0, "character": 1},
        },
    }
    try:
        service = session.open_document(
            "pkg/service.py",
            deadline=time.monotonic() + 10,
        )
        api = session.open_document(
            "pkg/api.py",
            deadline=time.monotonic() + 10,
        )
        scope = resolve_repository_scope(repository)
        prior = compute_workspace_revision(scope, deadline=time.monotonic() + 10)
        session.synchronize(prior, deadline=time.monotonic() + 10)
        session._publish_diagnostics(
            {
                "uri": api.source.uri,
                "version": 1,
                "diagnostics": [{**base, "message": "retained api diagnostic"}],
            }
        )
        (repository / "pkg/service.py").write_bytes(b"changed = True\n")
        revision = compute_workspace_revision(scope, deadline=time.monotonic() + 10)
        process = session._process
        assert process is not None

        notify_generation = LspProcess.notify_generation

        def block_change_notification(
            current: LspProcess,
            method: str,
            params: object,
            *,
            generation_nonce: str,
            deadline: float,
        ) -> bool:
            if current is process and method == "textDocument/didChange":
                assert generation_nonce == process.generation_nonce
                assert time.monotonic() < deadline
                wire_entered.set()
                if not release_wire.wait(max(0.0, deadline - time.monotonic())):
                    raise TimeoutError("interleaved diagnostics release expired")
                return True
            return notify_generation(
                current,
                method,
                params,
                generation_nonce=generation_nonce,
                deadline=deadline,
            )

        monkeypatch.setattr(LspProcess, "notify_generation", block_change_notification)

        def synchronize() -> None:
            try:
                sync_results.append(
                    session.synchronize(revision, deadline=time.monotonic() + 10)
                )
            except BaseException as error:
                sync_errors.append(error)

        worker = threading.Thread(target=synchronize)
        worker.start()
        assert wire_entered.wait(5)
        session._publish_diagnostics(
            {
                "uri": service.source.uri,
                "version": 2,
                "diagnostics": [
                    {**base, "message": "published between snapshot and commit"}
                ],
            }
        )
        release_wire.set()
        worker.join(5)

        assert not worker.is_alive()
        assert sync_errors == []
        assert len(sync_results) == 1
        assert set(session._diagnostics) == {service.source.uri, api.source.uri}
        assert session._diagnostics[service.source.uri].document_version == 2
        assert session._diagnostic_bytes == sum(
            snapshot.retained_bytes for snapshot in session._diagnostics.values()
        )
        assert session._diagnostic_bytes <= pyright_session_module._MAX_DIAGNOSTIC_BYTES
    finally:
        release_wire.set()
        if worker is not None:
            worker.join(5)
        session.close(deadline=time.monotonic() + 5)


@pytest.mark.parametrize(
    ("failed_method", "failure_mode"),
    [
        ("textDocument/didChange", "false"),
        ("textDocument/didClose", "exception"),
        ("workspace/didChangeWatchedFiles", "false"),
    ],
)
def test_synchronize_notification_failure_replays_prior_snapshot_without_commit(
    monkeypatch: pytest.MonkeyPatch,
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,
    failed_method: str,
    failure_mode: str,
) -> None:
    session = _session(repository, state_root, semantic_pyright)
    try:
        session.open_document("pkg/service.py", deadline=time.monotonic() + 10)
        scope = resolve_repository_scope(repository)
        prior = compute_workspace_revision(scope, deadline=time.monotonic() + 10)
        session.synchronize(prior, deadline=time.monotonic() + 10)
        process = session._process
        assert process is not None
        before_documents = dict(session._documents)
        before_document_bytes = session._document_bytes

        if failed_method == "textDocument/didChange":
            (repository / "pkg/service.py").write_bytes(b"changed = True\n")
        elif failed_method == "textDocument/didClose":
            (repository / "pkg/service.py").unlink()
        else:
            (repository / "pkg/watched.py").write_bytes(b"watched = True\n")
        revision = compute_workspace_revision(scope, deadline=time.monotonic() + 10)

        notify_generation = LspProcess.notify_generation
        notify = LspProcess.notify

        def fail_generation(
            current: LspProcess,
            method: str,
            params: object,
            *,
            generation_nonce: str,
            deadline: float,
        ) -> bool:
            delivered = notify_generation(
                current,
                method,
                params,
                generation_nonce=generation_nonce,
                deadline=deadline,
            )
            if current is not process or method != failed_method:
                return delivered
            if failure_mode == "exception":
                raise RuntimeError("injected notification failure")
            return False

        def fail_notify(
            current: LspProcess,
            method: str,
            params: object,
            *,
            deadline: float,
        ) -> object:
            delivered = notify(current, method, params, deadline=deadline)
            if current is not process or method != failed_method:
                return delivered
            if failure_mode == "exception":
                raise RuntimeError("injected notification failure")
            return False

        monkeypatch.setattr(LspProcess, "notify_generation", fail_generation)
        monkeypatch.setattr(LspProcess, "notify", fail_notify)

        with pytest.raises(RuntimeError, match="notification"):
            session.synchronize(revision, deadline=time.monotonic() + 10)

        assert session._workspace_revision is prior
        assert session._documents == before_documents
        assert session._document_bytes == before_document_bytes
        assert process.restart_count == 1
        generation = session._generation_nonce
        assert generation == process.generation_nonce
        assert all(
            session._wire_document_opened(document, generation)
            for document in before_documents.values()
        )
    finally:
        session.close(deadline=time.monotonic() + 5)


def test_synchronize_recovery_preserves_already_replayed_replacement_state(
    monkeypatch: pytest.MonkeyPatch,
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,
) -> None:
    session = _session(repository, state_root, semantic_pyright)
    try:
        document = session.open_document(
            "pkg/service.py",
            deadline=time.monotonic() + 10,
        )
        process = session._process
        failed_generation = session._generation_nonce
        assert process is not None and failed_generation is not None

        process.restart(time.monotonic() + 10)
        replacement_generation = session._generation_nonce
        assert replacement_generation == process.generation_nonce
        assert replacement_generation != failed_generation
        assert session.readiness == "query_ready"
        session._publish_diagnostics(
            {
                "uri": document.source.uri,
                "version": document.version,
                "diagnostics": [],
            }
        )
        with session._lock:
            readiness = session._readiness
            evidence = session._readiness_evidence
            ready_uris = dict(session._ready_uri_generations)
            diagnostics = dict(session._diagnostics)
            diagnostic_bytes = session._diagnostic_bytes
            assert session._synchronize_snapshot_replayed_locked(process)

        def forbidden_restart(_process: LspProcess, _deadline: float) -> None:
            pytest.fail("already replayed replacement generation restarted again")

        monkeypatch.setattr(LspProcess, "restart", forbidden_restart)

        session._recover_synchronize_snapshot(
            process,
            failed_generation,
            deadline=time.monotonic() + 10,
        )

        with session._lock:
            assert session._readiness == readiness == "query_ready"
            assert session._readiness_evidence == evidence
            assert session._ready_uri_generations == ready_uris
            assert session._diagnostics == diagnostics
            assert session._diagnostic_bytes == diagnostic_bytes
            assert session._generation_nonce == replacement_generation
            assert session._process is process
            assert session._synchronize_snapshot_replayed_locked(process)
    finally:
        session.close(deadline=time.monotonic() + 5)


def test_synchronize_deadline_recovery_quarantines_partially_mutated_process(
    monkeypatch: pytest.MonkeyPatch,
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,
) -> None:
    session = _session(repository, state_root, semantic_pyright)
    partial_changes: list[str] = []
    recovery_deadlines: list[float] = []
    stale_fresh_calls: list[str] = []
    failure_returned = False
    try:
        document = session.open_document(
            "pkg/service.py",
            deadline=time.monotonic() + 10,
        )
        scope = resolve_repository_scope(repository)
        prior = compute_workspace_revision(scope, deadline=time.monotonic() + 10)
        session.synchronize(prior, deadline=time.monotonic() + 10)
        process = session._process
        generation = session._generation_nonce
        assert process is not None and generation is not None
        (repository / "pkg/service.py").write_bytes(b"changed = True\n")
        (repository / "pkg/watched.py").write_bytes(b"watched = True\n")
        revision = compute_workspace_revision(scope, deadline=time.monotonic() + 10)
        notify_generation = LspProcess.notify_generation
        restart = LspProcess.restart

        def fail_after_partial_change(
            current: LspProcess,
            method: str,
            params: object,
            *,
            generation_nonce: str,
            deadline: float,
        ) -> bool:
            if current is process and failure_returned:
                stale_fresh_calls.append(method)
                raise AssertionError("fresh work reached quarantined process")
            if current is process and method == "textDocument/didChange":
                delivered = notify_generation(
                    current,
                    method,
                    params,
                    generation_nonce=generation_nonce,
                    deadline=deadline,
                )
                assert delivered is True
                partial_changes.append(generation_nonce)
                return True
            if current is process and method == "workspace/didChangeWatchedFiles":
                assert partial_changes == [generation]
                return False
            return notify_generation(
                current,
                method,
                params,
                generation_nonce=generation_nonce,
                deadline=deadline,
            )

        def expire_recovery(current: LspProcess, deadline: float) -> None:
            if current is process:
                assert partial_changes == [generation]
                recovery_deadlines.append(deadline)
                raise TimeoutError("injected synchronization recovery deadline expired")
            restart(current, deadline)

        monkeypatch.setattr(
            LspProcess,
            "notify_generation",
            fail_after_partial_change,
        )
        monkeypatch.setattr(LspProcess, "restart", expire_recovery)

        with pytest.raises(RuntimeError, match="notification recovery failed"):
            session.synchronize(revision, deadline=time.monotonic() + 10)
        failure_returned = True

        assert partial_changes == [generation]
        assert len(recovery_deadlines) == 1
        assert session._process is None
        assert session._startup_process is process
        assert session._generation_nonce is None
        assert session.readiness == "not_ready"
        assert session._workspace_revision is prior
        assert session._documents[document.source.uri] is document

        delta = session.synchronize(revision, deadline=time.monotonic() + 10)

        assert delta.changed == ("pkg/service.py",)
        assert session._process is not None and session._process is not process
        assert session._startup_process is None
        assert session._workspace_revision is revision
        assert stale_fresh_calls == []
    finally:
        session.close(deadline=time.monotonic() + 5)


def test_notification_interruption_outranks_later_recovery_interruption(
    monkeypatch: pytest.MonkeyPatch,
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,
) -> None:
    session = _session(repository, state_root, semantic_pyright)
    try:
        session.open_document("pkg/service.py", deadline=time.monotonic() + 10)
        scope = resolve_repository_scope(repository)
        prior = compute_workspace_revision(scope, deadline=time.monotonic() + 10)
        session.synchronize(prior, deadline=time.monotonic() + 10)
        process = session._process
        assert process is not None
        (repository / "pkg/service.py").write_bytes(b"changed = True\n")
        revision = compute_workspace_revision(scope, deadline=time.monotonic() + 10)
        notify_generation = LspProcess.notify_generation

        def make_notification_interruption() -> KeyboardInterrupt:
            try:
                raise KeyboardInterrupt("original notification interruption")
            except KeyboardInterrupt as error:
                return error

        notification_interruption = make_notification_interruption()
        notification_wrapper = RuntimeError("notification interruption wrapper")
        notification_wrapper.__cause__ = notification_interruption
        recovery_interruption = SystemExit(61)

        def interrupt_notification(
            current: LspProcess,
            method: str,
            params: object,
            *,
            generation_nonce: str,
            deadline: float,
        ) -> bool:
            if current is process and method == "textDocument/didChange":
                raise notification_wrapper
            return notify_generation(
                current,
                method,
                params,
                generation_nonce=generation_nonce,
                deadline=deadline,
            )

        def interrupt_recovery(
            _process: LspProcess,
            _failed_generation: str,
            *,
            deadline: float,
        ) -> None:
            assert time.monotonic() < deadline
            raise recovery_interruption

        monkeypatch.setattr(LspProcess, "notify_generation", interrupt_notification)
        monkeypatch.setattr(
            session,
            "_recover_synchronize_snapshot",
            interrupt_recovery,
        )

        with pytest.raises(
            KeyboardInterrupt,
            match="original notification interruption",
        ) as raised:
            session.synchronize(revision, deadline=time.monotonic() + 10)

        assert raised.value is notification_interruption
        assert raised.value.__cause__ is recovery_interruption
        assert recovery_interruption.__context__ is None
        assert notification_wrapper.__cause__ is notification_interruption
    finally:
        session.close(deadline=time.monotonic() + 5)


def test_wrapped_recovery_interruption_is_not_downgraded_to_runtime_error(
    monkeypatch: pytest.MonkeyPatch,
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,
) -> None:
    session = _session(repository, state_root, semantic_pyright)
    try:
        session.open_document("pkg/service.py", deadline=time.monotonic() + 10)
        scope = resolve_repository_scope(repository)
        prior = compute_workspace_revision(scope, deadline=time.monotonic() + 10)
        session.synchronize(prior, deadline=time.monotonic() + 10)
        process = session._process
        assert process is not None
        (repository / "pkg/service.py").write_bytes(b"changed = True\n")
        revision = compute_workspace_revision(scope, deadline=time.monotonic() + 10)
        notify_generation = LspProcess.notify_generation
        notification_error = RuntimeError("ordinary notification failure")
        recovery_interruption = SystemExit(67)
        recovery_wrapper = RuntimeError("recovery interruption wrapper")
        recovery_wrapper.__cause__ = recovery_interruption

        def fail_notification(
            current: LspProcess,
            method: str,
            params: object,
            *,
            generation_nonce: str,
            deadline: float,
        ) -> bool:
            if current is process and method == "textDocument/didChange":
                raise notification_error
            return notify_generation(
                current,
                method,
                params,
                generation_nonce=generation_nonce,
                deadline=deadline,
            )

        def wrap_recovery_interruption(
            _process: LspProcess,
            _failed_generation: str,
            *,
            deadline: float,
        ) -> None:
            assert time.monotonic() < deadline
            raise recovery_wrapper

        monkeypatch.setattr(LspProcess, "notify_generation", fail_notification)
        monkeypatch.setattr(
            session,
            "_recover_synchronize_snapshot",
            wrap_recovery_interruption,
        )

        with pytest.raises(SystemExit) as raised:
            session.synchronize(revision, deadline=time.monotonic() + 10)

        assert raised.value is recovery_interruption
        assert raised.value.__cause__ is notification_error
        assert raised.value.__context__ is None
        assert recovery_wrapper.__cause__ is recovery_interruption
    finally:
        session.close(deadline=time.monotonic() + 5)


def test_synchronize_commit_recovery_rethrows_interruption(
    monkeypatch: pytest.MonkeyPatch,
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,
) -> None:
    session = _session(repository, state_root, semantic_pyright)
    try:
        session.open_document("pkg/service.py", deadline=time.monotonic() + 10)
        scope = resolve_repository_scope(repository)
        prior = compute_workspace_revision(scope, deadline=time.monotonic() + 10)
        session.synchronize(prior, deadline=time.monotonic() + 10)
        (repository / "pkg/service.py").write_bytes(b"changed = True\n")
        revision = compute_workspace_revision(scope, deadline=time.monotonic() + 10)
        process = session._process
        assert process is not None
        notify_generation = LspProcess.notify_generation

        def change_state_before_commit(
            current: LspProcess,
            method: str,
            params: object,
            *,
            generation_nonce: str,
            deadline: float,
        ) -> bool:
            delivered = notify_generation(
                current,
                method,
                params,
                generation_nonce=generation_nonce,
                deadline=deadline,
            )
            if current is process and method == "textDocument/didChange":
                with session._lock:
                    session._workspace_revision = None
            return delivered

        def interrupt_commit_recovery(
            _process: LspProcess,
            _failed_generation: str,
            *,
            deadline: float,
        ) -> None:
            assert time.monotonic() < deadline
            raise SystemExit(41)

        monkeypatch.setattr(LspProcess, "notify_generation", change_state_before_commit)
        monkeypatch.setattr(
            session,
            "_recover_synchronize_snapshot",
            interrupt_commit_recovery,
        )

        with pytest.raises(SystemExit) as raised:
            session.synchronize(revision, deadline=time.monotonic() + 10)

        assert raised.value.code == 41
        traceback_names: list[str] = []
        current = raised.value.__traceback__
        while current is not None:
            traceback_names.append(current.tb_frame.f_code.co_name)
            current = current.tb_next
        assert "interrupt_commit_recovery" in traceback_names
    finally:
        session.close(deadline=time.monotonic() + 5)


def test_synchronize_commit_recovery_unwraps_interruption_without_cycle(
    monkeypatch: pytest.MonkeyPatch,
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,
) -> None:
    session = _session(repository, state_root, semantic_pyright)
    try:
        session.open_document("pkg/service.py", deadline=time.monotonic() + 10)
        scope = resolve_repository_scope(repository)
        prior = compute_workspace_revision(scope, deadline=time.monotonic() + 10)
        session.synchronize(prior, deadline=time.monotonic() + 10)
        (repository / "pkg/service.py").write_bytes(b"changed = True\n")
        revision = compute_workspace_revision(scope, deadline=time.monotonic() + 10)
        process = session._process
        assert process is not None
        notify_generation = LspProcess.notify_generation
        recovery_interruption = KeyboardInterrupt("wrapped commit recovery interruption")
        recovery_wrapper = RuntimeError("commit recovery interruption wrapper")
        recovery_wrapper.__cause__ = recovery_interruption

        def change_state_before_commit(
            current: LspProcess,
            method: str,
            params: object,
            *,
            generation_nonce: str,
            deadline: float,
        ) -> bool:
            delivered = notify_generation(
                current,
                method,
                params,
                generation_nonce=generation_nonce,
                deadline=deadline,
            )
            if current is process and method == "textDocument/didChange":
                with session._lock:
                    session._workspace_revision = None
            return delivered

        def wrap_commit_recovery_interruption(
            _process: LspProcess,
            _failed_generation: str,
            *,
            deadline: float,
        ) -> None:
            assert time.monotonic() < deadline
            raise recovery_wrapper

        monkeypatch.setattr(LspProcess, "notify_generation", change_state_before_commit)
        monkeypatch.setattr(
            session,
            "_recover_synchronize_snapshot",
            wrap_commit_recovery_interruption,
        )

        with pytest.raises(
            KeyboardInterrupt,
            match="wrapped commit recovery interruption",
        ) as raised:
            session.synchronize(revision, deadline=time.monotonic() + 10)

        assert raised.value is recovery_interruption
        assert isinstance(raised.value.__cause__, RuntimeError)
        assert "state changed before commit" in str(raised.value.__cause__)
        assert raised.value.__context__ is None
        assert recovery_wrapper.__cause__ is recovery_interruption
    finally:
        session.close(deadline=time.monotonic() + 5)


def test_synchronize_rejects_noop_recovery_as_unproven(
    monkeypatch: pytest.MonkeyPatch,
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,
) -> None:
    session = _session(repository, state_root, semantic_pyright)
    try:
        document = session.open_document(
            "pkg/service.py",
            deadline=time.monotonic() + 10,
        )
        scope = resolve_repository_scope(repository)
        prior = compute_workspace_revision(scope, deadline=time.monotonic() + 10)
        session.synchronize(prior, deadline=time.monotonic() + 10)
        process = session._process
        generation = session._generation_nonce
        assert process is not None and generation is not None
        (repository / "pkg/service.py").write_bytes(b"changed = True\n")
        revision = compute_workspace_revision(scope, deadline=time.monotonic() + 10)
        notify_generation = LspProcess.notify_generation

        def partial_change(
            current: LspProcess,
            method: str,
            params: object,
            *,
            generation_nonce: str,
            deadline: float,
        ) -> bool:
            delivered = notify_generation(
                current,
                method,
                params,
                generation_nonce=generation_nonce,
                deadline=deadline,
            )
            return False if current is process and method == "textDocument/didChange" else delivered

        monkeypatch.setattr(LspProcess, "notify_generation", partial_change)
        monkeypatch.setattr(
            LspProcess,
            "restart",
            lambda _self, _deadline: None,
        )

        with pytest.raises(RuntimeError, match="notification recovery failed"):
            session.synchronize(revision, deadline=time.monotonic() + 10)

        assert session._workspace_revision is prior
        assert session._documents[document.source.uri] is document
        assert session._generation_nonce is None
        assert session._process is None
        assert session._startup_process is process
        assert session.readiness == "not_ready"
    finally:
        session.close(deadline=time.monotonic() + 5)


def test_unchanged_content_sends_no_did_change_and_keeps_version(
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,
) -> None:
    session = _session(repository, state_root, semantic_pyright)
    try:
        document = session.open_document(
            "pkg/service.py", deadline=time.monotonic() + 10
        )
        scope = resolve_repository_scope(repository)
        session.synchronize(
            compute_workspace_revision(scope, deadline=time.monotonic() + 10),
            deadline=time.monotonic() + 10,
        )
        delta = session.synchronize(
            compute_workspace_revision(scope, deadline=time.monotonic() + 10),
            deadline=time.monotonic() + 10,
        )
        assert delta.changed == ()
        assert _client_messages(semantic_pyright, "textDocument/didChange") == ()
        assert session._documents[document.source.uri].version == 1
    finally:
        session.close(deadline=time.monotonic() + 5)


def test_created_and_deleted_files_produce_watched_file_events(
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,
) -> None:
    session = _session(repository, state_root, semantic_pyright)
    try:
        session.open_document("pkg/service.py", deadline=time.monotonic() + 10)
        scope = resolve_repository_scope(repository)
        session.synchronize(
            compute_workspace_revision(scope, deadline=time.monotonic() + 10),
            deadline=time.monotonic() + 10,
        )
        (repository / "pkg/created.py").write_bytes(b"value = 1\n")
        (repository / "pkg/base.py").unlink()
        session.synchronize(
            compute_workspace_revision(scope, deadline=time.monotonic() + 10),
            deadline=time.monotonic() + 10,
        )
        watched = _wait_client_messages(
            semantic_pyright, "workspace/didChangeWatchedFiles"
        )
        assert len(watched) == 1
        types = sorted(
            change["type"] for change in watched[0]["params"]["changes"]
        )
        assert types == [1, 3]
    finally:
        session.close(deadline=time.monotonic() + 5)


def test_watched_file_notification_stays_on_captured_generation(
    monkeypatch: pytest.MonkeyPatch,
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,
) -> None:
    session = _session(repository, state_root, semantic_pyright)
    try:
        session.open_document("pkg/service.py", deadline=time.monotonic() + 10)
        scope = resolve_repository_scope(repository)
        prior = compute_workspace_revision(scope, deadline=time.monotonic() + 10)
        session.synchronize(prior, deadline=time.monotonic() + 10)
        process = session._process
        generation = session._generation_nonce
        assert process is not None and generation is not None
        (repository / "pkg/watched.py").write_bytes(b"watched = True\n")
        revision = compute_workspace_revision(scope, deadline=time.monotonic() + 10)
        notify = LspProcess.notify
        notify_generation = LspProcess.notify_generation
        replaced = False

        def replace_generation(deadline: float) -> None:
            nonlocal replaced
            if replaced:
                return
            replaced = True
            process.restart(deadline)
            assert process.generation_nonce != generation

        def replace_before_notify(
            current: LspProcess,
            method: str,
            params: object,
            *,
            deadline: float,
        ) -> None:
            if current is process and method == "workspace/didChangeWatchedFiles":
                replace_generation(deadline)
            notify(current, method, params, deadline=deadline)

        def replace_before_generation_notify(
            current: LspProcess,
            method: str,
            params: object,
            *,
            generation_nonce: str,
            deadline: float,
        ) -> bool:
            if current is process and method == "workspace/didChangeWatchedFiles":
                assert generation_nonce == generation
                replace_generation(deadline)
            return notify_generation(
                current,
                method,
                params,
                generation_nonce=generation_nonce,
                deadline=deadline,
            )

        monkeypatch.setattr(LspProcess, "notify", replace_before_notify)
        monkeypatch.setattr(
            LspProcess,
            "notify_generation",
            replace_before_generation_notify,
        )

        with pytest.raises(RuntimeError, match="watched-files"):
            session.synchronize(revision, deadline=time.monotonic() + 10)

        assert replaced is True
        assert process.restart_count == 1
        assert process.generation_nonce != generation
        assert _client_messages(
            semantic_pyright,
            "workspace/didChangeWatchedFiles",
        ) == ()
        assert session._workspace_revision is prior
    finally:
        session.close(deadline=time.monotonic() + 5)


def test_deleted_open_document_sends_did_close_and_clears_retention(
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,
) -> None:
    session = _session(repository, state_root, semantic_pyright)
    try:
        document = session.open_document(
            "pkg/service.py", deadline=time.monotonic() + 10
        )
        scope = resolve_repository_scope(repository)
        session.synchronize(
            compute_workspace_revision(scope, deadline=time.monotonic() + 10),
            deadline=time.monotonic() + 10,
        )
        (repository / "pkg/service.py").unlink()
        session.synchronize(
            compute_workspace_revision(scope, deadline=time.monotonic() + 10),
            deadline=time.monotonic() + 10,
        )
        closed = _wait_client_messages(semantic_pyright, "textDocument/didClose")
        assert len(closed) == 1
        assert closed[0]["params"]["textDocument"]["uri"] == document.source.uri
        assert document.source.uri not in session._documents
        assert document.source.uri not in session._diagnostics
    finally:
        session.close(deadline=time.monotonic() + 5)


def test_rename_closes_old_open_uri_and_reports_watched_events(
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,
) -> None:
    session = _session(repository, state_root, semantic_pyright)
    try:
        document = session.open_document(
            "pkg/rename_target.py", deadline=time.monotonic() + 10
        )
        scope = resolve_repository_scope(repository)
        session.synchronize(
            compute_workspace_revision(scope, deadline=time.monotonic() + 10),
            deadline=time.monotonic() + 10,
        )
        (repository / "pkg/rename_target.py").rename(
            repository / "pkg/renamed.py"
        )
        delta = session.synchronize(
            compute_workspace_revision(scope, deadline=time.monotonic() + 10),
            deadline=time.monotonic() + 10,
        )
        assert delta.renamed == (("pkg/rename_target.py", "pkg/renamed.py"),)
        closed = _wait_client_messages(semantic_pyright, "textDocument/didClose")
        assert len(closed) == 1
        assert closed[0]["params"]["textDocument"]["uri"] == document.source.uri
        watched = _wait_client_messages(
            semantic_pyright, "workspace/didChangeWatchedFiles"
        )
        assert len(watched) == 1
        uris = {
            (change["uri"].rsplit("/", 1)[-1], change["type"])
            for change in watched[0]["params"]["changes"]
        }
        assert ("renamed.py", 1) in uris
        assert ("rename_target.py", 3) in uris
        assert document.source.uri not in session._documents
    finally:
        session.close(deadline=time.monotonic() + 5)


@pytest.mark.parametrize("operation", ["delete", "rename"])
def test_synchronize_removing_readiness_target_revokes_query_ready(
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,
    operation: str,
) -> None:
    session = _session(repository, state_root, semantic_pyright)
    try:
        document = session.open_document(
            "pkg/service.py",
            deadline=time.monotonic() + 10,
        )
        assert session.readiness == "query_ready"
        scope = resolve_repository_scope(repository)
        session.synchronize(
            compute_workspace_revision(scope, deadline=time.monotonic() + 10),
            deadline=time.monotonic() + 10,
        )
        if operation == "delete":
            (repository / "pkg/service.py").unlink()
        else:
            (repository / "pkg/service.py").rename(repository / "pkg/moved.py")

        session.synchronize(
            compute_workspace_revision(scope, deadline=time.monotonic() + 10),
            deadline=time.monotonic() + 10,
        )

        assert document.source.uri not in session._documents
        assert session._readiness_target_uri is None
        assert session.readiness == "protocol_initialized"
        assert session.workspace_symbols(
            "service",
            deadline=time.monotonic() + 10,
        ).coverage == "not_ready"
    finally:
        session.close(deadline=time.monotonic() + 5)


def test_content_hash_mismatch_after_revision_fails_stale(
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,
) -> None:
    session = _session(repository, state_root, semantic_pyright)
    try:
        document = session.open_document(
            "pkg/service.py", deadline=time.monotonic() + 10
        )
        scope = resolve_repository_scope(repository)
        session.synchronize(
            compute_workspace_revision(scope, deadline=time.monotonic() + 10),
            deadline=time.monotonic() + 10,
        )
        (repository / "pkg/service.py").write_bytes(
            b"class First:\n    pass\n"
        )
        revision = compute_workspace_revision(
            scope, deadline=time.monotonic() + 10
        )
        (repository / "pkg/service.py").write_bytes(
            b"class Second:\n    pass\n"
        )
        with pytest.raises(RuntimeError, match="hash differs from the revision"):
            session.synchronize(revision, deadline=time.monotonic() + 10)
        assert _client_messages(semantic_pyright, "textDocument/didChange") == ()
        assert session._documents[document.source.uri].version == 1
    finally:
        session.close(deadline=time.monotonic() + 5)


def test_synchronize_rejects_foreign_checkout_revision(
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,
    tmp_path: Path,
) -> None:
    session = _session(repository, state_root, semantic_pyright)
    try:
        session.open_document("pkg/service.py", deadline=time.monotonic() + 10)
        foreign = replace(
            compute_workspace_revision(
                resolve_repository_scope(repository),
                deadline=time.monotonic() + 10,
            ),
            checkout_id="deadbeef",
        )
        with pytest.raises(ValueError, match="must describe this checkout"):
            session.synchronize(foreign, deadline=time.monotonic() + 10)
    finally:
        session.close(deadline=time.monotonic() + 5)


def test_synchronize_does_not_persist_semantic_results(
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,
) -> None:
    session = _session(repository, state_root, semantic_pyright)
    try:
        session.open_document("pkg/service.py", deadline=time.monotonic() + 10)
        scope = resolve_repository_scope(repository)
        session.synchronize(
            compute_workspace_revision(scope, deadline=time.monotonic() + 10),
            deadline=time.monotonic() + 10,
        )
        cache_root = state_root / "cache"
        semantic_files = (
            list(cache_root.rglob("*.json")) if cache_root.exists() else []
        )
        lsp_owners = list((state_root / "run" / "lsp").iterdir()) if (
            state_root / "run" / "lsp"
        ).exists() else []
        assert semantic_files == []
        for owner in lsp_owners:
            assert not (owner / "diagnostics.json").exists()
            assert not (owner / "results.json").exists()
    finally:
        session.close(deadline=time.monotonic() + 5)


def _patch_discovery(
    monkeypatch: pytest.MonkeyPatch,
    identities: Mapping[str, object],
) -> None:
    import pyright_profile

    def _fake_discover(repository, *, state_root, deadline=None, **kwargs):
        return identities[repository.checkout_id]

    monkeypatch.setattr(pyright_profile, "discover_pyright", _fake_discover)


def _make_repo_fixture(tmp_path: Path, name: str) -> tuple[Path, SemanticPyrightFixture]:
    repo = create_python_repository(tmp_path / name)
    fixture = create_semantic_pyright_fixture(repo)
    return repo, fixture


def test_manager_reuses_matching_live_session(
    tmp_path: Path,
    state_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, fixture = _make_repo_fixture(tmp_path, "repo")
    scope = resolve_repository_scope(repo)
    _patch_discovery(monkeypatch, {scope.checkout_id: fixture.identity})
    manager = PyrightSessionManager(state_root=state_root)
    try:
        first = manager.get(scope, deadline=time.monotonic() + 10)
        second = manager.get(scope, deadline=time.monotonic() + 10)
        assert first is second
        assert len(manager._sessions) == 1
    finally:
        manager.close_all(deadline=time.monotonic() + 5)


def test_manager_keys_by_checkout_and_profile_identity(
    tmp_path: Path,
    state_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_a, fixture_a = _make_repo_fixture(tmp_path, "a")
    repo_b, fixture_b = _make_repo_fixture(tmp_path, "b")
    scope_a = resolve_repository_scope(repo_a)
    scope_b = resolve_repository_scope(repo_b)
    _patch_discovery(
        monkeypatch,
        {
            scope_a.checkout_id: fixture_a.identity,
            scope_b.checkout_id: fixture_b.identity,
        },
    )
    manager = PyrightSessionManager(state_root=state_root)
    try:
        session_a = manager.get(scope_a, deadline=time.monotonic() + 10)
        session_b = manager.get(scope_b, deadline=time.monotonic() + 10)
        assert session_a is not session_b
        assert len(manager._sessions) == 2
    finally:
        manager.close_all(deadline=time.monotonic() + 5)


def test_manager_key_includes_complete_qualified_identity(
    tmp_path: Path,
    state_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import pyright_profile

    repo, fixture = _make_repo_fixture(tmp_path, "full-identity")
    scope = resolve_repository_scope(repo)
    first_identity = fixture.identity
    second_identity = replace(
        fixture.identity,
        executable_sha256="c" * 64,
        package_sha256="d" * 64,
    )
    discovered = iter((first_identity, second_identity))
    monkeypatch.setattr(
        pyright_profile,
        "discover_pyright",
        lambda *_args, **_kwargs: next(discovered),
    )
    manager = PyrightSessionManager(state_root=state_root)
    try:
        first = manager.get(scope, deadline=time.monotonic() + 10)
        second = manager.get(scope, deadline=time.monotonic() + 10)
        assert first is not second
        assert first._identity is first_identity
        assert second._identity is second_identity
        assert len(manager._sessions) == 2
    finally:
        manager.close_all(deadline=time.monotonic() + 5)


def test_session_idle_close_reservation_blocks_new_operations(
    repository: Path,
    state_root: Path,
) -> None:
    session = PyrightSession(
        resolve_repository_scope(repository),
        _missing_identity(),
        state_root=state_root,
    )

    with session._operation():
        assert session._reserve_idle_close(time.monotonic() + 1) is False

    assert session._reserve_idle_close(time.monotonic() + 1) is True
    with pytest.raises(RuntimeError, match="closing"):
        with session._operation():
            pytest.fail("operation started after close reservation")
    assert session.active_operations == 0
    session.close(deadline=time.monotonic() + 1)
    assert session._closed is True


def test_session_close_reserves_before_waiting_for_active_operation(
    repository: Path,
    state_root: Path,
) -> None:
    session = PyrightSession(
        resolve_repository_scope(repository),
        _missing_identity(),
        state_root=state_root,
    )
    active = threading.Event()
    release = threading.Event()
    close_errors: list[BaseException] = []

    def hold_operation() -> None:
        with session._operation():
            active.set()
            assert release.wait(2)

    def close_session() -> None:
        try:
            session.close(deadline=time.monotonic() + 2)
        except BaseException as error:
            close_errors.append(error)

    operation = threading.Thread(target=hold_operation)
    closer = threading.Thread(target=close_session)
    operation.start()
    assert active.wait(1)
    closer.start()
    try:
        deadline = time.monotonic() + 1
        while not session._closing and time.monotonic() < deadline:
            time.sleep(0.001)
        assert session._closing is True
        assert closer.is_alive()
        with pytest.raises(RuntimeError, match="closing"):
            with session._operation():
                pytest.fail("operation started after close reservation")
    finally:
        release.set()
        operation.join(2)
        closer.join(2)

    assert not operation.is_alive()
    assert not closer.is_alive()
    assert close_errors == []
    assert session._closed is True


def test_failed_real_session_close_keeps_reservation_until_close_all_retry(
    monkeypatch: pytest.MonkeyPatch,
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,
) -> None:
    import pyright_profile

    scope = resolve_repository_scope(repository)
    monkeypatch.setattr(
        pyright_profile,
        "discover_pyright",
        lambda *_args, **_kwargs: semantic_pyright.identity,
    )
    manager = PyrightSessionManager(state_root=state_root)
    session = manager.get(scope, deadline=time.monotonic() + 10)
    session.start(deadline=time.monotonic() + 10)
    process = session._process
    assert process is not None
    generation = process._coordinator.active
    assert generation is not None and generation.tree is not None
    tree = generation.tree
    terminate = lsp_process.ProcessTree.terminate
    tree_fault = True

    def fail_tree(current: object, *, deadline: float) -> None:
        if current is tree and tree_fault:
            raise OSError("retained session tree close failed")
        terminate(current, deadline=deadline)  # type: ignore[arg-type]

    monkeypatch.setattr(lsp_process.ProcessTree, "terminate", fail_tree)
    try:
        assert session._reserve_idle_close(time.monotonic() + 1) is True
        with pytest.raises(OSError, match="retained session tree close failed"):
            session.close(deadline=time.monotonic() + 3)

        assert session._closing is True
        assert session._closed is False
        with pytest.raises(RuntimeError, match="closing"):
            with session._operation():
                pytest.fail("operation started after retained close failure")

        tree_fault = False
        manager.close_all(deadline=time.monotonic() + 5)
        assert session._closed is True
        assert manager._sessions == {}
    finally:
        tree_fault = False
        if manager._sessions:
            manager.close_all(deadline=time.monotonic() + 5)


def test_session_close_state_lock_wait_obeys_absolute_deadline(
    repository: Path,
    state_root: Path,
) -> None:
    session = PyrightSession(
        resolve_repository_scope(repository),
        _missing_identity(),
        state_root=state_root,
    )
    held = threading.Event()

    def hold_state_lock() -> None:
        session._lock.acquire()
        try:
            held.set()
            time.sleep(0.25)
        finally:
            session._lock.release()

    holder = threading.Thread(target=hold_state_lock)
    holder.start()
    assert held.wait(1)
    started = time.monotonic()
    try:
        with pytest.raises(TimeoutError, match="state lock"):
            session.close(deadline=started + 0.05)
        assert time.monotonic() - started < 0.2
        assert session._closed is False
    finally:
        holder.join(1)
        session.close(deadline=time.monotonic() + 1)
    assert not holder.is_alive()
    assert session._closed is True


def test_manager_evicts_lru_idle_and_never_exceeds_four(
    tmp_path: Path,
    state_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repos = []
    identities: dict[str, object] = {}
    scopes = []
    for index in range(5):
        repo, fixture = _make_repo_fixture(tmp_path, f"r{index}")
        scope = resolve_repository_scope(repo)
        scopes.append(scope)
        identities[scope.checkout_id] = fixture.identity
        repos.append(repo)
    _patch_discovery(monkeypatch, identities)
    manager = PyrightSessionManager(state_root=state_root)
    try:
        first = manager.get(scopes[0], deadline=time.monotonic() + 10)
        for index in range(1, 5):
            manager.get(scopes[index], deadline=time.monotonic() + 10)
        assert len(manager._sessions) == 4
        assert first._closed
        fifth = manager.get(scopes[0], deadline=time.monotonic() + 10)
        assert fifth is not first
        assert len(manager._sessions) == 4
    finally:
        manager.close_all(deadline=time.monotonic() + 5)


def test_manager_failed_eviction_retains_slot_and_closes_outside_global_lock(
    tmp_path: Path,
    state_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scopes: list[RepositoryScope] = []
    identities: dict[str, object] = {}
    for index in range(5):
        repo, fixture = _make_repo_fixture(tmp_path, f"eviction-{index}")
        scope = resolve_repository_scope(repo)
        scopes.append(scope)
        identities[scope.checkout_id] = fixture.identity
    _patch_discovery(monkeypatch, identities)
    manager = PyrightSessionManager(state_root=state_root)
    retained = [
        manager.get(scope, deadline=time.monotonic() + 10)
        for scope in scopes[:4]
    ]
    for index, session in enumerate(retained):
        with session._lock:
            session._last_used_monotonic = float(index)
    lock_was_available = threading.Event()

    def fail_close(*, deadline: float) -> None:
        def probe_manager_lock() -> None:
            remaining = deadline - time.monotonic()
            if remaining > 0 and manager._lock.acquire(timeout=remaining):
                lock_was_available.set()
                manager._lock.release()

        probe = threading.Thread(target=probe_manager_lock)
        probe.start()
        probe.join(max(0.0, deadline - time.monotonic()))
        raise OSError("injected close failure")

    try:
        with monkeypatch.context() as patch:
            patch.setattr(retained[0], "close", fail_close)
            with pytest.raises(OSError, match="injected close failure"):
                manager.get(scopes[4], deadline=time.monotonic() + 2)

        assert lock_was_available.is_set()
        assert len(manager._sessions) == 4
        assert retained[0] in manager._sessions.values()
        assert retained[0]._closed is False
        assert all(
            session._repository.checkout_id != scopes[4].checkout_id
            for session in manager._sessions.values()
        )
    finally:
        manager.close_all(deadline=time.monotonic() + 5)


def test_manager_returns_not_ready_when_all_four_active(
    tmp_path: Path,
    state_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repos = []
    identities: dict[str, object] = {}
    scopes = []
    for index in range(5):
        repo, fixture = _make_repo_fixture(tmp_path, f"active{index}")
        scope = resolve_repository_scope(repo)
        scopes.append(scope)
        identities[scope.checkout_id] = fixture.identity
        repos.append(repo)
    _patch_discovery(monkeypatch, identities)
    manager = PyrightSessionManager(state_root=state_root)
    sessions: list[PyrightSession] = []
    try:
        for index in range(4):
            session = manager.get(scopes[index], deadline=time.monotonic() + 10)
            sessions.append(session)
            session._active_operations = 1
        denied = manager.get(scopes[4], deadline=time.monotonic() + 10)
        assert denied._capacity_locked is True
        assert denied.start(deadline=time.monotonic() + 2) is None
        assert denied.readiness == "not_ready"
        assert "pyright_capacity_exhausted" in denied.degradation_codes
        assert len(manager._sessions) == 4
    finally:
        for session in sessions:
            with session._lock:
                session._active_operations = 0
                session._condition.notify_all()
        manager.close_all(deadline=time.monotonic() + 5)


def test_manager_close_all_closes_every_retained_session(
    tmp_path: Path,
    state_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repos = []
    identities: dict[str, object] = {}
    scopes = []
    for index in range(2):
        repo, fixture = _make_repo_fixture(tmp_path, f"close{index}")
        scope = resolve_repository_scope(repo)
        scopes.append(scope)
        identities[scope.checkout_id] = fixture.identity
        repos.append(repo)
    _patch_discovery(monkeypatch, identities)
    manager = PyrightSessionManager(state_root=state_root)
    retained = [
        manager.get(scopes[0], deadline=time.monotonic() + 10),
        manager.get(scopes[1], deadline=time.monotonic() + 10),
    ]
    manager.close_all(deadline=time.monotonic() + 5)
    assert all(session._closed for session in retained)
    assert manager._sessions == {}


def test_manager_close_all_retains_failures_continues_and_rethrows_first_interrupt(
    tmp_path: Path,
    state_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scopes: list[RepositoryScope] = []
    identities: dict[str, object] = {}
    for index in range(4):
        repo, fixture = _make_repo_fixture(tmp_path, f"interrupt-{index}")
        scope = resolve_repository_scope(repo)
        scopes.append(scope)
        identities[scope.checkout_id] = fixture.identity
    _patch_discovery(monkeypatch, identities)
    manager = PyrightSessionManager(state_root=state_root)
    sessions = [
        manager.get(scope, deadline=time.monotonic() + 10)
        for scope in scopes
    ]
    attempts: list[int] = []
    real_close = [session.close for session in sessions]

    def flaky_close(index: int, *, deadline: float) -> None:
        attempts.append(index)
        if attempts.count(index) == 1:
            if index == 0:
                raise OSError("first close failed")
            if index == 1:
                raise KeyboardInterrupt("first interruption")
            if index == 2:
                raise SystemExit(29)
        real_close[index](deadline=deadline)

    with monkeypatch.context() as patch:
        for index, session in enumerate(sessions):
            patch.setattr(
                session,
                "close",
                lambda *, deadline, index=index: flaky_close(
                    index,
                    deadline=deadline,
                ),
            )

        with pytest.raises(KeyboardInterrupt, match="first interruption"):
            manager.close_all(deadline=time.monotonic() + 5)

        assert attempts == [0, 1, 2, 3]
        assert set(manager._sessions.values()) == set(sessions[:3])
        assert sessions[3]._closed is True

        manager.close_all(deadline=time.monotonic() + 5)

    assert attempts == [0, 1, 2, 3, 0, 1, 2]
    assert manager._sessions == {}
    assert all(session._closed for session in sessions)


def test_manager_close_all_unwraps_earliest_interruption_without_cycle(
    tmp_path: Path,
    state_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scopes: list[RepositoryScope] = []
    identities: dict[str, object] = {}
    for index in range(3):
        repo, fixture = _make_repo_fixture(tmp_path, f"wrapped-interrupt-{index}")
        scope = resolve_repository_scope(repo)
        scopes.append(scope)
        identities[scope.checkout_id] = fixture.identity
    _patch_discovery(monkeypatch, identities)
    manager = PyrightSessionManager(state_root=state_root)
    sessions = [
        manager.get(scope, deadline=time.monotonic() + 10)
        for scope in scopes
    ]
    real_close = [session.close for session in sessions]
    attempts: list[int] = []
    ordinary_error = OSError("later ordinary manager close error")
    interruption = SystemExit(53)
    wrapper = RuntimeError("manager close interruption wrapper")
    wrapper.__cause__ = interruption

    def flaky_close(index: int, *, deadline: float) -> None:
        attempts.append(index)
        if attempts.count(index) == 1:
            if index == 0:
                raise wrapper
            if index == 1:
                raise ordinary_error
        real_close[index](deadline=deadline)

    with monkeypatch.context() as patch:
        for index, session in enumerate(sessions):
            patch.setattr(
                session,
                "close",
                lambda *, deadline, index=index: flaky_close(
                    index,
                    deadline=deadline,
                ),
            )

        with pytest.raises(SystemExit) as raised:
            manager.close_all(deadline=time.monotonic() + 5)

        assert raised.value is interruption
        assert raised.value.__cause__ is ordinary_error
        assert raised.value.__context__ is None
        assert wrapper.__cause__ is interruption
        assert attempts == [0, 1, 2]
        assert set(manager._sessions.values()) == set(sessions[:2])

        manager.close_all(deadline=time.monotonic() + 5)

    assert attempts == [0, 1, 2, 0, 1]
    assert manager._sessions == {}
    assert all(session._closed for session in sessions)


@pytest.mark.parametrize("operation", ["get", "close_all"])
def test_manager_global_lock_wait_obeys_absolute_deadline(
    tmp_path: Path,
    state_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    repo, fixture = _make_repo_fixture(tmp_path, f"deadline-{operation}")
    scope = resolve_repository_scope(repo)
    _patch_discovery(monkeypatch, {scope.checkout_id: fixture.identity})
    manager = PyrightSessionManager(state_root=state_root)
    held = threading.Event()
    release = threading.Event()

    def hold_manager_lock() -> None:
        manager._lock.acquire()
        try:
            held.set()
            assert release.wait(2)
        finally:
            manager._lock.release()

    holder = threading.Thread(target=hold_manager_lock)
    holder.start()
    assert held.wait(1)
    started = time.monotonic()
    try:
        with pytest.raises(TimeoutError, match="manager lock"):
            deadline = started + 0.05
            if operation == "get":
                manager.get(scope, deadline=deadline)
            else:
                manager.close_all(deadline=deadline)
        assert time.monotonic() - started < 0.3
    finally:
        release.set()
        holder.join(2)
        manager.close_all(deadline=time.monotonic() + 2)
    assert not holder.is_alive()


def test_manager_get_rechecks_closed_after_per_key_wait(
    tmp_path: Path,
    state_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import pyright_profile

    repo, fixture = _make_repo_fixture(tmp_path, "key-wait-close")
    scope = resolve_repository_scope(repo)
    discovered = threading.Event()

    def discover(*_args: object, **_kwargs: object) -> PyrightIdentity:
        discovered.set()
        return fixture.identity  # type: ignore[return-value]

    monkeypatch.setattr(pyright_profile, "discover_pyright", discover)
    manager = PyrightSessionManager(state_root=state_root)
    key = manager._profile_key(scope, fixture.identity)  # type: ignore[arg-type]
    with manager._lock:
        key_lock_state = manager._retain_key_lock_locked(
            key,
            time.monotonic() + 1,
        )
    key_lock_state.lock.acquire()
    errors: list[BaseException] = []
    close_errors: list[BaseException] = []
    close_done = threading.Event()

    def get_waiting() -> None:
        try:
            manager.get(scope, deadline=time.monotonic() + 2)
        except BaseException as error:
            errors.append(error)

    waiter = threading.Thread(target=get_waiting)
    waiter.start()
    assert discovered.wait(1)
    try:
        def close_manager() -> None:
            try:
                manager.close_all(deadline=time.monotonic() + 2)
            except BaseException as error:
                close_errors.append(error)
            finally:
                close_done.set()

        closer = threading.Thread(target=close_manager)
        closer.start()
        closed_deadline = time.monotonic() + 1
        while True:
            with manager._lock:
                if manager._closed:
                    break
            if time.monotonic() >= closed_deadline:
                pytest.fail("close_all did not close manager before key waiter release")
            time.sleep(0.01)
        assert close_done.wait(0.05) is False
    finally:
        key_lock_state.lock.release()
        manager._release_key_lock_reference(key, key_lock_state)
        waiter.join(2)
        if closer.ident is not None:
            closer.join(2)

    assert not waiter.is_alive()
    assert not closer.is_alive()
    assert close_errors == []
    assert len(errors) == 1
    assert isinstance(errors[0], RuntimeError)
    assert "manager is closed" in str(errors[0])
    assert manager._sessions == {}


def test_manager_per_key_lock_wait_obeys_absolute_deadline(
    tmp_path: Path,
    state_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, fixture = _make_repo_fixture(tmp_path, "key-deadline")
    scope = resolve_repository_scope(repo)
    _patch_discovery(monkeypatch, {scope.checkout_id: fixture.identity})
    manager = PyrightSessionManager(state_root=state_root)
    key = manager._profile_key(scope, fixture.identity)  # type: ignore[arg-type]
    with manager._lock:
        key_lock_state = manager._retain_key_lock_locked(
            key,
            time.monotonic() + 1,
        )
    key_lock_state.lock.acquire()
    started = time.monotonic()
    try:
        with pytest.raises(TimeoutError, match="key lock"):
            manager.get(scope, deadline=started + 0.05)
        assert time.monotonic() - started < 0.3
        assert manager._sessions == {}
    finally:
        key_lock_state.lock.release()
        manager._release_key_lock_reference(key, key_lock_state)
        manager.close_all(deadline=time.monotonic() + 2)


def test_manager_close_all_waits_for_get_retained_before_per_key_lock(
    tmp_path: Path,
    state_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, fixture = _make_repo_fixture(tmp_path, "key-retained-before-lock")
    scope = resolve_repository_scope(repo)
    _patch_discovery(monkeypatch, {scope.checkout_id: fixture.identity})
    manager = PyrightSessionManager(state_root=state_root)
    acquire_key_lock = manager._acquire_key_lock
    reference_retained = threading.Event()
    release_get = threading.Event()
    get_done = threading.Event()
    close_done = threading.Event()
    get_errors: list[BaseException] = []
    close_errors: list[BaseException] = []

    def pause_before_key_lock(lock: threading.Lock, deadline: float) -> None:
        reference_retained.set()
        if not release_get.wait(max(0.0, deadline - time.monotonic())):
            raise TimeoutError("retained key reference release expired")
        acquire_key_lock(lock, deadline)

    def get_session() -> None:
        try:
            manager.get(scope, deadline=time.monotonic() + 5)
        except BaseException as error:
            get_errors.append(error)
        finally:
            get_done.set()

    def close_manager() -> None:
        try:
            manager.close_all(deadline=time.monotonic() + 5)
        except BaseException as error:
            close_errors.append(error)
        finally:
            close_done.set()

    monkeypatch.setattr(manager, "_acquire_key_lock", pause_before_key_lock)
    getter = threading.Thread(target=get_session, name="retained-key-getter")
    closer = threading.Thread(target=close_manager, name="retained-key-closer")
    try:
        getter.start()
        assert reference_retained.wait(1)
        with manager._lock:
            assert len(manager._key_locks) == 1
            assert next(iter(manager._key_locks.values())).references == 1

        closer.start()
        assert close_done.wait(0.2) is False
        release_get.set()
        assert get_done.wait(2)
        assert close_done.wait(2)
    finally:
        release_get.set()
        getter.join(3)
        if closer.ident is not None:
            closer.join(3)
        with manager._lock:
            manager._prune_key_locks_locked()

    assert not getter.is_alive()
    assert not closer.is_alive()
    assert len(get_errors) == 1
    assert isinstance(get_errors[0], RuntimeError)
    assert "manager is closed" in str(get_errors[0])
    assert close_errors == []
    assert manager._sessions == {}
    assert manager._key_locks == {}
    assert manager._key_lock_releases.empty()


def test_manager_close_all_deadline_retains_key_reference_for_retry(
    tmp_path: Path,
    state_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, fixture = _make_repo_fixture(tmp_path, "key-close-race")
    scope = resolve_repository_scope(repo)
    _patch_discovery(monkeypatch, {scope.checkout_id: fixture.identity})
    manager = PyrightSessionManager(state_root=state_root)
    acquire_key_lock = manager._acquire_key_lock
    key_acquired = threading.Event()
    release_get = threading.Event()
    get_done = threading.Event()
    close_done = threading.Event()
    get_errors: list[BaseException] = []
    close_errors: list[BaseException] = []

    def pause_after_key_acquire(lock: threading.Lock, deadline: float) -> None:
        acquire_key_lock(lock, deadline)
        key_acquired.set()
        if not release_get.wait(max(0.0, deadline - time.monotonic())):
            raise TimeoutError("key close-race release expired")

    def get_session() -> None:
        try:
            manager.get(scope, deadline=time.monotonic() + 5)
        except BaseException as error:
            get_errors.append(error)
        finally:
            get_done.set()

    def close_manager() -> None:
        try:
            manager.close_all(deadline=time.monotonic() + 0.1)
        except BaseException as error:
            close_errors.append(error)
        finally:
            close_done.set()

    monkeypatch.setattr(manager, "_acquire_key_lock", pause_after_key_acquire)
    getter = threading.Thread(target=get_session)
    closer = threading.Thread(target=close_manager)
    reference_lock: threading.Lock | None = None
    try:
        getter.start()
        assert key_acquired.wait(1)
        with manager._lock:
            assert len(manager._key_locks) == 1
            state = next(iter(manager._key_locks.values()))
        reference_lock = state.reference_lock
        reference_lock.acquire()

        closer.start()
        assert close_done.wait(1)
        assert len(close_errors) == 1
        assert isinstance(close_errors[0], TimeoutError)
        with manager._lock:
            assert manager._key_locks
            assert state.references == 1
        release_get.set()

        assert get_done.wait(0.2), "key reference release ignored its closed manager"
        assert len(get_errors) == 1
        assert isinstance(get_errors[0], RuntimeError)
        assert "manager is closed" in str(get_errors[0])
    finally:
        release_get.set()
        if reference_lock is not None and reference_lock.locked():
            reference_lock.release()
        getter.join(2)
        if closer.ident is not None:
            closer.join(2)
        manager.close_all(deadline=time.monotonic() + 2)
        with manager._lock:
            manager._prune_key_locks_locked()

    assert not getter.is_alive()
    assert not closer.is_alive()
    assert manager._key_locks == {}
    assert manager._key_lock_releases.empty()


def test_manager_reference_gate_deadline_stays_bounded_across_sequential_waiters(
    tmp_path: Path,
    state_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, fixture = _make_repo_fixture(tmp_path, "key-reference-deadline")
    scope = resolve_repository_scope(repo)
    _patch_discovery(monkeypatch, {scope.checkout_id: fixture.identity})
    manager = PyrightSessionManager(state_root=state_root)
    acquire_key_lock = manager._acquire_key_lock
    keeper_entered = threading.Event()
    release_keeper = threading.Event()
    keeper_errors: list[BaseException] = []
    keeper_results: list[PyrightSession] = []

    def hold_keeper_before_key_acquire(
        lock: threading.Lock,
        deadline: float,
    ) -> None:
        if threading.current_thread().name == "key-reference-keeper":
            keeper_entered.set()
            if not release_keeper.wait(max(0.0, deadline - time.monotonic())):
                raise TimeoutError("reference keeper release expired")
        acquire_key_lock(lock, deadline)

    def keep_reference() -> None:
        try:
            keeper_results.append(
                manager.get(scope, deadline=time.monotonic() + 10)
            )
        except BaseException as error:
            keeper_errors.append(error)

    monkeypatch.setattr(manager, "_acquire_key_lock", hold_keeper_before_key_acquire)
    keeper = threading.Thread(target=keep_reference, name="key-reference-keeper")
    reference_lock: threading.Lock | None = None
    try:
        keeper.start()
        assert keeper_entered.wait(1)
        with manager._lock:
            assert len(manager._key_locks) == 1
            state = next(iter(manager._key_locks.values()))
        reference_lock = state.reference_lock
        reference_lock.acquire()

        for index in range(100):
            errors: list[BaseException] = []
            done = threading.Event()

            def contend() -> None:
                try:
                    manager.get(scope, deadline=time.monotonic() + 0.02)
                except BaseException as error:
                    errors.append(error)
                finally:
                    done.set()

            contender = threading.Thread(
                target=contend,
                name=f"key-reference-contender-{index}",
            )
            contender.start()
            assert done.wait(0.5), f"reference contender {index} exceeded its deadline"
            contender.join(1)
            assert not contender.is_alive()
            assert len(errors) == 1
            assert isinstance(errors[0], TimeoutError)
            assert "reference" in str(errors[0])
    finally:
        if reference_lock is not None and reference_lock.locked():
            reference_lock.release()
        release_keeper.set()
        keeper.join(3)
        manager.close_all(deadline=time.monotonic() + 3)

    assert not keeper.is_alive()
    assert keeper_errors == []
    assert len(keeper_results) == 1
    assert manager._key_locks == {}


def test_manager_key_lock_lives_through_waiters_and_releases_after_last_get(
    tmp_path: Path,
    state_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, fixture = _make_repo_fixture(tmp_path, "key-lifecycle")
    scope = resolve_repository_scope(repo)
    _patch_discovery(monkeypatch, {scope.checkout_id: fixture.identity})
    manager = PyrightSessionManager(state_root=state_root)
    acquire_key_lock = manager._acquire_key_lock
    calls_lock = threading.Lock()
    acquire_calls = 0
    first_acquired = threading.Event()
    release_first = threading.Event()
    second_waiting = threading.Event()
    second_acquired = threading.Event()
    release_second = threading.Event()
    third_waiting = threading.Event()
    third_done = threading.Event()
    results: list[PyrightSession] = []
    errors: list[BaseException] = []

    def controlled_acquire(lock: threading.Lock, deadline: float) -> None:
        nonlocal acquire_calls
        with calls_lock:
            acquire_calls += 1
            call = acquire_calls
        if call == 2:
            second_waiting.set()
        elif call == 3:
            third_waiting.set()
        acquire_key_lock(lock, deadline)
        if call == 1:
            first_acquired.set()
            assert release_first.wait(3)
        elif call == 2:
            second_acquired.set()
            assert release_second.wait(3)

    def get_session(*, third: bool = False) -> None:
        try:
            results.append(manager.get(scope, deadline=time.monotonic() + 5))
        except BaseException as error:
            errors.append(error)
        finally:
            if third:
                third_done.set()

    monkeypatch.setattr(manager, "_acquire_key_lock", controlled_acquire)
    first = threading.Thread(target=get_session)
    second = threading.Thread(target=get_session)
    third = threading.Thread(target=get_session, kwargs={"third": True})
    try:
        first.start()
        assert first_acquired.wait(1)
        second.start()
        assert second_waiting.wait(1)
        release_first.set()
        first.join(2)
        assert not first.is_alive()
        assert second_acquired.wait(1)

        third.start()
        assert third_waiting.wait(1)
        assert third_done.wait(0.05) is False
        release_second.set()
        second.join(2)
        third.join(2)

        assert not second.is_alive()
        assert not third.is_alive()
        assert errors == []
        assert len(results) == 3
        assert results[0] is results[1] is results[2]
        assert manager._key_locks == {}
    finally:
        release_first.set()
        release_second.set()
        for worker in (first, second, third):
            if worker.ident is not None:
                worker.join(2)
        manager.close_all(deadline=time.monotonic() + 2)

    assert manager._key_locks == {}


def test_manager_key_locks_do_not_accumulate_across_profile_evictions(
    tmp_path: Path,
    state_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import pyright_profile

    repo, fixture = _make_repo_fixture(tmp_path, "key-churn")
    scope = resolve_repository_scope(repo)
    identities = iter(
        replace(fixture.identity, executable_sha256=f"{index:064x}")
        for index in range(12)
    )
    monkeypatch.setattr(
        pyright_profile,
        "discover_pyright",
        lambda *_args, **_kwargs: next(identities),
    )
    manager = PyrightSessionManager(state_root=state_root)
    try:
        for _index in range(12):
            manager.get(scope, deadline=time.monotonic() + 5)
            assert manager._key_locks == {}
            assert len(manager._sessions) <= MAX_LSP_PROCESSES
    finally:
        manager.close_all(deadline=time.monotonic() + 2)

    assert manager._key_locks == {}


def test_manager_parallel_evictions_reserve_distinct_idle_sessions_with_capacity_four(
    tmp_path: Path,
    state_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scopes: list[RepositoryScope] = []
    identities: dict[str, object] = {}
    for index in range(6):
        repo, fixture = _make_repo_fixture(tmp_path, f"parallel-{index}")
        scope = resolve_repository_scope(repo)
        scopes.append(scope)
        identities[scope.checkout_id] = fixture.identity
    _patch_discovery(monkeypatch, identities)
    manager = PyrightSessionManager(state_root=state_root)
    retained = [
        manager.get(scope, deadline=time.monotonic() + 10)
        for scope in scopes[:4]
    ]
    for index, session in enumerate(retained):
        with session._lock:
            session._last_used_monotonic = float(index)
    close_entered = [threading.Event(), threading.Event()]
    release_close = threading.Event()
    real_close = [retained[index].close for index in range(2)]

    def blocked_close(index: int, *, deadline: float) -> None:
        close_entered[index].set()
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not release_close.wait(remaining):
            raise TimeoutError("parallel eviction release expired")
        real_close[index](deadline=deadline)

    results: list[PyrightSession] = []
    errors: list[BaseException] = []

    def get_new(scope: RepositoryScope) -> None:
        try:
            results.append(manager.get(scope, deadline=time.monotonic() + 5))
        except BaseException as error:
            errors.append(error)

    try:
        with monkeypatch.context() as patch:
            for index in range(2):
                patch.setattr(
                    retained[index],
                    "close",
                    lambda *, deadline, index=index: blocked_close(
                        index,
                        deadline=deadline,
                    ),
                )
            getters = [
                threading.Thread(target=get_new, args=(scope,))
                for scope in scopes[4:]
            ]
            for getter in getters:
                getter.start()
            assert all(event.wait(2) for event in close_entered)
            with manager._lock:
                assert len(manager._sessions) == MAX_LSP_PROCESSES
            assert retained[0]._closing is True
            assert retained[1]._closing is True
            for session in retained[:2]:
                with pytest.raises(RuntimeError, match="closing"):
                    with session._operation():
                        pytest.fail("operation started after eviction reservation")
            release_close.set()
            for getter in getters:
                getter.join(5)
                assert not getter.is_alive()

        assert errors == []
        assert len(results) == 2
        assert len(manager._sessions) == MAX_LSP_PROCESSES
        assert all(session in manager._sessions.values() for session in results)
        assert retained[0]._closed is True
        assert retained[1]._closed is True
    finally:
        release_close.set()
        manager.close_all(deadline=time.monotonic() + 5)


def test_manager_registers_atexit_once(
    tmp_path: Path,
    state_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, fixture = _make_repo_fixture(tmp_path, "atexit")
    scope = resolve_repository_scope(repo)
    _patch_discovery(monkeypatch, {scope.checkout_id: fixture.identity})
    manager = PyrightSessionManager(state_root=state_root)
    assert manager._atexit_registered is False
    manager.get(scope, deadline=time.monotonic() + 10)
    assert manager._atexit_registered is True
    manager.get(scope, deadline=time.monotonic() + 10)
    assert manager._atexit_registered is True
    manager.close_all(deadline=time.monotonic() + 5)
