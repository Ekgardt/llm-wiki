"""Pinned, read-only Pyright runtime discovery contracts."""

from __future__ import annotations

import builtins
import dataclasses
import io
import os
import signal
import socket
import stat
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import lsp_paths
import lsp_process_tree
import pyright_profile
import pytest
from pyright_profile import (
    PYRIGHT_CONFIGURATION,
    PYRIGHT_INITIALIZATION_OPTIONS,
    PYRIGHT_PACKAGE_INTEGRITY,
    PYRIGHT_PACKAGE_SHA256,
    PYRIGHT_PACKAGE_URL,
    PYRIGHT_SERVER_RELATIVE,
    PYRIGHT_VERSION,
    QUALIFIED_NODE_MAJOR,
    PyrightCandidates,
    PyrightIdentity,
    build_pyright_install_manifest,
    discover_pyright,
    thaw_pyright_profile_value,
    validate_pyright_install_manifest,
)
from reliable_memory import canonical_json_bytes, sha256_bytes
from repository_scope import resolve_repository_scope

from tests.code_kernel_helpers import create_pyright_fixture


class _FakeNodeProcess:
    def __init__(
        self,
        output: bytes,
        *,
        returncode: int = 0,
        times_out: bool = False,
        persistent_timeout: bool = False,
    ) -> None:
        self.stdout = io.BytesIO(output)
        self.stdin = io.BytesIO()
        self.stderr = io.BytesIO()
        self.returncode: int | None = None
        self._final_returncode = returncode
        self._times_out = times_out
        self._persistent_timeout = persistent_timeout
        self.killed = False
        self.kill_calls = 0
        self.wait_timeouts: list[float] = []
        self.tree: _FakeNodeTree | None = None

    def wait(self, timeout: float | None = None) -> int:
        if timeout is not None:
            self.wait_timeouts.append(timeout)
        if self._times_out and (self._persistent_timeout or not self.killed):
            raise subprocess.TimeoutExpired(("node", "--version"), timeout)
        self.returncode = -9 if self.killed else self._final_returncode
        return self.returncode

    def kill(self) -> None:
        self.kill_calls += 1
        self.killed = True


class _FakeNodeTree:
    def __init__(self, process: _FakeNodeProcess) -> None:
        self.process = process
        self.descendants_alive = False
        self.terminate_deadlines: list[float] = []
        self.close_calls = 0
        self.closed = False

    def has_live_descendants(self) -> bool:
        if self.process.returncode is None:
            raise RuntimeError("direct Node probe is still live")
        return self.descendants_alive

    def terminate(self, *, deadline: float) -> None:
        self.terminate_deadlines.append(deadline)
        if self.process.returncode is None:
            self.process.kill()
            self.process.wait(timeout=max(0.0, deadline - time.monotonic()))
        self.descendants_alive = False

    def close(self) -> None:
        self.close_calls += 1
        if self.process.returncode is None or self.descendants_alive:
            raise RuntimeError("Node probe tree is still live")
        self.closed = True


class _CloseErrorBytesIO(io.BytesIO):
    def close(self) -> None:
        if not getattr(self, "_failed_close", False):
            self._failed_close = True
            raise ValueError("close failed")
        super().close()


def _install_node_probe(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    output: bytes = b"v22.23.1\n",
    returncode: int = 0,
    times_out: bool = False,
    persistent_timeout: bool = False,
) -> tuple[Path, list[tuple[tuple[str, ...], dict[str, object]]], _FakeNodeProcess]:
    node = tmp_path / ("node.exe" if os.name == "nt" else "node")
    node.write_bytes(b"synthetic node executable\n")
    calls: list[tuple[tuple[str, ...], dict[str, object]]] = []
    process = _FakeNodeProcess(
        output,
        returncode=returncode,
        times_out=times_out,
        persistent_timeout=persistent_timeout,
    )
    tree = _FakeNodeTree(process)
    process.tree = tree

    def which(name: str, *, path: str | None = None) -> str | None:
        if name == "pyright-langserver":
            return None
        assert name == "node"
        assert path is not None
        return str(node)

    def popen(command: list[str], **kwargs: object) -> _FakeNodeProcess:
        calls.append((tuple(command), dict(kwargs)))
        return process

    def spawn_tree(
        command: list[str],
        *,
        cwd: Path,
        env: dict[str, str],
        deadline: float,
    ) -> _FakeNodeTree:
        calls.append(
            (
                tuple(command),
                {"cwd": cwd, "deadline": deadline, "env": dict(env)},
            )
        )
        return tree

    monkeypatch.setattr(pyright_profile.shutil, "which", which)
    monkeypatch.setattr(pyright_profile.subprocess, "Popen", popen)
    monkeypatch.setattr(
        pyright_profile,
        "ProcessTree",
        SimpleNamespace(spawn_with_deadline=spawn_tree),
        raising=False,
    )
    return node, calls, process


def _install_real_node_program(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    program: str,
) -> tuple[Path, list[subprocess.Popen[bytes]], list[lsp_process_tree.ProcessTree]]:
    node = tmp_path / ("node.exe" if os.name == "nt" else "node")
    node.write_bytes(b"synthetic node executable\n")
    processes: list[subprocess.Popen[bytes]] = []
    trees: list[lsp_process_tree.ProcessTree] = []
    real_popen = subprocess.Popen
    real_tree_spawn = lsp_process_tree.ProcessTree._spawn_with_deadline.__func__

    def popen(_command: list[str], **kwargs: object) -> subprocess.Popen[bytes]:
        process = real_popen([sys.executable, "-c", program], **kwargs)
        processes.append(process)
        return process

    def spawn_tree(
        cls: type[lsp_process_tree.ProcessTree],
        _command: list[str],
        *,
        cwd: Path,
        env: dict[str, str],
        deadline: float,
    ) -> lsp_process_tree.ProcessTree:
        tree = real_tree_spawn(
            cls,
            [sys.executable, "-c", program],
            cwd=cwd,
            env=env,
            deadline=deadline,
        )
        processes.append(tree.process)
        trees.append(tree)
        return tree

    monkeypatch.setattr(pyright_profile.shutil, "which", lambda *args, **kwargs: str(node))
    monkeypatch.setattr(pyright_profile.subprocess, "Popen", popen)
    monkeypatch.setattr(
        lsp_process_tree.ProcessTree,
        "spawn_with_deadline",
        classmethod(spawn_tree),
        raising=False,
    )
    monkeypatch.setattr(
        pyright_profile,
        "ProcessTree",
        lsp_process_tree.ProcessTree,
        raising=False,
    )
    return node, processes, trees


def _install_prestarted_node_program(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    program: str,
) -> tuple[Path, list[subprocess.Popen[bytes]], list[lsp_process_tree.ProcessTree]]:
    node = tmp_path / ("node.exe" if os.name == "nt" else "node")
    node.write_bytes(b"synthetic node executable\n")
    ready = tmp_path / "node-parent-ready"
    release = tmp_path / "node-parent-release"
    gated_program = (
        "import pathlib,time\n"
        f"pathlib.Path({str(ready)!r}).write_text('ready',encoding='ascii')\n"
        f"release=pathlib.Path({str(release)!r})\n"
        "while not release.exists(): time.sleep(0.005)\n"
        + program
    )
    real_tree_spawn = lsp_process_tree.ProcessTree._spawn_with_deadline.__func__
    tree = real_tree_spawn(
        lsp_process_tree.ProcessTree,
        [sys.executable, "-c", gated_program],
        cwd=tmp_path,
        env=pyright_profile._node_environment(),
        deadline=time.monotonic() + 5,
    )
    ready_deadline = time.monotonic() + 5
    while not ready.exists() and time.monotonic() < ready_deadline:
        time.sleep(0.005)
    assert ready.exists(), "prestarted Node parent did not become ready"

    def spawn_tree(*args: object, **kwargs: object) -> lsp_process_tree.ProcessTree:
        release.write_text("release", encoding="ascii")
        return tree

    monkeypatch.setattr(pyright_profile.shutil, "which", lambda *args, **kwargs: str(node))
    monkeypatch.setattr(
        pyright_profile,
        "ProcessTree",
        SimpleNamespace(spawn_with_deadline=spawn_tree),
    )
    return node, [tree.process], [tree]


