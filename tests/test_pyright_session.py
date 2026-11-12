"""Public contract, qualified identity and verified launch of the pinned Pyright session."""

from __future__ import annotations

import hashlib
import inspect
import json
import os
import shutil
import time
from dataclasses import FrozenInstanceError, fields
from pathlib import Path
from types import MappingProxyType
from typing import get_type_hints

import lsp_process
import pytest
from llm_wiki import (
    pyright_session_launch,
)
from llm_wiki.pyright_session_core import LanguageServerSession, PyrightSession
from llm_wiki.pyright_session_launch import LaunchServerGuard
from llm_wiki.pyright_session_manager import MAX_LSP_PROCESSES
from llm_wiki.pyright_session_startup import STARTUP_SECONDS
from llm_wiki.pyright_session_values import (
    LspDiagnostic,
    LspLocation,
    OpenDocument,
    ProviderCalls,
    ProviderDiagnostics,
    ProviderHover,
    ProviderLocations,
)
from lsp_positions import LspPosition, LspRange
from lsp_process import LspProcess, ProcessState
from lsp_profiles import PYRIGHT_PROFILE
from lsp_security import RepositorySource
from lsp_server_profile import LanguageServerProfile
from pyright_profile import (
    PyrightIdentity,
)
from repository_scope import RepositoryScope, resolve_repository_scope

from tests.code_kernel_helpers import (
    SemanticPyrightFixture,
)
from tests.pyright_session_helpers import (
    _copied_server_identity,
    _missing_identity,
    _session,
    semantic_pyright,  # noqa: F401
)
from tests.slow_machine import SHORT_TIMEOUT


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
            # CODE-08: the session drives whichever managed server the profile
            # names. It defaults to Pyright, so every existing caller and every
            # assertion below is unchanged by its presence.
            inspect.Parameter(
                "profile",
                inspect.Parameter.KEYWORD_ONLY,
                annotation=LanguageServerProfile,
                default=PYRIGHT_PROFILE,
            ),
        ),
        return_annotation=None,
    )
    assert PyrightSession is LanguageServerSession

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
    session.close(deadline=time.monotonic() + SHORT_TIMEOUT)

    assert get_type_hints(PyrightSession.__init__) == {
        "repository": RepositoryScope,
        "identity": PyrightIdentity,
        "state_root": Path,
        "profile": LanguageServerProfile,
        "return": type(None),
    }


def test_session_exposes_immutable_qualified_identity(
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,  # noqa: F811
) -> None:
    session = _session(repository, state_root, semantic_pyright)
    try:
        assert session.identity is semantic_pyright.identity
        with pytest.raises(AttributeError):
            session.identity = _missing_identity()  # type: ignore[misc]
    finally:
        session.close(deadline=time.monotonic() + SHORT_TIMEOUT)


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

    assert session.start(deadline=time.monotonic() + SHORT_TIMEOUT) is None
    assert session.readiness == "not_ready"
    assert session.readiness_evidence == ()
    assert session.degradation_codes == ("pyright_missing",)
    assert not (state_root / "run/lsp").exists()


def test_server_mutated_after_qualification_never_spawns(
    monkeypatch: pytest.MonkeyPatch,
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,  # noqa: F811
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

    session.start(deadline=time.monotonic() + SHORT_TIMEOUT)

    assert session.readiness == "not_ready"
    assert session.readiness_evidence == ()
    assert session.degradation_codes == ("pyright_executable_digest_mismatch",)


@pytest.mark.skipif(os.name != "posix", reason="POSIX verified launch descriptor")
def test_server_replaced_at_spawn_never_executes_replacement(
    monkeypatch: pytest.MonkeyPatch,
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,  # noqa: F811
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
        session.start(deadline=time.monotonic() + SHORT_TIMEOUT)

        assert replaced is True
        assert not replacement_marker.exists()
        assert session.readiness == "not_ready"
        assert session.degradation_codes == (
            "pyright_executable_digest_mismatch",
        )
    finally:
        session.close(deadline=time.monotonic() + SHORT_TIMEOUT)


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
    guard = LaunchServerGuard(
        server,
        hashlib.sha256(server.read_bytes()).hexdigest(),
        command=(str(node), str(server), str(output)),
        owner_root=owner,
        deadline=time.monotonic() + SHORT_TIMEOUT,
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
            deadline=time.monotonic() + SHORT_TIMEOUT,
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
    real_mkstemp = pyright_session_launch.tempfile.mkstemp

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

    monkeypatch.setattr(pyright_session_launch, "os", PosixOsProxy())
    monkeypatch.setattr(pyright_session_launch.tempfile, "mkstemp", record_mkstemp)
    monkeypatch.setattr(
        LaunchServerGuard,
        "_posix_launch_command",
        lambda _self, descriptor: ("server", str(descriptor)),
    )

    guard = LaunchServerGuard(
        server,
        hashlib.sha256(server.read_bytes()).hexdigest(),
        command=("python", str(server)),
        owner_root=owner,
        deadline=time.monotonic() + SHORT_TIMEOUT,
    )
    with guard as launch:
        assert isinstance(launch, lsp_process.GenerationLaunch)
        assert snapshots[-1].parent == owner
        assert unlinked[-1] == snapshots[-1]
    snapshots[-1].unlink()
    assert list(owner.iterdir()) == []

    failing = LaunchServerGuard(
        server,
        hashlib.sha256(server.read_bytes()).hexdigest(),
        command=("python", str(server)),
        owner_root=owner,
        deadline=time.monotonic() + SHORT_TIMEOUT,
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
    guard = LaunchServerGuard(
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
    monkeypatch.setattr(pyright_session_launch.os, "unlink", interrupt_unlink)

    with pytest.raises(
        KeyboardInterrupt,
        match="later launch guard close interruption",
    ) as raised:
        guard.close()

    assert raised.value.__cause__ is ordinary_error


def test_launch_guard_body_interruption_outranks_close_error(tmp_path: Path) -> None:
    server = tmp_path / "server.py"
    server.write_bytes(b"print('server')\n")

    class EnteredGuard(LaunchServerGuard):
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
    semantic_pyright: SemanticPyrightFixture,  # noqa: F811
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
        session.start(deadline=time.monotonic() + SHORT_TIMEOUT)
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
        session.close(deadline=time.monotonic() + SHORT_TIMEOUT)
