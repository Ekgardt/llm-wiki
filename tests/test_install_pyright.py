"""Explicit, bounded installation of the pinned managed Pyright artifact."""

from __future__ import annotations

import ast
import concurrent.futures
import dataclasses
import io
import json
import os
import shutil
import stat
import subprocess
import tarfile
import time
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import install_pyright as installer_module
import pyright_profile
import pytest
from install_pyright import InstalledPyright, PyrightInstallError, install_pyright
from pyright_profile import (
    PYRIGHT_PACKAGE_INTEGRITY,
    PYRIGHT_PACKAGE_SHA256,
    PYRIGHT_PACKAGE_URL,
    PYRIGHT_VERSION,
    build_pyright_install_manifest,
)
from reliable_memory import canonical_json_bytes, sha256_bytes

from tests.code_kernel_helpers import (
    PyrightInstallArtifactFixture,
    PyrightTarEntry,
    create_pyright_install_artifact,
    use_pyright_install_artifact_identity,
)


def _artifact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    **kwargs: object,
) -> PyrightInstallArtifactFixture:
    artifact = create_pyright_install_artifact(tmp_path / "pyright.tgz", **kwargs)
    use_pyright_install_artifact_identity(monkeypatch, artifact)
    return artifact


def _root(state_root: Path) -> Path:
    return state_root / "cache/code-tools/pyright/1.1.411"


def _installer_entries(state_root: Path) -> tuple[str, ...]:
    parent = state_root / "cache/code-tools/pyright"
    if not parent.exists():
        return ()
    return tuple(sorted(path.name for path in parent.iterdir()))


def _assert_no_owned_scratch(state_root: Path) -> None:
    assert all(not name.startswith(".install-pyright-") for name in _installer_entries(state_root))


def _error_code(error: pytest.ExceptionInfo[PyrightInstallError]) -> str:
    return error.value.code


