"""Live Git and non-Git workspace revision contracts."""

from __future__ import annotations

import inspect
import os
import signal
import subprocess
import time
import unicodedata
from dataclasses import fields
from pathlib import Path, PureWindowsPath

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
    monkeypatch.setattr(workspace_revision, "_git_head", lambda *args, **kwargs: next(heads))

    with pytest.raises(RuntimeError, match="HEAD.*changed|changed.*HEAD"):
        compute_workspace_revision(scope)


@pytest.mark.parametrize(
    ("probe", "expected_error", "expected_match"),
    [
        ("head", RuntimeError, "Git status changed"),
        ("status", PermissionError, "file snapshot changed"),
    ],
)
def test_final_git_probe_file_mutation_is_rejected(
    repository: Path,
    monkeypatch: pytest.MonkeyPatch,
    probe: str,
    expected_error: type[Exception],
    expected_match: str,
) -> None:
    scope = resolve_repository_scope(repository)
    target = repository / "pkg" / "api.py"
    if probe == "head":
        real_probe = workspace_revision._git_head
    else:
        real_probe = workspace_revision._git_status
    calls = 0

    def mutate_on_second_probe(*args, **kwargs):
        nonlocal calls
        calls += 1
        result = real_probe(*args, **kwargs)
        if calls == 2:
            target.write_text("class ChangedDuringFinalProbe:\n    pass\n", encoding="utf-8")
        return result

    monkeypatch.setattr(workspace_revision, f"_git_{probe}", mutate_on_second_probe)

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
    real_status = workspace_revision._git_status
    calls = 0

    def stage_during_second_status(*args, **kwargs):
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
        return real_status(*args, **kwargs)

    monkeypatch.setattr(workspace_revision, "_git_status", stage_during_second_status)

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


def test_git_status_is_nul_bounded_noninteractive_and_uses_sanitized_environment(
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
    head_calls = [call for call in calls if "rev-parse" in call[0]]
    assert len(status_calls) == 2
    assert len(head_calls) == 2
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
    for head_command, head_options in head_calls:
        assert head_command == [
            "git",
            "--no-optional-locks",
            "-c",
            "core.fsmonitor=false",
            "-C",
            scope.checkout_root,
            "rev-parse",
            "--verify",
            "HEAD^{commit}",
        ]
        assert head_options["stdin"] is subprocess.DEVNULL
        assert head_options["stdout"] is subprocess.PIPE
        assert head_options["stderr"] is subprocess.DEVNULL
        assert head_options["shell"] is False
        assert head_options["env"] == options["env"]


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

    head_process = Process(41, b"a" * 40 + b"\n")
    status_process = Process(42, None)
    processes = iter((head_process, status_process))
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

    with pytest.raises(ValueError, match="Git status.*byte ceiling"):
        compute_workspace_revision(scope)

    assert reads == [66, 9]
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


def test_live_git_head_output_is_bounded(repository: Path, monkeypatch: pytest.MonkeyPatch) -> None:
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
    monkeypatch.setattr(workspace_revision, "_terminate_process_tree", lambda process: process.kill())

    with pytest.raises(ValueError, match="Git HEAD.*byte ceiling"):
        compute_workspace_revision(scope)

    assert reads == [66]


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