def _pid_alive(pid: int) -> bool:
    if os.name == "nt":
        return lsp_process_tree._windows_pid_alive(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    if sys.platform.startswith("linux"):
        try:
            payload = (Path("/proc") / str(pid) / "stat").read_text(encoding="ascii")
        except (FileNotFoundError, OSError):
            return False
        closing = payload.rfind(")")
        if closing >= 0 and payload[closing + 2 :].split()[0] in {"Z", "X", "x"}:
            return False
    return True


def _pid_exits_within(pid: int, seconds: float = 2.0) -> bool:
    deadline = time.monotonic() + seconds
    while _pid_alive(pid) and time.monotonic() < deadline:
        time.sleep(0.02)
    return not _pid_alive(pid)


def _scope(repository: Path):
    return resolve_repository_scope(repository)


def _empty_candidates() -> PyrightCandidates:
    return PyrightCandidates(project_local=(), managed=(), system=())


def _configuration_entry(
    configuration: dict[str, object], source_path: str
) -> dict[str, object]:
    path = Path(source_path)
    return {
        "configuration": configuration,
        "source_directory": path.parent.as_posix(),
        "source_path": path.as_posix(),
    }


def _configuration_chain_fingerprint(
    entries: list[dict[str, object]],
) -> str:
    return sha256_bytes(
        canonical_json_bytes(
            {
                "base_lsp_configuration": thaw_pyright_profile_value(
                    PYRIGHT_CONFIGURATION
                ),
                "repository_configuration_chain": entries,
            }
        )
    )


def _configuration_fingerprint(
    configuration: dict[str, object], *, source_path: str = "pyrightconfig.json"
) -> str:
    return _configuration_chain_fingerprint(
        [_configuration_entry(configuration, source_path)]
    )


def _stat_with(
    value: os.stat_result,
    *,
    mode: int | None = None,
    file_attributes: int | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        st_dev=value.st_dev,
        st_ino=value.st_ino,
        st_mode=value.st_mode if mode is None else mode,
        st_mtime_ns=value.st_mtime_ns,
        st_size=value.st_size,
        st_file_attributes=(
            getattr(value, "st_file_attributes", 0)
            if file_attributes is None
            else file_attributes
        ),
    )


def test_exported_pyright_configuration_is_recursively_immutable() -> None:
    analysis = PYRIGHT_CONFIGURATION["python"]["analysis"]
    original = analysis["logLevel"]
    try:
        with pytest.raises(TypeError):
            analysis["logLevel"] = "Information"
    finally:
        if isinstance(analysis, dict):
            analysis["logLevel"] = original
    assert PYRIGHT_CONFIGURATION["python"]["analysis"]["logLevel"] == "Error"


def test_exported_pyright_initialization_lists_are_immutable() -> None:
    exclude = PYRIGHT_INITIALIZATION_OPTIONS["files"]["exclude"]
    try:
        with pytest.raises((AttributeError, TypeError)):
            exclude.append("**/outside")
    finally:
        if isinstance(exclude, list):
            exclude.clear()
    assert thaw_pyright_profile_value(PYRIGHT_INITIALIZATION_OPTIONS) == {
        "files": {"exclude": []}
    }


def test_thawed_profile_values_match_live_manifest_fingerprints() -> None:
    thaw = getattr(pyright_profile, "thaw_pyright_profile_value", None)
    assert callable(thaw)

    configuration = thaw(PYRIGHT_CONFIGURATION)
    initialization_options = thaw(PYRIGHT_INITIALIZATION_OPTIONS)
    manifest = build_pyright_install_manifest(server_sha256=sha256_bytes(b"server"))

    assert configuration == {
        "python": {
            "analysis": {
                "autoSearchPaths": True,
                "diagnosticMode": "openFilesOnly",
                "logLevel": "Error",
                "useLibraryCodeForTypes": True,
            }
        },
        "pyright": {
            "disableLanguageServices": False,
            "disableOrganizeImports": True,
            "disableTaggedHints": False,
        },
    }
    assert initialization_options == {"files": {"exclude": []}}
    assert manifest["configuration_sha256"] == sha256_bytes(
        canonical_json_bytes(configuration)
    )
    assert manifest["initialization_options_sha256"] == sha256_bytes(
        canonical_json_bytes(initialization_options)
    )

    configuration["python"]["analysis"]["logLevel"] = "Information"
    initialization_options["files"]["exclude"].append("**/outside")
    assert thaw(PYRIGHT_CONFIGURATION)["python"]["analysis"]["logLevel"] == "Error"
    assert thaw(PYRIGHT_INITIALIZATION_OPTIONS)["files"]["exclude"] == []


def test_constants_configuration_and_public_dataclasses_are_exact() -> None:
    assert PYRIGHT_VERSION == lsp_paths.PYRIGHT_VERSION == "1.1.411"
    assert PYRIGHT_PACKAGE_URL == (
        "https://registry.npmjs.org/pyright/-/pyright-1.1.411.tgz"
    )
    assert PYRIGHT_PACKAGE_SHA256 == (
        "bd5c488fc20fa237a944279bf32cae2f986cf10d5d5d9e8705819859daeb2f4a"
    )
    assert PYRIGHT_PACKAGE_INTEGRITY == (
        "sha512-03S/vmS5lF1S/tVbKc2WNXCMq8JWCwta/qIYjj1jvqbQhoy+N3NgBzHTSmUlbYD6DJwqQ5XHf108QujoqeURvw=="
    )
    assert QUALIFIED_NODE_MAJOR == 22
    assert PYRIGHT_SERVER_RELATIVE == Path("package/langserver.index.js")
    assert thaw_pyright_profile_value(PYRIGHT_CONFIGURATION) == {
        "python": {
            "analysis": {
                "autoSearchPaths": True,
                "diagnosticMode": "openFilesOnly",
                "logLevel": "Error",
                "useLibraryCodeForTypes": True,
            }
        },
        "pyright": {
            "disableLanguageServices": False,
            "disableOrganizeImports": True,
            "disableTaggedHints": False,
        },
    }
    assert thaw_pyright_profile_value(PYRIGHT_INITIALIZATION_OPTIONS) == {
        "files": {"exclude": []}
    }
    assert [field.name for field in dataclasses.fields(PyrightIdentity)] == [
        "status",
        "source",
        "version",
        "node_executable",
        "node_version",
        "node_major",
        "server_executable",
        "executable_sha256",
        "package_sha256",
        "initialization_options_sha256",
        "configuration_sha256",
        "qualified",
        "degradation_codes",
    ]
    assert PyrightIdentity.__slots__ == tuple(
        field.name for field in dataclasses.fields(PyrightIdentity)
    )
    assert PyrightCandidates.__slots__ == ("project_local", "managed", "system")
    assert not hasattr(object.__new__(PyrightIdentity), "__dict__")


def test_install_manifest_builder_and_validator_form_one_canonical_contract() -> None:
    server_sha256 = sha256_bytes(b"server")

    manifest = build_pyright_install_manifest(server_sha256=server_sha256)

    assert validate_pyright_install_manifest(manifest) == manifest
    assert canonical_json_bytes(manifest) == canonical_json_bytes(
        validate_pyright_install_manifest(manifest)
    )
    assert manifest == {
        "configuration_sha256": sha256_bytes(
            canonical_json_bytes(thaw_pyright_profile_value(PYRIGHT_CONFIGURATION))
        ),
        "initialization_options_sha256": sha256_bytes(
            canonical_json_bytes(
                thaw_pyright_profile_value(PYRIGHT_INITIALIZATION_OPTIONS)
            )
        ),
        "package_integrity": PYRIGHT_PACKAGE_INTEGRITY,
        "package_sha256": PYRIGHT_PACKAGE_SHA256,
        "package_url": PYRIGHT_PACKAGE_URL,
        "schema_version": "pyright-install/v1",
        "server_relative_path": "package/langserver.index.js",
        "server_sha256": server_sha256,
        "version": PYRIGHT_VERSION,
    }
    with pytest.raises(ValueError):
        validate_pyright_install_manifest({**manifest, "unexpected": True})


def test_discovery_prefers_qualified_exact_project_candidate(
    monkeypatch: pytest.MonkeyPatch,
    repository: Path,
    state_root: Path,
    tmp_path: Path,
) -> None:
    scope = _scope(repository)
    project = create_pyright_fixture(repository, lockfile_version=3)
    managed = create_pyright_fixture(
        lsp_paths.managed_pyright_root(state_root), managed=True
    )
    create_pyright_fixture(tmp_path / "system", lockfile_version=3)
    _node, calls, _process = _install_node_probe(monkeypatch, tmp_path)

    result = discover_pyright(scope, state_root=state_root)

    assert result.status == "qualified"
    assert result.source == "project-local"
    assert result.version == PYRIGHT_VERSION
    assert result.node_version == "v22.23.1"
    assert result.node_major == 22
    assert result.server_executable == project
    assert result.server_executable != managed
    assert result.package_sha256 is None
    assert result.qualified is True
    assert result.degradation_codes == ()
    assert len(calls) == 1


def test_explicit_project_package_json_cannot_impersonate_entrypoint(
    monkeypatch: pytest.MonkeyPatch,
    repository: Path,
    state_root: Path,
    tmp_path: Path,
) -> None:
    scope = _scope(repository)
    server = create_pyright_fixture(repository)
    _install_node_probe(monkeypatch, tmp_path)

    result = discover_pyright(
        scope,
        state_root=state_root,
        candidates=PyrightCandidates((server.with_name("package.json"),), (), ()),
    )

    assert result.status == "degraded"
    assert result.source == "project-local"
    assert result.qualified is False
    assert "pyright_source_path_mismatch" in result.degradation_codes


def test_explicit_project_candidate_outside_checkout_cannot_qualify(
    monkeypatch: pytest.MonkeyPatch,
    repository: Path,
    state_root: Path,
    tmp_path: Path,
) -> None:
    scope = _scope(repository)
    create_pyright_fixture(repository)
    outside = create_pyright_fixture(tmp_path / "outside-project")
    _install_node_probe(monkeypatch, tmp_path)

    result = discover_pyright(
        scope,
        state_root=state_root,
        candidates=PyrightCandidates((outside,), (), ()),
    )

    assert result.status == "degraded"
    assert result.source == "project-local"
    assert result.qualified is False
    assert "pyright_source_path_mismatch" in result.degradation_codes


def test_explicit_managed_candidate_outside_approved_root_cannot_qualify(
    monkeypatch: pytest.MonkeyPatch,
    repository: Path,
    state_root: Path,
    tmp_path: Path,
) -> None:
    scope = _scope(repository)
    outside = create_pyright_fixture(tmp_path / "outside-managed", managed=True)
    _install_node_probe(monkeypatch, tmp_path)

    result = discover_pyright(
        scope,
        state_root=state_root,
        candidates=PyrightCandidates((), (outside,), ()),
    )

    assert result.status == "degraded"
    assert result.source == "managed"
    assert result.qualified is False
    assert "pyright_source_path_mismatch" in result.degradation_codes


def test_degraded_project_candidate_does_not_fall_through_to_qualified_managed(
    monkeypatch: pytest.MonkeyPatch,
    repository: Path,
    state_root: Path,
    tmp_path: Path,
) -> None:
    scope = _scope(repository)
    project = create_pyright_fixture(repository, package_version="1.1.410")
    create_pyright_fixture(lsp_paths.managed_pyright_root(state_root), managed=True)
    _install_node_probe(monkeypatch, tmp_path)

    result = discover_pyright(scope, state_root=state_root)

    assert result.status == "degraded"
    assert result.source == "project-local"
    assert result.server_executable == project
    assert result.qualified is False
    assert "pyright_version_mismatch" in result.degradation_codes


def test_project_package_without_server_degrades_before_valid_managed_install(
    monkeypatch: pytest.MonkeyPatch,
    repository: Path,
    state_root: Path,
    tmp_path: Path,
) -> None:
    scope = _scope(repository)
    server = create_pyright_fixture(repository)
    server.unlink()
    create_pyright_fixture(lsp_paths.managed_pyright_root(state_root), managed=True)
    _install_node_probe(monkeypatch, tmp_path)

    result = discover_pyright(scope, state_root=state_root)

    assert result.status == "degraded"
    assert result.source == "project-local"
    assert result.server_executable == server
    assert "pyright_server_missing" in result.degradation_codes


def test_project_lock_entry_without_package_tree_is_presence_evidence(
    monkeypatch: pytest.MonkeyPatch,
    repository: Path,
    state_root: Path,
    tmp_path: Path,
) -> None:
    scope = _scope(repository)
    server = create_pyright_fixture(repository)
    server.unlink()
    server.with_name("package.json").unlink()
    server.parent.rmdir()
    create_pyright_fixture(lsp_paths.managed_pyright_root(state_root), managed=True)
    _install_node_probe(monkeypatch, tmp_path)

    result = discover_pyright(scope, state_root=state_root)

    assert result.status == "degraded"
    assert result.source == "project-local"
    assert result.server_executable == server
    assert "pyright_server_missing" in result.degradation_codes


def test_managed_manifest_without_package_tree_is_presence_evidence(
    monkeypatch: pytest.MonkeyPatch,
    repository: Path,
    state_root: Path,
    tmp_path: Path,
) -> None:
    scope = _scope(repository)
    server = create_pyright_fixture(
        lsp_paths.managed_pyright_root(state_root), managed=True
    )
    server.unlink()
    server.with_name("package.json").unlink()
    server.parent.rmdir()
    _install_node_probe(monkeypatch, tmp_path)

    result = discover_pyright(scope, state_root=state_root)

    assert result.status == "degraded"
    assert result.source == "managed"
    assert result.server_executable == server
    assert "pyright_server_missing" in result.degradation_codes


def test_empty_approved_managed_root_blocks_system_fallthrough(
    monkeypatch: pytest.MonkeyPatch,
    repository: Path,
    state_root: Path,
    tmp_path: Path,
) -> None:
    scope = _scope(repository)
    managed_root = lsp_paths.managed_pyright_root(state_root)
    managed_root.mkdir(parents=True)
    managed_server = managed_root / PYRIGHT_SERVER_RELATIVE
    system = create_pyright_fixture(tmp_path / "system")
    _install_node_probe(monkeypatch, tmp_path)

    result = discover_pyright(
        scope,
        state_root=state_root,
        candidates=PyrightCandidates((), (managed_server,), (system,)),
    )

    assert result.status == "degraded"
    assert result.source == "managed"
    assert result.server_executable == managed_server
    assert "pyright_manifest_missing" in result.degradation_codes
    assert "pyright_package_json_missing" in result.degradation_codes
    assert "pyright_server_missing" in result.degradation_codes


def test_managed_candidate_requires_canonical_receipt_and_recomputed_digest(
    monkeypatch: pytest.MonkeyPatch,
    repository: Path,
    state_root: Path,
    tmp_path: Path,
) -> None:
    scope = _scope(repository)
    (repository / "pyrightconfig.json").unlink()
    (repository / "pyproject.toml").unlink()
    server = create_pyright_fixture(
        lsp_paths.managed_pyright_root(state_root),
        managed=True,
        server_bytes=b"managed server\n",
    )
    node, _calls, _process = _install_node_probe(monkeypatch, tmp_path)

    result = discover_pyright(scope, state_root=state_root)

    assert result == PyrightIdentity(
        status="qualified",
        source="managed",
        version=PYRIGHT_VERSION,
        node_executable=node,
        node_version="v22.23.1",
        node_major=22,
        server_executable=server,
        executable_sha256=sha256_bytes(b"managed server\n"),
        package_sha256=PYRIGHT_PACKAGE_SHA256,
        initialization_options_sha256=sha256_bytes(
            canonical_json_bytes(
                thaw_pyright_profile_value(PYRIGHT_INITIALIZATION_OPTIONS)
            )
        ),
        configuration_sha256=sha256_bytes(
            canonical_json_bytes(thaw_pyright_profile_value(PYRIGHT_CONFIGURATION))
        ),
        qualified=True,
        degradation_codes=(),
    )


def test_system_candidate_without_equivalent_lockfile_provenance_is_degraded(
    monkeypatch: pytest.MonkeyPatch,
    repository: Path,
    state_root: Path,
    tmp_path: Path,
) -> None:
    scope = _scope(repository)
    system = create_pyright_fixture(tmp_path / "system", lockfile_version=None)
    _install_node_probe(monkeypatch, tmp_path)
    candidates = PyrightCandidates(project_local=(), managed=(), system=(system,))

    result = discover_pyright(
        scope, state_root=state_root, candidates=candidates
    )

    assert result.status == "degraded"
    assert result.source == "system"
    assert result.qualified is False
    assert "pyright_lockfile_missing" in result.degradation_codes


@pytest.mark.parametrize("approved_source", ["project-local", "managed"])
def test_system_candidates_cannot_launder_approved_source_entrypoints(
    monkeypatch: pytest.MonkeyPatch,
    repository: Path,
    state_root: Path,
    tmp_path: Path,
    approved_source: str,
) -> None:
    scope = _scope(repository)
    if approved_source == "project-local":
        server = create_pyright_fixture(repository)
    else:
        server = create_pyright_fixture(
            lsp_paths.managed_pyright_root(state_root), managed=True
        )
    _install_node_probe(monkeypatch, tmp_path)

    result = discover_pyright(
        scope,
        state_root=state_root,
        candidates=PyrightCandidates((), (), (server,)),
    )

    assert result.status == "degraded"
    assert result.source == "system"
    assert result.server_executable is None
    assert result.qualified is False
    assert "pyright_source_path_mismatch" in result.degradation_codes


def test_system_shim_cannot_launder_project_local_entrypoint(
    monkeypatch: pytest.MonkeyPatch,
    repository: Path,
    state_root: Path,
    tmp_path: Path,
) -> None:
    scope = _scope(repository)
    create_pyright_fixture(repository)
    shim = repository / "node_modules/.bin/pyright-langserver.cmd"
    shim.parent.mkdir(parents=True, exist_ok=True)
    shim.write_text("shim content is not trusted or parsed", encoding="utf-8")
    _install_node_probe(monkeypatch, tmp_path)

    result = discover_pyright(
        scope,
        state_root=state_root,
        candidates=PyrightCandidates((), (), (shim,)),
    )

    assert result.status == "degraded"
    assert result.source == "system"
    assert result.server_executable is None
    assert "pyright_source_path_mismatch" in result.degradation_codes


def test_system_package_metadata_without_server_is_presence_evidence(
    monkeypatch: pytest.MonkeyPatch,
    repository: Path,
    state_root: Path,
    tmp_path: Path,
) -> None:
    scope = _scope(repository)
    server = create_pyright_fixture(tmp_path / "system-package")
    server.unlink()
    _install_node_probe(monkeypatch, tmp_path)

    result = discover_pyright(
        scope,
        state_root=state_root,
        candidates=PyrightCandidates((), (), (server,)),
    )

    assert result.status == "degraded"
    assert result.source == "system"
    assert result.server_executable == server
    assert "pyright_server_missing" in result.degradation_codes


def test_system_windows_global_cmd_shim_maps_to_actual_package_entrypoint(
    monkeypatch: pytest.MonkeyPatch,
    repository: Path,
    state_root: Path,
    tmp_path: Path,
) -> None:
    scope = _scope(repository)
    prefix = tmp_path / "npm-prefix"
    server = create_pyright_fixture(prefix)
    shim = prefix / "pyright-langserver.cmd"
    shim.write_text("arbitrary shim text must not be parsed", encoding="utf-8")
    _install_node_probe(monkeypatch, tmp_path)

    result = discover_pyright(
        scope,
        state_root=state_root,
        candidates=PyrightCandidates((), (), (shim,)),
    )

    assert result.status == "qualified"
    assert result.source == "system"
    assert result.server_executable == server
    assert result.qualified is True


def test_default_system_discovery_uses_same_windows_shim_normalization(
    monkeypatch: pytest.MonkeyPatch,
    repository: Path,
    state_root: Path,
    tmp_path: Path,
) -> None:
    scope = _scope(repository)
    prefix = tmp_path / "default-npm-prefix"
    server = create_pyright_fixture(prefix)
    shim = prefix / "pyright-langserver.cmd"
    shim.write_text("arbitrary shim text must not be parsed", encoding="utf-8")
    node, _calls, _process = _install_node_probe(monkeypatch, tmp_path)

    def which(name: str, *, path: str | None = None) -> str | None:
        assert path is not None
        if name == "pyright-langserver":
            return str(shim)
        assert name == "node"
        return str(node)

    monkeypatch.setattr(pyright_profile.shutil, "which", which)

    result = discover_pyright(scope, state_root=state_root)

    assert result.status == "qualified"
    assert result.source == "system"
    assert result.server_executable == server


def test_system_local_bin_cmd_shim_maps_to_sibling_package(
    monkeypatch: pytest.MonkeyPatch,
    repository: Path,
    state_root: Path,
    tmp_path: Path,
) -> None:
    scope = _scope(repository)
    package_root = tmp_path / "local-package"
    server = create_pyright_fixture(package_root)
    shim = package_root / "node_modules/.bin/pyright-langserver.cmd"
    shim.parent.mkdir(parents=True)
    shim.write_text("arbitrary shim text must not be parsed", encoding="utf-8")
    _install_node_probe(monkeypatch, tmp_path)

    result = discover_pyright(
        scope,
        state_root=state_root,
        candidates=PyrightCandidates((), (), (shim,)),
    )

    assert result.status == "qualified"
    assert result.server_executable == server
    assert result.qualified is True


def test_system_posix_symlink_maps_only_to_expected_global_package_entrypoint(
    monkeypatch: pytest.MonkeyPatch,
    repository: Path,
    state_root: Path,
    tmp_path: Path,
) -> None:
    scope = _scope(repository)
    prefix = tmp_path / "posix-prefix"
    server = create_pyright_fixture(prefix / "lib")
    shim = prefix / "bin/pyright-langserver"
    shim.parent.mkdir(parents=True)
    shim.write_bytes(b"placeholder")
    real_lstat = Path.lstat

    def lstat(path: Path) -> os.stat_result:
        value = real_lstat(path)
        if path == shim:
            return _stat_with(value, mode=stat.S_IFLNK | 0o777)  # type: ignore[return-value]
        return value

    monkeypatch.setattr(Path, "lstat", lstat)
    monkeypatch.setattr(
        pyright_profile.os,
        "readlink",
        lambda path: "../lib/node_modules/pyright/langserver.index.js",
    )
    _install_node_probe(monkeypatch, tmp_path)

    result = discover_pyright(
        scope,
        state_root=state_root,
        candidates=PyrightCandidates((), (), (shim,)),
    )

    assert result.status == "qualified"
    assert result.server_executable == server
    assert result.qualified is True


def test_unrecognized_system_shim_is_not_read_or_accepted(
    monkeypatch: pytest.MonkeyPatch,
    repository: Path,
    state_root: Path,
    tmp_path: Path,
) -> None:
    scope = _scope(repository)
    shim = tmp_path / "unrecognized/.bin/pyright-langserver.cmd"
    shim.parent.mkdir(parents=True)
    shim.write_text("do not parse or hash me", encoding="utf-8")
    real_read = pyright_profile.read_stable_bytes

    def guarded_read(path: Path, *args: object, **kwargs: object) -> bytes:
        if path == shim:
            pytest.fail("unrecognized shim was read")
        return real_read(path, *args, **kwargs)

    monkeypatch.setattr(pyright_profile, "read_stable_bytes", guarded_read)
    _install_node_probe(monkeypatch, tmp_path)

    result = discover_pyright(
        scope,
        state_root=state_root,
        candidates=PyrightCandidates((), (), (shim,)),
    )

    assert result.status == "degraded"
    assert result.source == "system"
    assert result.server_executable is None
    assert "pyright_source_path_mismatch" in result.degradation_codes


@pytest.mark.parametrize(
    "candidate",
    [
        Path(r"\\server\share\pyright-langserver.cmd"),
        Path(r"\\?\C:\tools\pyright-langserver.cmd"),
    ],
    ids=("unc", "device"),
)
def test_system_candidates_reject_unc_and_device_paths_without_reading(
    monkeypatch: pytest.MonkeyPatch,
    repository: Path,
    state_root: Path,
    tmp_path: Path,
    candidate: Path,
) -> None:
    scope = _scope(repository)
    real_read = pyright_profile.read_stable_bytes

    def guarded_read(path: Path, *args: object, **kwargs: object) -> bytes:
        if path == candidate:
            pytest.fail("unsafe system path was read")
        return real_read(path, *args, **kwargs)

    monkeypatch.setattr(pyright_profile, "read_stable_bytes", guarded_read)
    _install_node_probe(monkeypatch, tmp_path)

    result = discover_pyright(
        scope,
        state_root=state_root,
        candidates=PyrightCandidates((), (), (candidate,)),
    )

    assert result.status == "degraded"
    assert result.server_executable is None
    assert "pyright_source_path_mismatch" in result.degradation_codes


def test_system_posix_symlink_rejects_target_outside_recognized_layout(
    monkeypatch: pytest.MonkeyPatch,
    repository: Path,
    state_root: Path,
    tmp_path: Path,
) -> None:
    scope = _scope(repository)
    shim = tmp_path / "posix-prefix/bin/pyright-langserver"
    shim.parent.mkdir(parents=True)
    shim.write_bytes(b"placeholder")
    real_lstat = Path.lstat

    def lstat(path: Path) -> os.stat_result:
        value = real_lstat(path)
        if path == shim:
            return _stat_with(value, mode=stat.S_IFLNK | 0o777)  # type: ignore[return-value]
        return value

    monkeypatch.setattr(Path, "lstat", lstat)
    monkeypatch.setattr(
        pyright_profile.os,
        "readlink",
        lambda path: "../../outside/langserver.index.js",
    )
    _install_node_probe(monkeypatch, tmp_path)

    result = discover_pyright(
        scope,
        state_root=state_root,
        candidates=PyrightCandidates((), (), (shim,)),
    )

    assert result.status == "degraded"
    assert result.server_executable is None
    assert "pyright_source_path_mismatch" in result.degradation_codes


def test_missing_discovery_has_no_network_process_installer_import_or_state_write(
    monkeypatch: pytest.MonkeyPatch,
    repository: Path,
    tmp_path: Path,
) -> None:
    scope = _scope(repository)
    untouched_state = tmp_path / "untouched-state"
    sys.modules.pop("install_pyright", None)
    real_import = builtins.__import__

    def guarded_import(name: str, *args: object, **kwargs: object):
        if name == "install_pyright" or name.startswith("install_pyright."):
            pytest.fail("discovery imported the installer")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda *args, **kwargs: pytest.fail("network used"),
    )
    monkeypatch.setattr(
        socket,
        "create_connection",
        lambda *args, **kwargs: pytest.fail("network used"),
    )
    monkeypatch.setattr(
        pyright_profile.subprocess,
        "Popen",
        lambda *args, **kwargs: pytest.fail("process started without a candidate"),
    )

    result = discover_pyright(
        scope,
        state_root=untouched_state,
        candidates=_empty_candidates(),
    )

    assert result.status == "missing"
    assert result.source is None
    assert result.version is None
    assert result.server_executable is None
    assert result.qualified is False
    assert result.degradation_codes == ("pyright_missing",)
    assert not untouched_state.exists()
    assert "install_pyright" not in sys.modules