def _windows_process_handle_count() -> int:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.GetProcessHandleCount.argtypes = (
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.DWORD),
    )
    kernel32.GetProcessHandleCount.restype = wintypes.BOOL
    count = wintypes.DWORD()
    if not kernel32.GetProcessHandleCount(
        kernel32.GetCurrentProcess(), ctypes.byref(count)
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    return int(count.value)


def test_installed_pyright_public_shape_is_exact() -> None:
    assert [field.name for field in dataclasses.fields(InstalledPyright)] == [
        "root",
        "version",
        "package_sha256",
        "package_integrity",
        "server_sha256",
        "manifest_sha256",
    ]
    assert InstalledPyright.__slots__ == tuple(
        field.name for field in dataclasses.fields(InstalledPyright)
    )
    assert not hasattr(object.__new__(InstalledPyright), "__dict__")


def test_successful_local_install_is_exact_and_canonical(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_root = tmp_path / "state"
    artifact = _artifact(tmp_path, monkeypatch)
    monkeypatch.setattr(
        installer_module,
        "_open_pinned_url",
        lambda *args, **kwargs: pytest.fail("network used for a local artifact"),
    )

    result = install_pyright(state_root=state_root, artifact=artifact.path)

    root = _root(state_root)
    server_sha256 = sha256_bytes(artifact.server_bytes)
    expected_manifest = build_pyright_install_manifest(server_sha256=server_sha256)
    manifest_bytes = canonical_json_bytes(expected_manifest)
    assert result == InstalledPyright(
        root=root,
        version=PYRIGHT_VERSION,
        package_sha256=artifact.package_sha256,
        package_integrity=artifact.package_integrity,
        server_sha256=server_sha256,
        manifest_sha256=sha256_bytes(manifest_bytes),
    )
    assert (root / "package/package.json").read_bytes() == canonical_json_bytes(
        {"name": "pyright", "version": "1.1.411"}
    )
    assert (root / "package/langserver.index.js").read_bytes() == artifact.server_bytes
    assert (root / "install-manifest.json").read_bytes() == manifest_bytes
    assert json.loads(manifest_bytes) == expected_manifest
    _assert_no_owned_scratch(state_root)


@pytest.mark.parametrize(
    ("field", "expected_code"),
    [
        ("sha256", "pyright_package_sha256_mismatch"),
        ("integrity", "pyright_package_integrity_mismatch"),
    ],
)
def test_digest_mismatch_precedes_tar_open_and_stage_creation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    expected_code: str,
) -> None:
    state_root = tmp_path / "state"
    artifact = _artifact(tmp_path, monkeypatch)
    if field == "sha256":
        monkeypatch.setattr(pyright_profile, "PYRIGHT_PACKAGE_SHA256", "0" * 64)
    else:
        monkeypatch.setattr(pyright_profile, "PYRIGHT_PACKAGE_INTEGRITY", "sha512-" + "A" * 88)
    monkeypatch.setattr(
        installer_module.tarfile,
        "open",
        lambda *args, **kwargs: pytest.fail("tar opened before both digests matched"),
    )

    with pytest.raises(PyrightInstallError) as error:
        install_pyright(state_root=state_root, artifact=artifact.path)

    assert _error_code(error) == expected_code
    assert not _root(state_root).exists()
    _assert_no_owned_scratch(state_root)


def test_local_artifact_never_opens_the_network(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact = _artifact(tmp_path, monkeypatch)
    monkeypatch.setattr(
        installer_module,
        "_open_pinned_url",
        lambda *args, **kwargs: pytest.fail("network used"),
    )

    install_pyright(state_root=tmp_path / "state", artifact=artifact.path)


def test_local_artifact_replacement_during_copy_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_root = tmp_path / "state"
    artifact = _artifact(tmp_path, monkeypatch)
    replacement = tmp_path / "replacement.tgz"
    replacement.write_bytes(artifact.path.read_bytes())
    real_copy = installer_module._copy_to_owned_file

    def replace_after_copy(*args: object, **kwargs: object):
        result = real_copy(*args, **kwargs)
        os.replace(replacement, artifact.path)
        return result

    monkeypatch.setattr(installer_module, "_copy_to_owned_file", replace_after_copy)

    with pytest.raises(PyrightInstallError) as error:
        install_pyright(state_root=state_root, artifact=artifact.path)

    assert _error_code(error) == "pyright_artifact_unsafe"
    assert not _root(state_root).exists()
    _assert_no_owned_scratch(state_root)


@pytest.mark.skipif(os.name != "nt", reason="Windows sharing contract")
def test_windows_local_artifact_copy_denies_writers_and_deleters(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_root = tmp_path / "state"
    artifact = _artifact(tmp_path, monkeypatch)
    moved = tmp_path / "temporarily-moved.tgz"
    real_copy = installer_module._copy_to_owned_file
    denied: list[str] = []

    def probe_exclusive_source(*args: object, **kwargs: object):
        try:
            with artifact.path.open("r+b"):
                pass
        except PermissionError:
            denied.append("write")
        try:
            artifact.path.rename(moved)
        except PermissionError:
            denied.append("delete")
        else:
            moved.rename(artifact.path)
        return real_copy(*args, **kwargs)

    monkeypatch.setattr(installer_module, "_copy_to_owned_file", probe_exclusive_source)

    install_pyright(state_root=state_root, artifact=artifact.path)

    assert denied == ["write", "delete"]


@pytest.mark.skipif(os.name != "posix", reason="POSIX ctime contract")
def test_posix_local_artifact_mutate_and_restore_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_root = tmp_path / "state"
    artifact = _artifact(tmp_path, monkeypatch)
    original = artifact.path.read_bytes()
    before = artifact.path.stat()
    real_copy = installer_module._copy_to_owned_file

    def mutate_and_restore(*args: object, **kwargs: object):
        result = real_copy(*args, **kwargs)
        changed = bytes([original[0] ^ 1]) + original[1:]
        with artifact.path.open("r+b") as stream:
            stream.write(changed)
            stream.flush()
            os.fsync(stream.fileno())
            stream.seek(0)
            stream.write(original)
            stream.flush()
            os.fsync(stream.fileno())
        os.utime(
            artifact.path,
            ns=(before.st_atime_ns, before.st_mtime_ns),
        )
        assert artifact.path.stat().st_ctime_ns != before.st_ctime_ns
        return result

    monkeypatch.setattr(installer_module, "_copy_to_owned_file", mutate_and_restore)

    with pytest.raises(PyrightInstallError) as error:
        install_pyright(state_root=state_root, artifact=artifact.path)

    assert _error_code(error) == "pyright_artifact_unsafe"
    assert not _root(state_root).exists()


def test_local_artifact_on_known_network_path_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_root = tmp_path / "state"
    artifact = _artifact(tmp_path, monkeypatch)
    real_local_check = installer_module._require_local_filesystem

    def reject_artifact(path: Path, deadline: float) -> None:
        if path == artifact.path:
            raise PermissionError("simulated network artifact")
        real_local_check(path, deadline)

    monkeypatch.setattr(installer_module, "_require_local_filesystem", reject_artifact)

    with pytest.raises(PyrightInstallError) as error:
        install_pyright(state_root=state_root, artifact=artifact.path)

    assert _error_code(error) == "pyright_artifact_unsafe"
    assert not _root(state_root).exists()
    _assert_no_owned_scratch(state_root)


class _Response(io.BytesIO):
    def __init__(
        self,
        content: bytes,
        *,
        url: str = PYRIGHT_PACKAGE_URL,
        status: int = 200,
    ) -> None:
        super().__init__(content)
        self.status = status
        self._url = url
        self.headers: dict[str, str] = {"Content-Length": str(len(content))}

    def geturl(self) -> str:
        return self._url

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()


def test_download_opens_only_exact_pinned_url_without_credentials(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact = _artifact(tmp_path, monkeypatch)
    content = artifact.path.read_bytes()
    calls: list[tuple[urllib.request.Request, float]] = []

    def open_url(request: urllib.request.Request, *, timeout: float) -> _Response:
        calls.append((request, timeout))
        return _Response(content)

    monkeypatch.setattr(installer_module, "_open_pinned_url", open_url)

    install_pyright(state_root=tmp_path / "state")

    assert len(calls) == 1
    request, timeout = calls[0]
    assert request.full_url == PYRIGHT_PACKAGE_URL
    assert request.get_method() == "GET"
    assert request.data is None
    assert "Authorization" not in dict(request.header_items())
    assert 0 < timeout <= installer_module.NETWORK_TIMEOUT_SECONDS


@pytest.mark.parametrize(
    ("url", "status", "expected_code"),
    [
        ("https://registry.npmjs.org/redirected.tgz", 200, "pyright_download_url_drift"),
        (PYRIGHT_PACKAGE_URL, 500, "pyright_download_status"),
    ],
)
def test_download_rejects_redirect_or_non_success_before_reading_body(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    url: str,
    status: int,
    expected_code: str,
) -> None:
    state_root = tmp_path / "state"

    class UnreadableResponse(_Response):
        def read(self, size: int = -1) -> bytes:
            pytest.fail("invalid response body was read")

    monkeypatch.setattr(
        installer_module,
        "_open_pinned_url",
        lambda *args, **kwargs: UnreadableResponse(b"ignored", url=url, status=status),
    )

    with pytest.raises(PyrightInstallError) as error:
        install_pyright(state_root=state_root)

    assert _error_code(error) == expected_code
    assert not _root(state_root).exists()
    _assert_no_owned_scratch(state_root)


def test_compressed_limit_is_enforced_while_copying(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_root = tmp_path / "state"
    artifact = _artifact(tmp_path, monkeypatch, server_bytes=os.urandom(4096))
    monkeypatch.setattr(installer_module, "MAX_COMPRESSED_BYTES", 256)

    with pytest.raises(PyrightInstallError) as error:
        install_pyright(state_root=state_root, artifact=artifact.path)

    assert _error_code(error) == "pyright_archive_compressed_limit"
    assert not _root(state_root).exists()
    _assert_no_owned_scratch(state_root)


def test_decompressed_tar_limit_includes_metadata_before_members(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_root = tmp_path / "state"
    artifact = _artifact(tmp_path, monkeypatch)
    monkeypatch.setattr(installer_module, "MAX_DECOMPRESSED_BYTES", 511)

    with pytest.raises(PyrightInstallError) as error:
        install_pyright(state_root=state_root, artifact=artifact.path)

    assert _error_code(error) == "pyright_archive_decompressed_limit"
    assert not _root(state_root).exists()
    _assert_no_owned_scratch(state_root)


def test_decompressed_limit_drains_appended_concatenated_gzip_member(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_root = tmp_path / "state"
    artifact = create_pyright_install_artifact(tmp_path / "pyright.tgz")
    limit = 128 * 1024
    with artifact.path.open("ab") as stream:
        stream.write(installer_module.gzip.compress(b"x" * limit, mtime=0))
    content = artifact.path.read_bytes()
    artifact = dataclasses.replace(
        artifact,
        package_sha256=installer_module.hashlib.sha256(content).hexdigest(),
        package_integrity="sha512-"
        + installer_module.base64.b64encode(
            installer_module.hashlib.sha512(content).digest()
        ).decode("ascii"),
    )
    use_pyright_install_artifact_identity(monkeypatch, artifact)
    monkeypatch.setattr(installer_module, "MAX_DECOMPRESSED_BYTES", limit)

    with pytest.raises(PyrightInstallError) as error:
        install_pyright(state_root=state_root, artifact=artifact.path)

    assert _error_code(error) == "pyright_archive_decompressed_limit"
    assert not _root(state_root).exists()
    _assert_no_owned_scratch(state_root)


@pytest.mark.parametrize(
    ("limit_name", "limit", "expected_code"),
    [
        ("MAX_MEMBER_BYTES", 8, "pyright_archive_member_limit"),
        ("MAX_TOTAL_FILE_BYTES", 16, "pyright_archive_aggregate_limit"),
        ("MAX_MEMBERS", 2, "pyright_archive_member_count_limit"),
    ],
)
def test_member_aggregate_and_count_limits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    limit_name: str,
    limit: int,
    expected_code: str,
) -> None:
    state_root = tmp_path / "state"
    artifact = _artifact(tmp_path, monkeypatch)
    monkeypatch.setattr(installer_module, limit_name, limit)

    with pytest.raises(PyrightInstallError) as error:
        install_pyright(state_root=state_root, artifact=artifact.path)

    assert _error_code(error) == expected_code
    assert not _root(state_root).exists()
    _assert_no_owned_scratch(state_root)


@pytest.mark.parametrize(
    "name",
    [
        "../outside",
        "/package/outside",
        "C:/package/outside",
        "//server/share/package/outside",
        "package\\outside",
        "package/./outside",
        "package//outside",
        "package/nul\x00tail",
        "package/control\x01name",
        "package/not-normalized-e\u0301",
        "other/file.js",
        "package/CON",
        "package/stream:name",
    ],
    ids=(
        "traversal",
        "absolute",
        "drive",
        "unc",
        "backslash",
        "dot",
        "empty-component",
        "nul",
        "control",
        "unicode-not-nfc",
        "wrong-prefix",
        "device-name",
        "alternate-data-stream",
    ),
)
def test_unsafe_member_paths_are_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
) -> None:
    entries = (
        PyrightTarEntry("package", kind=tarfile.DIRTYPE),
        PyrightTarEntry(name, b"unsafe"),
    )
    artifact = _artifact(tmp_path, monkeypatch, entries=entries)

    with pytest.raises(PyrightInstallError) as error:
        install_pyright(state_root=tmp_path / "state", artifact=artifact.path)

    assert _error_code(error) == "pyright_archive_path_unsafe"
    assert not _root(tmp_path / "state").exists()
    _assert_no_owned_scratch(tmp_path / "state")


@pytest.mark.parametrize(
    "entry",
    [
        PyrightTarEntry("package/link", kind=tarfile.SYMTYPE, linkname="target"),
        PyrightTarEntry("package/hard", kind=tarfile.LNKTYPE, linkname="package/target"),
        PyrightTarEntry("package/char", kind=tarfile.CHRTYPE),
        PyrightTarEntry("package/block", kind=tarfile.BLKTYPE),
        PyrightTarEntry("package/fifo", kind=tarfile.FIFOTYPE),
        PyrightTarEntry("package/sparse", kind=tarfile.GNUTYPE_SPARSE),
        PyrightTarEntry("package/socket", kind=b"s"),
    ],
    ids=("symlink", "hardlink", "char-device", "block-device", "fifo", "sparse", "unknown"),
)
def test_non_regular_tar_member_types_are_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    entry: PyrightTarEntry,
) -> None:
    artifact = _artifact(
        tmp_path,
        monkeypatch,
        entries=(PyrightTarEntry("package", kind=tarfile.DIRTYPE), entry),
        tar_format=tarfile.GNU_FORMAT,
    )

    with pytest.raises(PyrightInstallError) as error:
        install_pyright(state_root=tmp_path / "state", artifact=artifact.path)

    assert _error_code(error) == "pyright_archive_member_type"
    assert not _root(tmp_path / "state").exists()
    _assert_no_owned_scratch(tmp_path / "state")


@pytest.mark.parametrize(
    ("entries", "expected_code"),
    [
        (
            (
                PyrightTarEntry("package", kind=tarfile.DIRTYPE),
                PyrightTarEntry("package/file", b"one"),
                PyrightTarEntry("package/file", b"two"),
            ),
            "pyright_archive_duplicate_member",
        ),
        (
            (
                PyrightTarEntry("package", kind=tarfile.DIRTYPE),
                PyrightTarEntry("package/Name", b"one"),
                PyrightTarEntry("package/name", b"two"),
            ),
            "pyright_archive_name_collision",
        ),
        (
            (
                PyrightTarEntry("package", kind=tarfile.DIRTYPE),
                PyrightTarEntry("package/strasse", b"one"),
                PyrightTarEntry("package/stra\u00dfe", b"two"),
            ),
            "pyright_archive_name_collision",
        ),
        (
            (
                PyrightTarEntry("package", kind=tarfile.DIRTYPE),
                PyrightTarEntry("package/parent", b"file"),
                PyrightTarEntry("package/parent/child", b"child"),
            ),
            "pyright_archive_path_conflict",
        ),
        (
            (
                PyrightTarEntry("package", kind=tarfile.DIRTYPE),
                PyrightTarEntry("package/value", kind=tarfile.DIRTYPE),
                PyrightTarEntry("package/value", b"file"),
            ),
            "pyright_archive_path_conflict",
        ),
    ],
    ids=("duplicate", "case", "unicode-casefold", "file-parent", "file-directory"),
)
def test_duplicate_collision_and_file_directory_conflicts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    entries: tuple[PyrightTarEntry, ...],
    expected_code: str,
) -> None:
    artifact = _artifact(tmp_path, monkeypatch, entries=entries)

    with pytest.raises(PyrightInstallError) as error:
        install_pyright(state_root=tmp_path / "state", artifact=artifact.path)

    assert _error_code(error) == expected_code
    assert not _root(tmp_path / "state").exists()
    _assert_no_owned_scratch(tmp_path / "state")


def test_oversized_path_and_pax_metadata_are_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entries = (
        PyrightTarEntry("package", kind=tarfile.DIRTYPE),
        PyrightTarEntry("package/" + "a" * 80, b"value", pax_headers={"comment": "x" * 80}),
    )
    artifact = _artifact(tmp_path, monkeypatch, entries=entries)
    monkeypatch.setattr(installer_module, "MAX_PATH_BYTES", 64)
    with pytest.raises(PyrightInstallError) as path_error:
        install_pyright(state_root=tmp_path / "path-state", artifact=artifact.path)
    assert _error_code(path_error) == "pyright_archive_path_limit"

    monkeypatch.setattr(installer_module, "MAX_PATH_BYTES", 4096)
    monkeypatch.setattr(installer_module, "MAX_PAX_BYTES", 32)
    with pytest.raises(PyrightInstallError) as pax_error:
        install_pyright(state_root=tmp_path / "pax-state", artifact=artifact.path)
    assert _error_code(pax_error) == "pyright_archive_pax_limit"
    _assert_no_owned_scratch(tmp_path / "path-state")
    _assert_no_owned_scratch(tmp_path / "pax-state")


def test_raw_extended_tar_metadata_is_bounded_before_effective_member_validation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entries = (
        PyrightTarEntry("package", kind=tarfile.DIRTYPE),
        PyrightTarEntry(
            "package/package.json",
            canonical_json_bytes({"name": "pyright", "version": "1.1.411"}),
            pax_headers={"comment": "small"},
        ),
        PyrightTarEntry("package/langserver.index.js", b"server"),
    )
    artifact = _artifact(tmp_path, monkeypatch, entries=entries)
    monkeypatch.setattr(installer_module, "MAX_PAX_BYTES", 512)

    with pytest.raises(PyrightInstallError) as error:
        install_pyright(state_root=tmp_path / "state", artifact=artifact.path)

    assert _error_code(error) == "pyright_archive_pax_limit"
    assert not _root(tmp_path / "state").exists()
    _assert_no_owned_scratch(tmp_path / "state")


@pytest.mark.parametrize(
    ("package_bytes", "expected_code"),
    [
        (b"not json", "pyright_package_json_malformed"),
        (b"[]", "pyright_package_json_malformed"),
        (b'{"name":"pyright","name":"other","version":"1.1.411"}', "pyright_package_json_malformed"),
        (canonical_json_bytes({"name": "other", "version": "1.1.411"}), "pyright_package_mismatch"),
        (canonical_json_bytes({"name": "pyright", "version": "1.1.410"}), "pyright_version_mismatch"),
    ],
    ids=("invalid-json", "not-object", "duplicate-key", "wrong-name", "wrong-version"),
)
def test_package_metadata_is_strict_and_pinned(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    package_bytes: bytes,
    expected_code: str,
) -> None:
    artifact = _artifact(tmp_path, monkeypatch, package_bytes=package_bytes)

    with pytest.raises(PyrightInstallError) as error:
        install_pyright(state_root=tmp_path / "state", artifact=artifact.path)

    assert _error_code(error) == expected_code
    assert not _root(tmp_path / "state").exists()
    _assert_no_owned_scratch(tmp_path / "state")


@pytest.mark.parametrize(
    ("include_package", "include_server", "server_bytes", "expected_code"),
    [
        (False, True, b"server", "pyright_package_json_missing"),
        (True, False, b"server", "pyright_server_missing"),
        (True, True, b"", "pyright_server_empty"),
    ],
    ids=("missing-package", "missing-server", "empty-server"),
)
def test_required_package_and_server_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    include_package: bool,
    include_server: bool,
    server_bytes: bytes,
    expected_code: str,
) -> None:
    artifact = _artifact(
        tmp_path,
        monkeypatch,
        include_package=include_package,
        include_server=include_server,
        server_bytes=server_bytes,
    )

    with pytest.raises(PyrightInstallError) as error:
        install_pyright(state_root=tmp_path / "state", artifact=artifact.path)

    assert _error_code(error) == expected_code
    assert not _root(tmp_path / "state").exists()
    _assert_no_owned_scratch(tmp_path / "state")


def test_download_interruption_cleans_only_owned_scratch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_root = tmp_path / "state"
    parent = state_root / "cache/code-tools/pyright"
    parent.mkdir(parents=True)
    unrelated = parent / "keep-me"
    unrelated.write_bytes(b"unrelated")

    class InterruptedResponse(_Response):
        def __init__(self) -> None:
            super().__init__(b"x" * 1024)
            self.calls = 0

        def read(self, size: int = -1) -> bytes:
            self.calls += 1
            if self.calls == 1:
                return b"x" * 64
            raise OSError("download interrupted")

    monkeypatch.setattr(
        installer_module,
        "_open_pinned_url",
        lambda *args, **kwargs: InterruptedResponse(),
    )

    with pytest.raises(PyrightInstallError) as error:
        install_pyright(state_root=state_root)

    assert _error_code(error) == "pyright_download_failed"
    assert unrelated.read_bytes() == b"unrelated"
    assert not _root(state_root).exists()
    _assert_no_owned_scratch(state_root)


def test_lock_write_failure_is_typed_and_cleans_owned_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_root = tmp_path / "state"
    artifact = _artifact(tmp_path, monkeypatch)

    def fail_lock_write(*args: object, **kwargs: object) -> None:
        raise OSError("lock write interrupted")

    monkeypatch.setattr(installer_module, "_write_handle", fail_lock_write)

    with pytest.raises(PyrightInstallError) as error:
        install_pyright(state_root=state_root, artifact=artifact.path)

    assert _error_code(error) == "pyright_install_io_failed"
    assert not _root(state_root).exists()
    _assert_no_owned_scratch(state_root)


@pytest.mark.parametrize(
    ("target", "expected_message", "expected_code"),
    [
        (
            "_copy_member_data",
            "extract interrupted",
            "pyright_archive_extract_failed",
        ),
        ("_fsync_file", "fsync interrupted", "pyright_install_fsync_failed"),
        (
            "_atomic_publish_noreplace",
            "rename interrupted",
            "pyright_publish_failed",
        ),
    ],
    ids=("extract", "fsync", "rename"),
)
def test_extract_fsync_and_rename_interruptions_cleanup_owned_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    target: str,
    expected_message: str,
    expected_code: str,
) -> None:
    state_root = tmp_path / "state"
    artifact = _artifact(tmp_path, monkeypatch)
    parent = state_root / "cache/code-tools/pyright"
    parent.mkdir(parents=True)
    unrelated = parent / "keep-me"
    unrelated.write_bytes(b"unrelated")

    def interrupt(*args: object, **kwargs: object) -> None:
        raise OSError(expected_message)

    if target == "_fsync_file":
        real_fsync = installer_module._fsync_file
        calls = 0

        def interrupt_stage_fsync(*args: object, **kwargs: object) -> None:
            nonlocal calls
            calls += 1
            if calls == 3:
                raise OSError(expected_message)
            real_fsync(*args, **kwargs)

        monkeypatch.setattr(installer_module, target, interrupt_stage_fsync)
    else:
        monkeypatch.setattr(installer_module, target, interrupt)

    with pytest.raises(PyrightInstallError) as error:
        install_pyright(state_root=state_root, artifact=artifact.path)

    assert _error_code(error) == expected_code
    assert unrelated.read_bytes() == b"unrelated"
    assert not _root(state_root).exists()
    _assert_no_owned_scratch(state_root)


def test_stage_directory_fsync_failure_cleans_unpublished_tree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_root = tmp_path / "state"
    artifact = _artifact(tmp_path, monkeypatch)
    real_sync = installer_module._Stage.sync_directories
    real_fsync = installer_module._fsync_directory
    syncing_stage = False

    def sync_directories(self: object, deadline: float) -> None:
        nonlocal syncing_stage
        syncing_stage = True
        try:
            real_sync(self, deadline)
        finally:
            syncing_stage = False

    def fail_stage_directory(handle: object) -> None:
        if syncing_stage:
            raise OSError("stage directory fsync interrupted")
        real_fsync(handle)

    monkeypatch.setattr(installer_module._Stage, "sync_directories", sync_directories)
    monkeypatch.setattr(installer_module, "_fsync_directory", fail_stage_directory)

    with pytest.raises(PyrightInstallError) as error:
        install_pyright(state_root=state_root, artifact=artifact.path)

    assert _error_code(error) == "pyright_install_fsync_failed"
    assert not _root(state_root).exists()
    _assert_no_owned_scratch(state_root)


@pytest.mark.skipif(os.name != "nt", reason="Windows flush contract")
@pytest.mark.parametrize("failed_creation", [1, 2, 3], ids=("cache", "code-tools", "pyright"))
def test_windows_runtime_parent_creation_requires_successful_directory_flush(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failed_creation: int,
) -> None:
    state_root = tmp_path / "state"
    artifact = _artifact(tmp_path, monkeypatch)
    calls = 0

    def fail_selected_creation(handle: int) -> bool:
        nonlocal calls
        calls += 1
        return calls != failed_creation

    monkeypatch.setattr(
        installer_module._windows_workspace,
        "flush_directory",
        fail_selected_creation,
    )

    with pytest.raises(PyrightInstallError) as error:
        install_pyright(state_root=state_root, artifact=artifact.path)

    assert _error_code(error) == "pyright_install_fsync_failed"
    assert calls == failed_creation
    assert not _root(state_root).exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows handle ownership contract")
def test_windows_parent_flush_failures_release_handles_for_repeated_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    baseline = _windows_process_handle_count()

    for failed_creation in (1, 2, 3):
        for attempt in range(24):
            state_root = tmp_path / f"level-{failed_creation}-{attempt}" / "state"
            calls = 0

            def fail_selected_creation(handle: int) -> bool:
                nonlocal calls
                calls += 1
                return calls != failed_creation

            with monkeypatch.context() as scoped:
                scoped.setattr(
                    installer_module._windows_workspace,
                    "flush_directory",
                    fail_selected_creation,
                )
                with pytest.raises(PyrightInstallError) as error:
                    install_pyright(state_root=state_root)

            assert _error_code(error) == "pyright_install_fsync_failed"
            assert calls == failed_creation
            shutil.rmtree(state_root / "cache")
            state_root.rmdir()
            state_root.parent.rmdir()

    assert _windows_process_handle_count() <= baseline + 2


@pytest.mark.skipif(os.name != "nt", reason="Windows handle ownership contract")
def test_windows_parent_flush_primary_error_retains_close_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_root = tmp_path / "state"
    real_close = installer_module._windows_workspace.close_handle
    close_calls: dict[int, int] = {}
    flush_failed = False
    faulted = False

    def close_then_fail_once(handle: int) -> None:
        nonlocal faulted
        if flush_failed:
            close_calls[handle] = close_calls.get(handle, 0) + 1
        real_close(handle)
        if flush_failed and not faulted:
            faulted = True
            raise OSError("injected close failure")

    def fail_flush(handle: int) -> bool:
        nonlocal flush_failed
        flush_failed = True
        return False

    monkeypatch.setattr(
        installer_module._windows_workspace,
        "flush_directory",
        fail_flush,
    )
    monkeypatch.setattr(
        installer_module._windows_workspace,
        "close_handle",
        close_then_fail_once,
    )

    with pytest.raises(PyrightInstallError) as error:
        install_pyright(state_root=state_root)

    assert _error_code(error) == "pyright_install_fsync_failed"
    assert isinstance(error.value.__cause__, OSError)
    assert str(error.value.__cause__) == "injected close failure"
    assert isinstance(error.value.__cause__.__cause__, OSError)
    assert str(error.value.__cause__.__cause__) == "directory flush failed"
    assert faulted is True
    assert len(close_calls) == 2
    assert set(close_calls.values()) == {1}
    shutil.rmtree(state_root / "cache")

    state_root = tmp_path / "close-transition-state"
    close_calls.clear()
    flush_failed = False
    faulted = False

    def successful_flush(handle: int) -> bool:
        nonlocal flush_failed
        flush_failed = True
        return True

    monkeypatch.setattr(
        installer_module._windows_workspace,
        "flush_directory",
        successful_flush,
    )

    with pytest.raises(PyrightInstallError) as transition_error:
        install_pyright(state_root=state_root)

    assert _error_code(transition_error) == "pyright_state_root_unsafe"
    assert isinstance(transition_error.value.__cause__, OSError)
    assert str(transition_error.value.__cause__) == "injected close failure"
    assert faulted is True
    assert len(close_calls) == 2
    assert set(close_calls.values()) == {1}
    shutil.rmtree(state_root / "cache")


@pytest.mark.skipif(os.name != "nt", reason="Windows flush contract")
def test_windows_stage_flush_false_fails_before_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_root = tmp_path / "state"
    artifact = _artifact(tmp_path, monkeypatch)
    real_sync = installer_module._Stage.sync_directories
    real_flush = installer_module._windows_workspace.flush_directory
    syncing_stage = False

    def sync_directories(self: object, deadline: float) -> None:
        nonlocal syncing_stage
        syncing_stage = True
        try:
            real_sync(self, deadline)
        finally:
            syncing_stage = False

    def fail_stage_flush(handle: int) -> bool:
        return False if syncing_stage else real_flush(handle)

    monkeypatch.setattr(installer_module._Stage, "sync_directories", sync_directories)
    monkeypatch.setattr(
        installer_module._windows_workspace,
        "flush_directory",
        fail_stage_flush,
    )

    with pytest.raises(PyrightInstallError) as error:
        install_pyright(state_root=state_root, artifact=artifact.path)

    assert _error_code(error) == "pyright_install_fsync_failed"
    assert not _root(state_root).exists()
    _assert_no_owned_scratch(state_root)


@pytest.mark.skipif(os.name != "nt", reason="Windows flush contract")
def test_windows_publication_parent_flush_false_keeps_complete_tree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_root = tmp_path / "state"
    artifact = _artifact(tmp_path, monkeypatch)
    real_publish = installer_module._atomic_publish_noreplace
    published = False

    def publish(*args: object, **kwargs: object) -> None:
        nonlocal published
        real_publish(*args, **kwargs)
        published = True

    def fail_published_parent(handle: int) -> bool:
        return not published

    monkeypatch.setattr(installer_module, "_atomic_publish_noreplace", publish)
    monkeypatch.setattr(
        installer_module._windows_workspace,
        "flush_directory",
        fail_published_parent,
    )

    with pytest.raises(PyrightInstallError) as error:
        install_pyright(state_root=state_root, artifact=artifact.path)

    assert _error_code(error) == "pyright_install_fsync_failed"
    assert published is True
    assert (_root(state_root) / "install-manifest.json").is_file()
    _assert_no_owned_scratch(state_root)


def test_parent_fsync_failure_after_publish_keeps_complete_final_tree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_root = tmp_path / "state"
    artifact = _artifact(tmp_path, monkeypatch)
    real_publish = installer_module._atomic_publish_noreplace
    real_fsync = installer_module._fsync_directory
    published = False

    def publish(*args: object, **kwargs: object) -> None:
        nonlocal published
        real_publish(*args, **kwargs)
        published = True

    def fail_parent_after_publish(handle: object) -> None:
        if published:
            raise OSError("parent fsync interrupted")
        real_fsync(handle)

    monkeypatch.setattr(installer_module, "_atomic_publish_noreplace", publish)
    monkeypatch.setattr(installer_module, "_fsync_directory", fail_parent_after_publish)

    with pytest.raises(PyrightInstallError) as error:
        install_pyright(state_root=state_root, artifact=artifact.path)

    assert _error_code(error) == "pyright_install_fsync_failed"
    assert (_root(state_root) / "install-manifest.json").is_file()
    artifact.path.unlink()
    assert install_pyright(
        state_root=state_root,
        artifact=tmp_path / "missing.tgz",
    ).root == _root(state_root)
    _assert_no_owned_scratch(state_root)


@pytest.mark.parametrize("valid", [True, False], ids=("valid", "invalid"))
def test_target_appearing_at_publish_is_validated_and_never_overwritten(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    valid: bool,
) -> None:
    state_root = tmp_path / "state"
    artifact = _artifact(tmp_path, monkeypatch)

    def lose_publish_race(*args: object, **kwargs: object) -> None:
        root = _root(state_root)
        if valid:
            from tests.code_kernel_helpers import create_pyright_fixture

            create_pyright_fixture(
                root,
                managed=True,
                server_bytes=artifact.server_bytes,
            )
        else:
            root.mkdir()
        raise FileExistsError("simulated publication race")

    monkeypatch.setattr(
        installer_module,
        "_atomic_publish_noreplace",
        lose_publish_race,
    )

    if valid:
        result = install_pyright(state_root=state_root, artifact=artifact.path)
        assert result.root == _root(state_root)
        assert result.server_sha256 == sha256_bytes(artifact.server_bytes)
    else:
        with pytest.raises(PyrightInstallError) as error:
            install_pyright(state_root=state_root, artifact=artifact.path)
        assert _error_code(error) == "pyright_existing_install_invalid"
        assert not any(_root(state_root).iterdir())
    _assert_no_owned_scratch(state_root)


def test_valid_existing_install_is_idempotent_before_artifact_or_network_access(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_root = tmp_path / "state"
    artifact = _artifact(tmp_path, monkeypatch)
    expected = install_pyright(state_root=state_root, artifact=artifact.path)
    artifact.path.unlink()
    inaccessible = tmp_path / "missing.tgz"
    monkeypatch.setattr(
        installer_module,
        "_open_pinned_url",
        lambda *args, **kwargs: pytest.fail("network used for idempotent install"),
    )

    observed = install_pyright(state_root=state_root, artifact=inaccessible)

    assert observed == expected
    _assert_no_owned_scratch(state_root)


def test_existing_install_root_swap_before_return_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_root = tmp_path / "state"
    artifact = _artifact(tmp_path, monkeypatch)
    install_pyright(state_root=state_root, artifact=artifact.path)
    root = _root(state_root)
    displaced = root.with_name("validated-root")
    real_sha256 = installer_module.hashlib.sha256
    attempted = False
    swapped = False

    def swap_after_validation(content: bytes = b""):
        nonlocal attempted, swapped
        if not swapped and content == artifact.server_bytes:
            attempted = True
            root.rename(displaced)
            root.mkdir()
            swapped = True
        return real_sha256(content)

    monkeypatch.setattr(installer_module.hashlib, "sha256", swap_after_validation)

    with pytest.raises(PyrightInstallError) as error:
        install_pyright(state_root=state_root, artifact=tmp_path / "missing.tgz")

    assert attempted is True
    assert _error_code(error) == "pyright_existing_install_invalid"
    if swapped:
        assert not any(root.iterdir())


def test_existing_install_appearing_after_lock_is_revalidated_before_return(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.code_kernel_helpers import create_pyright_fixture

    state_root = tmp_path / "state"
    artifact = _artifact(tmp_path, monkeypatch)
    real_try_create_lock = installer_module._try_create_lock

    def acquire_then_publish(parent: object, deadline: float):
        lock = real_try_create_lock(parent, deadline)
        assert lock is not None
        create_pyright_fixture(
            _root(state_root),
            managed=True,
            server_bytes=artifact.server_bytes,
        )
        return lock

    monkeypatch.setattr(installer_module, "_try_create_lock", acquire_then_publish)
    monkeypatch.setattr(
        installer_module,
        "_copy_local_artifact",
        lambda *args, **kwargs: pytest.fail("artifact read after locked idempotent check"),
    )

    result = install_pyright(
        state_root=state_root,
        artifact=tmp_path / "missing.tgz",
    )

    assert result.root == _root(state_root)
    assert result.server_sha256 == sha256_bytes(artifact.server_bytes)


def test_existing_install_with_unsafe_extra_entry_is_refused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_root = tmp_path / "state"
    artifact = _artifact(tmp_path, monkeypatch)
    install_pyright(state_root=state_root, artifact=artifact.path)
    unsafe = _root(state_root) / "package/unsafe-entry"
    unsafe.write_bytes(b"placeholder")

    if os.name == "nt":
        real_list_directory = installer_module._windows_workspace.list_directory

        def list_directory(handle: int, *, max_entries: int):
            return [
                dataclasses.replace(entry, kind="link")
                if entry.name == unsafe.name
                else entry
                for entry in real_list_directory(handle, max_entries=max_entries)
            ]

        monkeypatch.setattr(
            installer_module._windows_workspace,
            "list_directory",
            list_directory,
        )
    else:
        unsafe.unlink()
        unsafe.symlink_to(_root(state_root) / "install-manifest.json")

    with pytest.raises(PyrightInstallError) as error:
        install_pyright(state_root=state_root, artifact=tmp_path / "must-not-be-read")

    assert _error_code(error) == "pyright_existing_install_invalid"


@pytest.mark.parametrize("damage", ["empty", "manifest", "server", "symlink"], ids=str)
def test_existing_invalid_or_unsafe_target_is_never_overwritten(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    damage: str,
) -> None:
    state_root = tmp_path / "state"
    artifact = _artifact(tmp_path, monkeypatch)
    root = _root(state_root)
    if damage == "empty":
        root.mkdir(parents=True)
    else:
        install_pyright(state_root=state_root, artifact=artifact.path)
        if damage == "manifest":
            (root / "install-manifest.json").write_bytes(b"{}")
        elif damage == "server":
            (root / "package/langserver.index.js").write_bytes(b"changed")
        else:
            server = root / "package/langserver.index.js"
            server.unlink()
            try:
                server.symlink_to(root / "install-manifest.json")
            except OSError:
                server.write_bytes(b"simulated reparse point")
                real_list_directory = installer_module._windows_workspace.list_directory

                def list_directory(handle: int, *, max_entries: int):
                    return [
                        dataclasses.replace(entry, kind="link")
                        if entry.name == server.name
                        else entry
                        for entry in real_list_directory(handle, max_entries=max_entries)
                    ]

                monkeypatch.setattr(
                    installer_module._windows_workspace,
                    "list_directory",
                    list_directory,
                )
    before = tuple(sorted(str(path.relative_to(root)) for path in root.rglob("*")))

    with pytest.raises(PyrightInstallError) as error:
        install_pyright(state_root=state_root, artifact=artifact.path)

    assert _error_code(error) == "pyright_existing_install_invalid"
    assert tuple(sorted(str(path.relative_to(root)) for path in root.rglob("*"))) == before
    _assert_no_owned_scratch(state_root)


def test_concurrent_installers_converge_on_one_valid_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_root = tmp_path / "state"
    artifact = _artifact(tmp_path, monkeypatch)

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(install_pyright, state_root=state_root, artifact=artifact.path)
            for _ in range(2)
        ]
        results = [future.result(timeout=10) for future in futures]

    assert results[0] == results[1]
    assert results[0].root == _root(state_root)
    assert _installer_entries(state_root) == (PYRIGHT_VERSION,)


def test_symlink_or_reparse_runtime_parent_is_rejected_before_artifact_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_root = tmp_path / "state"
    state_root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    cache = state_root / "cache"
    try:
        cache.symlink_to(outside, target_is_directory=True)
    except OSError:
        cache.mkdir()
        real_lstat = Path.lstat

        def lstat(path: Path):
            value = real_lstat(path)
            if path == cache:
                return SimpleNamespace(
                    st_mode=stat.S_IFDIR | 0o700,
                    st_file_attributes=0x400,
                    st_dev=value.st_dev,
                    st_ino=value.st_ino,
                    st_size=value.st_size,
                    st_mtime_ns=value.st_mtime_ns,
                )
            return value

        monkeypatch.setattr(Path, "lstat", lstat)

    with pytest.raises(PyrightInstallError) as error:
        install_pyright(state_root=state_root, artifact=tmp_path / "must-not-be-read")

    assert _error_code(error) == "pyright_state_root_unsafe"
    assert not _root(state_root).exists()


def test_known_network_state_root_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_root = tmp_path / "state"

    def reject_network(path: Path, deadline: float) -> None:
        raise PermissionError("simulated network state root")

    monkeypatch.setattr(installer_module, "_require_local_filesystem", reject_network)

    with pytest.raises(PyrightInstallError) as error:
        install_pyright(state_root=state_root, artifact=tmp_path / "must-not-be-read")

    assert _error_code(error) == "pyright_state_root_unsafe"


def test_cloud_synchronized_state_root_is_rejected_before_artifact_access(
    tmp_path: Path,
) -> None:
    state_root = tmp_path / "OneDrive" / "state"

    with pytest.raises(PyrightInstallError) as error:
        install_pyright(
            state_root=state_root,
            artifact=tmp_path / "must-not-be-read.tgz",
        )

    assert _error_code(error) == "pyright_state_root_unsafe"
    assert not _root(state_root).exists()


def test_simulated_darwin_state_validation_uses_no_subprocess(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    subprocess_calls: list[object] = []

    def forbidden_subprocess(*args: object, **kwargs: object):
        subprocess_calls.append(args)
        raise AssertionError("installer state validation invoked a subprocess")

    monkeypatch.setattr(installer_module.sys, "platform", "darwin")
    monkeypatch.setattr(subprocess, "run", forbidden_subprocess)
    monkeypatch.setattr(
        installer_module,
        "_known_network_path",
        lambda path: subprocess.run(["/sbin/mount"]),
        raising=False,
    )
    monkeypatch.setattr(
        installer_module,
        "_darwin_filesystem_details",
        lambda path, deadline: ("apfs", "/dev/disk3s1", 0x00001000),
        raising=False,
    )

    installer_module._prepare_state_root(
        tmp_path / "state",
        time.monotonic() + 2.0,
    )

    assert subprocess_calls == []


@pytest.mark.parametrize("deadline", [True, float("nan"), float("inf"), "later"])
def test_invalid_deadline_is_rejected_before_state_creation(
    tmp_path: Path,
    deadline: object,
) -> None:
    state_root = tmp_path / "state"
    with pytest.raises(ValueError, match="deadline"):
        install_pyright(state_root=state_root, deadline=deadline)  # type: ignore[arg-type]
    assert not state_root.exists()


def test_expired_deadline_is_rejected_before_state_creation(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    with pytest.raises(TimeoutError, match="deadline"):
        install_pyright(state_root=state_root, deadline=time.monotonic() - 1)
    assert not state_root.exists()


def test_state_validation_honors_end_to_end_deadline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed_deadlines: list[float] = []

    def slow_legacy_validation(path: Path) -> None:
        time.sleep(0.25)

    def bounded_lock_probe(path: Path, *, deadline: float):
        observed_deadlines.append(deadline)
        while time.monotonic() < deadline:
            time.sleep(0.001)
        return None

    monkeypatch.setattr(
        installer_module,
        "validate_state_root",
        slow_legacy_validation,
        raising=False,
    )
    monkeypatch.setattr(
        installer_module,
        "_sqlite_lock_probe",
        bounded_lock_probe,
        raising=False,
    )
    started = time.monotonic()

    with pytest.raises(TimeoutError, match="deadline"):
        install_pyright(
            state_root=tmp_path / "state",
            deadline=started + 0.02,
        )

    elapsed = time.monotonic() - started
    assert observed_deadlines == [pytest.approx(started + 0.02)]
    assert elapsed < 0.12


def test_nested_install_timeout_is_never_relabelled_as_io_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact = _artifact(tmp_path, monkeypatch)

    def expire_during_copy(*args: object, **kwargs: object):
        raise TimeoutError("Pyright installation deadline expired")

    monkeypatch.setattr(
        installer_module,
        "_copy_to_owned_file",
        expire_during_copy,
    )

    with pytest.raises(TimeoutError, match="deadline"):
        install_pyright(state_root=tmp_path / "state", artifact=artifact.path)


def test_cli_requires_absolute_state_and_artifact_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []
    expected = InstalledPyright(
        _root(tmp_path),
        PYRIGHT_VERSION,
        PYRIGHT_PACKAGE_SHA256,
        PYRIGHT_PACKAGE_INTEGRITY,
        "1" * 64,
        "2" * 64,
    )
    monkeypatch.setattr(
        installer_module,
        "install_pyright",
        lambda **kwargs: calls.append(kwargs) or expected,
    )

    with pytest.raises(SystemExit) as state_error:
        installer_module.main(["--state-root", "relative"])
    assert state_error.value.code == 2
    with pytest.raises(SystemExit) as artifact_error:
        installer_module.main(
            ["--state-root", str(tmp_path), "--artifact", "relative.tgz"]
        )
    assert artifact_error.value.code == 2

    assert installer_module.main(
        ["--state-root", str(tmp_path), "--artifact", str(tmp_path / "artifact.tgz")]
    ) == 0
    assert calls == [{"state_root": tmp_path, "artifact": tmp_path / "artifact.tgz"}]


def test_installer_source_has_no_archive_extract_npm_subprocess_or_shell_calls() -> None:
    source = Path(installer_module.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    forbidden_attributes = {"extract", "extractall", "unpack_archive"}
    assert "subprocess" not in source
    assert "npm" not in source.casefold()
    assert not any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in forbidden_attributes
        for node in ast.walk(tree)
    )


def test_profile_discovery_does_not_import_or_call_installer() -> None:
    source = Path(pyright_profile.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    assert not any(
        isinstance(node, (ast.Import, ast.ImportFrom))
        and (
            any(alias.name == "install_pyright" for alias in node.names)
            if isinstance(node, ast.Import)
            else node.module == "install_pyright"
        )
        for node in ast.walk(tree)
    )
