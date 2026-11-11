"""Repository-contained LSP source and safe diagnostic text tests."""

from __future__ import annotations

import contextlib
import inspect
import os
import re
import socket
import stat
import subprocess
import time
from collections import Counter
from dataclasses import FrozenInstanceError, fields
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

from tests.lsp_security_helpers import _symlink_or_skip, repository_fixture, scope_fixture  # noqa: F401


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

    validator = getattr(lsp_security, "validate_repository_relative_path", None)
    assert callable(validator)
    validate_signature = inspect.signature(validator)
    assert tuple(validate_signature.parameters) == ("value",)
    assert get_type_hints(validator) == {
        "value": str,
        "return": str,
    }

    reader = getattr(lsp_security, "read_repository_source_bytes", None)
    assert callable(reader)
    read_signature = inspect.signature(reader)
    assert tuple(read_signature.parameters) == (
        "repository",
        "relative_path",
        "max_bytes",
        "deadline",
    )
    assert read_signature.parameters["max_bytes"].kind is inspect.Parameter.KEYWORD_ONLY
    assert read_signature.parameters["deadline"].kind is inspect.Parameter.KEYWORD_ONLY
    assert read_signature.parameters["deadline"].default is None
    assert get_type_hints(reader) == {
        "repository": RepositoryScope,
        "relative_path": str,
        "max_bytes": int,
        "deadline": float | None,
        "return": bytes,
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


def test_public_relative_path_validator_is_lexical_and_reuses_canonical_rules() -> None:
    validator = getattr(lsp_security, "validate_repository_relative_path", None)
    assert callable(validator)
    assert validator("pkg/é.py") == "pkg/é.py"
    for invalid in (
        "pkg/CON.py",
        "pkg/api.py:stream",
        "pkg/trailing.",
        "pkg/trailing ",
        "pkg/e\u0301.py",
        "a" * 256 + "/api.py",
        "😀" * 64 + "/api.py",
        "a/" * 256 + "api.py",
    ):
        with pytest.raises(PathContainmentError):
            validator(invalid)


def test_public_relative_path_validator_is_available_to_star_imports() -> None:
    namespace: dict[str, object] = {}
    exec("from lsp_security import *", namespace)

    assert namespace["validate_repository_relative_path"] is (
        lsp_security.validate_repository_relative_path
    )


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


def test_read_repository_source_bytes_reads_one_bounded_contained_file(
    scope: RepositoryScope,
) -> None:
    reader = getattr(lsp_security, "read_repository_source_bytes", None)
    expected = (Path(scope.checkout_root) / "pkg/api.py").read_bytes()

    assert reader(
        scope,
        "pkg/api.py",
        max_bytes=1024,
        deadline=time.monotonic() + SHORT_TIMEOUT,
    ) == expected


@pytest.mark.skipif(os.name != "posix", reason="POSIX descriptor traversal")
def test_posix_repository_read_uses_retained_handle_across_swap_and_restore(
    repository: Path,
    scope: RepositoryScope,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = repository / "pkg" / "api.py"
    expected = target.read_bytes()
    saved = repository / "pkg" / "api-original.py"
    replacement = repository / "pkg" / "api-replacement.py"
    replacement.write_bytes(b"secret = True\n")
    real_read = os.read
    swapped = False

    def swap_during_read(descriptor: int, size: int) -> bytes:
        nonlocal swapped
        if not swapped:
            swapped = True
            target.rename(saved)
            replacement.rename(target)
            try:
                return real_read(descriptor, size)
            finally:
                target.rename(replacement)
                saved.rename(target)
        return real_read(descriptor, size)

    monkeypatch.setattr(lsp_security.os, "read", swap_during_read)
    reader = getattr(lsp_security, "read_repository_source_bytes", None)

    assert reader(scope, "pkg/api.py", max_bytes=1024) == expected
    assert swapped is True


@pytest.mark.skipif(os.name != "nt", reason="Windows handle traversal")
def test_windows_repository_read_uses_retained_handle_across_swap_and_restore(
    repository: Path,
    scope: RepositoryScope,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    supported, reason = windows_workspace.capability()
    if not supported:
        pytest.skip(reason or "Windows handle-relative APIs unavailable")
    target = repository / "pkg" / "api.py"
    expected = target.read_bytes()
    saved = repository / "pkg" / "api-original.py"
    replacement = repository / "pkg" / "api-replacement.py"
    replacement.write_bytes(b"secret = True\n")
    real_read_chunks = windows_workspace.read_chunks
    swapped = False

    def swap_during_read(handle: int, *, chunk_bytes: int, max_bytes: int):
        nonlocal swapped
        swapped = True
        target.rename(saved)
        replacement.rename(target)
        try:
            yield from real_read_chunks(
                handle,
                chunk_bytes=chunk_bytes,
                max_bytes=max_bytes,
            )
        finally:
            target.rename(replacement)
            saved.rename(target)

    monkeypatch.setattr(windows_workspace, "read_chunks", swap_during_read)
    reader = getattr(lsp_security, "read_repository_source_bytes", None)

    assert reader(scope, "pkg/api.py", max_bytes=1024) == expected
    assert swapped is True


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


@contextlib.contextmanager
def _bound_unix_socket(path: Path):
    """Bind by short relative name — macOS caps `sun_path` at 104 bytes.

    The pytest temporary directory alone exceeds that on macOS runners, so
    binding the absolute path fails with `AF_UNIX path too long` before the
    containment rule under test is ever reached.
    """
    listener = socket.socket(socket.AF_UNIX)
    previous = Path.cwd()
    try:
        os.chdir(path.parent)
        try:
            listener.bind(path.name)
        finally:
            os.chdir(previous)
        yield listener
    finally:
        listener.close()


@pytest.mark.skipif(
    os.name != "posix" or not hasattr(socket, "AF_UNIX"), reason="POSIX local socket"
)
def test_socket_device_like_entry_is_rejected(
    repository: Path, scope: RepositoryScope
) -> None:
    target = repository / "pkg" / "service.sock"
    with _bound_unix_socket(target):
        with pytest.raises(PathContainmentError):
            resolve_repository_source(scope, "pkg/service.sock")


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
    monkeypatch.setattr(lsp_security, "resolution_barrier", substitute)

    with pytest.raises(PathContainmentError):
        resolve_repository_source(scope, "pkg/api.py")

    assert Counter(opened) == Counter(closed)


from tests.slow_machine import SHORT_TIMEOUT  # noqa: E402


def _hold_shared_handles(ctypes, create_file, target: Path, held: list[int]) -> None:
    for desired_access in (0x80000000, 0x40000000, 0x00010000):
        held.append(_open_shared_handle(ctypes, create_file, target, desired_access))


def _all_opened_once(opened) -> bool:
    return all(count == 1 for count in Counter(opened).values())


def _root_or_handle_relative(path: str, directory: int | None) -> bool:
    """Either the absolute root itself, or a component opened against a handle."""
    if path == "/":
        return directory is None
    return not os.path.isabs(path) and directory is not None


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
    assert all(_root_or_handle_relative(path, directory) for path, directory in calls)
    assert scope.checkout_root not in {path for path, _directory in calls}


def _require_junction_created(created: subprocess.CompletedProcess) -> None:
    """Skip when junctions need a privilege this account lacks; fail on anything else."""
    if created.returncode == 0:
        return
    output = (created.stdout + created.stderr).decode(errors="replace")
    if "privilege" in output.casefold():
        pytest.skip("junction creation privilege unavailable")
    pytest.fail(f"junction creation failed: {output}")


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
    _require_junction_created(created)
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


def _windows_create_file(ctypes, wintypes, kernel32):
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
    return create_file


def _windows_close_handle(wintypes, kernel32):
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = (wintypes.HANDLE,)
    close_handle.restype = wintypes.BOOL
    return close_handle


def _close_all_handles(ctypes, close_handle, held: list[int]) -> list[int]:
    closed: list[int] = []
    for handle in reversed(held):
        if not close_handle(handle):
            raise ctypes.WinError(ctypes.get_last_error())
        closed.append(handle)
    return closed


def _open_shared_handle(ctypes, create_file, target: Path, desired_access: int) -> int:
    """Open the file with full sharing, as another process would hold it."""
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
    return int(handle)


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
    create_file = _windows_create_file(ctypes, wintypes, kernel32)
    close_handle = _windows_close_handle(wintypes, kernel32)
    target = repository / "pkg" / "api.py"
    held: list[int] = []
    external_closed: list[int] = []
    opened, closed = _track_windows_handles(monkeypatch)
    try:
        _hold_shared_handles(ctypes, create_file, target, held)
        source = resolve_repository_source(scope, "pkg/api.py")
        assert source.absolute_path == target.resolve(strict=True)
    finally:
        external_closed = _close_all_handles(ctypes, close_handle, held)

    assert Counter(opened) == Counter(closed)
    assert _all_opened_once(opened)
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

    monkeypatch.setattr(lsp_security, "resolution_barrier", substitute)

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
    monkeypatch.setattr(lsp_security, "resolution_barrier", substitute)

    with pytest.raises(PathContainmentError):
        resolve_repository_source(scope, "pkg/api.py")

    assert Counter(opened) == Counter(closed)


def _track_windows_handles(monkeypatch: pytest.MonkeyPatch) -> tuple[list[int], list[int]]:
    owned: list[int] = []
    closed: list[int] = []
    active: set[int] = set()
    original_own = lsp_security.OwnedHandles.own

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

    monkeypatch.setattr(lsp_security.OwnedHandles, "own", tracking_own)
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
    monkeypatch.setattr(lsp_security, "resolution_barrier", substitute)

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

    # The claim is that a thousand refusals do not grow the handle table. The
    # count can legitimately fall while unrelated objects are collected, and a
    # Windows runner measured 201 against a baseline of 257.
    assert handle_count() <= before


def test_security_boundary_documents_trusted_repository_not_sandbox() -> None:
    text = lsp_security.__doc__ or ""

    assert "trusted" in text.casefold()
    assert "not a sandbox" in text.casefold()
    assert "navigation evidence" in text.casefold()


def test_ambiguous_owner_acl_failure_names_the_principals_it_saw() -> None:
    import lsp_process

    path = PureWindowsPath("C:/state/run/lsp/owner")
    entries = [
        f"{path} RUNNER\\runneradmin:(OI)(CI)(F)",
        "BUILTIN\\Administrators:(OI)(CI)(F)",
    ]

    with pytest.raises(PermissionError) as failure:
        lsp_process.require_single_owner_ace(path, entries)

    message = str(failure.value)
    assert "found 2" in message
    assert "RUNNER\\runneradmin" in message
    assert "BUILTIN\\Administrators" in message


def test_single_owner_acl_entry_is_returned_unchanged() -> None:
    import lsp_process

    path = PureWindowsPath("C:/state/run/lsp/owner")
    entry = f"{path} RUNNER\\runneradmin:(OI)(CI)(F)"

    assert lsp_process.require_single_owner_ace(path, [entry]) == entry