@pytest.mark.parametrize("lockfile_version", [1, 2, 3])
def test_project_provenance_supports_npm_lockfile_versions_1_2_and_3(
    monkeypatch: pytest.MonkeyPatch,
    repository: Path,
    state_root: Path,
    tmp_path: Path,
    lockfile_version: int,
) -> None:
    scope = _scope(repository)
    server = create_pyright_fixture(repository, lockfile_version=lockfile_version)
    _install_node_probe(monkeypatch, tmp_path)

    result = discover_pyright(
        scope,
        state_root=state_root,
        candidates=PyrightCandidates((server,), (), ()),
    )

    assert result.status == "qualified"
    assert result.degradation_codes == ()


def test_lockfile_link_entry_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
    repository: Path,
    state_root: Path,
    tmp_path: Path,
) -> None:
    scope = _scope(repository)
    server = create_pyright_fixture(repository, lockfile_link=True)
    _install_node_probe(monkeypatch, tmp_path)

    result = discover_pyright(
        scope,
        state_root=state_root,
        candidates=PyrightCandidates((server,), (), ()),
    )

    assert result.status == "degraded"
    assert "pyright_lockfile_link" in result.degradation_codes


@pytest.mark.parametrize(
    ("package_name", "package_version", "integrity", "expected_code"),
    [
        ("not-pyright", PYRIGHT_VERSION, PYRIGHT_PACKAGE_INTEGRITY, "pyright_package_mismatch"),
        ("pyright", "1.1.410", PYRIGHT_PACKAGE_INTEGRITY, "pyright_version_mismatch"),
        ("pyright", PYRIGHT_VERSION, "sha512-wrong", "pyright_integrity_mismatch"),
    ],
)
def test_project_package_version_and_integrity_mismatches_are_degraded(
    monkeypatch: pytest.MonkeyPatch,
    repository: Path,
    state_root: Path,
    tmp_path: Path,
    package_name: str,
    package_version: str,
    integrity: str,
    expected_code: str,
) -> None:
    scope = _scope(repository)
    server = create_pyright_fixture(
        repository,
        package_name=package_name,
        package_version=package_version,
        integrity=integrity,
    )
    _install_node_probe(monkeypatch, tmp_path)

    result = discover_pyright(
        scope,
        state_root=state_root,
        candidates=PyrightCandidates((server,), (), ()),
    )

    assert result.status == "degraded"
    assert expected_code in result.degradation_codes
    assert result.degradation_codes == tuple(sorted(set(result.degradation_codes)))


@pytest.mark.parametrize(
    ("field", "value", "expected_code"),
    [
        ("package_url", "https://example.invalid/pyright.tgz", "pyright_package_url_mismatch"),
        ("package_sha256", "0" * 64, "pyright_package_sha256_mismatch"),
        ("server_sha256", "0" * 64, "pyright_executable_digest_mismatch"),
        ("configuration_sha256", "0" * 64, "pyright_configuration_mismatch"),
        (
            "initialization_options_sha256",
            "0" * 64,
            "pyright_initialization_options_mismatch",
        ),
    ],
)
def test_managed_receipt_package_digest_configuration_and_init_mismatches_degrade(
    monkeypatch: pytest.MonkeyPatch,
    repository: Path,
    state_root: Path,
    tmp_path: Path,
    field: str,
    value: str,
    expected_code: str,
) -> None:
    scope = _scope(repository)
    managed_root = lsp_paths.managed_pyright_root(state_root)
    server = create_pyright_fixture(
        managed_root,
        managed=True,
        manifest_overrides={field: value},
    )
    _install_node_probe(monkeypatch, tmp_path)

    result = discover_pyright(
        scope,
        state_root=state_root,
        candidates=PyrightCandidates((), (server,), ()),
    )

    assert result.status == "degraded"
    assert result.source == "managed"
    assert result.qualified is False
    assert expected_code in result.degradation_codes


