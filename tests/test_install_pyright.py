"""Explicit, bounded installation of the pinned managed Pyright artifact."""

from __future__ import annotations

import dataclasses
import http.client
import io
import json
import os
import socket
import tarfile
import threading
import time
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import install_pyright as installer_module
import pytest
from install_pyright import InstalledPyright, PyrightInstallError, install_pyright
from llm_wiki import pyright_configuration
from pyright_profile import (
    PYRIGHT_PACKAGE_URL,
    PYRIGHT_VERSION,
    build_pyright_install_manifest,
)
from reliable_memory import canonical_json_bytes, sha256_bytes

from tests.code_kernel_helpers import pyright_executed_tree_sha256
from tests.install_pyright_helpers import _artifact, _Response, _root
from tests.pyright_install_artifacts import (
    PyrightTarEntry,
    create_pyright_install_artifact,
    use_pyright_install_artifact_identity,
)
from tests.pyright_installer_helpers import (
    _assert_no_owned_scratch,
    _error_code,
)
from tests.slow_machine import SHORT_TIMEOUT


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
    expected_manifest = build_pyright_install_manifest(
        server_sha256=server_sha256,
        executed_tree_sha256=pyright_executed_tree_sha256(artifact.server_bytes, {}),
    )
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
        monkeypatch.setattr(pyright_configuration, "PYRIGHT_PACKAGE_SHA256", "0" * 64)
    else:
        monkeypatch.setattr(
            pyright_configuration, "PYRIGHT_PACKAGE_INTEGRITY", "sha512-" + "A" * 88
        )
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


