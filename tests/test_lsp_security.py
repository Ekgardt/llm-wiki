"""Repository-contained LSP source and safe diagnostic text tests."""

from __future__ import annotations

import inspect
import os
import re
import socket
import stat
import subprocess
import time
import unicodedata
from collections import Counter
from dataclasses import FrozenInstanceError, fields
from itertools import product
from pathlib import Path, PureWindowsPath
from typing import get_type_hints

import lsp_security
import pytest
import windows_workspace
from lsp_positions import path_to_file_uri
from lsp_security import (
    PathContainmentError,
    RepositorySource,
    normalize_provider_uri,
    redact_lsp_text,
    resolve_repository_source,
)
from repository_scope import RepositoryScope, resolve_repository_scope
from windows_workspace import WindowsEntry


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    root = tmp_path / "repository"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "api.py").write_text("def api():\n    return 1\n", encoding="utf-8")
    (root / "plain.py").write_text("value = 1\n", encoding="utf-8")
    return root


@pytest.fixture
def scope(repository: Path) -> RepositoryScope:
    return resolve_repository_scope(repository)


def test_security_public_api_has_exact_types_and_signatures() -> None:
    assert PathContainmentError.__bases__ == (ValueError,)
    assert tuple(field.name for field in fields(RepositorySource)) == (
        "repository_id",
        "checkout_id",
        "relative_path",
        "absolute_path",
        "uri",
    )

    resolve_signature = inspect.signature(resolve_repository_source)
    assert tuple(resolve_signature.parameters) == (
        "repository",
        "relative_path",
        "must_exist",
    )
    assert resolve_signature.parameters["must_exist"].kind is inspect.Parameter.KEYWORD_ONLY
    assert resolve_signature.parameters["must_exist"].default is True
    assert get_type_hints(resolve_repository_source) == {
        "repository": RepositoryScope,
        "relative_path": str,
        "must_exist": bool,
        "return": RepositorySource,
    }

    normalize_signature = inspect.signature(normalize_provider_uri)
    assert tuple(normalize_signature.parameters) == ("repository", "uri")
    assert get_type_hints(normalize_provider_uri) == {
        "repository": RepositoryScope,
        "uri": str,
        "return": RepositorySource | None,
    }

    redact_signature = inspect.signature(redact_lsp_text)
    assert tuple(redact_signature.parameters) == ("value", "repository")
    assert redact_signature.parameters["repository"].kind is inspect.Parameter.KEYWORD_ONLY
    assert redact_signature.parameters["repository"].default is None
    assert get_type_hints(redact_lsp_text) == {
        "value": str,
        "repository": RepositoryScope | None,
        "return": str,
    }


def test_repository_source_is_frozen_slotted_and_carries_canonical_identity(
    repository: Path, scope: RepositoryScope
) -> None:
    source = resolve_repository_source(scope, "pkg/api.py")

    assert source == RepositorySource(
        repository_id=scope.repository_id,
        checkout_id=scope.checkout_id,
        relative_path="pkg/api.py",
        absolute_path=(repository / "pkg/api.py").resolve(strict=True),
        uri=path_to_file_uri((repository / "pkg/api.py").resolve(strict=True)),
    )
    assert not hasattr(source, "__dict__")
    with pytest.raises(FrozenInstanceError):
        source.relative_path = "other.py"  # type: ignore[misc]


def test_resolve_accepts_normalized_nfc_unicode_file(
    repository: Path, scope: RepositoryScope
) -> None:
    target = repository / "pkg" / "é.py"
    target.write_text("pass\n", encoding="utf-8")

    source = resolve_repository_source(scope, "pkg/é.py")

    assert source.relative_path == "pkg/é.py"
    assert source.absolute_path == target.resolve(strict=True)
    assert source.uri.endswith("pkg/%C3%A9.py")


@pytest.mark.parametrize(
    "relative_path",
    [
        "",
        "/pkg/api.py",
        "//server/share/api.py",
        "C:/secret.py",
        "C:secret.py",
        r"\pkg\api.py",
        r"pkg\api.py",
        "pkg//api.py",
        "pkg/./api.py",
        "pkg/../api.py",
        "./pkg/api.py",
        "pkg/api.py/",
        "pkg/\x00api.py",
        "pkg/\napi.py",
        "pkg/\x7fapi.py",
        "pkg/\x85api.py",
        "pkg/e\u0301.py",
        "pkg/api.py:stream",
        "pkg/bad<name.py",
        "pkg/bad>name.py",
        'pkg/bad"name.py',
        "pkg/bad|name.py",
        "pkg/bad?name.py",
        "pkg/bad*name.py",
        "pkg/trailing.",
        "pkg/trailing ",
        "pkg/CON",
        "pkg/con.txt",
        "pkg/PRN.py",
        "pkg/AUX.tar.gz",
        "pkg/NUL",
        "pkg/COM1.py",
        "pkg/LPT9.log",
        "pkg/COM¹",
        "pkg/LPT³.txt",
        "pkg/CONIN$",
        "pkg/CONOUT$.txt",
        "a" * 256 + "/api.py",
        "😀" * 64 + "/api.py",
        "a/" * 2050 + "api.py",
    ],
)
def test_invalid_relative_path_forms_are_rejected_without_echo(
    scope: RepositoryScope, relative_path: str
) -> None:
    with pytest.raises(PathContainmentError) as caught:
        resolve_repository_source(scope, relative_path, must_exist=False)

    if relative_path:
        assert relative_path not in str(caught.value)


def test_invalid_argument_types_are_rejected(scope: RepositoryScope) -> None:
    with pytest.raises(TypeError):
        resolve_repository_source(scope, Path("pkg/api.py"))  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        resolve_repository_source(scope, "pkg/api.py", must_exist=1)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        resolve_repository_source(object(), "pkg/api.py")  # type: ignore[arg-type]


@pytest.mark.parametrize("relative_path", ["pkg", "plain.py/child.py"])
def test_existing_target_must_be_a_regular_file(
    scope: RepositoryScope, relative_path: str
) -> None:
    with pytest.raises(PathContainmentError):
        resolve_repository_source(scope, relative_path, must_exist=False)


@pytest.mark.skipif(os.name != "posix" or not hasattr(os, "mkfifo"), reason="POSIX FIFO")
def test_fifo_is_rejected_without_blocking(repository: Path, scope: RepositoryScope) -> None:
    fifo = repository / "pkg" / "events"
    os.mkfifo(fifo)

    with pytest.raises(PathContainmentError):
        resolve_repository_source(scope, "pkg/events")


@pytest.mark.skipif(
    os.name != "posix" or not hasattr(socket, "AF_UNIX"), reason="POSIX local socket"
)
def test_socket_device_like_entry_is_rejected(
    repository: Path, scope: RepositoryScope
) -> None:
    target = repository / "pkg" / "service.sock"
    listener = socket.socket(socket.AF_UNIX)
    try:
        listener.bind(str(target))
        with pytest.raises(PathContainmentError):
            resolve_repository_source(scope, "pkg/service.sock")
    finally:
        listener.close()


def _symlink_or_skip(link: Path, target: Path, *, directory: bool) -> None:
    try:
        link.symlink_to(target, target_is_directory=directory)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"symlink creation unavailable: {exc}")


def test_final_symlink_is_rejected(repository: Path, scope: RepositoryScope) -> None:
    outside = repository.parent / "outside.py"
    outside.write_text("secret = True\n", encoding="utf-8")
    _symlink_or_skip(repository / "pkg" / "link.py", outside, directory=False)

    with pytest.raises(PathContainmentError):
        resolve_repository_source(scope, "pkg/link.py")


def test_parent_symlink_is_rejected(repository: Path, scope: RepositoryScope) -> None:
    outside = repository.parent / "outside"
    outside.mkdir()
    (outside / "api.py").write_text("secret = True\n", encoding="utf-8")
    _symlink_or_skip(repository / "linked", outside, directory=True)

    with pytest.raises(PathContainmentError):
        resolve_repository_source(scope, "linked/api.py")


@pytest.mark.skipif(os.name != "posix", reason="POSIX descriptor traversal")
@pytest.mark.parametrize(
    ("relative_path", "must_exist"),
    [("pkg/api.py", True), ("pkg/generated.py", False)],
)
def test_posix_checkout_ancestor_symlink_is_rejected(
    tmp_path: Path,
    relative_path: str,
    must_exist: bool,
) -> None:
    ancestor = tmp_path / "trusted"
    repository = ancestor / "repository"
    (repository / "pkg").mkdir(parents=True)
    (repository / "pkg" / "api.py").write_text("pass\n", encoding="utf-8")
    scope = resolve_repository_scope(repository)
    moved = tmp_path / "trusted-original"
    ancestor.rename(moved)
    _symlink_or_skip(ancestor, moved, directory=True)

    with pytest.raises(PathContainmentError):
        resolve_repository_source(scope, relative_path, must_exist=must_exist)