def test_managed_receipt_reports_digest_mismatch_alongside_other_mismatches(
    monkeypatch: pytest.MonkeyPatch,
    repository: Path,
    state_root: Path,
    tmp_path: Path,
) -> None:
    scope = _scope(repository)
    server = create_pyright_fixture(
        lsp_paths.managed_pyright_root(state_root),
        managed=True,
        manifest_overrides={
            "configuration_sha256": "0" * 64,
            "server_sha256": "0" * 64,
        },
    )
    _install_node_probe(monkeypatch, tmp_path)

    result = discover_pyright(
        scope,
        state_root=state_root,
        candidates=PyrightCandidates((), (server,), ()),
    )

    assert "pyright_configuration_mismatch" in result.degradation_codes
    assert "pyright_executable_digest_mismatch" in result.degradation_codes


def test_noncanonical_managed_manifest_is_degraded(
    monkeypatch: pytest.MonkeyPatch,
    repository: Path,
    state_root: Path,
    tmp_path: Path,
) -> None:
    scope = _scope(repository)
    managed_root = lsp_paths.managed_pyright_root(state_root)
    server = create_pyright_fixture(managed_root, managed=True)
    manifest = managed_root / "install-manifest.json"
    manifest.write_bytes(manifest.read_bytes() + b"\n")
    _install_node_probe(monkeypatch, tmp_path)

    result = discover_pyright(
        scope,
        state_root=state_root,
        candidates=PyrightCandidates((), (server,), ()),
    )

    assert result.status == "degraded"
    assert "pyright_manifest_noncanonical" in result.degradation_codes


def test_repository_pyrightconfig_changes_identity_hash_and_remains_qualified(
    monkeypatch: pytest.MonkeyPatch,
    repository: Path,
    state_root: Path,
    tmp_path: Path,
) -> None:
    scope = _scope(repository)
    server = create_pyright_fixture(repository)
    configuration_path = repository / "pyrightconfig.json"
    strict_configuration = {"typeCheckingMode": "strict"}
    configuration_path.write_bytes(canonical_json_bytes(strict_configuration))
    _install_node_probe(monkeypatch, tmp_path)

    strict = discover_pyright(
        scope,
        state_root=state_root,
        candidates=PyrightCandidates((server,), (), ()),
    )

    expected_strict = _configuration_fingerprint(strict_configuration)
    assert strict.status == "qualified"
    assert strict.configuration_sha256 == expected_strict
    assert strict.configuration_sha256 != sha256_bytes(
        canonical_json_bytes(thaw_pyright_profile_value(PYRIGHT_CONFIGURATION))
    )

    basic_configuration = {"typeCheckingMode": "basic"}
    configuration_path.write_bytes(canonical_json_bytes(basic_configuration))
    _install_node_probe(monkeypatch, tmp_path)
    basic = discover_pyright(
        scope,
        state_root=state_root,
        candidates=PyrightCandidates((server,), (), ()),
    )
    assert basic.status == "qualified"
    assert basic.configuration_sha256 != strict.configuration_sha256


def test_repository_jsonc_preserves_strings_and_accepts_comments_and_trailing_commas(
    monkeypatch: pytest.MonkeyPatch,
    repository: Path,
    state_root: Path,
    tmp_path: Path,
) -> None:
    scope = _scope(repository)
    server = create_pyright_fixture(repository)
    (repository / "pyrightconfig.json").write_text(
        r'''
        {
          // Line comments are JSONC trivia.
          "include": [
            "src", /* Array trailing comma follows. */
          ],
          "marker": "literal // and /* block */ and ,] and escaped \"quote\"",
          /* Block comments may span
             multiple lines. */
          "typeCheckingMode": "strict",
        } // A line comment may end at EOF.
        ''',
        encoding="utf-8",
    )
    _install_node_probe(monkeypatch, tmp_path)

    result = discover_pyright(
        scope,
        state_root=state_root,
        candidates=PyrightCandidates((server,), (), ()),
    )

    assert result.status == "qualified"
    assert result.configuration_sha256 == _configuration_fingerprint(
        {
            "include": ["src"],
            "marker": 'literal // and /* block */ and ,] and escaped "quote"',
            "typeCheckingMode": "strict",
        }
    )


@pytest.mark.parametrize(
    "content",
    [
        b'{"value": 1, /* unclosed',
        b'{"value": // missing value\n}',
        b'{"value": 1,,}',
        b'{"value": "unterminated // not a comment}',
        b"{,}",
        b"[,]",
    ],
    ids=(
        "unclosed-block-comment",
        "missing-value",
        "duplicate-comma",
        "unclosed-string",
        "object-leading-comma",
        "array-leading-comma",
    ),
)
def test_malformed_repository_jsonc_degrades_stably(
    monkeypatch: pytest.MonkeyPatch,
    repository: Path,
    state_root: Path,
    tmp_path: Path,
    content: bytes,
) -> None:
    scope = _scope(repository)
    server = create_pyright_fixture(repository)
    (repository / "pyrightconfig.json").write_bytes(content)
    _install_node_probe(monkeypatch, tmp_path)

    result = discover_pyright(
        scope,
        state_root=state_root,
        candidates=PyrightCandidates((server,), (), ()),
    )

    assert result.status == "degraded"
    assert result.qualified is False
    assert "pyright_repository_config_malformed" in result.degradation_codes


@pytest.mark.parametrize(
    "pyproject_content",
    [
        '[project]\nname = "fixture"\n',
        "[tool.ruff]\nline-length = 88\n",
        '[tool]\npyright = "strict"\n',
    ],
    ids=("tool-absent", "pyright-missing", "pyright-non-object"),
)
def test_root_pyproject_without_object_pyright_blocks_ancestor_config_reads(
    monkeypatch: pytest.MonkeyPatch,
    repository: Path,
    state_root: Path,
    tmp_path: Path,
    pyproject_content: str,
) -> None:
    scope = _scope(repository)
    server = create_pyright_fixture(repository)
    (repository / "pyrightconfig.json").unlink()
    (repository / "pyproject.toml").write_text(pyproject_content, encoding="utf-8")
    ancestor = repository.parent / "pyrightconfig.json"
    ancestor.write_bytes(
        canonical_json_bytes({"typeCheckingMode": "strict"})
    )
    ancestor_inspections: list[Path] = []
    ancestor_reads: list[Path] = []
    real_lstat = Path.lstat
    real_read_stable_bytes = pyright_profile.read_stable_bytes

    def lstat(path: Path) -> os.stat_result:
        if path == ancestor:
            ancestor_inspections.append(path)
            raise AssertionError("ancestor config must not be inspected")
        return real_lstat(path)

    def read_stable_bytes(path: Path, max_bytes: int, *, label: str = "file") -> bytes:
        if path == ancestor:
            ancestor_reads.append(path)
            raise AssertionError("ancestor config must not be read")
        return real_read_stable_bytes(path, max_bytes, label=label)

    monkeypatch.setattr(Path, "lstat", lstat)
    monkeypatch.setattr(pyright_profile, "read_stable_bytes", read_stable_bytes)
    _install_node_probe(monkeypatch, tmp_path)
    first = discover_pyright(
        scope,
        state_root=state_root,
        candidates=PyrightCandidates((server,), (), ()),
    )

    ancestor.write_bytes(canonical_json_bytes({"typeCheckingMode": "basic"}))
    _install_node_probe(monkeypatch, tmp_path)
    second = discover_pyright(
        scope,
        state_root=state_root,
        candidates=PyrightCandidates((server,), (), ()),
    )

    assert first.source == second.source == "project-local"
    assert first.status == second.status == "degraded"
    assert first.qualified is second.qualified is False
    assert first.degradation_codes == second.degradation_codes == (
        "pyright_repository_config_ancestor_search",
    )
    assert first.configuration_sha256 == second.configuration_sha256 == sha256_bytes(
        canonical_json_bytes(thaw_pyright_profile_value(PYRIGHT_CONFIGURATION))
    )
    assert ancestor_inspections == []
    assert ancestor_reads == []


def test_malformed_root_pyproject_retains_malformed_degradation(
    monkeypatch: pytest.MonkeyPatch,
    repository: Path,
    state_root: Path,
    tmp_path: Path,
) -> None:
    scope = _scope(repository)
    server = create_pyright_fixture(repository)
    (repository / "pyrightconfig.json").unlink()
    (repository / "pyproject.toml").write_bytes(b"[tool.pyright\n")
    _install_node_probe(monkeypatch, tmp_path)

    result = discover_pyright(
        scope,
        state_root=state_root,
        candidates=PyrightCandidates((server,), (), ()),
    )

    assert result.status == "degraded"
    assert result.qualified is False
    assert "pyright_repository_config_malformed" in result.degradation_codes
    assert "pyright_repository_config_ancestor_search" not in result.degradation_codes


def test_repository_pyproject_pyright_changes_identity_hash(
    monkeypatch: pytest.MonkeyPatch,
    repository: Path,
    state_root: Path,
    tmp_path: Path,
) -> None:
    scope = _scope(repository)
    server = create_pyright_fixture(repository)
    (repository / "pyrightconfig.json").unlink()
    pyproject = repository / "pyproject.toml"
    pyproject.write_text(
        '[project]\nname = "fixture"\n\n[tool.pyright]\ntypeCheckingMode = "strict"\n',
        encoding="utf-8",
    )
    _install_node_probe(monkeypatch, tmp_path)

    strict = discover_pyright(
        scope,
        state_root=state_root,
        candidates=PyrightCandidates((server,), (), ()),
    )

    assert strict.status == "qualified"
    assert strict.configuration_sha256 == _configuration_fingerprint(
        {"typeCheckingMode": "strict"}, source_path="pyproject.toml"
    )

    pyproject.write_text(
        '[project]\nname = "fixture"\n\n[tool.pyright]\ntypeCheckingMode = "basic"\n',
        encoding="utf-8",
    )
    _install_node_probe(monkeypatch, tmp_path)
    basic = discover_pyright(
        scope,
        state_root=state_root,
        candidates=PyrightCandidates((server,), (), ()),
    )
    assert basic.status == "qualified"
    assert basic.configuration_sha256 == _configuration_fingerprint(
        {"typeCheckingMode": "basic"}, source_path="pyproject.toml"
    )
    assert basic.configuration_sha256 != strict.configuration_sha256


def test_repository_pyrightconfig_takes_precedence_over_pyproject_pyright(
    monkeypatch: pytest.MonkeyPatch,
    repository: Path,
    state_root: Path,
    tmp_path: Path,
) -> None:
    scope = _scope(repository)
    server = create_pyright_fixture(repository)
    (repository / "pyrightconfig.json").write_bytes(
        canonical_json_bytes({"typeCheckingMode": "basic"})
    )
    (repository / "pyproject.toml").write_text(
        '[tool.pyright]\ntypeCheckingMode = "strict"\n', encoding="utf-8"
    )
    _install_node_probe(monkeypatch, tmp_path)

    result = discover_pyright(
        scope,
        state_root=state_root,
        candidates=PyrightCandidates((server,), (), ()),
    )

    assert result.status == "qualified"
    assert result.configuration_sha256 == _configuration_fingerprint(
        {"typeCheckingMode": "basic"}
    )


def test_json_extends_json_records_base_first_chain_without_flattening(
    monkeypatch: pytest.MonkeyPatch,
    repository: Path,
    state_root: Path,
    tmp_path: Path,
) -> None:
    scope = _scope(repository)
    server = create_pyright_fixture(repository)
    config_dir = repository / "config"
    config_dir.mkdir()
    (config_dir / "base.json").write_bytes(
        canonical_json_bytes(
            {
                "defineConstant": {"BASE": True},
                "include": ["base"],
                "typeCheckingMode": "strict",
            }
        )
    )
    (repository / "pyrightconfig.json").write_bytes(
        canonical_json_bytes(
            {
                "defineConstant": {"CHILD": True},
                "extends": "config/base.json",
                "typeCheckingMode": "basic",
            }
        )
    )
    _install_node_probe(monkeypatch, tmp_path)

    result = discover_pyright(
        scope,
        state_root=state_root,
        candidates=PyrightCandidates((server,), (), ()),
    )

    assert result.status == "qualified"
    assert result.configuration_sha256 == _configuration_chain_fingerprint(
        [
            _configuration_entry(
                {
                    "defineConstant": {"BASE": True},
                    "include": ["base"],
                    "typeCheckingMode": "strict",
                },
                "config/base.json",
            ),
            _configuration_entry(
                {
                    "defineConstant": {"CHILD": True},
                    "extends": "config/base.json",
                    "typeCheckingMode": "basic",
                },
                "pyrightconfig.json",
            ),
        ]
    )


