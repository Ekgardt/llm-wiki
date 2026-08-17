"""Live Git and non-Git workspace revision contracts."""

from __future__ import annotations

import hashlib
import inspect
import os
import shutil
import signal
import struct
import subprocess
import sys
import time
import unicodedata
from dataclasses import fields
from pathlib import Path, PurePosixPath, PureWindowsPath

import pytest
import workspace_revision
from repository_scope import resolve_repository_scope
from workspace_revision import (
    MAX_REVISION_BYTES,
    MAX_REVISION_FILES,
    PYTHON_CONFIG_NAMES,
    RevisionEntry,
    WorkspaceDelta,
    WorkspaceRevision,
    compute_workspace_revision,
    diff_workspace_revisions,
)

from tests.code_kernel_helpers import copy_python_fixture


@pytest.fixture(autouse=True)
def _isolated_global_git_configuration(tmp_path_factory, monkeypatch) -> None:
    """Keep the developer's global Git configuration out of these contracts.

    `workspace_revision` deliberately declines its private-index fast path when
    a global Git configuration could change ignore, attribute or index
    semantics, and it only tolerates a `[user]` name/email section. Any ordinary
    `~/.gitconfig` — a credential helper, an alias, `init.defaultBranch` — is
    therefore enough to move every verifier here onto the exact fallback, so the
    optimization assertions passed only on a machine whose global configuration
    happened to be empty.
    """
    home = tmp_path_factory.mktemp("git-home")
    (home / ".config").mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    for name in ("GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM", "GIT_CONFIG"):
        monkeypatch.delenv(name, raising=False)


def _entries(revision: WorkspaceRevision) -> dict[str, RevisionEntry]:
    return {entry.path: entry for entry in revision.entries}


def _symlink_or_skip(link: Path, target: Path, *, directory: bool = False) -> None:
    try:
        os.symlink(target, link, target_is_directory=directory)
    except OSError:
        pytest.skip("symlink creation is unavailable")


def _junction_or_skip(link: Path, target: Path) -> None:
    if os.name != "nt":
        pytest.skip("Windows junction test")
    system_root = Path(os.environ.get("SystemRoot", r"C:\Windows"))
    completed = subprocess.run(
        [str(system_root / "System32" / "cmd.exe"), "/d", "/c", "mklink", "/J", str(link), str(target)],
        check=False,
        capture_output=True,
        timeout=10,
    )
    if completed.returncode != 0:
        pytest.skip("junction creation is unavailable")


def _linux_memfd_available() -> bool:
    return workspace_revision._private_index_platform_supported()


def _run_git(root: Path, *arguments: str, input_bytes: bytes | None = None) -> bytes:
    return subprocess.run(
        ["git", *arguments],
        cwd=root,
        input=input_bytes,
        check=True,
        capture_output=True,
        timeout=10,
    ).stdout


def _qualify_private_git_wrapper(
    monkeypatch: pytest.MonkeyPatch,
    wrapper: Path,
) -> None:
    installation = workspace_revision._private_git_installation()
    assert installation is not None
    executable = wrapper.resolve(strict=True)
    replacement = type(installation)(
        executable,
        workspace_revision._strong_identity(executable.lstat()),
        installation.system_config_paths,
        installation.system_attribute_paths,
    )
    monkeypatch.setattr(workspace_revision, "_private_git_installation", lambda: replacement)


def _rechecksum_index(content: bytes, hash_name: str = "sha1") -> bytes:
    digest_size = hashlib.new(hash_name).digest_size
    body = content[:-digest_size]
    return body + hashlib.new(hash_name, body).digest()


def _append_index_extension(
    content: bytes,
    signature: bytes,
    payload: bytes = b"",
    *,
    hash_name: str = "sha1",
) -> bytes:
    digest_size = hashlib.new(hash_name).digest_size
    body = content[:-digest_size] + signature + struct.pack("!I", len(payload)) + payload
    return body + hashlib.new(hash_name, body).digest()