def test_verified_scratch_cannot_be_swapped_before_extraction_or_accepted_later(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_root = tmp_path / "state"
    trusted_server = b"good pyright language server payload\n"
    evil_server = b"evil pyright language server payload\n"
    trusted = create_pyright_install_artifact(
        tmp_path / "trusted.tgz", server_bytes=trusted_server
    )
    evil = create_pyright_install_artifact(
        tmp_path / "evil.tgz", server_bytes=evil_server
    )
    trusted_content = trusted.path.read_bytes()
    evil_content = evil.path.read_bytes()
    common_size = max(len(trusted_content), len(evil_content))
    trusted_content = trusted_content.ljust(common_size, b"\0")
    evil_content = evil_content.ljust(common_size, b"\0")
    trusted.path.write_bytes(trusted_content)
    trusted = dataclasses.replace(
        trusted,
        package_sha256=installer_module.hashlib.sha256(trusted_content).hexdigest(),
        package_integrity="sha512-"
        + installer_module.base64.b64encode(
            installer_module.hashlib.sha512(trusted_content).digest()
        ).decode("ascii"),
    )
    use_pyright_install_artifact_identity(monkeypatch, trusted)
    real_close = installer_module.OwnedFile.close
    attacks = 0

    def replace_when_released(owned: object) -> None:
        nonlocal attacks
        real_close(owned)
        if attacks or not owned.name.startswith(".install-pyright-download-"):
            return
        scratch = state_root / "cache/code-tools/pyright" / owned.name
        before = scratch.stat()
        with scratch.open("r+b") as stream:
            stream.write(evil_content)
            stream.truncate()
            stream.flush()
            os.fsync(stream.fileno())
        os.utime(scratch, ns=(before.st_atime_ns, before.st_mtime_ns))
        attacks += 1

    monkeypatch.setattr(installer_module.OwnedFile, "close", replace_when_released)

    first = install_pyright(state_root=state_root, artifact=trusted.path)
    second = install_pyright(state_root=state_root, artifact=trusted.path)

    server = _root(state_root) / "package/langserver.index.js"
    assert attacks == 1
    assert first == second
    assert server.read_bytes() == trusted_server
    assert server.read_bytes() != evil_server
    _assert_no_owned_scratch(state_root)


@pytest.mark.skipif(os.name != "posix", reason="POSIX ctime contract")
def test_posix_verified_scratch_mutate_and_restore_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_root = tmp_path / "state"
    trusted = create_pyright_install_artifact(
        tmp_path / "trusted.tgz",
        server_bytes=b"good pyright language server payload\n",
    )
    evil = create_pyright_install_artifact(
        tmp_path / "evil.tgz",
        server_bytes=b"evil pyright language server payload\n",
    )
    trusted_content = trusted.path.read_bytes()
    evil_content = evil.path.read_bytes()
    common_size = max(len(trusted_content), len(evil_content))
    trusted_content = trusted_content.ljust(common_size, b"\0")
    evil_content = evil_content.ljust(common_size, b"\0")
    trusted.path.write_bytes(trusted_content)
    trusted = dataclasses.replace(
        trusted,
        package_sha256=installer_module.hashlib.sha256(trusted_content).hexdigest(),
        package_integrity="sha512-"
        + installer_module.base64.b64encode(
            installer_module.hashlib.sha512(trusted_content).digest()
        ).decode("ascii"),
    )
    use_pyright_install_artifact_identity(monkeypatch, trusted)
    real_verify = installer_module.verify_artifact_digests
    attacks = 0

    def replace_after_acceptance(*args: object, **kwargs: object) -> None:
        nonlocal attacks
        real_verify(*args, **kwargs)
        scratch = next(
            path
            for path in (state_root / "cache/code-tools/pyright").iterdir()
            if path.name.startswith(".install-pyright-download-")
        )
        before = scratch.stat()
        with scratch.open("r+b") as stream:
            stream.write(evil_content)
            stream.truncate()
            stream.flush()
            os.fsync(stream.fileno())
        os.utime(scratch, ns=(before.st_atime_ns, before.st_mtime_ns))
        attacks += 1

    monkeypatch.setattr(
        installer_module, "verify_artifact_digests", replace_after_acceptance
    )

    with pytest.raises(PyrightInstallError) as error:
        install_pyright(state_root=state_root, artifact=trusted.path)

    assert attacks == 1
    assert _error_code(error) == "pyright_artifact_changed"
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
    real_copy = installer_module.copy_to_owned_file

    def replace_after_copy(*args: object, **kwargs: object):
        result = real_copy(*args, **kwargs)
        os.replace(replacement, artifact.path)
        return result

    monkeypatch.setattr(installer_module, "copy_to_owned_file", replace_after_copy)

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
    real_copy = installer_module.copy_to_owned_file
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

    monkeypatch.setattr(installer_module, "copy_to_owned_file", probe_exclusive_source)

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
    real_copy = installer_module.copy_to_owned_file

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

    monkeypatch.setattr(installer_module, "copy_to_owned_file", mutate_and_restore)

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
    real_local_check = installer_module.require_local_filesystem

    def reject_artifact(path: Path, deadline: float) -> None:
        if path == artifact.path:
            raise PermissionError("simulated network artifact")
        real_local_check(path, deadline)

    monkeypatch.setattr(installer_module, "require_local_filesystem", reject_artifact)

    with pytest.raises(PyrightInstallError) as error:
        install_pyright(state_root=state_root, artifact=artifact.path)

    assert _error_code(error) == "pyright_artifact_unsafe"
    assert not _root(state_root).exists()
    _assert_no_owned_scratch(state_root)


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


def test_download_refreshes_remaining_deadline_on_socket_before_every_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent_path = tmp_path / "pyright"
    parent_path.mkdir()
    parent = installer_module.open_absolute_directory(parent_path, writable=True)
    destination = installer_module.new_owned_file(parent, "download")
    content = b"abcdefghijklmnopqrstuvwxyz"
    clock = 100.0

    class TrackingResponse(_Response):
        def __init__(self) -> None:
            super().__init__(content)
            self.read1_calls = 0

        def read(self, size: int = -1) -> bytes:
            pytest.fail("download used multi-read response.read")

        def read1(self, size: int = -1) -> bytes:
            assert size == installer_module.COPY_CHUNK_BYTES
            assert len(self.socket.timeouts) == self.read1_calls + 1
            self.read1_calls += 1
            return super().read1(size)

    response = TrackingResponse()

    def monotonic() -> float:
        nonlocal clock
        clock += 0.01
        return clock

    monkeypatch.setattr(installer_module.time, "monotonic", monotonic)
    monkeypatch.setattr(installer_module, "COPY_CHUNK_BYTES", 8)
    monkeypatch.setattr(
        installer_module, "_open_pinned_url", lambda *args, **kwargs: response
    )
    try:
        sha256, sha512 = installer_module.download_artifact(destination, 101.0)

        assert sha256 == installer_module.hashlib.sha256(content).hexdigest()
        assert sha512 == installer_module.hashlib.sha512(content).digest()
        assert response.read1_calls == 4
        assert len(response.socket.timeouts) == response.read1_calls
        assert all(0 < timeout < 1 for timeout in response.socket.timeouts)
        assert response.socket.timeouts == sorted(
            response.socket.timeouts, reverse=True
        )
    finally:
        destination.cleanup()
        parent.close()


def _send_drip_body(server, release_body, stop) -> None:
    if not release_body.wait(1.0):
        return
    for value in b"12345678":
        if stop.wait(0.1):
            return
        server.sendall(bytes((value,)))


def test_download_real_http_response_drip_feed_honors_absolute_deadline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent_path = tmp_path / "pyright"
    parent_path.mkdir()
    parent = installer_module.open_absolute_directory(parent_path, writable=True)
    destination = installer_module.new_owned_file(parent, "download")
    client, server = socket.socketpair()
    release_body = threading.Event()
    stop = threading.Event()
    server_errors: list[OSError] = []

    def serve() -> None:
        try:
            _send_drip_body(server, release_body, stop)
        except OSError as exc:
            if not stop.is_set():
                server_errors.append(exc)
        finally:
            server.close()

    worker = threading.Thread(target=serve, name="pyright-drip-server", daemon=True)
    response = http.client.HTTPResponse(client)
    response.url = PYRIGHT_PACKAGE_URL

    def open_url(*args: object, **kwargs: object) -> http.client.HTTPResponse:
        release_body.set()
        return response

    monkeypatch.setattr(installer_module, "_open_pinned_url", open_url)
    started = time.monotonic()
    try:
        server.sendall(
            b"HTTP/1.1 200 OK\r\n"
            b"Content-Length: 8\r\n"
            b"Connection: close\r\n\r\n"
        )
        worker.start()
        response.begin()
        started = time.monotonic()
        with pytest.raises(TimeoutError):
            installer_module.download_artifact(destination, started + 0.25)
    finally:
        stop.set()
        response.close()
        client.close()
        server.close()
        if worker.ident is not None:
            worker.join(timeout=SHORT_TIMEOUT)
        elapsed = time.monotonic() - started
        destination.cleanup()
        parent.close()

    assert not worker.is_alive()
    assert server_errors == []
    assert 0.15 <= elapsed < 0.55


def test_drip_fixture_reports_socket_refusal_without_waiting_for_a_response(tmp_path, monkeypatch):
    from unittest.mock import Mock

    client, server = socket.socketpair()
    client.settimeout(SHORT_TIMEOUT)
    refused = Mock(wraps=server)
    refused.sendall.side_effect = PermissionError("socket send refused")
    monkeypatch.setattr(socket, "socketpair", lambda: (client, refused))
    try:
        with pytest.raises(PermissionError, match="socket send refused"):
            test_download_real_http_response_drip_feed_honors_absolute_deadline(tmp_path, monkeypatch)
        assert (client.fileno(), server.fileno()) == (-1, -1)
    finally:
        client.close()
        server.close()


def test_download_rejects_response_without_callable_read1_before_body(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_root = tmp_path / "state"

    class ReadOnlyResponse(_Response):
        read1 = None

        def read(self, size: int = -1) -> bytes:
            pytest.fail("response without read1 reached blocking read")

    monkeypatch.setattr(
        installer_module,
        "_open_pinned_url",
        lambda *args, **kwargs: ReadOnlyResponse(b"blocked"),
    )

    with pytest.raises(PyrightInstallError) as error:
        install_pyright(state_root=state_root)

    assert _error_code(error) == "pyright_download_response_invalid"
    assert not _root(state_root).exists()
    _assert_no_owned_scratch(state_root)


def test_download_rechecks_deadline_after_ssl_like_fragment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent_path = tmp_path / "pyright"
    parent_path.mkdir()
    parent = installer_module.open_absolute_directory(parent_path, writable=True)
    destination = installer_module.new_owned_file(parent, "download")
    clock = [100.0]
    writes: list[bytes] = []

    class FragmentedSslResponse(_Response):
        def __init__(self) -> None:
            super().__init__(b"abcdefgh")
            self.fragments = iter((b"ab", b"cdefgh"))

        def _next_fragment(self) -> bytes:
            fragment = next(self.fragments, b"")
            if fragment == b"cdefgh":
                clock[0] = 100.3
            return fragment

        def read(self, size: int = -1) -> bytes:
            return self._next_fragment()

        def read1(self, size: int = -1) -> bytes:
            return self._next_fragment()

    response = FragmentedSslResponse()
    monkeypatch.setattr(installer_module.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(
        installer_module, "_open_pinned_url", lambda *args, **kwargs: response
    )
    monkeypatch.setattr(
        installer_module,
        "write_handle",
        lambda _handle, content, _deadline: writes.append(content),
    )
    try:
        with pytest.raises(TimeoutError):
            installer_module.download_artifact(destination, 100.25)
    finally:
        destination.cleanup()
        parent.close()

    assert writes == [b"ab"]
    assert len(response.socket.timeouts) == 2


def test_download_rejects_unadjustable_transport_before_blocking_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_root = tmp_path / "state"

    class UnadjustableResponse(_Response):
        def __init__(self) -> None:
            super().__init__(b"blocked")
            self.fp = SimpleNamespace(raw=SimpleNamespace())

        def read(self, size: int = -1) -> bytes:
            pytest.fail("unadjustable transport reached blocking read")

        def read1(self, size: int = -1) -> bytes:
            pytest.fail("unadjustable transport reached blocking read")

    monkeypatch.setattr(
        installer_module,
        "_open_pinned_url",
        lambda *args, **kwargs: UnadjustableResponse(),
    )

    with pytest.raises(PyrightInstallError) as error:
        install_pyright(state_root=state_root)

    assert _error_code(error) == "pyright_download_response_invalid"
    assert not _root(state_root).exists()
    _assert_no_owned_scratch(state_root)


@pytest.mark.parametrize(
    ("status", "expected_code"),
    [(302, "pyright_download_redirect"), (503, "pyright_download_failed")],
    ids=("redirect", "failure"),
)
def test_download_closes_http_error_before_chaining(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    status: int,
    expected_code: str,
) -> None:
    state_root = tmp_path / "state"
    body = io.BytesIO(b"error body")
    http_error = installer_module.urllib.error.HTTPError(
        PYRIGHT_PACKAGE_URL,
        status,
        "request failed",
        {},
        body,
    )

    def fail_open(*args: object, **kwargs: object) -> object:
        raise http_error

    monkeypatch.setattr(installer_module, "_open_pinned_url", fail_open)

    with pytest.raises(PyrightInstallError) as error:
        install_pyright(state_root=state_root)

    assert _error_code(error) == expected_code
    assert error.value.__cause__ is http_error
    assert http_error.closed is True
    assert body.closed is True
    assert not _root(state_root).exists()
    _assert_no_owned_scratch(state_root)


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

        def read1(self, size: int = -1) -> bytes:
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