@pytest.mark.skipif(os.name != "posix", reason="POSIX descriptor traversal")
def test_posix_checkout_ancestor_replacement_is_rejected_and_descriptors_close(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ancestor = tmp_path / "trusted"
    repository = ancestor / "repository"
    (repository / "pkg").mkdir(parents=True)
    (repository / "pkg" / "api.py").write_text("pass\n", encoding="utf-8")
    scope = resolve_repository_scope(repository)
    moved = tmp_path / "trusted-original"
    opened: list[int] = []
    closed: list[int] = []
    real_open = os.open
    real_close = os.close

    def tracking_open(*args, **kwargs) -> int:
        descriptor = real_open(*args, **kwargs)
        opened.append(descriptor)
        return descriptor

    def tracking_close(descriptor: int) -> None:
        closed.append(descriptor)
        real_close(descriptor)

    def substitute() -> None:
        ancestor.rename(moved)
        ancestor.symlink_to(moved, target_is_directory=True)

    monkeypatch.setattr(lsp_security.os, "open", tracking_open)
    monkeypatch.setattr(lsp_security.os, "close", tracking_close)
    monkeypatch.setattr(lsp_security, "_resolution_barrier", substitute)

    with pytest.raises(PathContainmentError):
        resolve_repository_source(scope, "pkg/api.py")

    assert Counter(opened) == Counter(closed)


@pytest.mark.skipif(os.name != "posix", reason="POSIX descriptor traversal")
def test_posix_checkout_walk_opens_only_root_or_handle_relative_components(
    scope: RepositoryScope,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, int | None]] = []
    real_open = os.open

    def tracking_open(path, flags, *args, **kwargs) -> int:
        calls.append((os.fspath(path), kwargs.get("dir_fd")))
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(lsp_security.os, "open", tracking_open)

    resolve_repository_source(scope, "pkg/api.py")

    assert calls
    assert all(
        (path == "/" and directory is None)
        or (not os.path.isabs(path) and directory is not None)
        for path, directory in calls
    )
    assert scope.checkout_root not in {path for path, _directory in calls}


@pytest.mark.skipif(os.name != "nt", reason="Windows junction")
def test_windows_junction_parent_is_rejected(
    repository: Path, scope: RepositoryScope
) -> None:
    supported, reason = windows_workspace.capability()
    if not supported:
        pytest.skip(reason or "Windows handle-relative APIs unavailable")
    outside = repository.parent / "outside-junction"
    outside.mkdir()
    (outside / "api.py").write_text("secret = True\n", encoding="utf-8")
    junction = repository / "junction"
    created = subprocess.run(
        ["cmd.exe", "/d", "/c", "mklink", "/J", str(junction), str(outside)],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=False,
        timeout=10,
    )
    if created.returncode != 0:
        output = (created.stdout + created.stderr).decode(errors="replace")
        if "privilege" in output.casefold():
            pytest.skip("junction creation privilege unavailable")
        pytest.fail(f"junction creation failed: {output}")
    assert junction.exists()
    assert os.lstat(junction).st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT

    with pytest.raises(PathContainmentError):
        resolve_repository_source(scope, "junction/api.py")


@pytest.mark.skipif(os.name != "nt", reason="Windows case semantics")
def test_windows_case_alias_is_rejected(scope: RepositoryScope) -> None:
    supported, reason = windows_workspace.capability()
    if not supported:
        pytest.skip(reason or "Windows handle-relative APIs unavailable")

    with pytest.raises(PathContainmentError):
        resolve_repository_source(scope, "PKG/api.py")


@pytest.mark.skipif(os.name != "nt", reason="Windows case semantics")
def test_windows_case_collision_is_rejected_before_open(
    scope: RepositoryScope, monkeypatch: pytest.MonkeyPatch
) -> None:
    supported, reason = windows_workspace.capability()
    if not supported:
        pytest.skip(reason or "Windows handle-relative APIs unavailable")
    original = windows_workspace.list_directory
    first = True

    def colliding(handle: int, *, max_entries: int) -> list[WindowsEntry]:
        nonlocal first
        if first:
            first = False
            return [
                WindowsEntry("pkg", "directory", b"a" * 16),
                WindowsEntry("PKG", "directory", b"b" * 16),
            ]
        return original(handle, max_entries=max_entries)

    monkeypatch.setattr(windows_workspace, "list_directory", colliding)

    with pytest.raises(PathContainmentError):
        resolve_repository_source(scope, "pkg/api.py")