def test_config_chain_distinguishes_accumulated_base_and_child_maps_from_child_only(
    monkeypatch: pytest.MonkeyPatch,
    repository: Path,
    state_root: Path,
    tmp_path: Path,
) -> None:
    scope = _scope(repository)
    server = create_pyright_fixture(repository)
    config_dir = repository / "config"
    config_dir.mkdir()
    base_configuration = {"defineConstant": {"BASE": True}}
    child_configuration = {
        "defineConstant": {"CHILD": True},
        "extends": "config/base.json",
    }
    (config_dir / "base.json").write_bytes(canonical_json_bytes(base_configuration))
    root = repository / "pyrightconfig.json"
    root.write_bytes(canonical_json_bytes(child_configuration))
    _install_node_probe(monkeypatch, tmp_path)

    inherited = discover_pyright(
        scope,
        state_root=state_root,
        candidates=PyrightCandidates((server,), (), ()),
    )

    assert inherited.status == "qualified"
    assert inherited.configuration_sha256 == _configuration_chain_fingerprint(
        [
            {
                "configuration": base_configuration,
                "source_directory": "config",
                "source_path": "config/base.json",
            },
            {
                "configuration": child_configuration,
                "source_directory": ".",
                "source_path": "pyrightconfig.json",
            },
        ]
    )

    child_only = {"defineConstant": {"CHILD": True}}
    root.write_bytes(canonical_json_bytes(child_only))
    _install_node_probe(monkeypatch, tmp_path)
    direct = discover_pyright(
        scope,
        state_root=state_root,
        candidates=PyrightCandidates((server,), (), ()),
    )
    assert direct.status == "qualified"
    assert inherited.configuration_sha256 != direct.configuration_sha256


def test_config_chain_fingerprint_preserves_each_include_directory_context(
    monkeypatch: pytest.MonkeyPatch,
    repository: Path,
    state_root: Path,
    tmp_path: Path,
) -> None:
    scope = _scope(repository)
    server = create_pyright_fixture(repository)
    config_dir = repository / "config"
    config_dir.mkdir()
    (config_dir / "base.json").write_bytes(
        canonical_json_bytes({"include": ["src"]})
    )
    root = repository / "pyrightconfig.json"
    root.write_bytes(canonical_json_bytes({"extends": "config/base.json"}))
    _install_node_probe(monkeypatch, tmp_path)

    inherited = discover_pyright(
        scope,
        state_root=state_root,
        candidates=PyrightCandidates((server,), (), ()),
    )

    root.write_bytes(canonical_json_bytes({"include": ["src"]}))
    _install_node_probe(monkeypatch, tmp_path)
    direct = discover_pyright(
        scope,
        state_root=state_root,
        candidates=PyrightCandidates((server,), (), ()),
    )
    assert inherited.status == direct.status == "qualified"
    assert inherited.configuration_sha256 != direct.configuration_sha256


def test_config_chain_fingerprint_distinguishes_json_and_toml_sources(
    monkeypatch: pytest.MonkeyPatch,
    repository: Path,
    state_root: Path,
    tmp_path: Path,
) -> None:
    scope = _scope(repository)
    server = create_pyright_fixture(repository)
    config_dir = repository / "config"
    config_dir.mkdir()
    (config_dir / "base.json").write_bytes(
        canonical_json_bytes({"include": ["src"]})
    )
    root = repository / "pyrightconfig.json"
    root.write_bytes(canonical_json_bytes({"extends": "config/base.json"}))
    _install_node_probe(monkeypatch, tmp_path)
    json_source = discover_pyright(
        scope,
        state_root=state_root,
        candidates=PyrightCandidates((server,), (), ()),
    )

    (config_dir / "base.toml").write_text(
        '[tool.pyright]\ninclude = ["src"]\n', encoding="utf-8"
    )
    root.write_bytes(canonical_json_bytes({"extends": "config/base.toml"}))
    _install_node_probe(monkeypatch, tmp_path)
    toml_source = discover_pyright(
        scope,
        state_root=state_root,
        candidates=PyrightCandidates((server,), (), ()),
    )

    assert json_source.status == toml_source.status == "qualified"
    assert json_source.configuration_sha256 != toml_source.configuration_sha256


def test_json_extends_toml_and_tracks_base_changes(
    monkeypatch: pytest.MonkeyPatch,
    repository: Path,
    state_root: Path,
    tmp_path: Path,
) -> None:
    scope = _scope(repository)
    server = create_pyright_fixture(repository)
    config_dir = repository / "config"
    config_dir.mkdir()
    base = config_dir / "base.toml"
    base.write_text(
        '[tool.pyright]\ninclude = ["base-a"]\ntypeCheckingMode = "strict"\n',
        encoding="utf-8",
    )
    (repository / "pyrightconfig.json").write_bytes(
        canonical_json_bytes(
            {"extends": "config/base.toml", "typeCheckingMode": "basic"}
        )
    )
    _install_node_probe(monkeypatch, tmp_path)

    first = discover_pyright(
        scope,
        state_root=state_root,
        candidates=PyrightCandidates((server,), (), ()),
    )

    assert first.status == "qualified"
    assert first.configuration_sha256 == _configuration_chain_fingerprint(
        [
            _configuration_entry(
                {"include": ["base-a"], "typeCheckingMode": "strict"},
                "config/base.toml",
            ),
            _configuration_entry(
                {"extends": "config/base.toml", "typeCheckingMode": "basic"},
                "pyrightconfig.json",
            ),
        ]
    )

    base.write_text(
        '[tool.pyright]\ninclude = ["base-b"]\ntypeCheckingMode = "strict"\n',
        encoding="utf-8",
    )
    _install_node_probe(monkeypatch, tmp_path)
    second = discover_pyright(
        scope,
        state_root=state_root,
        candidates=PyrightCandidates((server,), (), ()),
    )
    assert second.status == "qualified"
    assert second.configuration_sha256 == _configuration_chain_fingerprint(
        [
            _configuration_entry(
                {"include": ["base-b"], "typeCheckingMode": "strict"},
                "config/base.toml",
            ),
            _configuration_entry(
                {"extends": "config/base.toml", "typeCheckingMode": "basic"},
                "pyrightconfig.json",
            ),
        ]
    )
    assert second.configuration_sha256 != first.configuration_sha256


def test_pyproject_toml_extends_json_relative_to_pyproject(
    monkeypatch: pytest.MonkeyPatch,
    repository: Path,
    state_root: Path,
    tmp_path: Path,
) -> None:
    scope = _scope(repository)
    server = create_pyright_fixture(repository)
    (repository / "pyrightconfig.json").unlink()
    (repository / "base.json").write_bytes(
        canonical_json_bytes({"include": ["base"], "typeCheckingMode": "strict"})
    )
    (repository / "pyproject.toml").write_text(
        '[tool.pyright]\nextends = "base.json"\ntypeCheckingMode = "basic"\n',
        encoding="utf-8",
    )
    _install_node_probe(monkeypatch, tmp_path)

    result = discover_pyright(
        scope,
        state_root=state_root,
        candidates=PyrightCandidates((server,), (), ()),
    )

    assert result.status == "qualified"
    assert result.configuration_sha256 == _configuration_chain_fingerprint(
        [
            _configuration_entry(
                {"include": ["base"], "typeCheckingMode": "strict"},
                "base.json",
            ),
            _configuration_entry(
                {"extends": "base.json", "typeCheckingMode": "basic"},
                "pyproject.toml",
            ),
        ]
    )


@pytest.mark.parametrize(
    ("setup", "expected_code"),
    [
        ("cycle", "pyright_repository_config_extends_cycle"),
        ("outside", "pyright_repository_config_outside_repository"),
        ("absolute", "pyright_repository_config_extends_absolute"),
        ("missing", "pyright_repository_config_missing"),
        ("malformed", "pyright_repository_config_malformed"),
        ("oversized", "pyright_repository_config_oversized"),
        ("unsupported-format", "pyright_repository_config_unsupported_format"),
    ],
)
def test_invalid_repository_config_extends_degrades_stably(
    monkeypatch: pytest.MonkeyPatch,
    repository: Path,
    state_root: Path,
    tmp_path: Path,
    setup: str,
    expected_code: str,
) -> None:
    scope = _scope(repository)
    server = create_pyright_fixture(repository)
    root = repository / "pyrightconfig.json"
    base = repository / "base.json"
    if setup == "cycle":
        root.write_bytes(canonical_json_bytes({"extends": "base.json"}))
        base.write_bytes(canonical_json_bytes({"extends": "pyrightconfig.json"}))
    elif setup == "outside":
        outside = repository.parent / "outside.json"
        outside.write_bytes(canonical_json_bytes({"typeCheckingMode": "strict"}))
        root.write_bytes(canonical_json_bytes({"extends": "../outside.json"}))
    elif setup == "absolute":
        base.write_bytes(canonical_json_bytes({"typeCheckingMode": "strict"}))
        root.write_bytes(canonical_json_bytes({"extends": str(base)}))
    elif setup == "missing":
        root.write_bytes(canonical_json_bytes({"extends": "missing.json"}))
    elif setup == "malformed":
        root.write_bytes(canonical_json_bytes({"extends": "base.json"}))
        base.write_bytes(b"{")
    elif setup == "oversized":
        root.write_bytes(canonical_json_bytes({"extends": "base.json"}))
        base.write_bytes(b"x" * (256 * 1024 + 1))
    else:
        root.write_bytes(canonical_json_bytes({"extends": "base.yaml"}))
        (repository / "base.yaml").write_text("typeCheckingMode: strict\n", encoding="utf-8")
    _install_node_probe(monkeypatch, tmp_path)

    result = discover_pyright(
        scope,
        state_root=state_root,
        candidates=PyrightCandidates((server,), (), ()),
    )

    assert result.status == "degraded"
    assert result.qualified is False
    assert expected_code in result.degradation_codes


def test_repository_config_extends_depth_is_bounded(
    monkeypatch: pytest.MonkeyPatch,
    repository: Path,
    state_root: Path,
    tmp_path: Path,
) -> None:
    scope = _scope(repository)
    server = create_pyright_fixture(repository)
    paths = [repository / "pyrightconfig.json"] + [
        repository / f"base-{index}.json" for index in range(10)
    ]
    for index, path in enumerate(paths):
        value = (
            {"extends": paths[index + 1].name}
            if index + 1 < len(paths)
            else {"typeCheckingMode": "strict"}
        )
        path.write_bytes(canonical_json_bytes(value))
    _install_node_probe(monkeypatch, tmp_path)

    result = discover_pyright(
        scope,
        state_root=state_root,
        candidates=PyrightCandidates((server,), (), ()),
    )

    assert result.status == "degraded"
    assert "pyright_repository_config_extends_too_deep" in result.degradation_codes


def test_repository_config_total_bytes_are_bounded_across_extends(
    monkeypatch: pytest.MonkeyPatch,
    repository: Path,
    state_root: Path,
    tmp_path: Path,
) -> None:
    scope = _scope(repository)
    server = create_pyright_fixture(repository)
    paths = [repository / "pyrightconfig.json"] + [
        repository / f"large-{index}.json" for index in range(2)
    ]
    for index, path in enumerate(paths):
        value: dict[str, object] = {"padding": "x" * (200 * 1024)}
        if index + 1 < len(paths):
            value["extends"] = paths[index + 1].name
        path.write_bytes(canonical_json_bytes(value))
    _install_node_probe(monkeypatch, tmp_path)

    result = discover_pyright(
        scope,
        state_root=state_root,
        candidates=PyrightCandidates((server,), (), ()),
    )

    assert result.status == "degraded"
    assert "pyright_repository_config_total_oversized" in result.degradation_codes


def test_reparse_extended_repository_config_degrades_without_following(
    monkeypatch: pytest.MonkeyPatch,
    repository: Path,
    state_root: Path,
    tmp_path: Path,
) -> None:
    scope = _scope(repository)
    server = create_pyright_fixture(repository)
    base = repository / "base.json"
    base.write_bytes(canonical_json_bytes({"typeCheckingMode": "strict"}))
    (repository / "pyrightconfig.json").write_bytes(
        canonical_json_bytes({"extends": "base.json"})
    )
    real_lstat = Path.lstat

    def lstat(path: Path) -> os.stat_result:
        value = real_lstat(path)
        if path == base:
            return _stat_with(value, file_attributes=0x400)  # type: ignore[return-value]
        return value

    monkeypatch.setattr(Path, "lstat", lstat)
    _install_node_probe(monkeypatch, tmp_path)

    result = discover_pyright(
        scope,
        state_root=state_root,
        candidates=PyrightCandidates((server,), (), ()),
    )

    assert result.status == "degraded"
    assert "pyright_repository_config_unsafe" in result.degradation_codes


