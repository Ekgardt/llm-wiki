"""Pinned, read-only Pyright runtime discovery contracts."""

from __future__ import annotations

import builtins
import dataclasses
import io
import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import lsp_paths
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
    ) -> None:
        self.stdout = io.BytesIO(output)
        self.returncode: int | None = None
        self._final_returncode = returncode
        self._times_out = times_out
        self.killed = False
        self.wait_timeouts: list[float] = []

    def wait(self, timeout: float | None = None) -> int:
        if timeout is not None:
            self.wait_timeouts.append(timeout)
        if self._times_out and not self.killed:
            raise subprocess.TimeoutExpired(("node", "--version"), timeout)
        self.returncode = -9 if self.killed else self._final_returncode
        return self.returncode

    def kill(self) -> None:
        self.killed = True


def _install_node_probe(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    output: bytes = b"v22.23.1\n",
    returncode: int = 0,
    times_out: bool = False,
) -> tuple[Path, list[tuple[tuple[str, ...], dict[str, object]]], _FakeNodeProcess]:
    node = tmp_path / ("node.exe" if os.name == "nt" else "node")
    calls: list[tuple[tuple[str, ...], dict[str, object]]] = []
    process = _FakeNodeProcess(output, returncode=returncode, times_out=times_out)

    def which(name: str, *, path: str | None = None) -> str | None:
        assert name == "node"
        assert path is not None
        return str(node)

    def popen(command: list[str], **kwargs: object) -> _FakeNodeProcess:
        calls.append((tuple(command), dict(kwargs)))
        return process

    monkeypatch.setattr(pyright_profile.shutil, "which", which)
    monkeypatch.setattr(pyright_profile.subprocess, "Popen", popen)
    return node, calls, process


def _scope(repository: Path):
    return resolve_repository_scope(repository)


def _empty_candidates() -> PyrightCandidates:
    return PyrightCandidates(project_local=(), managed=(), system=())


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
    assert PYRIGHT_CONFIGURATION == {
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
    assert PYRIGHT_INITIALIZATION_OPTIONS == {"files": {"exclude": []}}
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
        "configuration_sha256": sha256_bytes(canonical_json_bytes(PYRIGHT_CONFIGURATION)),
        "initialization_options_sha256": sha256_bytes(
            canonical_json_bytes(PYRIGHT_INITIALIZATION_OPTIONS)
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


def test_managed_candidate_requires_canonical_receipt_and_recomputed_digest(
    monkeypatch: pytest.MonkeyPatch,
    repository: Path,
    state_root: Path,
    tmp_path: Path,
) -> None:
    scope = _scope(repository)
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
            canonical_json_bytes(PYRIGHT_INITIALIZATION_OPTIONS)
        ),
        configuration_sha256=sha256_bytes(canonical_json_bytes(PYRIGHT_CONFIGURATION)),
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


def test_symlinked_package_metadata_is_rejected_without_fallthrough(
    monkeypatch: pytest.MonkeyPatch,
    repository: Path,
    state_root: Path,
    tmp_path: Path,
) -> None:
    scope = _scope(repository)
    server = create_pyright_fixture(repository)
    package_json = server.with_name("package.json")
    target = tmp_path / "outside-package.json"
    target.write_text('{"name":"pyright","version":"1.1.411"}', encoding="utf-8")
    package_json.unlink()
    try:
        package_json.symlink_to(target)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable: {exc}")
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


def test_node_timeout_is_a_stable_degradation_and_kills_probe(
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

    result = discover_pyright(
        scope,
        state_root=state_root,
        candidates=PyrightCandidates((server,), (), ()),
    )

    assert process.killed is True
    assert result.status == "degraded"
    assert "pyright_node_probe_timeout" in result.degradation_codes


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
    assert options["shell"] is False
    assert options["stdin"] is subprocess.DEVNULL
    assert options["stdout"] is subprocess.PIPE
    assert options["stderr"] is subprocess.DEVNULL
    assert "PYRIGHT_TEST_SECRET" not in options["env"]
    assert process.wait_timeouts
    assert 0 < process.wait_timeouts[0] <= pyright_profile.NODE_PROBE_TIMEOUT_SECONDS