@pytest.mark.skipif(os.name != "nt", reason="Windows handle sharing")
def test_windows_source_containment_accepts_compatible_open_handles_and_closes_all(
    repository: Path,
    scope: RepositoryScope,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    supported, reason = windows_workspace.capability()
    if not supported:
        pytest.skip(reason or "Windows handle-relative APIs unavailable")
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    create_file.restype = wintypes.HANDLE
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = (wintypes.HANDLE,)
    close_handle.restype = wintypes.BOOL
    target = repository / "pkg" / "api.py"
    held: list[int] = []
    external_closed: list[int] = []
    opened, closed = _track_windows_handles(monkeypatch)
    try:
        for desired_access in (0x80000000, 0x40000000, 0x00010000):
            handle = create_file(
                str(target),
                desired_access,
                0x00000001 | 0x00000002 | 0x00000004,
                None,
                3,
                0x00000080,
                None,
            )
            if handle == ctypes.c_void_p(-1).value:
                raise ctypes.WinError(ctypes.get_last_error())
            held.append(int(handle))
        source = resolve_repository_source(scope, "pkg/api.py")
        assert source.absolute_path == target.resolve(strict=True)
    finally:
        for handle in reversed(held):
            if not close_handle(handle):
                raise ctypes.WinError(ctypes.get_last_error())
            external_closed.append(handle)

    assert Counter(opened) == Counter(closed)
    assert all(count == 1 for count in Counter(opened).values())
    assert Counter(external_closed) == Counter(held)


def test_missing_leaf_requires_explicit_opt_in(
    repository: Path, scope: RepositoryScope
) -> None:
    with pytest.raises(PathContainmentError):
        resolve_repository_source(scope, "pkg/deleted.py")

    source = resolve_repository_source(scope, "pkg/deleted.py", must_exist=False)

    assert source.relative_path == "pkg/deleted.py"
    assert source.absolute_path == repository.resolve(strict=True) / "pkg/deleted.py"
    assert source.uri == path_to_file_uri(source.absolute_path)


def test_missing_suffix_is_constructed_beneath_nearest_held_parent(
    repository: Path, scope: RepositoryScope
) -> None:
    source = resolve_repository_source(
        scope, "pkg/generated/nested/api.py", must_exist=False
    )

    assert source.absolute_path == (
        repository.resolve(strict=True) / "pkg/generated/nested/api.py"
    )
    assert source.relative_path == "pkg/generated/nested/api.py"


def test_missing_leaf_appearing_during_resolution_fails_closed_and_releases_parent(
    repository: Path,
    scope: RepositoryScope,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = repository / "pkg" / "generated.py"

    def substitute() -> None:
        target.write_text("substitute = True\n", encoding="utf-8")

    monkeypatch.setattr(lsp_security, "_resolution_barrier", substitute)

    with pytest.raises(PathContainmentError):
        resolve_repository_source(scope, "pkg/generated.py", must_exist=False)

    original = repository / "pkg"
    moved = repository / "pkg-moved"
    original.rename(moved)
    moved.rename(original)


def test_existing_file_is_valid_with_must_exist_false(scope: RepositoryScope) -> None:
    assert resolve_repository_source(
        scope, "pkg/api.py", must_exist=False
    ) == resolve_repository_source(scope, "pkg/api.py")


def test_provider_uri_normalizes_to_canonical_repository_source(
    scope: RepositoryScope,
) -> None:
    expected = resolve_repository_source(scope, "pkg/api.py")

    assert normalize_provider_uri(scope, expected.uri) == expected
    localhost = expected.uri.replace("file://", "file://localhost", 1)
    assert normalize_provider_uri(scope, localhost) == expected


def test_provider_uri_round_trips_literal_percent_escape_filename(
    repository: Path,
    scope: RepositoryScope,
) -> None:
    target = repository / "pkg" / "percent%20name.py"
    target.write_text("pass\n", encoding="utf-8")
    expected = resolve_repository_source(scope, "pkg/percent%20name.py")
    assert "%2520" in expected.uri

    assert normalize_provider_uri(scope, expected.uri) == expected


@pytest.mark.skipif(os.name != "nt", reason="Windows drive URI variants")
def test_provider_uri_accepts_equivalent_windows_drive_forms(
    scope: RepositoryScope,
) -> None:
    expected = resolve_repository_source(scope, "pkg/api.py")
    match = re.match(r"file:///([A-Z]):/", expected.uri)
    assert match is not None
    drive = match.group(1)
    variants = {
        expected.uri.replace(f"file:///{drive}:/", f"file:///{drive.lower()}:/", 1),
        expected.uri.replace(f"file:///{drive}:/", f"file:///{drive}%3A/", 1),
        expected.uri.replace("file:///", "file:/", 1),
        expected.uri.replace("file:///", "file:", 1),
    }

    assert {normalize_provider_uri(scope, uri) for uri in variants} == {expected}
    assert expected.uri.startswith(f"file:///{drive}:/")


@pytest.mark.skipif(os.name != "nt", reason="Windows path case semantics")
def test_provider_uri_rejects_checkout_component_case_alias(
    scope: RepositoryScope,
) -> None:
    expected = resolve_repository_source(scope, "pkg/api.py")
    aliased = expected.uri.replace("/repository/", "/REPOSITORY/", 1)
    assert aliased != expected.uri

    assert normalize_provider_uri(scope, aliased) is None


@pytest.mark.skipif(os.name != "posix", reason="POSIX local URI variant")
def test_provider_uri_accepts_rfc8089_posix_local_form(scope: RepositoryScope) -> None:
    expected = resolve_repository_source(scope, "pkg/api.py")
    local_form = expected.uri.replace("file://", "file:", 1)

    assert normalize_provider_uri(scope, local_form) == expected


def test_external_and_sibling_prefix_provider_locations_are_filtered(
    repository: Path, scope: RepositoryScope
) -> None:
    external = repository.parent / "external.py"
    external.write_text("external = True\n", encoding="utf-8")
    sibling = repository.parent / f"{repository.name}-sibling"
    sibling.mkdir()
    (sibling / "api.py").write_text("external = True\n", encoding="utf-8")

    assert normalize_provider_uri(scope, path_to_file_uri(external.resolve())) is None
    assert normalize_provider_uri(
        scope, path_to_file_uri((sibling / "api.py").resolve())
    ) is None


def test_missing_directory_and_link_provider_locations_are_filtered(
    repository: Path, scope: RepositoryScope
) -> None:
    outside = repository.parent / "provider-outside.py"
    outside.write_text("secret = True\n", encoding="utf-8")
    link = repository / "pkg" / "provider-link.py"
    _symlink_or_skip(link, outside, directory=False)

    assert normalize_provider_uri(
        scope, path_to_file_uri(repository / "pkg" / "missing.py")
    ) is None
    assert normalize_provider_uri(scope, path_to_file_uri(repository / "pkg")) is None
    assert normalize_provider_uri(scope, path_to_file_uri(link)) is None


@pytest.mark.parametrize(
    "uri_factory",
    [
        lambda base: "",
        lambda base: "https://example.test/api.py",
        lambda base: "untitled:pkg/api.py",
        lambda base: "file://server/share/api.py",
        lambda base: "file:////server/share/api.py",
        lambda base: "file://///server/share/api.py",
        lambda base: "file://./GLOBALROOT/Device/HarddiskVolume1/secret.py",
        lambda base: "file://%3F/C:/repo/secret.py",
        lambda base: r"file:///\\?\C:\repo\secret.py",
        lambda base: base + "?",
        lambda base: base + "?query",
        lambda base: base + "#",
        lambda base: base + "#fragment",
        lambda base: base.replace("api.py", "bad%2"),
        lambda base: base.replace("api.py", "bad%GG"),
        lambda base: base.replace("pkg/api.py", "pkg%2Fapi.py"),
        lambda base: base.replace("pkg/api.py", "pkg%5Capi.py"),
        lambda base: base.replace("pkg/api.py", "%2e%2e/api.py"),
        lambda base: base.replace("pkg/api.py", "%252e%252e/api.py"),
        lambda base: base.replace("api.py", "%FF.py"),
        lambda base: base.replace("api.py", "bad path.py"),
        lambda base: base.replace("api.py", "bad%00.py"),
        lambda base: base.replace("api.py", "bad%0A.py"),
        lambda base: base.replace("api.py", "bad%3F.py"),
        lambda base: base.replace("file://", "file://user:password@", 1),
        lambda base: "\n" + base,
        lambda base: base.replace("pkg/", "pkg/\t", 1),
    ],
)
def test_malformed_unsafe_or_nonlocal_provider_uri_is_filtered(
    scope: RepositoryScope, uri_factory
) -> None:
    base = resolve_repository_source(scope, "pkg/api.py").uri
    uri = uri_factory(base)

    assert normalize_provider_uri(scope, uri) is None


@pytest.mark.parametrize(
    "uri",
    [
        "file://server/share/api.py",
        "file:////server/share/api.py",
        "file://///server/share/api.py",
        "file://./GLOBALROOT/Device/HarddiskVolume1/secret.py",
        "file://%3F/C:/repo/secret.py",
        r"file:///\\.\PhysicalDrive0",
        r"file:///\\?\GLOBALROOT\Device\HarddiskVolume1\secret.py",
    ],
)
def test_network_and_device_uris_are_rejected_before_filesystem_access(
    scope: RepositoryScope,
    monkeypatch: pytest.MonkeyPatch,
    uri: str,
) -> None:
    calls: list[str] = []

    def forbidden(*_args, **_kwargs):
        calls.append("filesystem")
        raise AssertionError("filesystem access must not occur")

    monkeypatch.setattr(lsp_security, "file_uri_to_path", forbidden)
    monkeypatch.setattr(lsp_security, "resolve_repository_source", forbidden)

    assert normalize_provider_uri(scope, uri) is None
    assert calls == []


def test_provider_failures_never_raise_log_or_echo_raw_uri(
    scope: RepositoryScope,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    raw = resolve_repository_source(scope, "pkg/api.py").uri

    def fail(*_args, **_kwargs):
        raise RuntimeError("provider value leaked: " + raw)

    monkeypatch.setattr(lsp_security, "resolve_repository_source", fail)

    assert normalize_provider_uri(scope, raw) is None
    assert raw not in caplog.text


@pytest.mark.parametrize(
    ("value", "secret"),
    [
        ("Api_Key=alpha-value", "alpha-value"),
        ("AUTHORIZATION: Bearer beta-value", "Bearer beta-value"),
        ("password = 'gamma-value'", "gamma-value"),
        ('{"SeCrEt": "delta-value"}', "delta-value"),
        ("ToKeN=epsilon-value", "epsilon-value"),
        ("PASSWORD=alpha,beta;gamma", "beta;gamma"),
    ],
)
def test_redaction_removes_mixed_case_secret_assignments(value: str, secret: str) -> None:
    result = redact_lsp_text(value)

    assert secret not in result
    assert "<redacted>" in result


@pytest.mark.parametrize(
    ("value", "secret"),
    [
        ("OPENAI_API_KEY=alpha-prefixed", "alpha-prefixed"),
        ("npm_TOKEN: beta-prefixed", "beta-prefixed"),
        ("Client_Secret = 'gamma-prefixed'", "gamma-prefixed"),
        ('{"SERVICE_AUTHORIZATION": "Bearer delta-prefixed"}', "delta-prefixed"),
        ('{"database_PASSWORD": "epsilon-prefixed"}', "epsilon-prefixed"),
    ],
)
def test_redaction_removes_prefixed_environment_secret_assignments(
    value: str,
    secret: str,
) -> None:
    result = redact_lsp_text(value)

    assert secret not in result
    assert "<redacted>" in result


@pytest.mark.parametrize(
    "value",
    [
        "OPENAI_API_KEY is unset",
        "NPM_TOKEN",
        "CLIENT_SECRET may be configured later",
        "CLIENT_SECRET_NAME=public-label",
    ],
)
def test_redaction_leaves_unassigned_or_non_suffix_secret_words_unchanged(
    value: str,
) -> None:
    assert redact_lsp_text(value) == value


@pytest.mark.parametrize(
    "value",
    [
        "https://alice:hunter2@example.test/private",
        "ssh://deploy:private-key@example.test/repository",
        "custom+scheme://user%40name:pass%3Aword@example.test/path",
        "https://user:p@ss@example.test/private",
    ],
)
def test_redaction_removes_url_userinfo(value: str) -> None:
    result = redact_lsp_text(value)

    assert "@example.test" in result
    assert "<redacted>@" in result
    assert result.count("@") == 1
    assert "hunter2" not in result
    assert "private-key" not in result
    assert "pass%3Aword" not in result
    assert "p@ss" not in result


def test_redaction_removes_url_userinfo_from_long_rfc3986_scheme() -> None:
    scheme = "a" * 256
    value = f"{scheme}://alice:hunter2@example.test/private"

    result = redact_lsp_text(value)

    assert result == f"{scheme}://<redacted>@example.test/private"


def test_redaction_scans_every_punctuation_separated_url_authority() -> None:
    boundaries = (",", ";", "(", ")", "[", "]", "{", "}", "'", '"', "<", ">")
    credential_url = "https://bob:boundary-secret@example.test"
    for boundary in boundaries:
        value = f"https://public.example{boundary}{credential_url}"

        assert redact_lsp_text(value) == (
            f"https://public.example{boundary}https://<redacted>@example.test"
        )

    long_value = ",".join(
        ["https://public.example"] * 4096
        + ["https://bob:long-tail-secret@example.test"]
    )
    long_result = lsp_security._redact_url_userinfo(long_value)
    assert "long-tail-secret" not in long_result
    assert long_result.endswith("https://<redacted>@example.test")

    control_separated = "https://public.example\n" + credential_url
    control_result = redact_lsp_text(control_separated)
    assert "boundary-secret" not in control_result
    assert control_result.endswith("https://<redacted>@example.test")

    for userinfo in (
        "bob,operations:comma-secret",
        "bob;operations:semicolon-secret",
        "bob(operations):parenthesis-secret",
    ):
        result = redact_lsp_text(f"https://{userinfo}@example.test/private")
        assert result == "https://<redacted>@example.test/private"

    adjacent = (
        "https://alice:first-secret@public.example"
        "https://bob:second-secret@example.test"
    )
    adjacent_result = redact_lsp_text(adjacent)
    assert "first-secret" not in adjacent_result
    assert "second-secret" not in adjacent_result
    assert adjacent_result.count("<redacted>@") == 2


def test_redaction_removes_repository_and_home_native_and_uri_paths(
    scope: RepositoryScope,
) -> None:
    repository_root = Path(scope.checkout_root)
    repository_native = str(repository_root)
    repository_uri = path_to_file_uri(repository_root)
    home = Path.home().absolute()
    home_native = str(home)
    home_uri = path_to_file_uri(home)
    value = (
        f"source={repository_native}{os.sep}pkg{os.sep}api.py "
        f"uri={repository_uri}/pkg/api.py "
        f"home={home_native}{os.sep}.ssh{os.sep}config "
        f"home_uri={home_uri}/.ssh/config"
    )

    result = redact_lsp_text(value, repository=scope)

    assert repository_native not in result
    assert repository_uri not in result
    assert home_native not in result
    assert home_uri not in result
    assert "<repository>" in result
    assert "<home>" in result


_WINDOWS_NATIVE_SEPARATOR_ATOMS = ("/", "\\")
_WINDOWS_URI_SEPARATOR_ATOMS = ("/", "\\", "%2F", "%5C")


def _windows_separator_aliases(
    atoms: tuple[str, ...] = _WINDOWS_NATIVE_SEPARATOR_ATOMS,
) -> tuple[str, ...]:
    return tuple(
        "".join(tokens)
        for width in range(1, 4)
        for tokens in product(atoms, repeat=width)
    )


def _windows_path_with_separators(path: Path, separators: tuple[str, ...]) -> str:
    pure = PureWindowsPath(str(path))
    components = pure.parts[1:]
    assert len(separators) == len(components)
    value = pure.drive
    for separator, component in zip(separators, components):
        value += separator + component
    return "".join(
        character.swapcase()
        if index % 2 and character.isascii() and character.isalpha()
        else character
        for index, character in enumerate(value)
    )


def _encode_alternating_windows_path_letters(value: str) -> str:
    pieces: list[str] = []
    encode = True
    index = 0
    while index < len(value):
        character = value[index]
        if character == "%" and re.fullmatch(
            r"[0-9A-Fa-f]{2}", value[index + 1 : index + 3]
        ):
            pieces.append(value[index : index + 3])
            index += 3
            continue
        if character.isascii() and character.isalpha() and encode:
            pieces.append(f"%{ord(character):02X}")
            encode = False
        else:
            pieces.append(character)
            if character.isascii() and character.isalpha():
                encode = True
        index += 1
    return "".join(pieces)


def _encoded_windows_alias_root(path: Path) -> str:
    aliases = _windows_separator_aliases(_WINDOWS_URI_SEPARATOR_ATOMS)
    boundary_count = len(PureWindowsPath(str(path)).parts) - 1
    separators = tuple(
        aliases[(index * 17 + 5) % len(aliases)] for index in range(boundary_count)
    )
    return _encode_alternating_windows_path_letters(
        _windows_path_with_separators(path, separators)
    ).replace(":", "%3A", 1)


def _windows_separator_alias_variants(path: Path) -> tuple[str, ...]:
    aliases = _windows_separator_aliases()
    boundary_count = len(PureWindowsPath(str(path)).parts) - 1
    variants: set[str] = set()
    for boundary in range(boundary_count):
        for alias in aliases:
            separators = ["/"] * boundary_count
            separators[boundary] = alias
            variants.add(_windows_path_with_separators(path, tuple(separators)))
    for offset in range(len(aliases)):
        separators = tuple(
            aliases[(offset + boundary * 17) % len(aliases)]
            for boundary in range(boundary_count)
        )
        value = _windows_path_with_separators(path, separators)
        variants.add(value)
    return tuple(sorted(variants))


def _windows_normalized_component_alias(
    path: Path,
    *,
    dot_repetitions: int = 1,
    uri: bool = False,
) -> str:
    pure = PureWindowsPath(str(path))
    aliases = _WINDOWS_URI_SEPARATOR_ATOMS if uri else _WINDOWS_NATIVE_SEPARATOR_ATOMS
    trailing_aliases = (
        (".", "%2E", " ", "%20", ".%20")
        if uri
        else (".", " ", ". ")
    )
    value = pure.drive.swapcase()
    for index, component in enumerate(pure.parts[1:]):
        value += aliases[index % len(aliases)]
        for repetition in range(dot_repetitions):
            value += (
                (
                    "."
                    if not uri or (index + repetition) % 2 == 0
                    else "%2E"
                )
                + aliases[(index + repetition + 1) % len(aliases)]
            )
        value += (
            _encode_alternating_windows_path_letters(component)
            if uri
            else component.swapcase()
        )
        value += trailing_aliases[index % len(trailing_aliases)]
    return value


def test_redaction_removes_lexical_and_resolved_linked_home_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolved_home = tmp_path / "resolved-home"
    resolved_home.mkdir()
    lexical_home = tmp_path / "home-link"
    if os.name == "nt":
        created = subprocess.run(
            [
                "cmd.exe",
                "/d",
                "/c",
                "mklink",
                "/J",
                str(lexical_home),
                str(resolved_home),
            ],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            check=False,
            timeout=10,
        )
        if created.returncode != 0:
            output = (created.stdout + created.stderr).decode(errors="replace")
            if "privilege" in output.casefold():
                pytest.skip("junction creation privilege unavailable")
            pytest.fail(f"junction creation failed: {output}")
        assert os.lstat(lexical_home).st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT
    else:
        _symlink_or_skip(lexical_home, resolved_home, directory=True)
    resolved_home = lexical_home.resolve(strict=True)
    monkeypatch.setattr(
        lsp_security.Path,
        "home",
        classmethod(lambda _cls: lexical_home),
    )

    roots: list[str] = []
    for path in (lexical_home.absolute(), resolved_home):
        native = str(path)
        if os.name == "nt":
            roots.extend(_windows_separator_alias_variants(path))
        else:
            roots.extend((native, native.replace("\\", "/")))
        roots.append(path_to_file_uri(path))
    unique_roots = tuple(dict.fromkeys(roots))
    for root in unique_roots:
        result = redact_lsp_text(f"{root}/private")
        assert root not in result
        assert result.count("<home>") == 1
        assert "<home><home>" not in result


@pytest.mark.skipif(os.name != "nt", reason="Windows native path separators")
def test_redaction_removes_every_mixed_windows_repository_separator_combination(
    scope: RepositoryScope,
) -> None:
    variants = _windows_separator_alias_variants(Path(scope.checkout_root))
    aliases = set(_windows_separator_aliases())
    assert len(aliases) == 14
    assert {"/", "\\", "//", "\\\\", "/\\", "\\/"} <= aliases
    assert len(variants) >= 14

    for root in variants:
        result = redact_lsp_text(f"{root}\\pkg/api.py", repository=scope)
        assert root not in result
        assert result.count("<repository>") == 1


@pytest.mark.skipif(os.name != "nt", reason="Windows native path separators")
def test_redaction_leaves_windows_repository_and_home_sibling_prefixes(
    scope: RepositoryScope,
) -> None:
    repository_root = Path(scope.checkout_root)
    home = Path.home().absolute()
    for root_path, suffix, repository in (
        (repository_root, "-sibling", scope),
        (home, "-backup", None),
    ):
        for root in _windows_separator_alias_variants(root_path):
            for continuation in (suffix, "%2D" + suffix[1:]):
                value = f"{root}{continuation}\\private"
                assert redact_lsp_text(value, repository=repository) == value
        encoded_root = _encoded_windows_alias_root(root_path)
        for continuation in (suffix, "%2D" + suffix[1:]):
            value = (
                f"file:%2F%5Cloc%61lhost\\{encoded_root}"
                f"{continuation}/private"
            )
            assert redact_lsp_text(value, repository=repository) == value

    quoted = f'repository="{scope.checkout_root}" home="{home}"'
    assert redact_lsp_text(quoted, repository=scope) == (
        'repository="<repository>" home="<home>"'
    )


@pytest.mark.skipif(os.name != "nt", reason="Windows native path separators")
def test_redaction_handles_long_and_malformed_windows_separator_aliases_safely(
    scope: RepositoryScope,
) -> None:
    root = Path(scope.checkout_root)
    boundary_count = len(PureWindowsPath(str(root)).parts) - 1
    for malformed in ("%", "%2", "%2G", "%5", "%5Z", "%GG" * 2048):
        separators = ["/"] * boundary_count
        separators[0] = malformed
        value = _windows_path_with_separators(root, tuple(separators)) + "\x00"
        result = redact_lsp_text(value, repository=scope)
        assert isinstance(result, str)
        assert len(result) <= 1024
        assert "\x00" not in result

    separators = ["/"] * boundary_count
    separators[0] = "/\\" * 4096
    value = _windows_path_with_separators(root, tuple(separators)) + "/private"
    result = redact_lsp_text(value, repository=scope)
    assert "<repository>" in result
    assert str(root) not in result


@pytest.mark.skipif(os.name != "nt", reason="Windows path normalization")
def test_redaction_normalizes_windows_components_without_matching_siblings(
    scope: RepositoryScope,
) -> None:
    root = Path(scope.checkout_root)
    native_alias = _windows_normalized_component_alias(root)
    file_alias = (
        "file:%2F%5Cloc%61lhost\\"
        + _windows_normalized_component_alias(root, uri=True).replace(":", "%3A", 1)
    )
    for value in (native_alias + "/private", file_alias + "%2Fprivate"):
        quoted = f'"{value}"'
        result = redact_lsp_text(quoted, repository=scope)
        assert value not in result
        assert result.count("<repository>") == 1

    for value in (
        str(root).swapcase() + "-sibling/private",
        file_alias + "%2Dother/private",
    ):
        quoted = f'"{value}"'
        assert redact_lsp_text(quoted, repository=scope) == quoted

    pure = PureWindowsPath(str(root))
    parent_alias = pure.drive + "/../" + "/".join(pure.parts[1:])
    assert redact_lsp_text(parent_alias, repository=scope) == "<repository>"

    long_alias = _windows_normalized_component_alias(root, dot_repetitions=4096)
    long_value = f'"{long_alias}/private"'
    long_result = redact_lsp_text(long_value, repository=scope)
    assert long_result == '"<repository>"'


@pytest.mark.skipif(os.name != "nt", reason="Windows Unicode path aliases")
def test_redaction_decodes_percent_encoded_unicode_only_in_windows_file_uris(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "RÉPO-ΟΣ"
    repository.mkdir()
    scope = resolve_repository_scope(repository)
    root = Path(scope.checkout_root)
    native_alias = str(root).swapcase()
    file_alias = path_to_file_uri(root)
    for value in (native_alias + "/private", file_alias + "%5Cprivate"):
        result = redact_lsp_text(value, repository=scope)
        assert value not in result
        assert result.count("<repository>") == 1

    encoded_native = (
        str(root)
        .replace("É", "%C3%A9")
        .replace("Ο", "%CE%9F")
        .replace("Σ", "%CE%A3")
    )
    for value in (
        native_alias + "-other",
        encoded_native + "/private",
        file_alias + "%2Dother",
    ):
        assert redact_lsp_text(value, repository=scope) == value

    malformed = file_alias + "%C3" * 4096
    malformed_result = redact_lsp_text(malformed, repository=scope)
    assert "<repository>" not in malformed_result
    assert len(malformed_result) <= 1024


def test_redaction_semantically_normalizes_bounded_windows_path_tokens() -> None:
    root = Path(r"D:\projects\My RÉPO")
    native_aliases = (
        r"D:\decoy\..\projects\My RÉPO\private.py",
        r"D:/projects/scratch/../My RÉPO./private.py",
        r"D:\projects\My RÉPO\child\..\.",
        r"D:\\projects\\My RÉPO\private.py",
    )
    uri_aliases = (
        "file:///D:/decoy/../projects/My%20R%C3%A9PO/private.py",
        "file:%2F%2Flocalhost%2FD:%2Fprojects%2Fscratch%2F..%2F"
        "My%20R%C3%A9PO%2Fprivate.py",
        "file:/D:/projects/My%20R%C3%A9PO/child/../.",
    )
    for token in native_aliases + uri_aliases:
        assert lsp_security._redact_path(
            f'path="{token}"', root, "<repository>"
        ) == 'path="<repository>"'

    outside_aliases = (
        r"D:\projects\My RÉPO-other\private.py",
        r"D:\projects\My RÉPO\..\sibling\private.py",
        "file:///D:/projects/My%20R%C3%A9PO/../sibling/private.py",
        r"\\server\share\projects\My RÉPO\private.py",
        r"\\?\D:\projects\My RÉPO\private.py",
        "file://server/D:/projects/My%20R%C3%A9PO/private.py",
    )
    for token in outside_aliases:
        value = f'path="{token}"'
        assert lsp_security._redact_path(value, root, "<repository>") == value

    long_token = (
        "D:/"
        + "./" * 4096
        + "projects/My RÉPO/"
        + "child/../" * 4096
        + "private.py"
    )
    assert lsp_security._redact_path(
        f'path="{long_token}"', root, "<repository>"
    ) == 'path="<repository>"'

    malformed = r"D:\projects\My%FFRÉPO\private.py"
    assert lsp_security._redact_path(
        malformed, root, "<repository>"
    ) == malformed

    punctuation_root = Path(r"D:\projects\repo(name), operator's [v1]")
    punctuation_token = r"D:\projects\repo(name), operator's [v1]\private.py"
    assert lsp_security._redact_path(
        f'path="{punctuation_token}"', punctuation_root, "<repository>"
    ) == 'path="<repository>"'


def test_redaction_matches_simulated_mixed_windows_short_path_components(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = Path(r"D:\Long Parent Component\Long Repository Component")
    short = Path(r"D:\LONGPA~1\LONGRE~1")
    monkeypatch.setattr(
        windows_workspace,
        "get_short_path",
        lambda path: short if path == root else path,
        raising=False,
    )

    for token in (
        r"D:\LONGPA~1\Long Repository Component\private.py",
        r"D:\Long Parent Component\LONGRE~1\private.py",
        r"D:\LONGPA~1\LONGRE~1\private.py",
        "file:///D:/LONGPA~1/Long%20Repository%20Component/private.py",
        "file:///D:/Long%20Parent%20Component/LONGRE~1/private.py",
    ):
        value = f'"{token}"'
        assert lsp_security._redact_path(
            value, root, "<repository>"
        ) == '"<repository>"'

    for sibling in (
        r"D:\LONGPA~1\LONGRE~1-other\private.py",
        r"D:\Long Parent Component\Long Repository Component-old\private.py",
        "file:///D:/LONGPA~1/LONGRE~1-other/private.py",
    ):
        value = f'"{sibling}"'
        assert lsp_security._redact_path(
            value, root, "<repository>"
        ) == value

    def unavailable(_path: Path) -> Path:
        raise OSError("short names unavailable")

    monkeypatch.setattr(windows_workspace, "get_short_path", unavailable)
    long_token = str(PureWindowsPath(root) / "private.py")
    value = f'"{long_token}"'
    assert lsp_security._redact_path(
        value, root, "<repository>"
    ) == '"<repository>"'


@pytest.mark.skipif(os.name != "nt", reason="Windows real 8.3 redaction")
def test_redaction_matches_real_mixed_windows_short_path_components(
    tmp_path: Path,
) -> None:
    supported, reason = windows_workspace.capability()
    if not supported:
        pytest.skip(reason or "Windows native workspace APIs unavailable")
    root = (
        tmp_path
        / "long-redaction-parent-component"
        / "long-redaction-repository-component"
    )
    root.mkdir(parents=True)
    long = PureWindowsPath(root)
    short = PureWindowsPath(windows_workspace.get_short_path(root))
    long_components = long.parts[1:]
    short_components = short.parts[1:]
    differing = [
        index
        for index, (long_name, short_name) in enumerate(
            zip(long_components, short_components)
        )
        if long_name.casefold() != short_name.casefold()
    ]
    if len(differing) < 2:
        pytest.skip("two real 8.3 component aliases are unavailable")

    for short_index in differing[:2]:
        components = list(long_components)
        components[short_index] = short_components[short_index]
        token = long.drive + "\\" + "\\".join((*components, "private.py"))
        assert lsp_security._redact_path(
            token, root, "<repository>"
        ) == "<repository>"

    sibling_components = list(short_components)
    sibling_components[-1] += "-other"
    sibling = long.drive + "\\" + "\\".join((*sibling_components, "private.py"))
    assert lsp_security._redact_path(
        sibling, root, "<repository>"
    ) == sibling


def test_native_windows_percent_triplets_are_literal_and_file_uris_decode_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = Path(r"D:\projects\repo%20name")

    def unavailable(_path: Path) -> Path:
        raise OSError("short names unavailable")

    monkeypatch.setattr(
        windows_workspace, "get_short_path", unavailable, raising=False
    )
    native = r"D:\projects\repo%20name\private.py"
    assert lsp_security._redact_path(
        native, root, "<repository>"
    ) == "<repository>"

    for different_native in (
        r"D:\projects\repo name\private.py",
        r"%44%3A\projects\repo%20name\private.py",
    ):
        assert lsp_security._redact_path(
            different_native, root, "<repository>"
        ) == different_native

    encoded_once = "file:///D:/projects/repo%2520name/private.py"
    assert lsp_security._redact_path(
        encoded_once, root, "<repository>"
    ) == "<repository>"
    decoded_twice = "file:///D:/projects/repo%20name/private.py"
    assert lsp_security._redact_path(
        decoded_twice, root, "<repository>"
    ) == decoded_twice


def test_quoted_windows_paths_allow_apostrophes_and_preserve_closing_quote(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = Path(r"D:\projects\operator's repository")
    monkeypatch.setattr(
        windows_workspace, "get_short_path", lambda path: path, raising=False
    )

    value = f'path="{root}\\private.py" after'
    assert lsp_security._redact_path(
        value, root, "<repository>"
    ) == 'path="<repository>" after'

    sibling = f'path="{root}-old\\private.py" after'
    assert lsp_security._redact_path(
        sibling, root, "<repository>"
    ) == sibling

    plain_root = Path(r"D:\projects\operator")
    apostrophe_sibling = (
        rf"path={plain_root}'s repository\private.py after"
    )
    assert lsp_security._redact_path(
        apostrophe_sibling, plain_root, "<repository>"
    ) == apostrophe_sibling

    compact_root = Path(r"D:\projects\operator's-repository")
    compact_value = str(compact_root) + r"\private.py:12 - diagnostic"
    assert lsp_security._redact_path(
        compact_value, compact_root, "<repository>"
    ) == "<repository>:12 - diagnostic"


def test_windows_path_log_suffixes_preserve_valid_root_punctuation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = Path(r"D:\projects\repo(name),operator's[v1]}")
    monkeypatch.setattr(windows_workspace, "get_short_path", lambda path: path)
    source = str(PureWindowsPath(root) / "source.py")

    for prefix, suffix in (
        ("", ":12"),
        ("", ":12:34"),
        ('"', '":12:34).,;]}'),
        ('"', '"'),
    ):
        value = f"path={prefix}{source}{suffix}"
        assert lsp_security._redact_path(
            value, root, "<repository>"
        ) == f"path={prefix}<repository>{suffix}"

    for punctuation in (".", ",", ";", ")", "]", "}", ".,;)]}"):
        value = str(root) + punctuation
        result = lsp_security._redact_path(value, root, "<repository>")
        expected_suffix = "" if punctuation == "." else punctuation
        assert result == "<repository>" + expected_suffix

        sibling = str(root) + "-other" + punctuation
        assert lsp_security._redact_path(
            sibling, root, "<repository>"
        ) == sibling


def test_windows_tokenizer_handles_terminal_suffixes_and_long_punctuation_roots(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    components = tuple(
        f"segment-{index:02d}-" + "(),;[]{}" * 12
        for index in range(12)
    )
    root_text = "D:\\" + "\\".join(components)
    assert len(root_text) > 1024
    root = Path(root_text)
    monkeypatch.setattr(windows_workspace, "get_short_path", lambda path: path)
    source = root_text + r"\source.py"
    uri = "file:///D:/" + "/".join(components) + "/source.py"

    cases = (
        (source + ":12:34 - diagnostic", "<repository>:12:34 - diagnostic"),
        (uri + ":56:78 - uri diagnostic", "<repository>:56:78 - uri diagnostic"),
        (
            f'path="{source}":90:12). next',
            'path="<repository>":90:12). next',
        ),
        (
            root_text + ".,;)]} - root diagnostic",
            "<repository>.,;)]} - root diagnostic",
        ),
    )
    for value, expected in cases:
        assert lsp_security._redact_path(value, root, "<repository>") == expected

    sibling = root_text + "-other" + r"\source.py:12:34 - diagnostic"
    assert lsp_security._redact_path(
        sibling, root, "<repository>"
    ) == sibling


def test_windows_tokenizer_scales_near_linearly_for_200_400_800_tokens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = Path(r"D:\projects\linear-repository")
    root_text = str(root)
    uri_root = "file:///D:/projects/linear-repository"
    monkeypatch.setattr(windows_workspace, "get_short_path", lambda path: path)
    real_canonicalize = lsp_security._canonical_windows_path_token
    canonical_calls = 0

    def canonicalize(*args, **kwargs):
        nonlocal canonical_calls
        canonical_calls += 1
        return real_canonicalize(*args, **kwargs)

    monkeypatch.setattr(
        lsp_security, "_canonical_windows_path_token", canonicalize
    )

    def measure(count: int) -> float:
        tokens: list[str] = []
        for index in range(count):
            if index % 4 == 0:
                token = f"path={root_text}\\pkg\\module-{index}.py:12:34).,;]}}"
            elif index % 4 == 1:
                token = f"uri={uri_root}/pkg/module-{index}.py:56:78).,;]}}"
            elif index % 4 == 2:
                token = f'path="{root_text}\\pkg\\module-{index}.py"'
            else:
                token = (
                    f"path={root_text}-sibling\\module-{index}.py:90:12).,;]}}"
                )
            tokens.append(token)
        value = " ".join(tokens)
        calls_before = canonical_calls
        started = time.perf_counter()
        result = lsp_security._redact_path(value, root, "<repository>")
        elapsed = time.perf_counter() - started
        assert result.count("<repository>") == count * 3 // 4
        assert canonical_calls - calls_before <= count * 2 + 2
        return elapsed

    timings = tuple(
        min(measure(count) for _attempt in range(2))
        for count in (200, 400, 800)
    )

    assert timings[1] <= max(0.05, timings[0] * 3.25)
    assert timings[2] <= max(0.05, timings[1] * 3.25)
    assert sum(timings) < 5.0


def test_windows_file_uri_token_ceiling_does_not_split_percent_triplet(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = Path(r"D:\projects\bounded-repository")
    monkeypatch.setattr(windows_workspace, "get_short_path", lambda path: path)
    prefix = "file:///D:/projects/bounded-repository/"
    filler_length = lsp_security._MAX_REDACTION_PATH_TOKEN - len(prefix) - 1
    filler = "./" * (filler_length // 2) + "." * (filler_length % 2)
    value = prefix + filler + "%2E/child.py"

    assert lsp_security._redact_path(
        value, root, "<repository>"
    ) == "<repository>%2E/child.py"


def test_redaction_removes_localhost_file_uri_roots(scope: RepositoryScope) -> None:
    repository_uri = path_to_file_uri(Path(scope.checkout_root)).replace(
        "file://", "file://localhost", 1
    )
    home_uri = path_to_file_uri(Path.home().absolute()).replace(
        "file://", "file://localhost", 1
    )

    result = redact_lsp_text(
        f"{repository_uri}/pkg/api.py {home_uri}/.ssh/config",
        repository=scope,
    )

    assert repository_uri not in result
    assert home_uri not in result
    assert "<repository>" in result
    assert "<home>" in result


@pytest.mark.parametrize(
    "authority",
    ["loc%61lhost", "%4Co%43a%4Ch%4Fs%54", "LOCAL%68OST"],
)
def test_redaction_removes_percent_encoded_localhost_file_uri_roots(
    scope: RepositoryScope,
    authority: str,
) -> None:
    if os.name == "nt":
        repository_uri = (
            f"file:%2F\\{authority}/%5c"
            f"{_encoded_windows_alias_root(Path(scope.checkout_root))}"
        )
        home_uri = (
            f"file:%5C/{authority}%2f\\"
            f"{_encoded_windows_alias_root(Path.home().absolute())}"
        )
    else:
        repository_uri = _encode_alternating_uri_letters(
            path_to_file_uri(Path(scope.checkout_root))
        ).replace(
            "file:///", f"file://{authority}/", 1
        )
        home_uri = _encode_alternating_uri_letters(
            path_to_file_uri(Path.home().absolute())
        ).replace(
            "file:///", f"file://{authority}/", 1
        )

    result = redact_lsp_text(
        f"{repository_uri}/pkg/api.py {home_uri}/.ssh/config",
        repository=scope,
    )

    assert repository_uri not in result
    assert home_uri not in result
    assert "<repository>" in result
    assert "<home>" in result


@pytest.mark.parametrize("authority", ["loc%G1lhost", "localhost%", "%FF"])
def test_redaction_handles_malformed_file_uri_authority_safely(
    scope: RepositoryScope,
    authority: str,
) -> None:
    uri = path_to_file_uri(Path(scope.checkout_root)).replace(
        "file:///", f"file://{authority}/", 1
    )

    result = redact_lsp_text(uri + "\x00", repository=scope)

    assert isinstance(result, str)
    assert len(result) <= 1024
    assert "\x00" not in result


def _encode_alternating_uri_letters(uri: str) -> str:
    pieces = [uri[:5]]
    encode = True
    index = 5
    while index < len(uri):
        character = uri[index]
        if character == "%" and re.match(r"[0-9A-Fa-f]{2}", uri[index + 1 : index + 3]):
            pieces.append(uri[index : index + 3])
            index += 3
            continue
        if character.isascii() and character.isalpha() and encode:
            pieces.append(f"%{ord(character):02X}")
            encode = False
        else:
            pieces.append(character)
            if character.isascii() and character.isalpha():
                encode = True
        index += 1
    return "".join(pieces)


def test_redaction_removes_arbitrarily_percent_encoded_repository_and_home_uris(
    scope: RepositoryScope,
) -> None:
    repository_uri = _encode_alternating_uri_letters(
        path_to_file_uri(Path(scope.checkout_root))
    )
    home_uri = _encode_alternating_uri_letters(path_to_file_uri(Path.home().absolute()))

    result = redact_lsp_text(
        f"source={repository_uri}/pkg/api.py home={home_uri}/.ssh/config",
        repository=scope,
    )

    assert repository_uri not in result
    assert home_uri not in result
    assert "<repository>" in result
    assert "<home>" in result


def test_redaction_handles_mixed_literal_utf8_uri_before_truncation(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "projects" / "répository"
    repository.mkdir(parents=True)
    scope = resolve_repository_scope(repository)
    encoded = path_to_file_uri(repository)
    mixed = encoded.replace("projects", "%70ro%6Aects").replace("%C3%A9", "é")
    assert mixed != encoded

    result = redact_lsp_text(
        "x" * 1000 + " " + mixed + "/%GG\x00",
        repository=scope,
    )

    assert len(result) <= 1024
    assert "<repository>" in result
    assert mixed not in result
    assert "\x00" not in result


def test_redaction_neutralizes_cr_lf_and_control_injection() -> None:
    value = "first\r\nforged=entry\t\x00\x1b\x7f\x85\u2028\u2029last"

    result = redact_lsp_text(value)

    assert not any(ord(character) < 32 or 127 <= ord(character) <= 159 for character in result)
    assert "\u2028" not in result and "\u2029" not in result
    assert "forged=entry" in result


def test_redaction_neutralizes_unicode_format_and_bidi_controls() -> None:
    controls = "\u200b\u200d\u202e\u2066\ufeff"
    assert all(unicodedata.category(character) == "Cf" for character in controls)

    result = redact_lsp_text("before" + controls + "after")

    assert not any(unicodedata.category(character) == "Cf" for character in result)
    assert result == "beforeafter"


def test_redaction_normalizes_controls_before_detecting_credentials() -> None:
    key = "OPENAI_API_KEY"
    insertions = (
        "\x00",
        "\r\n",
        "\x85",
        "\x1b[0m",
        "\x1b[38;5;196m",
        "\u200b",
        "\u200d",
        "\u202e",
        "\u2066",
        "\ufeff",
    )
    for insertion in insertions:
        for boundary in range(len(key) + 1):
            value = key[:boundary] + insertion + key[boundary:] + "=boundary-secret"
            result = redact_lsp_text(value)
            assert "boundary-secret" not in result
            assert not any(lsp_security._is_control(character) for character in result)
            assert result == key + "=<redacted>"

    long_value = "TOKEN" + "\u200b" * 32_768 + "=long-control-secret"
    assert redact_lsp_text(long_value) == "TOKEN=<redacted>"


def test_redaction_strips_every_terminal_string_mode_before_credentials() -> None:
    terminated = (
        "\x1b[38;5;196m",
        "\x9b38;5;196m",
        "\x1b]0;printable-payload\x07",
        "\x1b]0;printable-payload\x1b\\",
        "\x9d0;printable-payload\x9c",
        "\x1bPprintable-payload\x1b\\",
        "\x90printable-payload\x9c",
        "\x1bXprintable-payload\x1b\\",
        "\x98printable-payload\x9c",
        "\x1b^printable-payload\x1b\\",
        "\x9eprintable-payload\x9c",
        "\x1b_printable-payload\x1b\\",
        "\x9fprintable-payload\x9c",
    )
    for key in ("TOKEN", "OPENAI_API_KEY"):
        for sequence in terminated:
            for boundary in range(len(key) + 1):
                value = key[:boundary] + sequence + key[boundary:] + "=mode-secret"
                result = redact_lsp_text(value)
                assert result == key + "=<redacted>"
                assert "mode-secret" not in result
                assert "printable-payload" not in result
                assert not any(lsp_security._is_control(character) for character in result)

    unterminated = (
        "\x1b]unterminated-payload",
        "\x9dunterminated-payload",
        "\x1bPunterminated-payload",
        "\x90unterminated-payload",
        "\x1bXunterminated-payload",
        "\x98unterminated-payload",
        "\x1b^unterminated-payload",
        "\x9eunterminated-payload",
        "\x1b_unterminated-payload",
        "\x9funterminated-payload",
    )
    for sequence in unterminated:
        for boundary in range(len("TOKEN") + 1):
            value = "TOKEN"[:boundary] + sequence + "TOKEN"[boundary:] + "=tail-secret"
            result = redact_lsp_text(value)
            assert "tail-secret" not in result
            assert "unterminated-payload" not in result
            assert not any(lsp_security._is_control(character) for character in result)

    assert lsp_security._normalize_log_text("prefix\x1b[31") == "prefix"
    assert lsp_security._normalize_log_text("prefix\x9b31") == "prefix"


def test_redaction_consumes_generic_two_character_escape_controls() -> None:
    specialized = frozenset("PX[\\]^_")
    finals = tuple(
        character
        for character in map(chr, range(ord("@"), ord("_") + 1))
        if character not in specialized
    ) + ("c",)

    for final in finals:
        sequence = "\x1b" + final
        assert lsp_security._normalize_log_text(
            "before" + sequence + "after"
        ) == "beforeafter"
        for key in ("TOKEN", "OPENAI_API_KEY"):
            for boundary in range(len(key) + 1):
                value = key[:boundary] + sequence + key[boundary:] + "=escape-secret"
                assert redact_lsp_text(value) == key + "=<redacted>"
        assert redact_lsp_text(
            "https://oper" + sequence + "ator:url-secret@example.test/private"
        ) == "https://<redacted>@example.test/private"

    assert lsp_security._normalize_log_text("before\x1b") == "before"
    assert lsp_security._normalize_log_text("before\x1bc") == "before"


def test_redaction_consumes_iso_escape_intermediate_sequences() -> None:
    intermediates = tuple(map(chr, range(0x20, 0x30)))
    sequences = tuple("\x1b" + intermediate + "B" for intermediate in intermediates)
    sequences += ("\x1b" + "".join(intermediates) + "B",)

    for sequence in sequences:
        assert lsp_security._normalize_log_text(
            "before" + sequence + "after"
        ) == "beforeafter"
        for key in ("TOKEN", "OPENAI_API_KEY"):
            for boundary in range(len(key) + 1):
                value = key[:boundary] + sequence + key[boundary:] + "=escape-secret"
                assert redact_lsp_text(value) == key + "=<redacted>"
        assert redact_lsp_text(
            "https://oper" + sequence + "ator:url-secret@example.test/private"
        ) == "https://<redacted>@example.test/private"

    for intermediate in intermediates:
        assert lsp_security._normalize_log_text(
            "before\x1b" + intermediate
        ) == "before"
    assert lsp_security._normalize_log_text(
        "before\x1b" + "".join(intermediates)
    ) == "before"


@pytest.mark.parametrize(
    "sequence",
    (
        "\x1b \x1b[31m",
        "\x1b \x1b#\x1b[31m",
        "\x1b#\x1b]title\x07",
        "\x1b(\x1b]title\x1b\\",
        "\x1b)\x1bPpayload\x1b\\",
        "\x1b*\x1bXpayload\x1b\\",
        "\x1b+\x1b^payload\x1b\\",
        "\x1b-\x1b_payload\x1b\\",
    ),
)
def test_redaction_restarts_escape_intermediates_at_nested_escape(
    sequence: str,
) -> None:
    value = "TOK" + sequence + "EN=restart-secret"

    assert lsp_security._normalize_log_text(value) == "TOKEN=restart-secret"
    assert redact_lsp_text(value) == "TOKEN=<redacted>"


def test_redaction_happens_before_sanitized_output_is_truncated() -> None:
    value = "x" * 1000 + " ToKeN=" + "s" * 200 + "\r\nforged"

    result = redact_lsp_text(value)

    assert len(result) <= 1024
    assert "sssss" not in result
    assert "<redacted>" in result
    assert "\r" not in result and "\n" not in result


def test_redaction_leaves_normal_safe_text_unchanged() -> None:
    value = "Pyright returned 3 definitions for pkg/api.py"

    assert redact_lsp_text(value) == value


def test_redaction_rejects_wrong_types(scope: RepositoryScope) -> None:
    with pytest.raises(TypeError):
        redact_lsp_text(b"text")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        redact_lsp_text("text", repository=object())  # type: ignore[arg-type]


@pytest.mark.skipif(os.name != "posix", reason="POSIX descriptor traversal")
@pytest.mark.parametrize(
    "relative_path",
    ["missing.py", "pkg", "plain.py/child.py"],
)
def test_posix_descriptors_close_on_failure(
    scope: RepositoryScope,
    monkeypatch: pytest.MonkeyPatch,
    relative_path: str,
) -> None:
    opened: list[int] = []
    closed: list[int] = []
    real_open = os.open
    real_close = os.close

    def tracking_open(*args, **kwargs) -> int:
        descriptor = real_open(*args, **kwargs)
        opened.append(descriptor)
        return descriptor

    def tracking_close(descriptor: int) -> None:
        closed.append(descriptor)
        real_close(descriptor)

    monkeypatch.setattr(lsp_security.os, "open", tracking_open)
    monkeypatch.setattr(lsp_security.os, "close", tracking_close)

    with pytest.raises(PathContainmentError):
        resolve_repository_source(scope, relative_path)

    assert Counter(opened) == Counter(closed)


@pytest.mark.skipif(os.name != "posix", reason="POSIX descriptor traversal")
def test_posix_parent_substitution_is_detected_and_descriptors_close(
    repository: Path,
    scope: RepositoryScope,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opened: list[int] = []
    closed: list[int] = []
    real_open = os.open
    real_close = os.close
    original = repository / "pkg"
    moved = repository / "pkg-original"

    def tracking_open(*args, **kwargs) -> int:
        descriptor = real_open(*args, **kwargs)
        opened.append(descriptor)
        return descriptor

    def tracking_close(descriptor: int) -> None:
        closed.append(descriptor)
        real_close(descriptor)

    def substitute() -> None:
        original.rename(moved)
        original.mkdir()
        (original / "api.py").write_text("substitute = True\n", encoding="utf-8")

    monkeypatch.setattr(lsp_security.os, "open", tracking_open)
    monkeypatch.setattr(lsp_security.os, "close", tracking_close)
    monkeypatch.setattr(lsp_security, "_resolution_barrier", substitute)

    with pytest.raises(PathContainmentError):
        resolve_repository_source(scope, "pkg/api.py")

    assert Counter(opened) == Counter(closed)


def _track_windows_handles(monkeypatch: pytest.MonkeyPatch) -> tuple[list[int], list[int]]:
    owned: list[int] = []
    closed: list[int] = []
    active: set[int] = set()
    original_own = lsp_security._OwnedHandles.own

    def tracking_own(self, handle: int) -> int:
        result = original_own(self, handle)
        owned.append(handle)
        active.add(handle)
        return result

    original_close = windows_workspace.close_handle

    def tracking_close(handle: int) -> None:
        original_close(handle)
        if handle in active:
            active.remove(handle)
            closed.append(handle)

    monkeypatch.setattr(lsp_security._OwnedHandles, "own", tracking_own)
    monkeypatch.setattr(windows_workspace, "close_handle", tracking_close)
    return owned, closed


def _windows_short_component_or_skip(path: Path) -> str:
    try:
        short_path = windows_workspace.get_short_path(path)
    except OSError:
        pytest.skip("Windows short-name lookup is unavailable")
    short_name = short_path.name
    if short_name.casefold() == path.name.casefold() or "~" not in short_name:
        pytest.skip("8.3 name generation is disabled on this volume")
    return short_name


@pytest.mark.skipif(os.name != "nt", reason="Windows handle traversal")
@pytest.mark.parametrize(
    ("relative_path", "omitted_name"),
    [("pkg/generated.py", "pkg"), ("pkg/api.py", "api.py")],
)
def test_windows_unenumerated_exact_component_is_not_treated_as_missing(
    scope: RepositoryScope,
    monkeypatch: pytest.MonkeyPatch,
    relative_path: str,
    omitted_name: str,
) -> None:
    supported, reason = windows_workspace.capability()
    if not supported:
        pytest.skip(reason or "Windows handle-relative APIs unavailable")
    original_list = windows_workspace.list_directory

    def omit_component(handle: int, *, max_entries: int) -> list[WindowsEntry]:
        return [
            entry
            for entry in original_list(handle, max_entries=max_entries)
            if entry.name != omitted_name
        ]

    monkeypatch.setattr(windows_workspace, "list_directory", omit_component)
    opened, closed = _track_windows_handles(monkeypatch)

    with pytest.raises(PathContainmentError):
        resolve_repository_source(scope, relative_path, must_exist=False)

    assert Counter(opened) == Counter(closed)
    assert all(count == 1 for count in Counter(opened).values())


@pytest.mark.skipif(os.name != "nt", reason="Windows 8.3 aliases")
@pytest.mark.parametrize("kind", ["file", "directory"])
def test_windows_short_file_and_directory_aliases_are_rejected_and_handles_close(
    repository: Path,
    scope: RepositoryScope,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    supported, reason = windows_workspace.capability()
    if not supported:
        pytest.skip(reason or "Windows handle-relative APIs unavailable")
    if kind == "file":
        target = repository / "pkg" / "long-generated-source-component.py"
        target.write_text("generated = True\n", encoding="utf-8")
        relative_path = f"pkg/{_windows_short_component_or_skip(target)}"
    else:
        target = repository / "long-generated-directory-component"
        target.mkdir()
        relative_path = f"{_windows_short_component_or_skip(target)}/missing.py"
    opened, closed = _track_windows_handles(monkeypatch)

    with pytest.raises(PathContainmentError):
        resolve_repository_source(scope, relative_path, must_exist=False)

    assert Counter(opened) == Counter(closed)
    assert all(count == 1 for count in Counter(opened).values())


@pytest.mark.skipif(os.name != "nt", reason="Windows 8.3 junction alias")
def test_windows_short_junction_alias_is_rejected_without_leaking_handles(
    repository: Path,
    scope: RepositoryScope,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    supported, reason = windows_workspace.capability()
    if not supported:
        pytest.skip(reason or "Windows handle-relative APIs unavailable")
    outside = repository.parent / "outside-short-junction"
    outside.mkdir()
    junction = repository / "long-generated-junction-component"
    created = subprocess.run(
        ["cmd.exe", "/d", "/c", "mklink", "/J", str(junction), str(outside)],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=False,
        timeout=10,
    )
    if created.returncode != 0:
        pytest.skip("junction creation is unavailable")
    short_name = _windows_short_component_or_skip(junction)
    opened, closed = _track_windows_handles(monkeypatch)

    with pytest.raises(PathContainmentError):
        resolve_repository_source(scope, f"{short_name}/missing.py", must_exist=False)

    assert Counter(opened) == Counter(closed)
    assert all(count == 1 for count in Counter(opened).values())


@pytest.mark.skipif(os.name != "nt", reason="Windows handle traversal")
@pytest.mark.parametrize(
    "relative_path",
    ["missing.py", "pkg", "plain.py/child.py"],
)
def test_windows_handles_close_on_failure(
    scope: RepositoryScope,
    monkeypatch: pytest.MonkeyPatch,
    relative_path: str,
) -> None:
    supported, reason = windows_workspace.capability()
    if not supported:
        pytest.skip(reason or "Windows handle-relative APIs unavailable")
    opened, closed = _track_windows_handles(monkeypatch)

    with pytest.raises(PathContainmentError):
        resolve_repository_source(scope, relative_path)

    assert Counter(opened) == Counter(closed)
    assert all(count == 1 for count in Counter(opened).values())


@pytest.mark.skipif(os.name != "nt", reason="Windows handle traversal")
def test_windows_parent_identity_substitution_is_detected_and_handles_close(
    scope: RepositoryScope,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    supported, reason = windows_workspace.capability()
    if not supported:
        pytest.skip(reason or "Windows handle-relative APIs unavailable")
    opened, closed = _track_windows_handles(monkeypatch)
    original_identity = windows_workspace.identity
    root_handles: set[int] = set()
    original_root_open = windows_workspace.open_directory_path
    substituted = False

    def root_open(path: Path) -> int:
        handle = original_root_open(path)
        root_handles.add(handle)
        return handle

    def identity(handle: int, *, directory: bool | None = None):
        value = original_identity(handle, directory=directory)
        if substituted and directory is True and handle not in root_handles:
            return value[0], bytes([value[1][0] ^ 1]) + value[1][1:], value[2]
        return value

    def substitute() -> None:
        nonlocal substituted
        substituted = True

    monkeypatch.setattr(windows_workspace, "open_directory_path", root_open)
    monkeypatch.setattr(windows_workspace, "identity", identity)
    monkeypatch.setattr(lsp_security, "_resolution_barrier", substitute)

    with pytest.raises(PathContainmentError):
        resolve_repository_source(scope, "pkg/api.py")

    assert Counter(opened) == Counter(closed)
    assert all(count == 1 for count in Counter(opened).values())


@pytest.mark.skipif(os.name != "nt", reason="Windows handle traversal")
def test_windows_source_identity_substitution_is_detected_and_handles_close(
    scope: RepositoryScope,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    supported, reason = windows_workspace.capability()
    if not supported:
        pytest.skip(reason or "Windows handle-relative APIs unavailable")
    opened, closed = _track_windows_handles(monkeypatch)
    original_identity = windows_workspace.identity

    def identity(handle: int, *, directory: bool | None = None):
        value = original_identity(handle, directory=directory)
        if directory is False:
            return value[0], bytes([value[1][0] ^ 1]) + value[1][1:], value[2]
        return value

    monkeypatch.setattr(windows_workspace, "identity", identity)

    with pytest.raises(PathContainmentError, match="changed before open"):
        resolve_repository_source(scope, "pkg/api.py")

    assert Counter(opened) == Counter(closed)
    assert all(count == 1 for count in Counter(opened).values())


@pytest.mark.skipif(os.name != "nt", reason="Windows handle traversal")
def test_windows_thousand_failures_do_not_grow_native_handle_count(
    scope: RepositoryScope,
) -> None:
    supported, reason = windows_workspace.capability()
    if not supported:
        pytest.skip(reason or "Windows handle-relative APIs unavailable")
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    get_current_process = kernel32.GetCurrentProcess
    get_current_process.restype = wintypes.HANDLE
    get_handle_count = kernel32.GetProcessHandleCount
    get_handle_count.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    get_handle_count.restype = wintypes.BOOL

    def handle_count() -> int:
        count = wintypes.DWORD()
        if not get_handle_count(get_current_process(), ctypes.byref(count)):
            raise ctypes.WinError(ctypes.get_last_error())
        return int(count.value)

    with pytest.raises(PathContainmentError):
        resolve_repository_source(scope, "missing.py")
    before = handle_count()
    for _index in range(1000):
        with pytest.raises(PathContainmentError):
            resolve_repository_source(scope, "missing.py")

    assert handle_count() == before


def test_security_boundary_documents_trusted_repository_not_sandbox() -> None:
    text = lsp_security.__doc__ or ""

    assert "trusted" in text.casefold()
    assert "not a sandbox" in text.casefold()
    assert "navigation evidence" in text.casefold()