@pytest.mark.parametrize("config_format", ["json", "toml"])
def test_deep_repository_config_domain_degrades_without_recursion_error(
    monkeypatch: pytest.MonkeyPatch,
    repository: Path,
    state_root: Path,
    tmp_path: Path,
    config_format: str,
) -> None:
    scope = _scope(repository)
    server = create_pyright_fixture(repository)
    nested = b"[" * 500 + b"0" + b"]" * 500
    if config_format == "json":
        (repository / "pyrightconfig.json").write_bytes(
            b'{"nested":' + nested + b"}"
        )
    else:
        (repository / "pyrightconfig.json").unlink()
        (repository / "pyproject.toml").write_bytes(
            b"[tool.pyright]\nnested = " + nested + b"\n"
        )
    _install_node_probe(monkeypatch, tmp_path)

    result = discover_pyright(
        scope,
        state_root=state_root,
        candidates=PyrightCandidates((server,), (), ()),
    )

    assert result.status == "degraded"
    assert "pyright_repository_config_too_deep" in result.degradation_codes


@pytest.mark.parametrize("config_format", ["json", "toml"])
def test_repository_config_domain_node_count_is_bounded_iteratively(
    monkeypatch: pytest.MonkeyPatch,
    repository: Path,
    state_root: Path,
    tmp_path: Path,
    config_format: str,
) -> None:
    scope = _scope(repository)
    server = create_pyright_fixture(repository)
    values = b",".join([b"0"] * 66_000)
    if config_format == "json":
        (repository / "pyrightconfig.json").write_bytes(
            b'{"nodes":[' + values + b"]}"
        )
    else:
        (repository / "pyrightconfig.json").unlink()
        (repository / "pyproject.toml").write_bytes(
            b"[tool.pyright]\nnodes = [" + values + b"]\n"
        )
    _install_node_probe(monkeypatch, tmp_path)

    result = discover_pyright(
        scope,
        state_root=state_root,
        candidates=PyrightCandidates((server,), (), ()),
    )

    assert result.status == "degraded"
    assert "pyright_repository_config_too_many_nodes" in result.degradation_codes


@pytest.mark.parametrize(
    "value",
    ["1.25", "1979-05-27T07:32:00Z"],
    ids=("float", "datetime"),
)
def test_repository_toml_rejects_values_outside_canonical_json_domain(
    monkeypatch: pytest.MonkeyPatch,
    repository: Path,
    state_root: Path,
    tmp_path: Path,
    value: str,
) -> None:
    scope = _scope(repository)
    server = create_pyright_fixture(repository)
    (repository / "pyrightconfig.json").unlink()
    (repository / "pyproject.toml").write_text(
        f"[tool.pyright]\nunsupported = {value}\n", encoding="utf-8"
    )
    _install_node_probe(monkeypatch, tmp_path)

    result = discover_pyright(
        scope,
        state_root=state_root,
        candidates=PyrightCandidates((server,), (), ()),
    )

    assert result.status == "degraded"
    assert "pyright_repository_config_unsupported_value" in result.degradation_codes


def test_repository_config_canonicalization_recursion_degrades_stably(
    monkeypatch: pytest.MonkeyPatch,
    repository: Path,
    state_root: Path,
    tmp_path: Path,
) -> None:
    scope = _scope(repository)
    server = create_pyright_fixture(repository)
    (repository / "pyrightconfig.json").write_bytes(
        canonical_json_bytes({"typeCheckingMode": "strict"})
    )
    real_canonical = pyright_profile.canonical_json_bytes

    def canonical(value: object) -> bytes:
        if isinstance(value, dict) and "base_lsp_configuration" in value:
            raise RecursionError("reviewer reproduction")
        return real_canonical(value)

    monkeypatch.setattr(pyright_profile, "canonical_json_bytes", canonical)
    _install_node_probe(monkeypatch, tmp_path)

    result = discover_pyright(
        scope,
        state_root=state_root,
        candidates=PyrightCandidates((server,), (), ()),
    )

    assert result.status == "degraded"
    assert "pyright_repository_config_too_deep" in result.degradation_codes


def test_deep_managed_manifest_degrades_without_recursion_error(
    monkeypatch: pytest.MonkeyPatch,
    repository: Path,
    state_root: Path,
    tmp_path: Path,
) -> None:
    scope = _scope(repository)
    managed_root = lsp_paths.managed_pyright_root(state_root)
    server = create_pyright_fixture(managed_root, managed=True)
    manifest = managed_root / "install-manifest.json"
    raw = manifest.read_bytes()
    nested = b"[" * 500 + b"0" + b"]" * 500
    manifest.write_bytes(raw[:-1] + b',"nested":' + nested + b"}")
    _install_node_probe(monkeypatch, tmp_path)

    result = discover_pyright(
        scope,
        state_root=state_root,
        candidates=PyrightCandidates((), (server,), ()),
    )

    assert result.status == "degraded"
    assert "pyright_manifest_too_deep" in result.degradation_codes


def test_managed_manifest_canonicalization_recursion_degrades_stably(
    monkeypatch: pytest.MonkeyPatch,
    repository: Path,
    state_root: Path,
    tmp_path: Path,
) -> None:
    scope = _scope(repository)
    server = create_pyright_fixture(
        lsp_paths.managed_pyright_root(state_root), managed=True
    )
    real_canonical = pyright_profile.canonical_json_bytes

    def canonical(value: object) -> bytes:
        if isinstance(value, dict) and value.get("schema_version") == "pyright-install/v1":
            raise RecursionError("reviewer reproduction")
        return real_canonical(value)

    monkeypatch.setattr(pyright_profile, "canonical_json_bytes", canonical)
    _install_node_probe(monkeypatch, tmp_path)

    result = discover_pyright(
        scope,
        state_root=state_root,
        candidates=PyrightCandidates((), (server,), ()),
    )

    assert result.status == "degraded"
    assert "pyright_manifest_too_deep" in result.degradation_codes


def test_managed_receipt_attests_base_profile_while_identity_includes_repository_config(
    monkeypatch: pytest.MonkeyPatch,
    repository: Path,
    state_root: Path,
    tmp_path: Path,
) -> None:
    scope = _scope(repository)
    server = create_pyright_fixture(
        lsp_paths.managed_pyright_root(state_root), managed=True
    )
    repository_configuration = {"typeCheckingMode": "strict"}
    (repository / "pyrightconfig.json").write_bytes(
        canonical_json_bytes(repository_configuration)
    )
    _install_node_probe(monkeypatch, tmp_path)

    result = discover_pyright(scope, state_root=state_root)

    assert result.status == "qualified"
    assert result.configuration_sha256 == _configuration_fingerprint(
        repository_configuration
    )
    manifest = build_pyright_install_manifest(server_sha256=sha256_bytes(server.read_bytes()))
    assert manifest["configuration_sha256"] == sha256_bytes(
        canonical_json_bytes(thaw_pyright_profile_value(PYRIGHT_CONFIGURATION))
    )


@pytest.mark.parametrize(
    ("content", "expected_code"),
    [
        (b"{", "pyright_repository_config_malformed"),
        (b"x" * (256 * 1024 + 1), "pyright_repository_config_oversized"),
    ],
    ids=("malformed", "oversized"),
)
def test_invalid_repository_pyrightconfig_degrades_without_crashing(
    monkeypatch: pytest.MonkeyPatch,
    repository: Path,
    state_root: Path,
    tmp_path: Path,
    content: bytes,
    expected_code: str,
) -> None:
    scope = _scope(repository)
    server = create_pyright_fixture(repository)
    (repository / "pyrightconfig.json").write_bytes(content)
    _install_node_probe(monkeypatch, tmp_path)

    result = discover_pyright(
        scope,
        state_root=state_root,
        candidates=PyrightCandidates((server,), (), ()),
    )

    assert result.status == "degraded"
    assert result.qualified is False
    assert expected_code in result.degradation_codes


def test_reparse_repository_pyrightconfig_degrades_through_no_follow_read(
    monkeypatch: pytest.MonkeyPatch,
    repository: Path,
    state_root: Path,
    tmp_path: Path,
) -> None:
    scope = _scope(repository)
    server = create_pyright_fixture(repository)
    configuration_path = repository / "pyrightconfig.json"
    configuration_path.write_bytes(canonical_json_bytes({"typeCheckingMode": "strict"}))
    real_lstat = Path.lstat

    def lstat(path: Path) -> os.stat_result:
        value = real_lstat(path)
        if path == configuration_path:
            return _stat_with(value, file_attributes=0x400)  # type: ignore[return-value]
        return value

    monkeypatch.setattr(Path, "lstat", lstat)
    _install_node_probe(monkeypatch, tmp_path)

    result = discover_pyright(
        scope,
        state_root=state_root,
        candidates=PyrightCandidates((server,), (), ()),
    )

    assert result.status == "degraded"
    assert "pyright_repository_config_unsafe" in result.degradation_codes


def test_reparse_package_metadata_is_rejected_without_fallthrough(
    monkeypatch: pytest.MonkeyPatch,
    repository: Path,
    state_root: Path,
    tmp_path: Path,
) -> None:
    scope = _scope(repository)
    server = create_pyright_fixture(repository)
    package_json = server.with_name("package.json")
    real_lstat = Path.lstat

    def lstat(path: Path) -> os.stat_result:
        value = real_lstat(path)
        if path == package_json:
            return _stat_with(value, file_attributes=0x400)  # type: ignore[return-value]
        return value

    monkeypatch.setattr(Path, "lstat", lstat)
    create_pyright_fixture(lsp_paths.managed_pyright_root(state_root), managed=True)
    _install_node_probe(monkeypatch, tmp_path)

    result = discover_pyright(scope, state_root=state_root)

    assert result.source == "project-local"
    assert result.status == "degraded"
    assert "pyright_package_json_unsafe" in result.degradation_codes


@pytest.mark.parametrize(
    ("content", "expected_code"),
    [
        (b"{", "pyright_package_json_malformed"),
        (b"x" * (pyright_profile.MAX_PACKAGE_JSON_BYTES + 1), "pyright_package_json_oversized"),
    ],
    ids=("malformed", "oversized"),
)
def test_malformed_and_oversized_package_metadata_are_degraded(
    monkeypatch: pytest.MonkeyPatch,
    repository: Path,
    state_root: Path,
    tmp_path: Path,
    content: bytes,
    expected_code: str,
) -> None:
    scope = _scope(repository)
    server = create_pyright_fixture(repository)
    server.with_name("package.json").write_bytes(content)
    _install_node_probe(monkeypatch, tmp_path)

    result = discover_pyright(
        scope,
        state_root=state_root,
        candidates=PyrightCandidates((server,), (), ()),
    )

    assert result.status == "degraded"
    assert expected_code in result.degradation_codes


def test_expired_deadline_fails_before_candidate_metadata_or_node_probe(
    monkeypatch: pytest.MonkeyPatch,
    repository: Path,
    state_root: Path,
) -> None:
    scope = _scope(repository)
    server = create_pyright_fixture(repository)
    monkeypatch.setattr(
        pyright_profile.subprocess,
        "Popen",
        lambda *args, **kwargs: pytest.fail("expired discovery started Node"),
    )

    with pytest.raises(TimeoutError, match="Pyright discovery deadline"):
        discover_pyright(
            scope,
            state_root=state_root,
            candidates=PyrightCandidates((server,), (), ()),
            deadline=time.monotonic() - 1,
        )


def test_node_missing_is_a_stable_degradation(
    monkeypatch: pytest.MonkeyPatch,
    repository: Path,
    state_root: Path,
) -> None:
    scope = _scope(repository)
    server = create_pyright_fixture(repository)
    monkeypatch.setattr(pyright_profile.shutil, "which", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        pyright_profile.subprocess,
        "Popen",
        lambda *args, **kwargs: pytest.fail("missing Node was started"),
    )

    result = discover_pyright(
        scope,
        state_root=state_root,
        candidates=PyrightCandidates((server,), (), ()),
    )

    assert result.node_executable is None
    assert result.node_version is None
    assert result.node_major is None
    assert result.status == "degraded"
    assert "pyright_node_missing" in result.degradation_codes


@pytest.mark.parametrize(
    "found",
    [
        r"\\server\share\node.exe",
        r"\\?\C:\tools\node.exe",
        "relative/node",
    ],
    ids=("windows-unc", "windows-device", "posix-relative"),
)
def test_node_executable_rejects_nonlocal_which_results_without_inspection(
    monkeypatch: pytest.MonkeyPatch,
    repository: Path,
    state_root: Path,
    found: str,
) -> None:
    scope = _scope(repository)
    server = create_pyright_fixture(repository)
    candidate = Path(found)
    inspections: list[Path] = []
    spawn_calls: list[object] = []
    real_lstat = Path.lstat

    def lstat(path: Path) -> os.stat_result:
        if path == candidate:
            inspections.append(path)
        return real_lstat(path)

    def popen(*args: object, **kwargs: object) -> _FakeNodeProcess:
        spawn_calls.append((args, kwargs))
        return _FakeNodeProcess(b"v22.23.1\n")

    monkeypatch.setattr(Path, "lstat", lstat)
    monkeypatch.setattr(pyright_profile.shutil, "which", lambda *args, **kwargs: found)
    monkeypatch.setattr(pyright_profile.subprocess, "Popen", popen)

    result = discover_pyright(
        scope,
        state_root=state_root,
        candidates=PyrightCandidates((server,), (), ()),
    )

    assert result.node_executable == candidate
    assert result.status == "degraded"
    assert "pyright_node_executable_unsafe" in result.degradation_codes
    assert inspections == []
    assert spawn_calls == []