def _index_entries_end(content: bytes, hash_name: str = "sha1") -> int:
    hash_size = hashlib.new(hash_name).digest_size
    count = struct.unpack_from("!I", content, 8)[0]
    offset = 12
    for _entry in range(count):
        fixed_end = offset + 40 + hash_size + 2
        path_end = content.index(b"\0", fixed_end)
        offset += ((path_end + 1 - offset + 7) // 8) * 8
    return offset


def _verification_git_call_counts(
    scope,
    expected: WorkspaceRevision,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[bool, int, int]:
    real_exact = workspace_revision._git_state
    real_private = workspace_revision._git_state_with_private_index
    exact_calls = 0
    private_calls = 0

    def recording_exact(*args, **kwargs):
        nonlocal exact_calls
        exact_calls += 1
        return real_exact(*args, **kwargs)

    def recording_private(*args, **kwargs):
        nonlocal private_calls
        private_calls += 1
        return real_private(*args, **kwargs)

    monkeypatch.setattr(workspace_revision, "_git_state", recording_exact)
    monkeypatch.setattr(workspace_revision, "_git_state_with_private_index", recording_private)
    result = workspace_revision.verify_workspace_revision_unchanged(scope, expected)
    return result, exact_calls, private_calls


def test_public_contract_has_exact_constants_dataclass_fields_and_signatures() -> None:
    assert PYTHON_CONFIG_NAMES == frozenset(
        {
            ".python-version",
            "Pipfile",
            "Pipfile.lock",
            "poetry.lock",
            "pyproject.toml",
            "pyrightconfig.json",
            "setup.cfg",
            "tox.ini",
            "uv.lock",
        }
    )
    assert MAX_REVISION_FILES == 100_000
    assert MAX_REVISION_BYTES == 2 * 1024 * 1024 * 1024
    assert [field.name for field in fields(RevisionEntry)] == ["path", "kind", "sha256", "size"]
    assert [field.name for field in fields(WorkspaceRevision)] == [
        "repository_id",
        "checkout_id",
        "git_head",
        "entries",
        "revision_sha256",
    ]
    assert [field.name for field in fields(WorkspaceDelta)] == [
        "created",
        "changed",
        "renamed",
        "deleted",
        "configuration_changed",
    ]
    assert RevisionEntry.__dataclass_params__.frozen
    assert WorkspaceRevision.__dataclass_params__.frozen
    assert WorkspaceDelta.__dataclass_params__.frozen
    assert RevisionEntry.__slots__ == ("path", "kind", "sha256", "size")
    assert str(inspect.signature(compute_workspace_revision)) == (
        "(repository: 'RepositoryScope', *, deadline: 'float | None' = None, "
        "cancelled: 'Callable[[], bool] | None' = None) -> 'WorkspaceRevision'"
    )
    assert str(inspect.signature(diff_workspace_revisions)) == (
        "(before: 'WorkspaceRevision', after: 'WorkspaceRevision') -> 'WorkspaceDelta'"
    )


def test_revision_changes_for_dirty_untracked_deleted_and_config(repository: Path) -> None:
    scope = resolve_repository_scope(repository)
    before = compute_workspace_revision(scope)
    (repository / "pkg/api.py").write_text("class Changed:\n    pass\n", encoding="utf-8")
    (repository / "pkg/new.py").write_text("value = 1\n", encoding="utf-8")
    (repository / "pkg/base.py").unlink()
    (repository / "pyrightconfig.json").write_text(
        '{"typeCheckingMode":"strict"}', encoding="utf-8"
    )

    after = compute_workspace_revision(scope)

    assert after.revision_sha256 != before.revision_sha256
    assert {item.kind for item in after.entries} >= {
        "modified",
        "untracked",
        "deleted",
        "configuration",
    }
    assert _entries(after)["pkg/base.py"] == RevisionEntry("pkg/base.py", "deleted", None, 0)
    delta = diff_workspace_revisions(before, after)
    assert delta.created == ("pkg/new.py",)
    assert delta.changed == ("pkg/api.py", "pyrightconfig.json")
    assert delta.deleted == ("pkg/base.py",)
    assert delta.configuration_changed is True


def test_revision_bounds_containment_resolution_to_unique_paths(
    repository: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scope = resolve_repository_scope(repository)
    real_resolve = Path.resolve
    resolved_paths: list[Path] = []

    def recording_resolve(path: Path, *args, **kwargs) -> Path:
        resolved_paths.append(path)
        return real_resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", recording_resolve)

    compute_workspace_revision(scope)

    unique_paths = {os.fspath(path) for path in resolved_paths}
    assert len(resolved_paths) <= len(unique_paths) * 3


def test_unchanged_verifier_uses_one_combined_git_state(
    repository: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scope = resolve_repository_scope(repository)
    expected = compute_workspace_revision(scope)
    real_exact_state = workspace_revision._git_state
    real_private_state = workspace_revision._git_state_with_private_index
    exact_calls = 0
    private_calls = 0

    def recording_exact(*args, **kwargs):
        nonlocal exact_calls
        exact_calls += 1
        return real_exact_state(*args, **kwargs)

    def recording_private(*args, **kwargs):
        nonlocal private_calls
        private_calls += 1
        return real_private_state(*args, **kwargs)

    monkeypatch.setattr(workspace_revision, "_git_state", recording_exact)
    monkeypatch.setattr(workspace_revision, "_git_state_with_private_index", recording_private)

    assert workspace_revision.verify_workspace_revision_unchanged(scope, expected) is True
    assert exact_calls + private_calls == 1


def test_unchanged_verifier_hashes_content_instead_of_trusting_metadata(
    repository: Path,
) -> None:
    scope = resolve_repository_scope(repository)
    expected = compute_workspace_revision(scope)
    target = repository / "pkg" / "api.py"
    original = target.stat()
    content = target.read_bytes()
    replacement = bytes([content[0] ^ 1]) + content[1:]
    target.write_bytes(replacement)
    os.utime(target, ns=(original.st_atime_ns, original.st_mtime_ns))

    assert workspace_revision.verify_workspace_revision_unchanged(scope, expected) is False


def test_unchanged_verifier_detects_new_relevant_inventory(repository: Path) -> None:
    scope = resolve_repository_scope(repository)
    expected = compute_workspace_revision(scope)
    (repository / "pkg" / "new_source.py").write_text("value = 1\n", encoding="utf-8")

    assert workspace_revision.verify_workspace_revision_unchanged(scope, expected) is False


@pytest.mark.skipif(not _linux_memfd_available(), reason="Linux memfd optimization")
@pytest.mark.parametrize(
    "scenario",
    [
        "clean",
        "modified",
        "untracked",
        "deleted",
        "staged-modification",
        "unstaged-rename",
        "staged-rename",
        "config",
        "ignored-relevant",
    ],
)
def test_private_index_verifier_matches_forced_exact_fallback(
    repository: Path,
    monkeypatch: pytest.MonkeyPatch,
    scenario: str,
) -> None:
    if scenario == "modified":
        (repository / "pkg/api.py").write_text("changed = True\n", encoding="utf-8")
    elif scenario == "untracked":
        (repository / "pkg/new.py").write_text("new = True\n", encoding="utf-8")
    elif scenario == "deleted":
        (repository / "pkg/base.py").unlink()
    elif scenario == "staged-modification":
        (repository / "pkg/api.py").write_text("staged = True\n", encoding="utf-8")
        _run_git(repository, "add", "pkg/api.py")
    elif scenario == "unstaged-rename":
        (repository / "pkg/rename_target.py").rename(repository / "pkg/renamed.py")
    elif scenario == "staged-rename":
        _run_git(repository, "mv", "pkg/rename_target.py", "pkg/renamed.py")
    elif scenario == "config":
        (repository / "pyrightconfig.json").write_text(
            '{"typeCheckingMode":"strict"}\n', encoding="utf-8"
        )
    elif scenario == "ignored-relevant":
        (repository / ".gitignore").write_text("ignored.py\n", encoding="utf-8")
        _run_git(repository, "add", ".gitignore")
        _run_git(repository, "commit", "-m", "ignore relevant source")
        (repository / "ignored.py").write_text("ignored = True\n", encoding="utf-8")

    scope = resolve_repository_scope(repository)
    expected = compute_workspace_revision(scope)
    with monkeypatch.context() as forced:
        forced.setattr(workspace_revision, "_try_private_git_state", lambda *args, **kwargs: None)
        exact = workspace_revision.verify_workspace_revision_unchanged(scope, expected)

    private_calls = 0
    real_private = workspace_revision._git_state_with_private_index

    def recording_private(*args, **kwargs):
        nonlocal private_calls
        private_calls += 1
        return real_private(*args, **kwargs)

    monkeypatch.setattr(workspace_revision, "_git_state_with_private_index", recording_private)

    optimized = workspace_revision.verify_workspace_revision_unchanged(scope, expected)

    assert optimized is exact
    assert private_calls == 1


@pytest.mark.skipif(not _linux_memfd_available(), reason="Linux memfd optimization")
def test_private_index_verifier_matches_sha256_fallback_when_supported(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "sha256"
    root.mkdir()
    initialized = subprocess.run(
        ["git", "init", "-q", "--object-format=sha256", "-b", "main"],
        cwd=root,
        check=False,
        capture_output=True,
        timeout=10,
    )
    if initialized.returncode != 0:
        pytest.skip("Git SHA-256 repositories are unavailable")
    _run_git(root, "config", "user.name", "Revision Test")
    _run_git(root, "config", "user.email", "revision@example.invalid")
    (root / "module.py").write_text("value = 1\n", encoding="utf-8")
    _run_git(root, "add", "module.py")
    _run_git(root, "commit", "-m", "fixture")
    scope = resolve_repository_scope(root)
    expected = compute_workspace_revision(scope)

    with monkeypatch.context() as forced:
        forced.setattr(workspace_revision, "_try_private_git_state", lambda *args, **kwargs: None)
        exact = workspace_revision.verify_workspace_revision_unchanged(scope, expected)

    private_calls = 0
    real_private = workspace_revision._git_state_with_private_index

    def recording_private(*args, **kwargs):
        nonlocal private_calls
        private_calls += 1
        return real_private(*args, **kwargs)

    monkeypatch.setattr(workspace_revision, "_git_state_with_private_index", recording_private)

    assert workspace_revision.verify_workspace_revision_unchanged(scope, expected) is exact
    assert exact is True
    assert private_calls == 1


@pytest.mark.skipif(not _linux_memfd_available(), reason="Linux memfd optimization")
def test_private_index_declines_changed_clean_filter_stale_true(
    repository: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (repository / ".gitattributes").write_text("pkg/api.py filter=rewrite\n", encoding="ascii")
    _run_git(repository, "config", "filter.rewrite.clean", "cat")
    _run_git(repository, "config", "filter.rewrite.required", "true")
    _run_git(repository, "add", ".gitattributes")
    _run_git(repository, "commit", "-m", "add clean filter")
    scope = resolve_repository_scope(repository)
    expected = compute_workspace_revision(scope)
    _run_git(
        repository,
        "config",
        "filter.rewrite.clean",
        "sed s/PublicApi/PublicApiChanged/",
    )
    target = repository / "pkg/api.py"
    info = target.stat()
    os.utime(target, ns=(info.st_atime_ns, info.st_mtime_ns + 2_000_000_000))

    with monkeypatch.context() as forced:
        forced.setattr(workspace_revision, "_try_private_git_state", lambda *args, **kwargs: None)
        exact = workspace_revision.verify_workspace_revision_unchanged(scope, expected)
    optimized, exact_calls, private_calls = _verification_git_call_counts(
        scope, expected, monkeypatch
    )

    assert exact is False
    assert optimized is exact
    assert exact_calls == 1
    assert private_calls == 0


@pytest.mark.skipif(not _linux_memfd_available(), reason="Linux memfd optimization")
@pytest.mark.parametrize(
    "attributes",
    [
        "*.py text\n",
        "*.py text eol=crlf\n",
        "*.py filter=optional\n",
        "*.py ident\n",
        "*.py working-tree-encoding=UTF-8\n",
    ],
)
def test_private_index_declines_worktree_attribute_semantics(
    repository: Path,
    monkeypatch: pytest.MonkeyPatch,
    attributes: str,
) -> None:
    (repository / ".gitattributes").write_text(attributes, encoding="ascii")
    _run_git(repository, "add", ".gitattributes")
    _run_git(repository, "commit", "-m", "add attributes")
    scope = resolve_repository_scope(repository)
    expected = compute_workspace_revision(scope)

    result, exact_calls, private_calls = _verification_git_call_counts(
        scope, expected, monkeypatch
    )

    assert result is True
    assert exact_calls == 1
    assert private_calls == 0


@pytest.mark.skipif(not _linux_memfd_available(), reason="Linux memfd optimization")
@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("core.autocrlf", "true"),
        ("core.eol", "crlf"),
        ("filter.optional.clean", "cat"),
        ("core.attributesFile", "custom-attributes"),
        ("core.excludesFile", "custom-excludes"),
        ("include.path", "included-config"),
    ],
)
def test_private_index_declines_conversion_or_include_config(
    repository: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    key: str,
    value: str,
) -> None:
    if value in {"custom-attributes", "custom-excludes", "included-config"}:
        target = tmp_path / value
        target.write_text("", encoding="ascii")
        value = str(target)
    _run_git(repository, "config", key, value)
    scope = resolve_repository_scope(repository)
    expected = compute_workspace_revision(scope)

    result, exact_calls, private_calls = _verification_git_call_counts(
        scope, expected, monkeypatch
    )

    assert result is True
    assert exact_calls == 1
    assert private_calls == 0


@pytest.mark.skipif(not _linux_memfd_available(), reason="Linux memfd optimization")
@pytest.mark.parametrize("source", ["info", "global", "system"])
def test_private_index_declines_repository_global_and_system_attributes(
    repository: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source: str,
) -> None:
    if source == "info":
        attributes = repository / ".git/info/attributes"
        attributes.parent.mkdir(parents=True, exist_ok=True)
    elif source == "global":
        config_home = tmp_path / "xdg"
        attributes = config_home / "git/attributes"
        attributes.parent.mkdir(parents=True)
        monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    else:
        attributes = tmp_path / "system-gitattributes"
        monkeypatch.setattr(
            workspace_revision,
            "_SYSTEM_GIT_ATTRIBUTE_PATHS",
            (attributes,),
            raising=False,
        )
    attributes.write_text("*.py text\n", encoding="ascii")
    scope = resolve_repository_scope(repository)
    expected = compute_workspace_revision(scope)

    result, exact_calls, private_calls = _verification_git_call_counts(
        scope, expected, monkeypatch
    )

    assert result is True
    assert exact_calls == 1
    assert private_calls == 0


@pytest.mark.skipif(not _linux_memfd_available(), reason="Linux memfd optimization")
@pytest.mark.parametrize("source", ["global", "system"])
def test_private_index_declines_global_and_system_config_files(
    repository: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source: str,
) -> None:
    if source == "global":
        config_home = tmp_path / "xdg"
        config = config_home / "git/config"
        config.parent.mkdir(parents=True)
        monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    else:
        config = tmp_path / "system-gitconfig"
        monkeypatch.setattr(
            workspace_revision,
            "_SYSTEM_GIT_CONFIG_PATHS",
            (config,),
            raising=False,
        )
    config.write_text("[core]\n\tautocrlf = false\n", encoding="ascii")
    scope = resolve_repository_scope(repository)
    expected = compute_workspace_revision(scope)

    result, exact_calls, private_calls = _verification_git_call_counts(
        scope, expected, monkeypatch
    )

    assert result is True
    assert exact_calls == 1
    assert private_calls == 0


@pytest.mark.skipif(not _linux_memfd_available(), reason="Linux memfd optimization")
@pytest.mark.parametrize(
    "case",
    [
        "nested-empty-attributes",
        "irrelevant-attribute-name",
        "empty-global-attributes",
        "global-user-config",
        "system-user-config",
    ],
)
def test_private_index_accepts_semantically_irrelevant_attribute_and_config_files(
    repository: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
) -> None:
    if case in {"nested-empty-attributes", "irrelevant-attribute-name"}:
        nested = repository / "docs"
        nested.mkdir()
        name = ".gitattributes" if case == "nested-empty-attributes" else "rules.gitattributes"
        (nested / name).write_text("# no active rules\n", encoding="ascii")
        _run_git(repository, "add", f"docs/{name}")
        _run_git(repository, "commit", "-m", "add inert metadata name")
    elif case == "empty-global-attributes":
        config_home = tmp_path / "xdg"
        attributes = config_home / "git/attributes"
        attributes.parent.mkdir(parents=True)
        attributes.write_text("# no active rules\n", encoding="ascii")
        monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    elif case == "global-user-config":
        config_home = tmp_path / "xdg"
        config = config_home / "git/config"
        config.parent.mkdir(parents=True)
        config.write_text("[user]\n\tname = Irrelevant Identity\n", encoding="ascii")
        monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    else:
        config = tmp_path / "system-gitconfig"
        config.write_text("[user]\n\temail = irrelevant@example.invalid\n", encoding="ascii")
        monkeypatch.setattr(
            workspace_revision,
            "_SYSTEM_GIT_CONFIG_PATHS",
            (config,),
            raising=False,
        )
    scope = resolve_repository_scope(repository)
    expected = compute_workspace_revision(scope)

    result, exact_calls, private_calls = _verification_git_call_counts(
        scope, expected, monkeypatch
    )

    assert result is True
    assert exact_calls == 0
    assert private_calls == 1


@pytest.mark.skipif(not _linux_memfd_available(), reason="Linux Git ignore semantics")
def test_private_index_preserves_default_global_ignore_semantics(
    repository: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_home = tmp_path / "xdg"
    global_ignore = config_home / "git/ignore"
    global_ignore.parent.mkdir(parents=True)
    global_ignore.write_text("hidden.py\n", encoding="ascii")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    (repository / "hidden.py").write_text("hidden = True\n", encoding="ascii")
    scope = resolve_repository_scope(repository)
    expected = compute_workspace_revision(scope)

    result, exact_calls, private_calls = _verification_git_call_counts(
        scope, expected, monkeypatch
    )

    assert result is True
    assert exact_calls == 0
    assert private_calls == 1


@pytest.mark.skipif(not _linux_memfd_available(), reason="Linux memfd optimization")
def test_private_index_still_rejects_active_nested_attributes(
    repository: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    nested = repository / "pkg/.gitattributes"
    nested.write_text("*.py text eol=crlf\n", encoding="ascii")
    _run_git(repository, "add", "pkg/.gitattributes")
    _run_git(repository, "commit", "-m", "add nested attributes")
    scope = resolve_repository_scope(repository)
    expected = compute_workspace_revision(scope)

    result, exact_calls, private_calls = _verification_git_call_counts(
        scope, expected, monkeypatch
    )

    assert result is True
    assert exact_calls == 1
    assert private_calls == 0


@pytest.mark.skipif(not _linux_memfd_available(), reason="Linux memfd optimization")
@pytest.mark.parametrize(
    ("name", "value"),
    [("GIT_ATTR_SOURCE", "HEAD"), ("GIT_CONFIG_COUNT", "0")],
)
def test_private_index_declines_attribute_or_config_selector_environment(
    repository: Path,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    value: str,
) -> None:
    monkeypatch.setenv(name, value)
    scope = resolve_repository_scope(repository)
    expected = compute_workspace_revision(scope)

    result, exact_calls, private_calls = _verification_git_call_counts(
        scope, expected, monkeypatch
    )

    assert result is True
    assert exact_calls == 1
    assert private_calls == 0


def test_private_index_parser_rejects_malformed_binary_indexes(repository: Path) -> None:
    raw = (repository / ".git/index").read_bytes()
    malformed_checksum = raw[:-1] + bytes([raw[-1] ^ 1])
    malformed_header = bytearray(raw)
    malformed_header[:4] = b"NOPE"
    malformed_count = bytearray(raw)
    struct.pack_into("!I", malformed_count, 8, struct.unpack_from("!I", raw, 8)[0] + 1)
    malformed_path = bytearray(raw)
    path_offset = malformed_path.find(b"pkg/api.py\0")
    assert path_offset >= 0
    malformed_path[path_offset] = ord("/")
    malformed_terminator = bytearray(raw)
    malformed_terminator[path_offset + len(b"pkg/api.py")] = ord("x")
    cases = (
        malformed_checksum,
        _rechecksum_index(bytes(malformed_header)),
        _rechecksum_index(bytes(malformed_count)),
        _rechecksum_index(bytes(malformed_path)),
        _rechecksum_index(bytes(malformed_terminator)),
        raw[:-1],
    )

    for content in cases:
        assert workspace_revision._parse_git_index(
            content,
            hash_name="sha1",
            deadline=time.monotonic() + 5,
            cancelled=None,
        ) is None


def test_private_index_checksum_cancellation_is_bounded_by_hash_chunks(
    repository: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = (repository / ".git/index").read_bytes()
    large = _append_index_extension(raw, b"TREE", b"x" * (4 * 1024 * 1024))
    real_new = hashlib.new
    updates = 0

    class ThrottledHash:
        def __init__(self, hash_name: str) -> None:
            self.backing = real_new(hash_name)
            self.digest_size = self.backing.digest_size

        def update(self, content) -> None:
            nonlocal updates
            time.sleep(len(content) / (8 * 1024 * 1024))
            self.backing.update(content)
            updates += 1

        def digest(self) -> bytes:
            return self.backing.digest()

    def throttled_new(hash_name: str, content=b""):
        result = ThrottledHash(hash_name)
        if content:
            result.update(content)
        return result

    monkeypatch.setattr(workspace_revision.hashlib, "new", throttled_new)
    started = time.monotonic()
    with pytest.raises(TimeoutError, match="cancelled"):
        workspace_revision._parse_git_index(
            large,
            hash_name="sha1",
            deadline=None,
            cancelled=lambda: updates > 0,
        )
    elapsed = time.monotonic() - started

    assert updates == 1
    assert elapsed < 0.25


def test_private_index_rebuild_honors_cancellation_before_cpu_loop(repository: Path) -> None:
    parsed = workspace_revision._parse_git_index(
        (repository / ".git/index").read_bytes(),
        hash_name="sha1",
        deadline=None,
        cancelled=None,
    )
    assert parsed is not None
    expanded = type(parsed)(
        parsed.content,
        parsed.entries_end,
        parsed.checksum_offset,
        parsed.entries * 10_000,
    )

    with pytest.raises(TimeoutError, match="cancelled"):
        workspace_revision._refresh_private_index(
            expanded,
            {},
            hash_name="sha1",
            deadline=None,
            cancelled=lambda: True,
        )


def test_private_index_parser_rejects_unsupported_extensions_and_modes(
    repository: Path,
) -> None:
    raw = (repository / ".git/index").read_bytes()
    for signature in (b"link", b"sdir", b"abcd", b"ABCD", b"UNTR", b"FSMN"):
        assert workspace_revision._parse_git_index(
            _append_index_extension(raw, signature),
            hash_name="sha1",
            deadline=time.monotonic() + 5,
            cancelled=None,
        ) is None

    nonregular = bytearray(raw)
    struct.pack_into("!I", nonregular, 12 + 24, 0o120000)
    assert workspace_revision._parse_git_index(
        _rechecksum_index(bytes(nonregular)),
        hash_name="sha1",
        deadline=time.monotonic() + 5,
        cancelled=None,
    ) is None
    zero_oid = bytearray(raw)
    zero_oid[12 + 40 : 12 + 60] = b"\0" * 20
    assert workspace_revision._parse_git_index(
        _rechecksum_index(bytes(zero_oid)),
        hash_name="sha1",
        deadline=time.monotonic() + 5,
        cancelled=None,
    ) is None


@pytest.mark.skipif(not _linux_memfd_available(), reason="Linux memfd optimization")
def test_private_index_memfd_strips_every_optional_extension(
    repository: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    index = repository / ".git/index"
    raw = index.read_bytes()
    scope = resolve_repository_scope(repository)
    expected = compute_workspace_revision(scope)
    expected_head, expected_status = workspace_revision._git_state(
        repository,
        allow_missing_head=False,
        deadline=None,
        cancelled=None,
    )
    entries_end = _index_entries_end(raw)
    extension_free = raw[:entries_end]
    extension_free += hashlib.sha1(extension_free).digest()
    index.write_bytes(_append_index_extension(extension_free, b"TREE", b"untrusted-cache"))
    inspected = False

    def inspect_private_index(_root: Path, descriptor: int, **_kwargs):
        nonlocal inspected
        content = os.pread(descriptor, 64 * 1024 * 1024, 0)
        private_entries_end = _index_entries_end(content)
        inspected = True
        assert len(content) == private_entries_end + hashlib.sha1().digest_size
        assert hashlib.sha1(content[:private_entries_end]).digest() == content[private_entries_end:]
        return expected_head, expected_status

    monkeypatch.setattr(
        workspace_revision,
        "_git_state_with_private_index",
        inspect_private_index,
    )

    assert workspace_revision.verify_workspace_revision_unchanged(scope, expected) is True
    assert inspected


@pytest.mark.skipif(not _linux_memfd_available(), reason="Linux memfd optimization")
def test_private_index_invalidates_every_non_content_proven_entry(
    repository: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    irrelevant = repository / "README.txt"
    irrelevant.write_text("tracked but outside revision content\n", encoding="utf-8")
    _run_git(repository, "add", "README.txt")
    _run_git(repository, "commit", "-m", "track irrelevant file")
    scope = resolve_repository_scope(repository)
    expected = compute_workspace_revision(scope)
    expected_head, expected_status = workspace_revision._git_state(
        repository,
        allow_missing_head=False,
        deadline=None,
        cancelled=None,
    )
    inspected = False

    def inspect_private_index(_root: Path, descriptor: int, **_kwargs):
        nonlocal inspected
        content = os.pread(descriptor, 64 * 1024 * 1024, 0)
        parsed = workspace_revision._parse_git_index(
            content,
            hash_name="sha1",
            deadline=None,
            cancelled=None,
        )
        assert parsed is not None
        entry = next(item for item in parsed.entries if item.path == "README.txt")
        assert struct.unpack_from("!6I", content, entry.offset) == (0,) * 6
        assert struct.unpack_from("!3I", content, entry.offset + 28) == (0,) * 3
        inspected = True
        return expected_head, expected_status

    monkeypatch.setattr(
        workspace_revision,
        "_git_state_with_private_index",
        inspect_private_index,
    )

    assert workspace_revision.verify_workspace_revision_unchanged(scope, expected) is True
    assert inspected


@pytest.mark.skipif(not _linux_memfd_available(), reason="Linux memfd optimization")
def test_private_index_detects_mutated_tracked_file_outside_relevance_set(
    repository: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    irrelevant = repository / "README.txt"
    irrelevant.write_text("before\n", encoding="utf-8")
    _run_git(repository, "add", "README.txt")
    _run_git(repository, "commit", "-m", "track irrelevant file")
    scope = resolve_repository_scope(repository)
    expected = compute_workspace_revision(scope)
    irrelevant.write_text("after\n", encoding="utf-8")

    with monkeypatch.context() as forced:
        forced.setattr(workspace_revision, "_try_private_git_state", lambda *args, **kwargs: None)
        exact = workspace_revision.verify_workspace_revision_unchanged(scope, expected)
    optimized, exact_calls, private_calls = _verification_git_call_counts(
        scope, expected, monkeypatch
    )

    assert exact is False
    assert optimized is False
    assert exact_calls == 0
    assert private_calls == 1


@pytest.mark.skipif(not _linux_memfd_available(), reason="Linux memfd optimization")
def test_private_index_large_unmatched_tracked_asset_uses_exact_fallback(
    repository: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asset = repository / "large-asset.bin"
    with asset.open("wb") as stream:
        stream.truncate(64 * 1024 * 1024)
    _run_git(repository, "add", "large-asset.bin")
    _run_git(repository, "commit", "-m", "add large tracked asset")
    scope = resolve_repository_scope(repository)
    expected = compute_workspace_revision(scope)

    result, exact_calls, private_calls = _verification_git_call_counts(
        scope, expected, monkeypatch
    )

    assert result is True
    assert exact_calls == 1
    assert private_calls == 0


@pytest.mark.skipif(not _linux_memfd_available(), reason="Linux tracked path semantics")
def test_private_index_nonregular_unmatched_tracked_path_uses_exact_fallback(
    repository: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tracked = repository / "README.txt"
    tracked.write_text("tracked\n", encoding="ascii")
    _run_git(repository, "add", "README.txt")
    _run_git(repository, "commit", "-m", "add tracked asset")
    scope = resolve_repository_scope(repository)
    expected = compute_workspace_revision(scope)
    outside = tmp_path / "outside.txt"
    outside.write_text("replacement\n", encoding="ascii")
    tracked.unlink()
    _symlink_or_skip(tracked, outside)

    result, exact_calls, private_calls = _verification_git_call_counts(
        scope, expected, monkeypatch
    )

    assert result is False
    assert exact_calls == 1
    assert private_calls == 0


@pytest.mark.skipif(not _linux_memfd_available(), reason="Linux memfd optimization")
def test_private_index_preserves_exact_tracked_deletion_status(
    repository: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deleted = repository / "pkg/base.py"
    deleted.unlink()
    scope = resolve_repository_scope(repository)
    expected = compute_workspace_revision(scope)
    exact_head, exact_status = workspace_revision._git_state(
        repository,
        allow_missing_head=False,
        deadline=None,
        cancelled=None,
    )

    assert ("pkg/base.py", "deleted") in workspace_revision._status_paths(exact_status)
    result, exact_calls, private_calls = _verification_git_call_counts(
        scope, expected, monkeypatch
    )

    assert exact_head == expected.git_head
    assert result is True
    assert exact_calls == 0
    assert private_calls == 1


@pytest.mark.skipif(not _linux_memfd_available(), reason="Linux memfd optimization")
@pytest.mark.parametrize("reported", ["missing", "modified"])
def test_private_index_rejects_deleted_path_status_disagreement(
    repository: Path,
    monkeypatch: pytest.MonkeyPatch,
    reported: str,
) -> None:
    (repository / "pkg/base.py").unlink()
    scope = resolve_repository_scope(repository)
    expected = compute_workspace_revision(scope)
    _head, exact_status = workspace_revision._git_state(
        repository,
        allow_missing_head=False,
        deadline=None,
        cancelled=None,
    )
    if reported == "missing":
        private_status = b"\0".join(
            record for record in exact_status.split(b"\0") if record.startswith(b"#")
        )
    else:
        private_status = exact_status.replace(b"1 .D ", b"1 .M ", 1)
        assert private_status != exact_status
    monkeypatch.setattr(
        workspace_revision,
        "_git_state_with_private_index",
        lambda *args, **kwargs: (expected.git_head, private_status),
    )

    assert workspace_revision.verify_workspace_revision_unchanged(scope, expected) is False


@pytest.mark.skipif(not _linux_memfd_available(), reason="Linux memfd optimization")
def test_private_index_matches_exact_staged_delete_with_restored_untracked_path(
    repository: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _run_git(repository, "rm", "--cached", "pkg/base.py")
    scope = resolve_repository_scope(repository)
    expected = compute_workspace_revision(scope)

    assert next(entry for entry in expected.entries if entry.path == "pkg/base.py").kind == "untracked"
    result, exact_calls, private_calls = _verification_git_call_counts(
        scope, expected, monkeypatch
    )

    assert result is True
    assert exact_calls == 0
    assert private_calls == 1


@pytest.mark.skipif(not _linux_memfd_available(), reason="Linux index variants")
def test_private_index_parser_rejects_v4_split_and_colliding_paths(
    tmp_path: Path,
) -> None:
    roots = []
    for name in ("v4", "split", "collision"):
        root = tmp_path / name
        root.mkdir()
        _run_git(root, "init", "-q", "-b", "main")
        _run_git(root, "config", "user.name", "Revision Test")
        _run_git(root, "config", "user.email", "revision@example.invalid")
        (root / "a.py").write_text("a = 1\n", encoding="utf-8")
        if name == "collision":
            (root / "A.py").write_text("A = 1\n", encoding="utf-8")
        _run_git(root, "add", "--all")
        _run_git(root, "commit", "-m", "fixture")
        roots.append(root)
    _run_git(roots[0], "update-index", "--index-version", "4")
    _run_git(roots[1], "update-index", "--split-index")

    for root in roots:
        assert workspace_revision._parse_git_index(
            (root / ".git/index").read_bytes(),
            hash_name="sha1",
            deadline=time.monotonic() + 5,
            cancelled=None,
        ) is None


@pytest.mark.parametrize(
    "uncertainty",
    ["platform", "layout", "candidate-race", "oversized"],
)
def test_private_index_uncertainty_uses_exact_fallback(
    repository: Path,
    monkeypatch: pytest.MonkeyPatch,
    uncertainty: str,
) -> None:
    scope = resolve_repository_scope(repository)
    expected = compute_workspace_revision(scope)
    if uncertainty == "platform":
        monkeypatch.setattr(workspace_revision, "_private_index_platform_supported", lambda: False)
    else:
        monkeypatch.setattr(workspace_revision, "_private_index_platform_supported", lambda: True)
        if uncertainty == "layout":
            monkeypatch.setattr(workspace_revision, "_ordinary_index_path", lambda _root: None)
        elif uncertainty == "candidate-race":
            monkeypatch.setattr(
                workspace_revision,
                "_ordinary_index_path",
                lambda root: root / ".git/index-removed-after-qualification",
            )
        else:
            monkeypatch.setattr(workspace_revision, "_MAX_PRIVATE_INDEX_BYTES", 1)

    exact_calls = 0
    real_exact = workspace_revision._git_state

    def recording_exact(*args, **kwargs):
        nonlocal exact_calls
        exact_calls += 1
        return real_exact(*args, **kwargs)

    monkeypatch.setattr(workspace_revision, "_git_state", recording_exact)
    monkeypatch.setattr(
        workspace_revision,
        "_git_state_with_private_index",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("private status used")),
    )

    assert workspace_revision.verify_workspace_revision_unchanged(scope, expected) is True
    assert exact_calls == 1


@pytest.mark.skipif(not _linux_memfd_available(), reason="Linux index variants")
@pytest.mark.parametrize("variant", ["v4", "split"])
def test_private_index_git_variants_use_exact_fallback(
    repository: Path,
    monkeypatch: pytest.MonkeyPatch,
    variant: str,
) -> None:
    if variant == "v4":
        _run_git(repository, "update-index", "--index-version", "4")
    else:
        _run_git(repository, "update-index", "--split-index")
    scope = resolve_repository_scope(repository)
    expected = compute_workspace_revision(scope)
    exact_calls = 0
    real_exact = workspace_revision._git_state

    def recording_exact(*args, **kwargs):
        nonlocal exact_calls
        exact_calls += 1
        return real_exact(*args, **kwargs)

    monkeypatch.setattr(workspace_revision, "_git_state", recording_exact)
    monkeypatch.setattr(
        workspace_revision,
        "_git_state_with_private_index",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("private status used")),
    )

    workspace_revision.verify_workspace_revision_unchanged(scope, expected)

    assert exact_calls == 1


@pytest.mark.skipif(not _linux_memfd_available(), reason="Linux worktree layouts")
@pytest.mark.parametrize("layout", ["linked-worktree", "symlink-index"])
def test_private_index_nonordinary_layout_uses_exact_fallback(
    repository: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    layout: str,
) -> None:
    if layout == "linked-worktree":
        root = tmp_path / "linked"
        _run_git(repository, "worktree", "add", "--detach", str(root), "HEAD")
    else:
        root = repository
        index = root / ".git/index"
        target = root / ".git/index-owned"
        index.rename(target)
        index.symlink_to(target.name)
    scope = resolve_repository_scope(root)
    expected = compute_workspace_revision(scope)
    exact_calls = 0
    real_exact = workspace_revision._git_state

    def recording_exact(*args, **kwargs):
        nonlocal exact_calls
        exact_calls += 1
        return real_exact(*args, **kwargs)

    monkeypatch.setattr(workspace_revision, "_git_state", recording_exact)
    monkeypatch.setattr(
        workspace_revision,
        "_git_state_with_private_index",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("private status used")),
    )

    workspace_revision.verify_workspace_revision_unchanged(scope, expected)

    assert exact_calls == 1


@pytest.mark.skipif(not _linux_memfd_available(), reason="Linux Git installation semantics")
def test_private_index_custom_git_prefix_uses_exact_fallback(
    repository: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_git = shutil.which("git")
    if real_git is None:
        pytest.skip("Git executable is unavailable")
    scope = resolve_repository_scope(repository)
    expected = compute_workspace_revision(scope)
    custom_bin = tmp_path / "custom-prefix/bin"
    custom_bin.mkdir(parents=True)
    wrapper = custom_bin / "git"
    wrapper.write_text(
        "#!/bin/sh\n" 'exec "$REVISION_REAL_GIT" "$@"\n',
        encoding="ascii",
    )
    wrapper.chmod(0o755)
    monkeypatch.setenv("REVISION_REAL_GIT", real_git)
    monkeypatch.setenv("PATH", f"{custom_bin}{os.pathsep}{os.environ.get('PATH', '')}")

    result, exact_calls, private_calls = _verification_git_call_counts(
        scope, expected, monkeypatch
    )

    assert result is True
    assert exact_calls == 1
    assert private_calls == 0


@pytest.mark.skipif(not _linux_memfd_available(), reason="Linux memfd optimization")
def test_private_index_rejects_symlinked_reference_parent_before_ref_mutation(
    repository: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_head = _run_git(repository, "rev-parse", "HEAD").decode("ascii").strip()
    _run_git(repository, "commit", "--allow-empty", "-m", "second head")
    branch = _run_git(repository, "symbolic-ref", "--short", "HEAD").decode("ascii").strip()
    heads = repository / ".git/refs/heads"
    external_heads = tmp_path / "external-heads"
    heads.rename(external_heads)
    try:
        heads.symlink_to(external_heads, target_is_directory=True)
    except OSError:
        external_heads.rename(heads)
        pytest.skip("directory symlinks are unavailable")
    scope = resolve_repository_scope(repository)
    expected = compute_workspace_revision(scope)
    exact_calls = 0
    private_calls = 0
    real_exact = workspace_revision._git_state
    real_private = workspace_revision._git_state_with_private_index

    def recording_exact(*args, **kwargs):
        nonlocal exact_calls
        exact_calls += 1
        return real_exact(*args, **kwargs)

    def recording_private(*args, **kwargs):
        nonlocal private_calls
        private_calls += 1
        return real_private(*args, **kwargs)

    monkeypatch.setattr(workspace_revision, "_git_state", recording_exact)
    monkeypatch.setattr(workspace_revision, "_git_state_with_private_index", recording_private)

    assert workspace_revision.verify_workspace_revision_unchanged(scope, expected) is True
    (external_heads / branch).write_text(f"{first_head}\n", encoding="ascii")
    assert workspace_revision.verify_workspace_revision_unchanged(scope, expected) is False
    assert exact_calls == 2
    assert private_calls == 0


@pytest.mark.skipif(not _linux_memfd_available(), reason="Linux memfd optimization")
def test_private_index_rejects_symlinked_info_parent_even_without_attributes(
    repository: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    info = repository / ".git/info"
    external_info = tmp_path / "external-info"
    external_info.mkdir()
    try:
        info.symlink_to(external_info, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks are unavailable")
    scope = resolve_repository_scope(repository)
    expected = compute_workspace_revision(scope)

    result, exact_calls, private_calls = _verification_git_call_counts(
        scope, expected, monkeypatch
    )

    assert result is True
    assert exact_calls == 1
    assert private_calls == 0


@pytest.mark.skipif(not _linux_memfd_available(), reason="Linux memfd optimization")
@pytest.mark.parametrize(
    "race",
    [
        "index",
        "head",
        "source",
        "directory",
        "directory-restored-mtime",
        "tracked-irrelevant-restored-mtime",
        "deleted",
        "symlink",
    ],
)
def test_private_index_races_never_return_stale_true(
    repository: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    race: str,
) -> None:
    target = repository / "pkg/api.py"
    deleted = repository / "pkg/base.py"
    deleted_content = deleted.read_bytes()
    if race == "deleted":
        deleted.unlink()
    irrelevant = repository / "README.txt"
    if race == "tracked-irrelevant-restored-mtime":
        irrelevant.write_text("before\n", encoding="utf-8")
        _run_git(repository, "add", "README.txt")
        _run_git(repository, "commit", "-m", "track irrelevant file")
    outside = tmp_path / "outside.py"
    outside.write_bytes(target.read_bytes())
    root_info = repository.stat()
    scope = resolve_repository_scope(repository)
    expected = compute_workspace_revision(scope)
    real_private = workspace_revision._git_state_with_private_index
    fired = False

    def mutate_after_private_status(*args, **kwargs):
        nonlocal fired
        result = real_private(*args, **kwargs)
        if race == "index":
            oid = _run_git(
                repository,
                "hash-object",
                "-w",
                "--stdin",
                input_bytes=b"index-only replacement\n",
            ).strip()
            _run_git(
                repository,
                "update-index",
                "--cacheinfo",
                "100644",
                oid.decode("ascii"),
                "pkg/api.py",
            )
        elif race == "head":
            _run_git(repository, "commit", "--allow-empty", "-m", "move head")
        elif race == "source":
            target.write_text("changed_after_status = True\n", encoding="utf-8")
        elif race in {"directory", "directory-restored-mtime"}:
            (repository / "inventory-race.txt").write_text("changed\n", encoding="utf-8")
            if race == "directory-restored-mtime":
                os.utime(repository, ns=(root_info.st_atime_ns, root_info.st_mtime_ns))
        elif race == "tracked-irrelevant-restored-mtime":
            irrelevant_info = irrelevant.stat()
            irrelevant.write_text("after!\n", encoding="utf-8")
            os.utime(
                irrelevant,
                ns=(irrelevant_info.st_atime_ns, irrelevant_info.st_mtime_ns),
            )
        elif race == "deleted":
            deleted.write_bytes(deleted_content)
        else:
            target.unlink()
            target.symlink_to(outside)
        fired = True
        return result

    monkeypatch.setattr(
        workspace_revision,
        "_git_state_with_private_index",
        mutate_after_private_status,
    )

    try:
        unchanged = workspace_revision.verify_workspace_revision_unchanged(scope, expected)
    except (PermissionError, RuntimeError):
        unchanged = False

    assert fired
    assert unchanged is False


@pytest.mark.skipif(not _linux_memfd_available(), reason="Linux memfd optimization")
@pytest.mark.parametrize(
    "phase",
    [
        "during-discovery",
        "after-content-hash",
        "before-private-proof",
        "after-private-status",
        "before-final-proof",
    ],
)
def test_private_index_ignored_relevant_inventory_race_uses_fresh_exact_scan(
    repository: Path,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
) -> None:
    (repository / ".gitignore").write_text("late.py\n", encoding="ascii")
    _run_git(repository, "add", ".gitignore")
    _run_git(repository, "commit", "-m", "ignore late source")
    scope = resolve_repository_scope(repository)
    expected = compute_workspace_revision(scope)
    root_info = repository.stat()
    mutated = False

    def create_ignored_source() -> None:
        nonlocal mutated
        if mutated:
            return
        (repository / "late.py").write_text("late = True\n", encoding="ascii")
        os.utime(repository, ns=(root_info.st_atime_ns, root_info.st_mtime_ns))
        mutated = True

    if phase == "during-discovery":
        real_validate_directory = workspace_revision._validate_directory_snapshot

        def mutate_during_discovery(*args, **kwargs):
            result = real_validate_directory(*args, **kwargs)
            snapshot = args[1]
            if snapshot.path == repository:
                create_ignored_source()
            return result

        monkeypatch.setattr(
            workspace_revision,
            "_validate_directory_snapshot",
            mutate_during_discovery,
        )
    elif phase == "after-content-hash":
        real_hash = workspace_revision._hash_file_for_verification

        def mutate_after_hash(*args, **kwargs):
            result = real_hash(*args, **kwargs)
            create_ignored_source()
            return result

        monkeypatch.setattr(
            workspace_revision,
            "_hash_file_for_verification",
            mutate_after_hash,
        )
    elif phase == "before-private-proof":
        real_semantics = workspace_revision._private_raw_semantics_safe

        def mutate_before_proof(*args, **kwargs):
            result = real_semantics(*args, **kwargs)
            create_ignored_source()
            return result

        monkeypatch.setattr(
            workspace_revision,
            "_private_raw_semantics_safe",
            mutate_before_proof,
        )
    elif phase == "after-private-status":
        real_private = workspace_revision._git_state_with_private_index

        def mutate_after_status(*args, **kwargs):
            result = real_private(*args, **kwargs)
            create_ignored_source()
            return result

        monkeypatch.setattr(
            workspace_revision,
            "_git_state_with_private_index",
            mutate_after_status,
        )
    else:
        real_validate_proof = workspace_revision._validate_private_git_proof

        def mutate_before_final_proof(*args, **kwargs):
            create_ignored_source()
            return real_validate_proof(*args, **kwargs)

        monkeypatch.setattr(
            workspace_revision,
            "_validate_private_git_proof",
            mutate_before_final_proof,
        )

    assert workspace_revision.verify_workspace_revision_unchanged(scope, expected) is False
    assert mutated


@pytest.mark.skipif(not _linux_memfd_available(), reason="Linux filename semantics")
def test_private_index_unknown_irrelevant_backslash_name_uses_exact_fallback(
    repository: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (repository / ".gitignore").write_text("*.txt\n", encoding="ascii")
    _run_git(repository, "add", ".gitignore")
    _run_git(repository, "commit", "-m", "ignore text assets")
    (repository / "ignored\\asset.txt").write_text("ignored\n", encoding="ascii")
    scope = resolve_repository_scope(repository)
    expected = compute_workspace_revision(scope)

    result, exact_calls, private_calls = _verification_git_call_counts(
        scope, expected, monkeypatch
    )

    assert result is True
    assert exact_calls == 1
    assert private_calls == 0


@pytest.mark.skipif(not _linux_memfd_available(), reason="Linux memfd optimization")
def test_private_index_rejects_restored_mtime_mutation_after_content_hash(
    repository: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scope = resolve_repository_scope(repository)
    expected = compute_workspace_revision(scope)
    target = repository / "pkg/api.py"
    real_hash = workspace_revision._hash_file_for_verification
    fired = False

    def mutate_after_hash(path: Path, **kwargs):
        nonlocal fired
        result = real_hash(path, **kwargs)
        if path == target and not fired:
            info = target.stat()
            content = target.read_bytes()
            target.write_bytes(bytes([content[0] ^ 1]) + content[1:])
            os.utime(target, ns=(info.st_atime_ns, info.st_mtime_ns))
            fired = True
        return result

    monkeypatch.setattr(workspace_revision, "_hash_file_for_verification", mutate_after_hash)

    try:
        unchanged = workspace_revision.verify_workspace_revision_unchanged(scope, expected)
    except PermissionError:
        unchanged = False

    assert fired
    assert unchanged is False


def test_private_index_parser_honors_cancellation(repository: Path) -> None:
    raw = (repository / ".git/index").read_bytes()
    calls = 0

    def cancelled() -> bool:
        nonlocal calls
        calls += 1
        return calls >= 3

    with pytest.raises(TimeoutError, match="cancel"):
        workspace_revision._parse_git_index(
            raw,
            hash_name="sha1",
            deadline=time.monotonic() + 5,
            cancelled=cancelled,
        )

    assert calls == 3


@pytest.mark.skipif(not _linux_memfd_available(), reason="Linux memfd optimization")
def test_private_index_memfd_closes_on_success_and_operational_timeout(
    repository: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scope = resolve_repository_scope(repository)
    expected = compute_workspace_revision(scope)
    real_create = os.memfd_create
    real_exact = workspace_revision._git_state
    descriptors: list[int] = []
    exact_calls = 0

    def recording_create(*args, **kwargs):
        descriptor = real_create(*args, **kwargs)
        descriptors.append(descriptor)
        return descriptor

    monkeypatch.setattr(workspace_revision.os, "memfd_create", recording_create)
    assert workspace_revision.verify_workspace_revision_unchanged(scope, expected) is True

    monkeypatch.setattr(
        workspace_revision,
        "_git_state_with_private_index",
        lambda *args, **kwargs: (_ for _ in ()).throw(TimeoutError("private status timeout")),
    )

    def recording_exact(*args, **kwargs):
        nonlocal exact_calls
        exact_calls += 1
        return real_exact(*args, **kwargs)

    monkeypatch.setattr(workspace_revision, "_git_state", recording_exact)
    assert workspace_revision.verify_workspace_revision_unchanged(scope, expected) is True

    assert len(descriptors) == 2
    assert exact_calls == 1
    for descriptor in descriptors:
        with pytest.raises(OSError):
            os.fstat(descriptor)
    assert not (repository / ".git/index.lock").exists()


@pytest.mark.skipif(not _linux_memfd_available(), reason="Linux memfd optimization")
def test_private_index_is_fully_sealed_before_git_can_read_it(
    repository: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import fcntl

    required = (
        fcntl.F_SEAL_WRITE
        | fcntl.F_SEAL_GROW
        | fcntl.F_SEAL_SHRINK
        | fcntl.F_SEAL_SEAL
    )
    real_private = workspace_revision._git_state_with_private_index
    real_create = os.memfd_create
    flags_seen: list[int] = []

    def recording_create(name: str, flags: int) -> int:
        flags_seen.append(flags)
        return real_create(name, flags)

    def assert_sealed(root: Path, descriptor: int, **kwargs):
        assert fcntl.fcntl(descriptor, fcntl.F_GET_SEALS) & required == required
        with pytest.raises(OSError):
            os.pwrite(descriptor, b"X", 0)
        return real_private(root, descriptor, **kwargs)

    monkeypatch.setattr(workspace_revision.os, "memfd_create", recording_create)
    monkeypatch.setattr(workspace_revision, "_git_state_with_private_index", assert_sealed)
    scope = resolve_repository_scope(repository)
    expected = compute_workspace_revision(scope)

    assert workspace_revision.verify_workspace_revision_unchanged(scope, expected) is True
    assert flags_seen == [os.MFD_CLOEXEC | os.MFD_ALLOW_SEALING]


@pytest.mark.skipif(not _linux_memfd_available(), reason="Linux memfd optimization")
def test_private_index_inherited_by_git_descendant_remains_read_only(
    repository: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_git = shutil.which("git")
    if real_git is None:
        pytest.skip("Git executable is unavailable")
    scope = resolve_repository_scope(repository)
    expected = compute_workspace_revision(scope)
    wrapper_root = tmp_path / "sealed-git-wrapper"
    wrapper_root.mkdir()
    result = wrapper_root / "result"
    probe = wrapper_root / "probe.py"
    probe.write_text(
        "import os\n"
        "from pathlib import Path\n"
        "descriptor = os.open(os.environ['GIT_INDEX_FILE'], os.O_RDWR)\n"
        "try:\n"
        "    os.pwrite(descriptor, b'D', 0)\n"
        "except OSError:\n"
        "    outcome = 'sealed'\n"
        "else:\n"
        "    outcome = 'writable'\n"
        "finally:\n"
        "    os.close(descriptor)\n"
        "Path(os.environ['REVISION_SEAL_RESULT']).write_text(outcome, encoding='ascii')\n",
        encoding="ascii",
    )
    wrapper = wrapper_root / "git"
    wrapper.write_text(
        "#!/bin/sh\n"
        '"$REVISION_PYTHON" "$REVISION_SEAL_PROBE"\n'
        'exec "$REVISION_REAL_GIT" "$@"\n',
        encoding="ascii",
    )
    wrapper.chmod(0o755)
    _qualify_private_git_wrapper(monkeypatch, wrapper)
    monkeypatch.setenv("PATH", f"{wrapper_root}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("REVISION_PYTHON", sys.executable)
    monkeypatch.setenv("REVISION_REAL_GIT", real_git)
    monkeypatch.setenv("REVISION_SEAL_PROBE", str(probe))
    monkeypatch.setenv("REVISION_SEAL_RESULT", str(result))

    assert workspace_revision.verify_workspace_revision_unchanged(scope, expected) is True
    assert result.read_text(encoding="ascii") == "sealed"


@pytest.mark.skipif(not _linux_memfd_available(), reason="Linux memfd optimization")
@pytest.mark.parametrize(
    "failure",
    [
        "memfd-create",
        "memfd-fchmod",
        "memfd-write",
        "memfd-fsync",
        "memfd-lseek",
        "memfd-utime",
        "memfd-fcntl",
        "metadata-stat",
        "metadata-open",
        "metadata-read",
        "subprocess-launch",
        "subprocess-read",
        "post-proof-stat",
    ],
)
def test_private_index_operational_uncertainty_uses_exact_fallback(
    repository: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    scope = resolve_repository_scope(repository)
    expected = compute_workspace_revision(scope)
    fired = False
    private_descriptor: int | None = None

    real_memfd_create = workspace_revision.os.memfd_create

    def recording_memfd_create(*args, **kwargs):
        nonlocal fired, private_descriptor
        if failure == "memfd-create":
            fired = True
            raise OSError("injected memfd creation failure")
        private_descriptor = real_memfd_create(*args, **kwargs)
        return private_descriptor

    monkeypatch.setattr(workspace_revision.os, "memfd_create", recording_memfd_create)

    if failure in {
        "memfd-fchmod",
        "memfd-write",
        "memfd-fsync",
        "memfd-lseek",
        "memfd-utime",
    }:
        operation_name = failure.removeprefix("memfd-")
        real_operation = getattr(workspace_revision.os, operation_name)

        def fail_descriptor_operation(descriptor, *args, **kwargs):
            nonlocal fired
            if descriptor == private_descriptor and not fired:
                fired = True
                raise OSError(f"injected {operation_name} failure")
            return real_operation(descriptor, *args, **kwargs)

        monkeypatch.setattr(workspace_revision.os, operation_name, fail_descriptor_operation)
    elif failure == "memfd-fcntl":
        assert workspace_revision._fcntl is not None
        real_fcntl = workspace_revision._fcntl.fcntl

        def fail_fcntl(descriptor, *args, **kwargs):
            nonlocal fired
            if descriptor == private_descriptor and not fired:
                fired = True
                raise OSError("injected fcntl failure")
            return real_fcntl(descriptor, *args, **kwargs)

        monkeypatch.setattr(workspace_revision._fcntl, "fcntl", fail_fcntl)
    elif failure == "metadata-stat":
        real_stat = workspace_revision.os.stat

        def fail_metadata_stat(path, *args, **kwargs):
            nonlocal fired
            if path == "index" and kwargs.get("dir_fd") is not None and not fired:
                fired = True
                raise OSError("injected metadata stat failure")
            return real_stat(path, *args, **kwargs)

        monkeypatch.setattr(workspace_revision.os, "stat", fail_metadata_stat)
    elif failure == "metadata-open":
        real_open = workspace_revision.os.open

        def fail_metadata_open(path, *args, **kwargs):
            nonlocal fired
            if path == "index" and kwargs.get("dir_fd") is not None and not fired:
                fired = True
                raise OSError("injected metadata open failure")
            return real_open(path, *args, **kwargs)

        monkeypatch.setattr(workspace_revision.os, "open", fail_metadata_open)
    elif failure == "metadata-read":
        real_open = workspace_revision.os.open
        real_read = workspace_revision.os.read
        index_descriptor: int | None = None

        def record_metadata_open(path, *args, **kwargs):
            nonlocal index_descriptor
            descriptor = real_open(path, *args, **kwargs)
            if path == "index" and kwargs.get("dir_fd") is not None:
                index_descriptor = descriptor
            return descriptor

        def fail_metadata_read(descriptor, *args, **kwargs):
            nonlocal fired
            if descriptor == index_descriptor and not fired:
                fired = True
                raise OSError("injected metadata read failure")
            return real_read(descriptor, *args, **kwargs)

        monkeypatch.setattr(workspace_revision.os, "open", record_metadata_open)
        monkeypatch.setattr(workspace_revision.os, "read", fail_metadata_read)
    elif failure in {"subprocess-launch", "subprocess-read"}:
        real_popen = workspace_revision.subprocess.Popen

        class FailingReader:
            def __init__(self, wrapped) -> None:
                self.wrapped = wrapped

            def read(self, *args, **kwargs):
                nonlocal fired
                fired = True
                raise OSError("injected subprocess pipe failure")

            def close(self) -> None:
                self.wrapped.close()

        def fail_private_process(*args, **kwargs):
            nonlocal fired
            if kwargs.get("pass_fds"):
                if failure == "subprocess-launch":
                    fired = True
                    raise OSError("injected subprocess launch failure")
                process = real_popen(*args, **kwargs)
                process.stdout = FailingReader(process.stdout)
                return process
            return real_popen(*args, **kwargs)

        monkeypatch.setattr(workspace_revision.subprocess, "Popen", fail_private_process)
    elif failure == "post-proof-stat":
        def fail_post_proof(*args, **kwargs):
            nonlocal fired
            fired = True
            raise OSError("injected post-proof stat failure")

        monkeypatch.setattr(workspace_revision, "_validate_private_git_proof", fail_post_proof)

    result, exact_calls, _private_calls = _verification_git_call_counts(
        scope, expected, monkeypatch
    )

    assert fired
    assert result is True
    assert exact_calls == 1


@pytest.mark.skipif(not _linux_memfd_available(), reason="Linux memfd optimization")
@pytest.mark.parametrize("metadata", ["config", "HEAD", "index"])
def test_private_index_malformed_metadata_uses_exact_fallback(
    repository: Path,
    monkeypatch: pytest.MonkeyPatch,
    metadata: str,
) -> None:
    scope = resolve_repository_scope(repository)
    expected = compute_workspace_revision(scope)
    real_read = workspace_revision._read_owned_file
    fired = False
    malformed = {
        "config": b"[include]\npath = /outside\n",
        "HEAD": b"not-an-object-id\n",
        "index": b"not-an-index",
    }

    def return_malformed(root: Path, path: Path, *args, **kwargs):
        nonlocal fired
        result = real_read(root, path, *args, **kwargs)
        if path.name == metadata and result is not None and not fired:
            fired = True
            return type(result)(malformed[metadata], result.fence)
        return result

    monkeypatch.setattr(workspace_revision, "_read_owned_file", return_malformed)

    result, exact_calls, private_calls = _verification_git_call_counts(
        scope, expected, monkeypatch
    )

    assert fired
    assert result is True
    assert exact_calls == 1
    assert private_calls == 0


@pytest.mark.skipif(not _linux_memfd_available(), reason="Linux metadata descriptors")
@pytest.mark.parametrize("metadata", ["config", "HEAD", "index", "ref"])
def test_private_metadata_fifo_is_rejected_without_opening(
    repository: Path,
    metadata: str,
) -> None:
    if metadata == "ref":
        reference = _run_git(repository, "symbolic-ref", "HEAD").decode("ascii").strip()
        target = repository / ".git" / reference
    else:
        target = repository / ".git" / metadata
    original = target.read_bytes()
    target.unlink()
    try:
        os.mkfifo(target)
    except OSError:
        target.write_bytes(original)
        pytest.skip("FIFO creation is unavailable")
    try:
        started = time.monotonic()
        result = workspace_revision._read_owned_file(
            repository,
            target,
            64 * 1024 * 1024,
            deadline=started + 0.25,
            cancelled=None,
        )
        elapsed = time.monotonic() - started
    finally:
        target.unlink()
        target.write_bytes(original)

    assert result is None
    assert elapsed < 0.1


@pytest.mark.skipif(not _linux_memfd_available(), reason="Linux metadata descriptors")
@pytest.mark.parametrize("metadata", ["config", "HEAD", "index", "ref"])
def test_private_metadata_open_race_cannot_block_past_caller_deadline(
    repository: Path,
    monkeypatch: pytest.MonkeyPatch,
    metadata: str,
) -> None:
    scope = resolve_repository_scope(repository)
    expected = compute_workspace_revision(scope)
    if metadata == "ref":
        reference = _run_git(repository, "symbolic-ref", "HEAD").decode("ascii").strip()
        target_name = PurePosixPath(reference).name
    else:
        target_name = metadata
    real_open = workspace_revision.os.open
    fired = False

    def simulate_fifo_race(path, flags, *args, **kwargs):
        nonlocal fired
        if (
            path == target_name
            and kwargs.get("dir_fd") is not None
            and not flags & getattr(os, "O_DIRECTORY", 0)
            and not fired
        ):
            fired = True
            if not flags & os.O_NONBLOCK:
                time.sleep(1.0)
            raise BlockingIOError("injected FIFO replacement")
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(workspace_revision.os, "open", simulate_fifo_race)
    started = time.monotonic()
    result = workspace_revision.verify_workspace_revision_unchanged(
        scope,
        expected,
        deadline=started + 0.5,
    )
    elapsed = time.monotonic() - started

    assert fired
    assert result is True
    assert elapsed < 0.5


@pytest.mark.skipif(not _linux_memfd_available(), reason="Linux memfd optimization")
def test_private_index_post_proof_cancellation_propagates(
    repository: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scope = resolve_repository_scope(repository)
    expected = compute_workspace_revision(scope)
    real_validate = workspace_revision._validate_private_git_proof
    validation_started = False

    def cancelled() -> bool:
        return validation_started

    def cancel_during_validation(*args, **kwargs):
        nonlocal validation_started
        validation_started = True
        return real_validate(*args, **kwargs)

    monkeypatch.setattr(
        workspace_revision,
        "_validate_private_git_proof",
        cancel_during_validation,
    )

    with pytest.raises(TimeoutError, match="cancelled"):
        workspace_revision.verify_workspace_revision_unchanged(
            scope,
            expected,
            cancelled=cancelled,
        )
    assert validation_started


@pytest.mark.skipif(not _linux_memfd_available(), reason="Linux memfd optimization")
@pytest.mark.parametrize(
    "source",
    ["local-config", "global-config", "absent-global-config", "info-attributes"],
)
def test_private_index_semantics_files_are_fenced_after_status(
    repository: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source: str,
) -> None:
    if source == "local-config":
        target = repository / ".git/config"
        replacement = target.read_text(encoding="utf-8") + "[filter \"late\"]\nclean = cat\n"
    elif source in {"global-config", "absent-global-config"}:
        config_home = tmp_path / "xdg"
        target = config_home / "git/config"
        target.parent.mkdir(parents=True)
        if source == "global-config":
            target.write_text("[user]\nname = Inert Identity\n", encoding="ascii")
        replacement = "[core]\nautocrlf = false\n"
        monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    else:
        target = repository / ".git/info/attributes"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("# initially inert\n", encoding="ascii")
        replacement = "*.py text\n"
    scope = resolve_repository_scope(repository)
    expected = compute_workspace_revision(scope)
    real_private = workspace_revision._git_state_with_private_index
    mutated = False

    def mutate_after_private_status(*args, **kwargs):
        nonlocal mutated
        result = real_private(*args, **kwargs)
        target.write_text(replacement, encoding="utf-8")
        mutated = True
        return result

    monkeypatch.setattr(
        workspace_revision,
        "_git_state_with_private_index",
        mutate_after_private_status,
    )
    result, exact_calls, private_calls = _verification_git_call_counts(
        scope, expected, monkeypatch
    )

    assert mutated
    assert result is True
    assert exact_calls == 1
    assert private_calls == 1


@pytest.mark.skipif(not _linux_memfd_available(), reason="Linux Git ignore semantics")
@pytest.mark.parametrize("source", ["worktree", "info", "global"])
def test_private_index_ignore_sources_are_fenced_through_final_proof(
    repository: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source: str,
) -> None:
    if source == "worktree":
        target = repository / ".gitignore"
        target.write_text("hidden.py\n", encoding="ascii")
        _run_git(repository, "add", ".gitignore")
        _run_git(repository, "commit", "-m", "ignore hidden source")
    elif source == "info":
        target = repository / ".git/info/exclude"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("hidden.py\n", encoding="ascii")
    else:
        config_home = tmp_path / "xdg"
        target = config_home / "git/ignore"
        target.parent.mkdir(parents=True)
        target.write_text("hidden.py\n", encoding="ascii")
        monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    (repository / "hidden.py").write_text("hidden = True\n", encoding="ascii")
    scope = resolve_repository_scope(repository)
    expected = compute_workspace_revision(scope)
    real_validate = workspace_revision._validate_private_git_proof
    mutated = False

    def mutate_before_final_proof(*args, **kwargs):
        nonlocal mutated
        target.write_text("", encoding="ascii")
        mutated = True
        return real_validate(*args, **kwargs)

    monkeypatch.setattr(
        workspace_revision,
        "_validate_private_git_proof",
        mutate_before_final_proof,
    )

    result, exact_calls, private_calls = _verification_git_call_counts(
        scope, expected, monkeypatch
    )

    assert mutated
    assert result is False
    assert exact_calls == 1
    assert private_calls == 1


@pytest.mark.skipif(not _linux_memfd_available(), reason="Linux memfd optimization")
def test_private_index_status_passes_only_sealable_memfd_with_sanitized_environment(
    repository: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scope = resolve_repository_scope(repository)
    expected = compute_workspace_revision(scope)
    real_popen = subprocess.Popen
    real_create = os.memfd_create
    installation = workspace_revision._private_git_installation()
    assert installation is not None
    calls: list[tuple[list[str], dict[str, object]]] = []
    created: list[tuple[int, int]] = []
    monkeypatch.setenv("GIT_DIR", str(repository.parent / "hostile.git"))
    monkeypatch.setenv("GIT_INDEX_FILE", str(repository.parent / "hostile.index"))

    def recording_create(name: str, flags: int = os.MFD_CLOEXEC) -> int:
        descriptor = real_create(name, flags)
        created.append((descriptor, flags))
        return descriptor

    def recording_popen(command, **kwargs):
        calls.append((command, kwargs))
        return real_popen(command, **kwargs)

    monkeypatch.setattr(workspace_revision.os, "memfd_create", recording_create)
    monkeypatch.setattr(workspace_revision.subprocess, "Popen", recording_popen)

    assert workspace_revision.verify_workspace_revision_unchanged(scope, expected) is True

    assert len(created) == 1
    descriptor, flags = created[0]
    assert flags & os.MFD_CLOEXEC
    assert flags & os.MFD_ALLOW_SEALING
    assert len(calls) == 1
    command, options = calls[0]
    assert command == [
        str(installation.executable),
        "--no-optional-locks",
        "-c",
        "core.fsmonitor=false",
        "-C",
        scope.checkout_root,
        "status",
        "--porcelain=v2",
        "--branch",
        "-z",
        "--untracked-files=all",
        "--ignore-submodules=all",
    ]
    assert options["pass_fds"] == (descriptor,)
    assert options["close_fds"] is True
    assert options["start_new_session"] is True
    assert options["shell"] is False
    assert options["env"]["GIT_INDEX_FILE"] == f"/proc/self/fd/{descriptor}"
    assert options["env"]["GIT_OPTIONAL_LOCKS"] == "0"
    assert options["env"]["GIT_TERMINAL_PROMPT"] == "0"
    assert "GIT_DIR" not in options["env"]
    assert "GIT_CONFIG_COUNT" not in options["env"]
    assert not (repository / ".git/index.lock").exists()


@pytest.mark.skipif(not _linux_memfd_available(), reason="Linux memfd optimization")
def test_private_index_memfd_creation_failure_uses_exact_fallback(
    repository: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scope = resolve_repository_scope(repository)
    expected = compute_workspace_revision(scope)
    exact_calls = 0
    real_exact = workspace_revision._git_state

    def recording_exact(*args, **kwargs):
        nonlocal exact_calls
        exact_calls += 1
        return real_exact(*args, **kwargs)

    monkeypatch.setattr(workspace_revision, "_git_state", recording_exact)
    monkeypatch.setattr(
        workspace_revision.os,
        "memfd_create",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("memfd unavailable")),
    )

    assert workspace_revision.verify_workspace_revision_unchanged(scope, expected) is True
    assert exact_calls == 1


@pytest.mark.skipif(not _linux_memfd_available(), reason="Linux memfd optimization")
@pytest.mark.parametrize("stop", ["deadline", "cancelled"])
def test_private_index_blocked_status_is_bounded_and_leak_free(
    repository: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stop: str,
) -> None:
    scope = resolve_repository_scope(repository)
    expected = compute_workspace_revision(scope)
    wrapper_root = tmp_path / "git-wrapper"
    wrapper_root.mkdir()
    wrapper = wrapper_root / "git"
    pid_path = wrapper_root / "pid"
    wrapper.write_text(
        "#!/bin/sh\n"
        "printf '%s' \"$$\" > \"$REVISION_TEST_PID\"\n"
        "sleep 30\n",
        encoding="ascii",
    )
    wrapper.chmod(0o755)
    _qualify_private_git_wrapper(monkeypatch, wrapper)
    monkeypatch.setenv("REVISION_TEST_PID", str(pid_path))
    monkeypatch.setenv("PATH", f"{wrapper_root}{os.pathsep}{os.environ.get('PATH', '')}")
    real_create = os.memfd_create
    descriptors: list[int] = []

    def recording_create(*args, **kwargs):
        descriptor = real_create(*args, **kwargs)
        descriptors.append(descriptor)
        return descriptor

    monkeypatch.setattr(workspace_revision.os, "memfd_create", recording_create)
    started = time.monotonic()
    deadline = started + (0.1 if stop == "deadline" else 1.0)
    cancelled = None if stop == "deadline" else lambda: time.monotonic() >= started + 0.05

    with pytest.raises(TimeoutError, match="deadline|cancel"):
        workspace_revision.verify_workspace_revision_unchanged(
            scope,
            expected,
            deadline=deadline,
            cancelled=cancelled,
        )

    assert time.monotonic() - started < 0.75
    assert descriptors
    for descriptor in descriptors:
        with pytest.raises(OSError):
            os.fstat(descriptor)
    pid = int(pid_path.read_text(encoding="ascii"))
    for _ in range(100):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.01)
    else:
        pytest.fail("timed-out Git process remained alive")
    assert not (repository / ".git/index.lock").exists()


@pytest.mark.skipif(not _linux_memfd_available(), reason="Linux process groups")
def test_private_index_success_cleans_descendants_before_reaping_group_leader(
    repository: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_git = shutil.which("git")
    if real_git is None:
        pytest.skip("Git executable is unavailable")
    scope = resolve_repository_scope(repository)
    expected = compute_workspace_revision(scope)
    wrapper_root = tmp_path / "successful-git-wrapper"
    wrapper_root.mkdir()
    descendant_pid_path = wrapper_root / "descendant-pid"
    wrapper = wrapper_root / "git"
    wrapper.write_text(
        "#!/bin/sh\n"
        '"$REVISION_REAL_GIT" "$@"\n'
        "status=$?\n"
        "sleep 30 </dev/null >/dev/null 2>&1 &\n"
        'printf "%s" "$!" > "$REVISION_DESCENDANT_PID"\n'
        'exit "$status"\n',
        encoding="ascii",
    )
    wrapper.chmod(0o755)
    _qualify_private_git_wrapper(monkeypatch, wrapper)
    monkeypatch.setenv("REVISION_REAL_GIT", real_git)
    monkeypatch.setenv("REVISION_DESCENDANT_PID", str(descendant_pid_path))
    monkeypatch.setenv("PATH", f"{wrapper_root}{os.pathsep}{os.environ.get('PATH', '')}")

    descendant_pid = None
    try:
        assert workspace_revision.verify_workspace_revision_unchanged(scope, expected) is True
        descendant_pid = int(descendant_pid_path.read_text(encoding="ascii"))
        for _ in range(100):
            try:
                process_state = Path(f"/proc/{descendant_pid}/stat").read_text(
                    encoding="ascii"
                ).split()[2]
            except FileNotFoundError:
                break
            if process_state == "Z":
                break
            time.sleep(0.01)
        else:
            pytest.fail("successful private Git descendant remained alive")
    finally:
        if descendant_pid is not None:
            try:
                os.kill(descendant_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


@pytest.mark.skipif(not _linux_memfd_available(), reason="Linux memfd optimization")
def test_private_index_avoids_real_index_worktree_refresh_without_timing(
    repository: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scope = resolve_repository_scope(repository)
    expected = compute_workspace_revision(scope)
    target = repository / "pkg/api.py"
    info = target.stat()
    os.utime(target, ns=(info.st_atime_ns, info.st_mtime_ns + 2_000_000_000))
    index_path = repository / ".git/index"
    original_index = index_path.read_bytes()
    private_calls = 0
    real_private = workspace_revision._git_state_with_private_index

    def inspect_private(root: Path, descriptor: int, **kwargs):
        nonlocal private_calls
        private_calls += 1
        private_content = os.pread(descriptor, len(original_index) + 1, 0)
        assert private_content != original_index
        return real_private(root, descriptor, **kwargs)

    monkeypatch.setattr(workspace_revision, "_git_state_with_private_index", inspect_private)
    monkeypatch.setattr(
        workspace_revision,
        "_git_state",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("real-index status refresh used")
        ),
    )

    assert workspace_revision.verify_workspace_revision_unchanged(scope, expected) is True
    assert private_calls == 1
    assert index_path.read_bytes() == original_index
    assert not (repository / ".git/index.lock").exists()


def test_verifier_reuses_computed_inventory_without_rescanning(
    repository: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scope = resolve_repository_scope(repository)
    expected = compute_workspace_revision(scope)

    def unexpected_rescan(*args, **kwargs):
        raise AssertionError("unchanged computed inventory was rescanned")
        yield  # pragma: no cover - keep this replacement a generator

    monkeypatch.setattr(workspace_revision, "_relevant_files", unexpected_rescan)

    assert workspace_revision.verify_workspace_revision_unchanged(scope, expected) is True


def test_inventory_hint_rescans_after_directory_change(
    repository: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (repository / ".gitignore").write_text("ignored.txt\n", encoding="ascii")
    _run_git(repository, "add", ".gitignore")
    _run_git(repository, "commit", "-m", "ignore irrelevant file")
    scope = resolve_repository_scope(repository)
    expected = compute_workspace_revision(scope)
    (repository / "ignored.txt").write_text("ignored\n", encoding="ascii")
    scans = 0
    real_relevant_files = workspace_revision._relevant_files

    def record_scan(*args, **kwargs):
        nonlocal scans
        scans += 1
        yield from real_relevant_files(*args, **kwargs)

    monkeypatch.setattr(workspace_revision, "_relevant_files", record_scan)

    assert workspace_revision.verify_workspace_revision_unchanged(scope, expected) is True
    assert scans == 1


@pytest.mark.skipif(not _linux_memfd_available(), reason="Linux private inventory snapshots")
def test_inventory_hint_rescans_after_file_metadata_change(
    repository: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scope = resolve_repository_scope(repository)
    expected = compute_workspace_revision(scope)
    target = repository / "pkg/api.py"
    info = target.stat()
    os.utime(target, ns=(info.st_atime_ns, info.st_mtime_ns + 2_000_000_000))
    scans = 0
    real_relevant_files = workspace_revision._relevant_files

    def record_scan(*args, **kwargs):
        nonlocal scans
        scans += 1
        yield from real_relevant_files(*args, **kwargs)

    monkeypatch.setattr(workspace_revision, "_relevant_files", record_scan)

    assert workspace_revision.verify_workspace_revision_unchanged(scope, expected) is True
    assert scans == 1


def test_inventory_hint_declines_oversized_inventory(
    repository: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(workspace_revision, "_MAX_INVENTORY_HINT_ENTRIES", 0)
    scope = resolve_repository_scope(repository)
    expected = compute_workspace_revision(scope)
    scans = 0
    real_relevant_files = workspace_revision._relevant_files

    def record_scan(*args, **kwargs):
        nonlocal scans
        scans += 1
        yield from real_relevant_files(*args, **kwargs)

    monkeypatch.setattr(workspace_revision, "_relevant_files", record_scan)

    assert workspace_revision.verify_workspace_revision_unchanged(scope, expected) is True
    assert scans == 1


def test_inventory_hint_does_not_swallow_one_shot_cancellation(
    repository: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scope = resolve_repository_scope(repository)
    expected = compute_workspace_revision(scope)
    real_validate = workspace_revision._validate_directory_snapshot
    cancellation_armed = False
    armed_once = False

    def arm_cancellation(*args, **kwargs):
        nonlocal cancellation_armed, armed_once
        result = real_validate(*args, **kwargs)
        if not armed_once:
            armed_once = True
            cancellation_armed = True
        return result

    def cancelled() -> bool:
        nonlocal cancellation_armed
        if not cancellation_armed:
            return False
        cancellation_armed = False
        return True

    monkeypatch.setattr(
        workspace_revision,
        "_validate_directory_snapshot",
        arm_cancellation,
    )

    with pytest.raises(TimeoutError, match="cancelled"):
        workspace_revision.verify_workspace_revision_unchanged(
            scope,
            expected,
            cancelled=cancelled,
        )


def test_hash_file_reads_owned_descriptor_without_fdopen(
    repository: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = repository / "pkg" / "api.py"
    content = path.read_bytes()
    read_sizes: list[int] = []
    real_read = os.read

    def bounded_read(descriptor: int, size: int) -> bytes:
        read_sizes.append(size)
        return real_read(descriptor, size)

    monkeypatch.setattr(workspace_revision.os, "read", bounded_read)
    monkeypatch.setattr(
        workspace_revision.os,
        "fdopen",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("fdopen used")),
    )

    digest, size, _snapshot = workspace_revision._hash_file(
        path,
        root=repository,
        resolved_root=repository.resolve(strict=True),
        directory_snapshots={},
        remaining_bytes=MAX_REVISION_BYTES,
        deadline=time.monotonic() + 5,
        cancelled=None,
    )

    assert digest == workspace_revision.hashlib.sha256(content).hexdigest()
    assert size == len(content)
    assert read_sizes
    assert max(read_sizes) <= 1024 * 1024


def test_reused_scope_observes_live_head_after_empty_commit(repository: Path) -> None:
    scope = resolve_repository_scope(repository)
    before = compute_workspace_revision(scope)
    subprocess.run(
        ["git", "commit", "--allow-empty", "-m", "move head"],
        cwd=repository,
        check=True,
        capture_output=True,
        timeout=10,
    )
    current_head = subprocess.run(
        ["git", "rev-parse", "--verify", "HEAD^{commit}"],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    ).stdout.strip()

    after = compute_workspace_revision(scope)

    assert before.git_head == scope.git_commit
    assert after.git_head == current_head
    assert after.git_head != before.git_head
    assert after.revision_sha256 != before.revision_sha256


def test_delta_detects_content_identical_rename(repository: Path) -> None:
    scope = resolve_repository_scope(repository)
    before = compute_workspace_revision(scope)
    (repository / "pkg/rename_target.py").rename(repository / "pkg/renamed.py")

    after = compute_workspace_revision(scope)
    delta = diff_workspace_revisions(before, after)

    assert delta.renamed == (("pkg/rename_target.py", "pkg/renamed.py"),)
    assert delta.created == ()
    assert delta.deleted == ()


@pytest.mark.parametrize(
    ("source", "destination"),
    [
        ("pyrightconfig.json", "pkg/settings.json"),
        ("pkg/settings.py", "pyrightconfig.json"),
    ],
)
def test_content_rename_reports_configuration_change_for_either_path(
    source: str, destination: str
) -> None:
    digest = "a" * 64
    before = WorkspaceRevision(
        "repository:test",
        "checkout:test",
        None,
        (RevisionEntry(source, "configuration", digest, 1),),
        "before",
    )
    after = WorkspaceRevision(
        "repository:test",
        "checkout:test",
        None,
        (RevisionEntry(destination, "configuration", digest, 1),),
        "after",
    )

    delta = diff_workspace_revisions(before, after)

    assert delta.renamed == ((source, destination),)
    assert delta.configuration_changed is True


def test_ambiguous_content_matches_remain_created_and_deleted(repository: Path) -> None:
    scope = resolve_repository_scope(repository)
    original = repository / "pkg/rename_target.py"
    duplicate = repository / "pkg/rename_duplicate.py"
    duplicate.write_bytes(original.read_bytes())
    subprocess.run(["git", "add", "."], cwd=repository, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "duplicate"], cwd=repository, check=True, capture_output=True
    )
    scope = resolve_repository_scope(repository)
    before = compute_workspace_revision(scope)
    original.unlink()
    duplicate.unlink()
    first = repository / "pkg/renamed_a.py"
    second = repository / "pkg/renamed_b.py"
    first.write_text('RENAME_VALUE = "stable"\n', encoding="utf-8")
    second.write_bytes(first.read_bytes())

    delta = diff_workspace_revisions(before, compute_workspace_revision(scope))

    assert delta.renamed == ()
    assert delta.created == ("pkg/renamed_a.py", "pkg/renamed_b.py")
    assert delta.deleted == ("pkg/rename_duplicate.py", "pkg/rename_target.py")


def test_porcelain_type_two_marks_only_rename_sources_deleted() -> None:
    metadata = b"R. N... 100644 100644 100644 " + b"a" * 40 + b" " + b"b" * 40
    rename = b"2 " + metadata + b" R100 pkg/renamed.py\0pkg/original.py\0"
    copy = b"2 " + metadata + b" C100 pkg/copied.py\0pkg/source.py\0"

    assert workspace_revision._status_paths(rename) == [
        ("pkg/original.py", "deleted"),
        ("pkg/renamed.py", "modified"),
    ]
    assert workspace_revision._status_paths(copy) == [("pkg/copied.py", "modified")]


def test_porcelain_type_two_marks_deleted_destination_from_xy_status() -> None:
    metadata = b".D N... 100644 100644 000000 " + b"a" * 40 + b" " + b"b" * 40
    rename = b"2 " + metadata + b" R100 pkg/renamed.py\0pkg/original.py\0"
    copy = b"2 " + metadata + b" C100 pkg/copied.py\0pkg/source.py\0"

    assert workspace_revision._status_paths(rename) == [
        ("pkg/original.py", "deleted"),
        ("pkg/renamed.py", "deleted"),
    ]
    assert workspace_revision._status_paths(copy) == [("pkg/copied.py", "deleted")]


def test_non_git_manifest_includes_only_sources_and_root_configs(tmp_path: Path) -> None:
    root = copy_python_fixture(tmp_path / "plain")
    (root / "pkg/types.pyi").write_text("value: int\n", encoding="utf-8")
    (root / "requirements-dev.txt").write_text("pytest\n", encoding="utf-8")
    (root / "notes.txt").write_text("not relevant\n", encoding="utf-8")
    (root / "pkg/pyrightconfig.json").write_text("{}", encoding="utf-8")

    revision = compute_workspace_revision(resolve_repository_scope(root))
    paths = tuple(entry.path for entry in revision.entries)

    assert revision.git_head is None
    assert paths == tuple(sorted(paths))
    assert "pkg/types.pyi" in paths
    assert "pyproject.toml" in paths
    assert "pyrightconfig.json" in paths
    assert "requirements-dev.txt" in paths
    assert "uv.lock" in paths
    assert "notes.txt" not in paths
    assert "pkg/pyrightconfig.json" not in paths
    assert all(entry.kind in {"source", "configuration"} for entry in revision.entries)


@pytest.mark.parametrize("kind", ["file", "config", "directory"])
def test_non_git_manifest_rejects_relevant_symlinks(tmp_path: Path, kind: str) -> None:
    root = tmp_path / "plain"
    root.mkdir()
    outside_file = tmp_path / "outside.py"
    outside_file.write_text("secret = 1\n", encoding="utf-8")
    outside_directory = tmp_path / "outside"
    outside_directory.mkdir()
    (outside_directory / "nested.py").write_text("secret = 2\n", encoding="utf-8")
    if kind == "file":
        _symlink_or_skip(root / "linked.py", outside_file)
    elif kind == "config":
        _symlink_or_skip(root / "pyrightconfig.json", outside_file)
    else:
        _symlink_or_skip(root / "linked-directory", outside_directory, directory=True)

    with pytest.raises(PermissionError, match="symlink|reparse|directory"):
        compute_workspace_revision(resolve_repository_scope(root))


def test_non_git_manifest_rejects_windows_junction(tmp_path: Path) -> None:
    root = tmp_path / "plain"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "nested.py").write_text("secret = True\n", encoding="utf-8")
    _junction_or_skip(root / "linked-directory", outside)

    with pytest.raises(PermissionError, match="reparse|directory"):
        compute_workspace_revision(resolve_repository_scope(root))


def test_git_status_rejects_relevant_symlink(repository: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside.py"
    outside.write_text("secret = True\n", encoding="utf-8")
    _symlink_or_skip(repository / "pkg" / "linked.py", outside)

    with pytest.raises(PermissionError, match="symlink|reparse"):
        compute_workspace_revision(resolve_repository_scope(repository))


def test_hash_rejects_file_replaced_by_symlink_before_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "plain"
    root.mkdir()
    candidate = root / "candidate.py"
    candidate.write_text("safe = True\n", encoding="utf-8")
    outside = tmp_path / "outside.py"
    outside.write_text("secret = True\n", encoding="utf-8")
    scope = resolve_repository_scope(root)
    real_hash = workspace_revision._hash_file
    replaced = False

    def replace_then_hash(path: Path, **kwargs):
        nonlocal replaced
        if not replaced and path == candidate:
            candidate.unlink()
            _symlink_or_skip(candidate, outside)
            replaced = True
        return real_hash(path, **kwargs)

    monkeypatch.setattr(workspace_revision, "_hash_file", replace_then_hash)

    with pytest.raises(PermissionError, match="symlink|escape|changed|regular"):
        compute_workspace_revision(scope)


def test_hash_rejects_parent_replaced_by_internal_directory_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "plain"
    package = root / "pkg"
    replacement = root / "replacement"
    package.mkdir(parents=True)
    replacement.mkdir()
    candidate = package / "candidate.py"
    candidate.write_text("safe = True\n", encoding="utf-8")
    (replacement / "candidate.py").write_text("secret = True\n", encoding="utf-8")
    scope = resolve_repository_scope(root)
    real_hash = workspace_revision._hash_file
    replaced = False

    def replace_then_hash(path: Path, **kwargs):
        nonlocal replaced
        if not replaced and path == candidate:
            candidate.unlink()
            package.rmdir()
            _symlink_or_skip(package, replacement, directory=True)
            replaced = True
        return real_hash(path, **kwargs)

    monkeypatch.setattr(workspace_revision, "_hash_file", replace_then_hash)

    with pytest.raises(PermissionError, match="symlink|directory|changed"):
        compute_workspace_revision(scope)


def test_hash_rejects_parent_identity_change_even_when_file_inode_is_preserved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "plain"
    package = root / "pkg"
    package.mkdir(parents=True)
    candidate = package / "candidate.py"
    candidate.write_text("stable = True\n", encoding="utf-8")
    scope = resolve_repository_scope(root)
    real_open = os.open
    replaced = False

    def replace_parent_during_open(path, flags, *args, **kwargs):
        nonlocal replaced
        if not replaced and Path(path) == candidate:
            old_package = root / "old-pkg"
            package.rename(old_package)
            package.mkdir()
            os.link(old_package / "candidate.py", candidate)
            replaced = True
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(workspace_revision.os, "open", replace_parent_during_open)

    with pytest.raises(PermissionError, match="parent|directory|changed"):
        compute_workspace_revision(scope)


def test_final_fence_rejects_file_changed_after_hash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "plain"
    root.mkdir()
    candidate = root / "candidate.py"
    candidate.write_text("before = True\n", encoding="utf-8")
    scope = resolve_repository_scope(root)
    real_hash = workspace_revision._hash_file

    def mutate_after_hash(path: Path, **kwargs):
        result = real_hash(path, **kwargs)
        candidate.write_text("after = True\n", encoding="utf-8")
        return result

    monkeypatch.setattr(workspace_revision, "_hash_file", mutate_after_hash)

    with pytest.raises(PermissionError, match="changed|consistency|snapshot"):
        compute_workspace_revision(scope)


def test_final_fence_rejects_git_head_change(repository: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    scope = resolve_repository_scope(repository)
    heads = iter(("a" * 40, "b" * 40))
    monkeypatch.setattr(
        workspace_revision,
        "_git_state",
        lambda *args, **kwargs: (next(heads), b""),
    )

    with pytest.raises(RuntimeError, match="HEAD.*changed|changed.*HEAD"):
        compute_workspace_revision(scope)


@pytest.mark.parametrize(
    ("mutation_moment", "expected_error", "expected_match"),
    [
        ("before", RuntimeError, "Git status changed"),
        ("after", PermissionError, "file snapshot changed"),
    ],
)
def test_final_git_probe_file_mutation_is_rejected(
    repository: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation_moment: str,
    expected_error: type[Exception],
    expected_match: str,
) -> None:
    scope = resolve_repository_scope(repository)
    target = repository / "pkg" / "api.py"
    real_probe = workspace_revision._git_state
    calls = 0

    def mutate_on_second_probe(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2 and mutation_moment == "before":
            target.write_text("class ChangedDuringFinalProbe:\n    pass\n", encoding="utf-8")
        result = real_probe(*args, **kwargs)
        if calls == 2 and mutation_moment == "after":
            target.write_text("class ChangedDuringFinalProbe:\n    pass\n", encoding="utf-8")
        return result

    monkeypatch.setattr(workspace_revision, "_git_state", mutate_on_second_probe)

    with pytest.raises(expected_error, match=expected_match):
        compute_workspace_revision(scope)

    assert calls == 2


def test_final_status_rejects_index_only_transition(
    repository: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = repository / "pkg" / "api.py"
    changed = b"class IndexOnlyTransition:\n    pass\n"
    target.write_bytes(changed)
    scope = resolve_repository_scope(repository)
    real_state = workspace_revision._git_state
    calls = 0

    def stage_during_second_state(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            subprocess.run(
                ["git", "add", "pkg/api.py"],
                cwd=repository,
                check=True,
                capture_output=True,
                timeout=10,
            )
        return real_state(*args, **kwargs)

    monkeypatch.setattr(workspace_revision, "_git_state", stage_during_second_state)

    with pytest.raises(RuntimeError, match="status.*changed|changed.*status"):
        compute_workspace_revision(scope)

    assert calls == 2
    assert target.read_bytes() == changed


def test_hash_rejects_symlink_swap_at_descriptor_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "plain"
    root.mkdir()
    candidate = root / "candidate.py"
    candidate.write_text("safe = True\n", encoding="utf-8")
    outside = tmp_path / "outside.py"
    outside.write_text("secret = True\n", encoding="utf-8")
    probe = root / "probe.py"
    _symlink_or_skip(probe, outside)
    probe.unlink()
    scope = resolve_repository_scope(root)
    real_open = os.open
    replaced = False

    def replace_during_open(path, flags, *args, **kwargs):
        nonlocal replaced
        if not replaced and Path(path) == candidate:
            candidate.unlink()
            os.symlink(outside, candidate)
            replaced = True
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(workspace_revision.os, "open", replace_during_open)

    with pytest.raises(PermissionError, match="open|symlink|changed"):
        compute_workspace_revision(scope)


def test_all_relevant_root_configuration_names_are_in_deterministic_order(tmp_path: Path) -> None:
    root = tmp_path / "plain"
    root.mkdir()
    for name in reversed(sorted(PYTHON_CONFIG_NAMES)):
        (root / name).write_text(name, encoding="utf-8")
    for name in ("requirements-z.txt", "requirements.txt", "requirements-a.txt"):
        (root / name).write_text(name, encoding="utf-8")

    first = compute_workspace_revision(resolve_repository_scope(root))
    second = compute_workspace_revision(resolve_repository_scope(root))
    expected = tuple(sorted((*PYTHON_CONFIG_NAMES, "requirements-a.txt", "requirements-z.txt", "requirements.txt")))

    assert tuple(entry.path for entry in first.entries) == expected
    assert all(entry.kind == "configuration" for entry in first.entries)
    assert first == second


def test_git_state_is_nul_bounded_noninteractive_and_uses_sanitized_environment(
    repository: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scope = resolve_repository_scope(repository)
    real_popen = subprocess.Popen
    calls: list[tuple[list[str], dict[str, object]]] = []
    monkeypatch.setenv("GIT_DIR", str(repository.parent / "hostile.git"))
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.worktree")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", str(repository.parent))

    def recording_popen(command, **kwargs):
        calls.append((command, kwargs))
        return real_popen(command, **kwargs)

    monkeypatch.setattr(workspace_revision.subprocess, "Popen", recording_popen)

    compute_workspace_revision(scope)

    status_calls = [call for call in calls if "status" in call[0]]
    assert len(status_calls) == 2
    assert not any("rev-parse" in command for command, _options in calls)
    for command, options in status_calls:
        assert command == [
            "git",
            "--no-optional-locks",
            "-c",
            "core.fsmonitor=false",
            "-C",
            scope.checkout_root,
            "status",
            "--porcelain=v2",
            "--branch",
            "-z",
            "--untracked-files=all",
            "--ignore-submodules=all",
        ]
        assert options["stdin"] is subprocess.DEVNULL
        assert options["stdout"] is subprocess.PIPE
        assert options["stderr"] is subprocess.DEVNULL
        assert options["shell"] is False
        if os.name == "nt":
            assert options["creationflags"] & subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            assert options["start_new_session"] is True
        assert options["env"]["GIT_TERMINAL_PROMPT"] == "0"
        assert options["env"]["GIT_OPTIONAL_LOCKS"] == "0"
        assert "GIT_DIR" not in options["env"]
        assert not any(name.startswith("GIT_CONFIG_") for name in options["env"])
    options = status_calls[0][1]
    assert all(call_options["env"] == options["env"] for _command, call_options in status_calls)


def test_git_status_output_is_read_to_a_fixed_ceiling_and_overflow_kills_process(
    repository: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scope = resolve_repository_scope(repository)
    reads: list[int] = []

    class Output:
        def __init__(self, content: bytes | None) -> None:
            self.content = content

        def read(self, size: int) -> bytes:
            reads.append(size)
            return b"x" * size if self.content is None else self.content

        def close(self) -> None:
            pass

    class Process:
        def __init__(self, pid: int, content: bytes | None) -> None:
            self.pid = pid
            self.stdout = Output(content)
            self.returncode = None
            self.killed = False

        def poll(self):
            return self.returncode

        def wait(self, timeout=None):
            if self.returncode is None:
                self.returncode = 0
            return self.returncode

        def kill(self):
            self.killed = True
            self.returncode = -9

    state_process = Process(42, None)
    processes = iter((state_process,))
    monkeypatch.setattr(workspace_revision, "MAX_GIT_STATUS_BYTES", 8)
    monkeypatch.setattr(
        workspace_revision.subprocess, "Popen", lambda *args, **kwargs: next(processes)
    )
    terminated = []
    monkeypatch.setattr(
        workspace_revision,
        "_terminate_process_tree",
        lambda target: terminated.append(target.pid),
        raising=False,
    )

    with pytest.raises(ValueError, match="Git state.*byte ceiling"):
        compute_workspace_revision(scope)

    assert reads == [9]
    assert terminated == [42]


def test_output_overflow_cleans_tree_after_direct_process_exit(
    repository: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Output:
        def read(self, size: int) -> bytes:
            return b"x" * size

        def close(self) -> None:
            pass

    class Process:
        stdout = Output()
        returncode = 0
        pid = 42

        def poll(self):
            return self.returncode

        def wait(self, timeout=None):
            return self.returncode

        def kill(self):
            self.returncode = -9

    terminated = []
    monkeypatch.setattr(workspace_revision.subprocess, "Popen", lambda *args, **kwargs: Process())
    monkeypatch.setattr(
        workspace_revision,
        "_terminate_process_tree",
        lambda process: terminated.append(process.pid),
    )

    with pytest.raises(ValueError, match="byte ceiling"):
        workspace_revision._git_output(
            repository,
            ["status"],
            maximum_bytes=8,
            label="Git status",
            deadline=None,
            cancelled=None,
        )

    assert terminated == [42]


def test_live_git_state_output_is_bounded(repository: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    scope = resolve_repository_scope(repository)
    reads: list[int] = []

    class Output:
        def __init__(self, overflow: bool) -> None:
            self.overflow = overflow

        def read(self, size: int) -> bytes:
            reads.append(size)
            return b"x" * size if self.overflow else b""

        def close(self) -> None:
            pass

    class Process:
        def __init__(self, overflow: bool) -> None:
            self.pid = 42
            self.stdout = Output(overflow)
            self.returncode = None
            self.killed = False

        def poll(self):
            return self.returncode

        def wait(self, timeout=None):
            if self.returncode is None:
                self.returncode = 0
            return self.returncode

        def kill(self):
            self.killed = True
            self.returncode = -9

    processes = iter((Process(True),))
    monkeypatch.setattr(
        workspace_revision.subprocess,
        "Popen",
        lambda *args, **kwargs: next(processes),
    )
    monkeypatch.setattr(workspace_revision, "MAX_GIT_STATUS_BYTES", 64)
    monkeypatch.setattr(workspace_revision, "_terminate_process_tree", lambda process: process.kill())

    with pytest.raises(ValueError, match="Git state.*byte ceiling"):
        compute_workspace_revision(scope)

    assert reads == [65]


def test_windows_tree_cleanup_uses_system_taskkill(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []

    class Terminator:
        returncode = 0

        def communicate(self, timeout=None):
            calls.append(("communicate", timeout))
            return b"", b""

    class Process:
        pid = 42
        returncode = None

        def poll(self):
            return self.returncode

        def wait(self, timeout=None):
            self.returncode = -9
            return self.returncode

        def kill(self):
            self.returncode = -9

    def popen(command, **kwargs):
        calls.append((command, kwargs))
        return Terminator()

    monkeypatch.setenv("SystemRoot", r"D:\Windows")
    monkeypatch.setattr(workspace_revision.subprocess, "Popen", popen)
    terminate = getattr(workspace_revision, "_terminate_process_tree", lambda *args, **kwargs: None)

    terminate(Process(), platform_name="nt")

    command, options = calls[0]
    assert command == [
        str(PureWindowsPath(r"D:\Windows") / "System32" / "taskkill.exe"),
        "/PID",
        "42",
        "/T",
        "/F",
    ]
    assert options["stdin"] is subprocess.DEVNULL
    assert options["stdout"] is subprocess.DEVNULL
    assert options["stderr"] is subprocess.DEVNULL
    assert options["shell"] is False


def test_posix_tree_cleanup_uses_term_then_kill(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []

    class Process:
        pid = 42
        returncode = None
        waits = 0

        def poll(self):
            return self.returncode

        def wait(self, timeout=None):
            self.waits += 1
            calls.append(("wait", timeout))
            if self.waits == 1:
                raise subprocess.TimeoutExpired("git", timeout)
            self.returncode = -9
            return self.returncode

        def kill(self):
            calls.append(("direct-kill",))
            self.returncode = -9

    monkeypatch.setattr(
        workspace_revision.os,
        "killpg",
        lambda pid, sig: calls.append(("killpg", pid, sig)),
        raising=False,
    )
    terminate = getattr(workspace_revision, "_terminate_process_tree", lambda *args, **kwargs: None)

    terminate(Process(), platform_name="posix")

    assert ("killpg", 42, signal.SIGTERM) in calls
    assert ("killpg", 42, getattr(signal, "SIGKILL", 9)) in calls


@pytest.mark.parametrize("stop", ["deadline", "cancelled"])
def test_revision_honors_preflight_deadline_and_cancellation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stop: str
) -> None:
    root = tmp_path / "plain"
    root.mkdir()
    scope = resolve_repository_scope(root)
    probes = []
    monkeypatch.setattr(workspace_revision.subprocess, "Popen", lambda *args, **kwargs: probes.append(args))
    options = {"deadline": time.monotonic() - 1} if stop == "deadline" else {"cancelled": lambda: True}

    with pytest.raises(TimeoutError, match="deadline|cancel"):
        compute_workspace_revision(scope, **options)

    assert probes == []


def test_revision_checks_cancellation_during_manifest_hashing(tmp_path: Path) -> None:
    root = tmp_path / "plain"
    root.mkdir()
    for index in range(4):
        (root / f"{index}.py").write_text(str(index), encoding="utf-8")
    calls = 0

    def cancelled() -> bool:
        nonlocal calls
        calls += 1
        return calls >= 4

    with pytest.raises(TimeoutError, match="cancel"):
        compute_workspace_revision(resolve_repository_scope(root), cancelled=cancelled)

    assert calls == 4


def test_revision_checks_cancellation_during_recursive_discovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "plain"
    root.mkdir()
    first = root / "first.py"
    second = root / "second.py"
    first.write_text("first", encoding="utf-8")
    second.write_text("second", encoding="utf-8")
    scope = resolve_repository_scope(root)
    calls = 0

    def cancelled() -> bool:
        nonlocal calls
        calls += 1
        return False

    def paths(_path: Path, _pattern: str):
        yield first
        if calls < 2:
            raise AssertionError("recursive discovery did not check cancellation")
        yield second

    monkeypatch.setattr(Path, "rglob", paths)

    compute_workspace_revision(scope, cancelled=cancelled)


def test_revision_checks_cancellation_between_hash_chunks(tmp_path: Path) -> None:
    root = tmp_path / "plain"
    root.mkdir()
    (root / "large.py").write_bytes(b"x" * (2 * 1024 * 1024))
    calls = 0

    def cancelled() -> bool:
        nonlocal calls
        calls += 1
        return calls >= 5

    with pytest.raises(TimeoutError, match="cancel"):
        compute_workspace_revision(resolve_repository_scope(root), cancelled=cancelled)

    assert calls == 5


def test_git_status_uses_only_remaining_deadline_and_kills_blocked_process(
    repository: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import threading

    scope = resolve_repository_scope(repository)
    killed = threading.Event()

    class Output:
        def read(self, _size: int) -> bytes:
            killed.wait(1)
            return b""

        def close(self) -> None:
            pass

    class Process:
        stdout = Output()
        returncode = None
        pid = 42

        def poll(self):
            return self.returncode

        def wait(self, timeout=None):
            return self.returncode

        def kill(self):
            self.returncode = -9
            killed.set()

    monkeypatch.setattr(workspace_revision.subprocess, "Popen", lambda *args, **kwargs: Process())
    monkeypatch.setattr(
        workspace_revision,
        "_terminate_process_tree",
        lambda process: process.kill(),
        raising=False,
    )
    started = time.monotonic()

    with pytest.raises(TimeoutError, match="deadline"):
        compute_workspace_revision(scope, deadline=started + 0.03)

    assert time.monotonic() - started < 0.5
    assert killed.is_set()


def test_git_pipe_reader_cannot_hold_caller_past_deadline(
    repository: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scope = resolve_repository_scope(repository)

    class Output:
        def read(self, _size: int) -> bytes:
            time.sleep(1)
            return b""

        def close(self) -> None:
            pass

    class Process:
        stdout = Output()
        returncode = None
        pid = 42

        def poll(self):
            return self.returncode

        def wait(self, timeout=None):
            if self.returncode is None:
                raise subprocess.TimeoutExpired("git", timeout)
            return self.returncode

        def kill(self):
            self.returncode = -9

    monkeypatch.setattr(workspace_revision.subprocess, "Popen", lambda *args, **kwargs: Process())
    monkeypatch.setattr(
        workspace_revision,
        "_terminate_process_tree",
        lambda process: process.kill(),
    )
    started = time.monotonic()

    with pytest.raises(TimeoutError, match="deadline"):
        compute_workspace_revision(
            scope, deadline=started + 0.03
        )

    assert time.monotonic() - started < 0.5


@pytest.mark.skipif(os.name == "nt", reason="distinct NFC-equivalent names require POSIX")
def test_manifest_rejects_unicode_normalization_collisions(tmp_path: Path) -> None:
    root = tmp_path / "plain"
    root.mkdir()
    composed = "caf\u00e9.py"
    decomposed = unicodedata.normalize("NFD", composed)
    (root / composed).write_text("one", encoding="utf-8")
    (root / decomposed).write_text("two", encoding="utf-8")

    with pytest.raises(ValueError, match="normalization collision"):
        compute_workspace_revision(resolve_repository_scope(root))


@pytest.mark.skipif(os.name == "nt", reason="decomposed names require POSIX")
def test_unchanged_verifier_accepts_decomposed_unicode_path(tmp_path: Path) -> None:
    root = tmp_path / "plain"
    root.mkdir()
    composed = "caf\u00e9.py"
    decomposed = unicodedata.normalize("NFD", composed)
    (root / decomposed).write_text("value = 1\n", encoding="utf-8")
    scope = resolve_repository_scope(root)
    expected = compute_workspace_revision(scope)

    assert expected.entries[0].path == composed
    assert workspace_revision.verify_workspace_revision_unchanged(scope, expected) is True


def test_revision_enforces_file_count_ceiling(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "plain"
    root.mkdir()
    (root / "a.py").write_text("a", encoding="utf-8")
    (root / "b.py").write_text("b", encoding="utf-8")
    monkeypatch.setattr(workspace_revision, "MAX_REVISION_FILES", 1)

    with pytest.raises(ValueError, match="file-count ceiling"):
        compute_workspace_revision(resolve_repository_scope(root))


def test_revision_bounds_examined_irrelevant_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "plain"
    root.mkdir()
    for index in range(3):
        (root / f"irrelevant-{index}.txt").write_text("ignored", encoding="utf-8")
    monkeypatch.setattr(workspace_revision, "MAX_REVISION_FILES", 2)

    with pytest.raises(ValueError, match="examined.*ceiling|entry.*ceiling"):
        compute_workspace_revision(resolve_repository_scope(root))


def test_revision_enforces_total_byte_ceiling(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "plain"
    root.mkdir()
    (root / "a.py").write_bytes(b"123")
    (root / "b.py").write_bytes(b"456")
    monkeypatch.setattr(workspace_revision, "MAX_REVISION_BYTES", 5)

    with pytest.raises(ValueError, match="byte ceiling"):
        compute_workspace_revision(resolve_repository_scope(root))


def test_revision_rejects_known_oversized_file_before_reading_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "plain"
    root.mkdir()
    oversized = root / "oversized.py"
    oversized.write_bytes(b"123456")
    monkeypatch.setattr(workspace_revision, "MAX_REVISION_BYTES", 5)
    real_read_bytes = Path.read_bytes

    def guarded_read_bytes(path: Path) -> bytes:
        if path == oversized:
            raise AssertionError("known oversized file must not be allocated")
        return real_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", guarded_read_bytes)

    with pytest.raises(ValueError, match="byte ceiling"):
        compute_workspace_revision(resolve_repository_scope(root))
