"""Repository-contained LSP source and safe diagnostic text tests."""

from __future__ import annotations

import inspect
import os
import re
import socket
import subprocess
from collections import Counter
from dataclasses import FrozenInstanceError, fields
from pathlib import Path
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
    command = f'mklink /J "{junction}" "{outside}"'
    created = subprocess.run(
        ["cmd.exe", "/d", "/s", "/c", command],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=False,
        timeout=10,
    )
    if created.returncode != 0:
        pytest.skip("junction creation unavailable")

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


def test_redaction_removes_native_separator_variants(scope: RepositoryScope) -> None:
    slash = scope.checkout_root.replace("\\", "/")
    backslash = slash.replace("/", "\\")

    result = redact_lsp_text(f"{slash}/pkg/api.py {backslash}\\pkg\\api.py", repository=scope)

    assert slash not in result
    assert backslash not in result
    assert result.count("<repository>") == 2


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


def test_redaction_neutralizes_cr_lf_and_control_injection() -> None:
    value = "first\r\nforged=entry\t\x00\x1b\x7f\x85\u2028\u2029last"

    result = redact_lsp_text(value)

    assert not any(ord(character) < 32 or 127 <= ord(character) <= 159 for character in result)
    assert "\u2028" not in result and "\u2029" not in result
    assert "forged=entry" in result


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
    opened: list[int] = []
    closed: list[int] = []
    for name in ("open_directory_path", "open_directory", "open_file"):
        original = getattr(windows_workspace, name)

        def tracking_open(*args, _original=original, **kwargs) -> int:
            handle = _original(*args, **kwargs)
            opened.append(handle)
            return handle

        monkeypatch.setattr(windows_workspace, name, tracking_open)
    original_close = windows_workspace.close_handle

    def tracking_close(handle: int) -> None:
        closed.append(handle)
        original_close(handle)

    monkeypatch.setattr(windows_workspace, "close_handle", tracking_close)
    return opened, closed


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

    assert Counter(opened) == (Counter(opened) & Counter(closed))


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

    assert Counter(opened) == (Counter(opened) & Counter(closed))


def test_security_boundary_documents_trusted_repository_not_sandbox() -> None:
    text = lsp_security.__doc__ or ""

    assert "trusted" in text.casefold()
    assert "not a sandbox" in text.casefold()
    assert "navigation evidence" in text.casefold()