def test_node_executable_rejects_reparse_point_before_spawn(
    monkeypatch: pytest.MonkeyPatch,
    repository: Path,
    state_root: Path,
    tmp_path: Path,
) -> None:
    scope = _scope(repository)
    server = create_pyright_fixture(repository)
    node, calls, _process = _install_node_probe(monkeypatch, tmp_path)
    real_lstat = Path.lstat

    def lstat(path: Path) -> os.stat_result:
        value = real_lstat(path)
        if path == node:
            return _stat_with(value, file_attributes=0x400)  # type: ignore[return-value]
        return value

    monkeypatch.setattr(Path, "lstat", lstat)

    result = discover_pyright(
        scope,
        state_root=state_root,
        candidates=PyrightCandidates((server,), (), ()),
    )

    assert result.node_executable == node
    assert result.status == "degraded"
    assert "pyright_node_executable_unsafe" in result.degradation_codes
    assert calls == []


def test_node_executable_rejects_network_path_before_spawn(
    monkeypatch: pytest.MonkeyPatch,
    repository: Path,
    state_root: Path,
    tmp_path: Path,
) -> None:
    scope = _scope(repository)
    server = create_pyright_fixture(repository)
    node, calls, _process = _install_node_probe(monkeypatch, tmp_path)
    monkeypatch.setattr(
        pyright_profile,
        "_known_network_path",
        lambda path: path == node,
        raising=False,
    )

    result = discover_pyright(
        scope,
        state_root=state_root,
        candidates=PyrightCandidates((server,), (), ()),
    )

    assert result.node_executable == node
    assert result.status == "degraded"
    assert "pyright_node_executable_unsafe" in result.degradation_codes
    assert calls == []


@pytest.mark.parametrize(
    "output",
    [b"22.23.1\n", b"v22.23\n", b"v22.23.1 extra\n", b" v22.23.1\n", b"\xff"],
)
def test_node_version_output_must_be_exact(
    monkeypatch: pytest.MonkeyPatch,
    repository: Path,
    state_root: Path,
    tmp_path: Path,
    output: bytes,
) -> None:
    scope = _scope(repository)
    server = create_pyright_fixture(repository)
    _install_node_probe(monkeypatch, tmp_path, output=output)

    result = discover_pyright(
        scope,
        state_root=state_root,
        candidates=PyrightCandidates((server,), (), ()),
    )

    assert result.node_version is None
    assert result.node_major is None
    assert "pyright_node_version_malformed" in result.degradation_codes


@pytest.mark.parametrize("inherited_stream", ["stdout", "stderr"])
def test_node_probe_contains_descendant_inheriting_output_before_read(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    inherited_stream: str,
) -> None:
    pid_file = tmp_path / f"{inherited_stream}-descendant.pid"
    if inherited_stream == "stdout":
        redirection = "stdout=sys.stdout, stderr=subprocess.DEVNULL"
    else:
        redirection = "stdout=subprocess.DEVNULL, stderr=sys.stderr"
    program = (
        "import subprocess,sys\n"
        "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(1.2)'],"
        f"{redirection})\n"
        f"open({str(pid_file)!r},'w',encoding='ascii').write(str(child.pid))\n"
        "print('v22.1.0',flush=True)\n"
    )
    installer = (
        _install_prestarted_node_program
        if inherited_stream == "stdout"
        else _install_real_node_program
    )
    _node, processes, trees = installer(
        monkeypatch, tmp_path, program
    )
    operation_seconds = 0.3 if inherited_stream == "stdout" else 1.0
    started = time.monotonic()

    result = pyright_profile._probe_node(started + operation_seconds)
    elapsed = time.monotonic() - started
    descendant_pid = int(pid_file.read_text(encoding="ascii"))
    try:
        assert elapsed <= (
            operation_seconds + pyright_profile.NODE_PROBE_CLEANUP_SECONDS + 0.25
        )
        assert result[1] is None
        assert result[2] is None
        assert result[3] & {"pyright_node_probe_timeout", "pyright_node_probe_failed"}
        assert all(process.returncode == 0 for process in processes)
        assert _pid_exits_within(descendant_pid)
        assert all(
            tree.process_group is None and tree.windows_job is None for tree in trees
        ) or all(
            tree in pyright_profile._pending_node_probe_cleanup_snapshot()
            for tree in trees
        )
    finally:
        if _pid_alive(descendant_pid):
            try:
                os.kill(descendant_pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass


def test_node_probe_huge_output_kills_inheriting_descendant_within_bound(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    pid_file = tmp_path / "huge-output-descendant.pid"
    program = (
        "import subprocess,sys\n"
        "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(2.5)'])\n"
        f"open({str(pid_file)!r},'w',encoding='ascii').write(str(child.pid))\n"
        "sys.stdout.buffer.write(b'x'*(2*1024*1024))\n"
        "sys.stdout.buffer.flush()\n"
    )
    _node, _processes, trees = _install_real_node_program(
        monkeypatch, tmp_path, program
    )
    started = time.monotonic()

    result = pyright_profile._probe_node(started + 1.0)
    elapsed = time.monotonic() - started
    descendant_pid = int(pid_file.read_text(encoding="ascii"))
    try:
        assert elapsed <= 1.0 + pyright_profile.NODE_PROBE_CLEANUP_SECONDS + 0.25
        assert result[1] is None
        assert result[2] is None
        assert result[3] & {"pyright_node_probe_timeout", "pyright_node_probe_failed"}
        assert _pid_exits_within(descendant_pid)
        assert all(
            tree.process_group is None and tree.windows_job is None for tree in trees
        ) or all(
            tree in pyright_profile._pending_node_probe_cleanup_snapshot()
            for tree in trees
        )
    finally:
        if _pid_alive(descendant_pid):
            try:
                os.kill(descendant_pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass


def test_node_probe_wall_bound_includes_only_one_cleanup_allowance(
    monkeypatch: pytest.MonkeyPatch,
    repository: Path,
    state_root: Path,
    tmp_path: Path,
) -> None:
    class Clock:
        now = 100.0

        def monotonic(self) -> float:
            return self.now

    clock = Clock()
    scope = _scope(repository)
    server = create_pyright_fixture(repository)
    _node, _calls, process = _install_node_probe(monkeypatch, tmp_path, output=b"")
    tree = process.tree
    assert tree is not None
    real_wait = process.wait
    real_terminate = tree.terminate

    def wait(timeout: float | None = None) -> int:
        assert timeout is not None
        process.wait_timeouts.append(timeout)
        clock.now += timeout
        raise subprocess.TimeoutExpired(("node", "--version"), timeout)

    def terminate(*, deadline: float) -> None:
        assert deadline == pytest.approx(100.8)
        clock.now = deadline
        raise TimeoutError("tree cleanup deadline expired")

    monkeypatch.setattr(pyright_profile.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(process, "wait", wait)
    monkeypatch.setattr(tree, "terminate", terminate)

    result = discover_pyright(
        scope,
        state_root=state_root,
        candidates=PyrightCandidates((server,), (), ()),
        deadline=100.3,
    )

    try:
        assert clock.now == pytest.approx(100.8)
        assert result.node_version is None
        assert result.node_major is None
        assert result.status == "degraded"
        assert pyright_profile._pending_node_probe_cleanup_snapshot() == (tree,)
    finally:
        monkeypatch.setattr(process, "wait", real_wait)
        monkeypatch.setattr(tree, "terminate", real_terminate)
        process._times_out = False
        pyright_profile._retry_node_probe_cleanups()

    assert pyright_profile._pending_node_probe_cleanup_snapshot() == ()


def test_node_blocking_output_timeout_kills_reaps_and_closes_probe(
    monkeypatch: pytest.MonkeyPatch,
    repository: Path,
    state_root: Path,
    tmp_path: Path,
) -> None:
    scope = _scope(repository)
    server = create_pyright_fixture(repository)
    _node, _calls, process = _install_node_probe(
        monkeypatch,
        tmp_path,
        output=b"x" * (pyright_profile.MAX_NODE_VERSION_BYTES + 1),
        times_out=True,
    )

    result = discover_pyright(
        scope,
        state_root=state_root,
        candidates=PyrightCandidates((server,), (), ()),
    )

    assert process.killed is True
    assert len(process.wait_timeouts) == 2
    assert process.stdout.closed
    assert result.status == "degraded"
    assert "pyright_node_probe_timeout" in result.degradation_codes


def test_node_timeout_cleanup_never_extends_the_hard_cleanup_deadline(
    monkeypatch: pytest.MonkeyPatch,
    repository: Path,
    state_root: Path,
    tmp_path: Path,
) -> None:
    scope = _scope(repository)
    server = create_pyright_fixture(repository)
    _node, _calls, process = _install_node_probe(
        monkeypatch,
        tmp_path,
        output=b"",
        times_out=True,
    )
    real_monotonic = time.monotonic
    operation_expired = False
    real_wait = process.wait

    def wait(timeout: float | None = None) -> int:
        nonlocal operation_expired
        try:
            return real_wait(timeout)
        finally:
            if not process.killed:
                operation_expired = True

    def monotonic() -> float:
        elapsed = pyright_profile.NODE_PROBE_TIMEOUT_SECONDS + 1.0
        return real_monotonic() + (elapsed if operation_expired else 0.0)

    monkeypatch.setattr(process, "wait", wait)
    monkeypatch.setattr(pyright_profile.time, "monotonic", monotonic)

    result = discover_pyright(
        scope,
        state_root=state_root,
        candidates=PyrightCandidates((server,), (), ()),
    )

    assert process.returncode == -9
    assert len(process.wait_timeouts) == 2
    cleanup_timeout = process.wait_timeouts[1]
    assert cleanup_timeout == 0
    assert "pyright_node_probe_timeout" in result.degradation_codes


def test_node_persistent_timeout_is_retained_for_bounded_retry(
    monkeypatch: pytest.MonkeyPatch,
    repository: Path,
    state_root: Path,
    tmp_path: Path,
) -> None:
    scope = _scope(repository)
    server = create_pyright_fixture(repository)
    _node, _calls, process = _install_node_probe(
        monkeypatch,
        tmp_path,
        output=b"",
        times_out=True,
        persistent_timeout=True,
    )

    result = discover_pyright(
        scope,
        state_root=state_root,
        candidates=PyrightCandidates((server,), (), ()),
    )

    assert process.killed is True
    assert len(process.wait_timeouts) == 2
    assert not process.stdin.closed
    assert not process.stdout.closed
    assert not process.stderr.closed
    assert result.status == "degraded"
    assert "pyright_node_probe_failed" in result.degradation_codes
    assert pyright_profile._pending_node_probe_cleanup_snapshot() == (process.tree,)

    process._persistent_timeout = False
    pyright_profile._retry_node_probe_cleanups()

    assert process.returncode == -9
    assert process.stdin.closed
    assert process.stdout.closed
    assert process.stderr.closed
    assert pyright_profile._pending_node_probe_cleanup_snapshot() == ()


def test_node_stdout_access_error_overrides_timeout_as_probe_failure(
    monkeypatch: pytest.MonkeyPatch,
    repository: Path,
    state_root: Path,
    tmp_path: Path,
) -> None:
    scope = _scope(repository)
    server = create_pyright_fixture(repository)
    _node, _calls, process = _install_node_probe(
        monkeypatch, tmp_path, output=b"", times_out=True
    )

    def stdout_error(_process: _FakeNodeProcess) -> io.BytesIO:
        raise OSError("stdout unavailable")

    monkeypatch.setattr(
        _FakeNodeProcess,
        "stdout",
        property(stdout_error),
        raising=False,
    )

    result = discover_pyright(
        scope,
        state_root=state_root,
        candidates=PyrightCandidates((server,), (), ()),
    )

    assert process.killed is True
    assert len(process.wait_timeouts) == 2
    assert result.status == "degraded"
    assert "pyright_node_probe_failed" in result.degradation_codes
    assert pyright_profile._pending_node_probe_cleanup_snapshot() == (process.tree,)

    monkeypatch.delattr(_FakeNodeProcess, "stdout")
    pyright_profile._retry_node_probe_cleanups()

    assert pyright_profile._pending_node_probe_cleanup_snapshot() == ()


@pytest.mark.parametrize(
    "error",
    [
        OSError("spawn failed"),
        ValueError("invalid spawn"),
        subprocess.SubprocessError("subprocess failed"),
    ],
    ids=("oserror", "value-error", "subprocess-error"),
)
def test_node_spawn_errors_are_stable_probe_failures(
    monkeypatch: pytest.MonkeyPatch,
    repository: Path,
    state_root: Path,
    tmp_path: Path,
    error: Exception,
) -> None:
    scope = _scope(repository)
    server = create_pyright_fixture(repository)
    _install_node_probe(monkeypatch, tmp_path)

    def fail_spawn(*args: object, **kwargs: object) -> object:
        raise error

    monkeypatch.setattr(pyright_profile.ProcessTree, "spawn_with_deadline", fail_spawn)

    result = discover_pyright(
        scope,
        state_root=state_root,
        candidates=PyrightCandidates((server,), (), ()),
    )

    assert result.status == "degraded"
    assert "pyright_node_probe_failed" in result.degradation_codes


@pytest.mark.parametrize(
    "error",
    [
        OSError("wait failed"),
        ValueError("invalid wait"),
        subprocess.SubprocessError("subprocess wait failed"),
    ],
    ids=("oserror", "value-error", "subprocess-error"),
)
def test_node_wait_errors_are_stable_probe_failures(
    monkeypatch: pytest.MonkeyPatch,
    repository: Path,
    state_root: Path,
    tmp_path: Path,
    error: Exception,
) -> None:
    scope = _scope(repository)
    server = create_pyright_fixture(repository)
    _node, _calls, process = _install_node_probe(monkeypatch, tmp_path)

    real_wait = process.wait
    wait_calls = 0

    def fail_first_wait(timeout: float | None = None) -> int:
        nonlocal wait_calls
        wait_calls += 1
        if wait_calls == 1:
            raise error
        return real_wait(timeout)

    monkeypatch.setattr(process, "wait", fail_first_wait)

    result = discover_pyright(
        scope,
        state_root=state_root,
        candidates=PyrightCandidates((server,), (), ()),
    )

    assert result.status == "degraded"
    assert "pyright_node_probe_failed" in result.degradation_codes
    assert process.killed is True
    assert wait_calls == 2
    assert process.stdout.closed


def test_node_timeout_cleanup_error_is_a_stable_probe_failure(
    monkeypatch: pytest.MonkeyPatch,
    repository: Path,
    state_root: Path,
    tmp_path: Path,
) -> None:
    scope = _scope(repository)
    server = create_pyright_fixture(repository)
    _node, _calls, process = _install_node_probe(
        monkeypatch, tmp_path, output=b"", times_out=True
    )
    real_kill = process.kill
    monkeypatch.setattr(
        process,
        "kill",
        lambda: (_ for _ in ()).throw(RuntimeError("kill failed")),
    )

    result = discover_pyright(
        scope,
        state_root=state_root,
        candidates=PyrightCandidates((server,), (), ()),
    )

    assert result.status == "degraded"
    assert "pyright_node_probe_failed" in result.degradation_codes
    assert pyright_profile._pending_node_probe_cleanup_snapshot() == (process.tree,)

    monkeypatch.setattr(process, "kill", real_kill)
    pyright_profile._retry_node_probe_cleanups()

    assert process.returncode == -9
    assert pyright_profile._pending_node_probe_cleanup_snapshot() == ()


def test_node_stream_close_error_is_a_stable_probe_failure(
    monkeypatch: pytest.MonkeyPatch,
    repository: Path,
    state_root: Path,
    tmp_path: Path,
) -> None:
    scope = _scope(repository)
    server = create_pyright_fixture(repository)
    _node, _calls, process = _install_node_probe(monkeypatch, tmp_path)
    process.stdout = _CloseErrorBytesIO(b"v22.23.1\n")

    result = discover_pyright(
        scope,
        state_root=state_root,
        candidates=PyrightCandidates((server,), (), ()),
    )

    assert result.status == "degraded"
    assert "pyright_node_probe_failed" in result.degradation_codes
    assert pyright_profile._pending_node_probe_cleanup_snapshot() == (process.tree,)

    process.stdout = io.BytesIO()
    pyright_profile._retry_node_probe_cleanups()

    assert pyright_profile._pending_node_probe_cleanup_snapshot() == ()


def test_node_oversized_completed_output_is_bounded_and_closed(
    monkeypatch: pytest.MonkeyPatch,
    repository: Path,
    state_root: Path,
    tmp_path: Path,
) -> None:
    scope = _scope(repository)
    server = create_pyright_fixture(repository)
    _node, _calls, process = _install_node_probe(
        monkeypatch,
        tmp_path,
        output=b"x" * (pyright_profile.MAX_NODE_VERSION_BYTES + 1),
    )

    result = discover_pyright(
        scope,
        state_root=state_root,
        candidates=PyrightCandidates((server,), (), ()),
    )

    assert result.status == "degraded"
    assert "pyright_node_output_oversized" in result.degradation_codes
    assert process.stdout.closed


def test_node_probe_never_constructs_a_reader_thread(
    monkeypatch: pytest.MonkeyPatch,
    repository: Path,
    state_root: Path,
    tmp_path: Path,
) -> None:
    scope = _scope(repository)
    server = create_pyright_fixture(repository)
    _install_node_probe(monkeypatch, tmp_path)
    created_names: list[object] = []
    real_thread = threading.Thread

    def thread(*args: object, **kwargs: object) -> threading.Thread:
        created_names.append(kwargs.get("name"))
        if kwargs.get("name") == "pyright-node-version-reader":
            raise AssertionError("Node probe must not construct a reader thread")
        return real_thread(*args, **kwargs)

    monkeypatch.setattr(threading, "Thread", thread)

    result = discover_pyright(
        scope,
        state_root=state_root,
        candidates=PyrightCandidates((server,), (), ()),
    )

    assert result.status == "qualified"
    assert "pyright-node-version-reader" not in created_names


def test_repeated_node_probes_reap_processes_and_close_output(
    monkeypatch: pytest.MonkeyPatch,
    repository: Path,
    state_root: Path,
    tmp_path: Path,
) -> None:
    scope = _scope(repository)
    server = create_pyright_fixture(repository)
    node = tmp_path / ("node.exe" if os.name == "nt" else "node")
    node.write_bytes(b"synthetic node executable\n")
    processes: list[_FakeNodeProcess] = []
    trees: list[_FakeNodeTree] = []

    def spawn_tree(*args: object, **kwargs: object) -> _FakeNodeTree:
        process = _FakeNodeProcess(b"v22.23.1\n")
        tree = _FakeNodeTree(process)
        process.tree = tree
        processes.append(process)
        trees.append(tree)
        return tree

    monkeypatch.setattr(pyright_profile.shutil, "which", lambda *args, **kwargs: str(node))
    monkeypatch.setattr(
        pyright_profile,
        "ProcessTree",
        SimpleNamespace(spawn_with_deadline=spawn_tree),
    )

    results = [
        discover_pyright(
            scope,
            state_root=state_root,
            candidates=PyrightCandidates((server,), (), ()),
        )
        for _index in range(3)
    ]

    assert all(result.status == "qualified" for result in results)
    assert len(processes) == 3
    assert all(process.returncode == 0 for process in processes)
    assert all(tree.closed for tree in trees)
    assert all(process.stdin.closed for process in processes)
    assert all(process.stdout.closed for process in processes)
    assert all(process.stderr.closed for process in processes)


def test_repeated_timed_out_real_node_probes_are_reaped_before_return(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _node, processes, trees = _install_real_node_program(
        monkeypatch,
        tmp_path,
        "import time; time.sleep(30)",
    )
    monkeypatch.setattr(pyright_profile, "NODE_PROBE_TIMEOUT_SECONDS", 0.05)

    try:
        results = [pyright_profile._probe_node(None) for _index in range(3)]
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)

    assert len(processes) == 3
    assert all(process.poll() is not None for process in processes)
    assert all(tree.process_group is None and tree.windows_job is None for tree in trees)
    assert all(result[3] == {"pyright_node_probe_timeout"} for result in results)


def test_node_probe_cleanup_ownership_is_bounded_before_spawn(
    monkeypatch: pytest.MonkeyPatch,
    repository: Path,
    state_root: Path,
    tmp_path: Path,
) -> None:
    scope = _scope(repository)
    server = create_pyright_fixture(repository)
    node = tmp_path / ("node.exe" if os.name == "nt" else "node")
    node.write_bytes(b"synthetic node executable\n")
    processes: list[_FakeNodeProcess] = []
    trees: list[_FakeNodeTree] = []

    def spawn_tree(*args: object, **kwargs: object) -> _FakeNodeTree:
        process = _FakeNodeProcess(b"", times_out=True, persistent_timeout=True)
        tree = _FakeNodeTree(process)
        process.tree = tree
        processes.append(process)
        trees.append(tree)
        return tree

    monkeypatch.setattr(pyright_profile.shutil, "which", lambda *args, **kwargs: str(node))
    monkeypatch.setattr(
        pyright_profile,
        "ProcessTree",
        SimpleNamespace(spawn_with_deadline=spawn_tree),
    )

    try:
        results = [
            discover_pyright(
                scope,
                state_root=state_root,
                candidates=PyrightCandidates((server,), (), ()),
            )
            for _index in range(pyright_profile._MAX_NODE_PROBE_OWNERS + 1)
        ]

        assert len(processes) == pyright_profile._MAX_NODE_PROBE_OWNERS
        assert pyright_profile._pending_node_probe_cleanup_snapshot() == tuple(trees)
        assert all("pyright_node_probe_failed" in result.degradation_codes for result in results)
    finally:
        for process in processes:
            process._persistent_timeout = False
        pyright_profile._retry_node_probe_cleanups()

    assert all(process.returncode == -9 for process in processes)
    assert pyright_profile._pending_node_probe_cleanup_snapshot() == ()


def test_node_probe_cleanup_is_registered_for_normal_interpreter_exit(
    tmp_path: Path,
) -> None:
    marker = tmp_path / "node-probe-reaped.txt"
    scripts = Path(pyright_profile.__file__).resolve().parent
    environment = os.environ.copy()
    environment["PYTHONPATH"] = os.pathsep.join(
        value for value in (str(scripts), environment.get("PYTHONPATH", "")) if value
    )
    program = f"""
from pathlib import Path
import pyright_profile

class Process:
    def __init__(self):
        self.returncode = None
        self.stdin = Stream()
        self.stdout = Stream()
        self.stderr = Stream()

class Stream:
    closed = False

    def close(self):
        self.closed = True

class Tree:
    def __init__(self):
        self.process = Process()

    def terminate(self, *, deadline):
        Path({str(marker)!r}).write_text("reaped", encoding="utf-8")
        self.process.returncode = -9

    def close(self):
        pass

owner = pyright_profile._reserve_node_probe_owner()
assert owner is not None
pyright_profile._retain_node_probe_owner(owner, Tree())
"""

    completed = subprocess.run(
        [sys.executable, "-c", program],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert marker.read_text(encoding="utf-8") == "reaped"


@pytest.mark.parametrize("error", [KeyboardInterrupt(), SystemExit(2)])
def test_node_probe_does_not_swallow_process_control_exceptions(
    monkeypatch: pytest.MonkeyPatch,
    repository: Path,
    state_root: Path,
    tmp_path: Path,
    error: BaseException,
) -> None:
    scope = _scope(repository)
    server = create_pyright_fixture(repository)
    _install_node_probe(monkeypatch, tmp_path)

    def interrupt(*args: object, **kwargs: object) -> object:
        raise error

    monkeypatch.setattr(pyright_profile.ProcessTree, "spawn_with_deadline", interrupt)

    with pytest.raises(type(error)):
        discover_pyright(
            scope,
            state_root=state_root,
            candidates=PyrightCandidates((server,), (), ()),
        )


def test_node_major_mismatch_preserves_full_version(
    monkeypatch: pytest.MonkeyPatch,
    repository: Path,
    state_root: Path,
    tmp_path: Path,
) -> None:
    scope = _scope(repository)
    server = create_pyright_fixture(repository)
    _install_node_probe(monkeypatch, tmp_path, output=b"v20.20.2\n")

    result = discover_pyright(
        scope,
        state_root=state_root,
        candidates=PyrightCandidates((server,), (), ()),
    )

    assert result.node_version == "v20.20.2"
    assert result.node_major == 20
    assert result.qualified is False
    assert "pyright_node_major_mismatch" in result.degradation_codes


def test_node_probe_is_shell_free_credentials_reduced_bounded_and_records_full_version(
    monkeypatch: pytest.MonkeyPatch,
    repository: Path,
    state_root: Path,
    tmp_path: Path,
) -> None:
    scope = _scope(repository)
    server = create_pyright_fixture(repository)
    monkeypatch.setenv("PYRIGHT_TEST_SECRET", "must-not-leak")
    node, calls, process = _install_node_probe(
        monkeypatch, tmp_path, output=b"v22.23.1\n"
    )

    result = discover_pyright(
        scope,
        state_root=state_root,
        candidates=PyrightCandidates((server,), (), ()),
        deadline=time.monotonic() + 5,
    )

    assert result.node_executable == node
    assert result.node_version == "v22.23.1"
    assert result.node_major == 22
    assert len(calls) == 1
    command, options = calls[0]
    assert command == (str(node), "--version")
    assert options["cwd"] == node.parent
    assert isinstance(options["deadline"], float)
    assert "PYRIGHT_TEST_SECRET" not in options["env"]
    assert process.wait_timeouts
    assert 0 < process.wait_timeouts[0] <= pyright_profile.NODE_PROBE_TIMEOUT_SECONDS
    assert process.stdin.closed
    assert process.stdout.closed
    assert process.stderr.closed
