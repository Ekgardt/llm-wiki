"""A workspace revision hashes only what it owns, reads git within fixed ceilings and deadlines, and pairs renames."""

from __future__ import annotations

import os
import signal
import subprocess
import time
import unicodedata
from pathlib import Path, PureWindowsPath

import pytest
import workspace_revision
from repository_scope import resolve_repository_scope
from workspace_revision import (
    MAX_REVISION_BYTES,
    PYTHON_CONFIG_NAMES,
    RevisionEntry,
    WorkspaceRevision,
    compute_workspace_revision,
    diff_workspace_revisions,
)

from tests.shared_fixtures import copy_python_fixture
from tests.slow_machine import SHORT_TIMEOUT
from tests.workspace_revision_helpers import (
    _isolated_global_git_configuration,  # noqa: F401
    _linux_memfd_available,
    _run_git,
    _symlink_or_skip,
)


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


def _commands_containing(calls, fragment: str) -> list:
    return [command for command, _options in calls if fragment in command]


def _status_calls(calls) -> list:
    """Every recorded Popen call whose command ran `git status`.

    Kept out of the test body because a comprehension with a filter is two
    branches to the complexity gate, and the test has no room for them.
    """
    return [call for call in calls if "status" in call[0]]


def _assert_status_call(command, options, scope) -> None:
    assert command == _expected_status_command(scope)
    _assert_bounded_pipes(options)
    _assert_own_process_group(options)
    _assert_sanitized_git_environment(options["env"])


def _expected_status_command(scope) -> list[str]:
    return [
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


def _assert_bounded_pipes(options) -> None:
    assert options["stdin"] is subprocess.DEVNULL
    assert options["stdout"] is subprocess.PIPE
    assert options["stderr"] is subprocess.DEVNULL
    assert options["shell"] is False


def _assert_own_process_group(options) -> None:
    """The child must be signalable without touching this process's group."""
    if os.name == "nt":
        assert options["creationflags"] & subprocess.CREATE_NEW_PROCESS_GROUP
        return
    assert options["start_new_session"] is True


def _assert_sanitized_git_environment(env) -> None:
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert env["GIT_OPTIONAL_LOCKS"] == "0"
    assert "GIT_DIR" not in env
    assert not any(name.startswith("GIT_CONFIG_") for name in env)


def test_verifier_reuses_computed_inventory_without_rescanning(
    repository: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scope = resolve_repository_scope(repository)
    expected = compute_workspace_revision(scope)

    def unexpected_rescan(*args, **kwargs):
        raise AssertionError("unchanged computed inventory was rescanned")
        yield  # pragma: no cover - keep this replacement a generator

    monkeypatch.setattr(workspace_revision, "relevant_files", unexpected_rescan)

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
    real_relevant_files = workspace_revision.relevant_files

    def record_scan(*args, **kwargs):
        nonlocal scans
        scans += 1
        yield from real_relevant_files(*args, **kwargs)

    monkeypatch.setattr(workspace_revision, "relevant_files", record_scan)

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
    real_relevant_files = workspace_revision.relevant_files

    def record_scan(*args, **kwargs):
        nonlocal scans
        scans += 1
        yield from real_relevant_files(*args, **kwargs)

    monkeypatch.setattr(workspace_revision, "relevant_files", record_scan)

    assert workspace_revision.verify_workspace_revision_unchanged(scope, expected) is True
    assert scans == 1


def test_inventory_hint_declines_oversized_inventory(
    repository: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(workspace_revision, "MAX_INVENTORY_HINT_ENTRIES", 0)
    scope = resolve_repository_scope(repository)
    expected = compute_workspace_revision(scope)
    scans = 0
    real_relevant_files = workspace_revision.relevant_files

    def record_scan(*args, **kwargs):
        nonlocal scans
        scans += 1
        yield from real_relevant_files(*args, **kwargs)

    monkeypatch.setattr(workspace_revision, "relevant_files", record_scan)

    assert workspace_revision.verify_workspace_revision_unchanged(scope, expected) is True
    assert scans == 1


def test_inventory_hint_does_not_swallow_one_shot_cancellation(
    repository: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scope = resolve_repository_scope(repository)
    expected = compute_workspace_revision(scope)
    real_validate = workspace_revision.validate_directory_snapshot
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
        "validate_directory_snapshot",
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

    digest, size, _snapshot = workspace_revision.hash_file(
        path,
        root=repository,
        resolved_root=repository.resolve(strict=True),
        directory_snapshots={},
        remaining_bytes=MAX_REVISION_BYTES,
        deadline=time.monotonic() + SHORT_TIMEOUT,
        cancelled=None,
    )

    assert (digest, size) == (workspace_revision.hashlib.sha256(content).hexdigest(), len(content))
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
def test_a_content_rename_of_a_configuration_file_is_paired(
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

    assert workspace_revision.status_paths(rename) == [
        ("pkg/original.py", "deleted"),
        ("pkg/renamed.py", "modified"),
    ]
    assert workspace_revision.status_paths(copy) == [("pkg/copied.py", "modified")]


def test_porcelain_type_two_marks_deleted_destination_from_xy_status() -> None:
    metadata = b".D N... 100644 100644 000000 " + b"a" * 40 + b" " + b"b" * 40
    rename = b"2 " + metadata + b" R100 pkg/renamed.py\0pkg/original.py\0"
    copy = b"2 " + metadata + b" C100 pkg/copied.py\0pkg/source.py\0"

    assert workspace_revision.status_paths(rename) == [
        ("pkg/original.py", "deleted"),
        ("pkg/renamed.py", "deleted"),
    ]
    assert workspace_revision.status_paths(copy) == [("pkg/copied.py", "deleted")]


def test_non_git_manifest_includes_only_sources_and_root_configs(tmp_path: Path) -> None:
    root = copy_python_fixture(tmp_path / "plain")
    (root / "pkg/types.pyi").write_text("value: int\n", encoding="utf-8")
    (root / "requirements-dev.txt").write_text("pytest\n", encoding="utf-8")
    (root / "notes.txt").write_text("not relevant\n", encoding="utf-8")
    (root / "pkg/pyrightconfig.json").write_text("{}", encoding="utf-8")

    revision = compute_workspace_revision(resolve_repository_scope(root))
    paths = tuple(entry.path for entry in revision.entries)

    # The superset holds the five names that must be present, the disjoint set the
    # two that must be absent; one tuple compare costs the gate one branch.
    assert (
        revision.git_head,
        paths == tuple(sorted(paths)),
        set(paths)
        >= {
            "pkg/types.pyi",
            "pyproject.toml",
            "pyrightconfig.json",
            "requirements-dev.txt",
            "uv.lock",
        },
        {"notes.txt", "pkg/pyrightconfig.json"}.isdisjoint(paths),
        all(entry.kind in {"source", "configuration"} for entry in revision.entries),
    ) == (None, True, True, True, True)


@pytest.mark.parametrize("kind", ["file", "config", "directory"])
def test_non_git_manifest_rejects_relevant_symlinks(tmp_path: Path, kind: str) -> None:
    root = tmp_path / "plain"
    root.mkdir()
    outside_file = tmp_path / "outside.py"
    outside_file.write_text("secret = 1\n", encoding="utf-8")
    outside_directory = tmp_path / "outside"
    outside_directory.mkdir()
    (outside_directory / "nested.py").write_text("secret = 2\n", encoding="utf-8")
    def link_relevant_source():
        if kind == "file":
            _symlink_or_skip(root / "linked.py", outside_file)
            return
        if kind == "config":
            _symlink_or_skip(root / "pyrightconfig.json", outside_file)
            return
        _symlink_or_skip(root / "linked-directory", outside_directory, directory=True)

    link_relevant_source()

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
    real_hash = workspace_revision.hash_file
    replaced = False

    def replace_then_hash(path: Path, **kwargs):
        nonlocal replaced
        if not replaced and path == candidate:
            candidate.unlink()
            _symlink_or_skip(candidate, outside)
            replaced = True
        return real_hash(path, **kwargs)

    monkeypatch.setattr(workspace_revision, "hash_file", replace_then_hash)

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
    real_hash = workspace_revision.hash_file
    replaced = False

    def replace_then_hash(path: Path, **kwargs):
        nonlocal replaced
        if not replaced and path == candidate:
            candidate.unlink()
            package.rmdir()
            _symlink_or_skip(package, replacement, directory=True)
            replaced = True
        return real_hash(path, **kwargs)

    monkeypatch.setattr(workspace_revision, "hash_file", replace_then_hash)

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
    real_hash = workspace_revision.hash_file

    def mutate_after_hash(path: Path, **kwargs):
        result = real_hash(path, **kwargs)
        candidate.write_text("after = True\n", encoding="utf-8")
        return result

    monkeypatch.setattr(workspace_revision, "hash_file", mutate_after_hash)

    with pytest.raises(PermissionError, match="changed|consistency|snapshot"):
        compute_workspace_revision(scope)


def test_final_fence_rejects_git_head_change(repository: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    scope = resolve_repository_scope(repository)
    heads = iter(("a" * 40, "b" * 40))
    monkeypatch.setattr(
        workspace_revision,
        "git_state",
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
    real_probe = workspace_revision.git_state
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

    monkeypatch.setattr(workspace_revision, "git_state", mutate_on_second_probe)

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
    real_state = workspace_revision.git_state
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

    monkeypatch.setattr(workspace_revision, "git_state", stage_during_second_state)

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

    assert (
        tuple(entry.path for entry in first.entries),
        all(entry.kind == "configuration" for entry in first.entries),
        first == second,
    ) == (expected, True, True)


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

    status_calls = _status_calls(calls)
    assert (len(status_calls), _commands_containing(calls, "rev-parse")) == (2, [])
    for _command, options in status_calls:
        _assert_status_call(_command, options, scope)
    shared_env = status_calls[0][1]["env"]
    assert all(options["env"] == shared_env for _command, options in status_calls)


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
        "terminate_process_tree",
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
        "terminate_process_tree",
        lambda process: terminated.append(process.pid),
    )

    with pytest.raises(ValueError, match="byte ceiling"):
        workspace_revision.git_output(
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
    monkeypatch.setattr(workspace_revision, "terminate_process_tree", lambda process: process.kill())

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
    terminate = getattr(workspace_revision, "terminate_process_tree", lambda *args, **kwargs: None)

    terminate(Process(), platform_name="nt")

    command, options = calls[0]
    assert (
        command,
        options["stdin"],
        options["stdout"],
        options["stderr"],
        options["shell"],
    ) == (
        [
            str(PureWindowsPath(r"D:\Windows") / "System32" / "taskkill.exe"),
            "/PID",
            "42",
            "/T",
            "/F",
        ],
        subprocess.DEVNULL,
        subprocess.DEVNULL,
        subprocess.DEVNULL,
        False,
    )


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
    terminate = getattr(workspace_revision, "terminate_process_tree", lambda *args, **kwargs: None)

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
        "terminate_process_tree",
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
        "terminate_process_tree",
        lambda process: process.kill(),
    )
    started = time.monotonic()

    with pytest.raises(TimeoutError, match="deadline"):
        compute_workspace_revision(
            scope, deadline=started + 0.03
        )

    # The fake pipe read blocks for a full second, so returning before it
    # could finish is the whole claim; the exact margin is scheduler-
    # dependent and measured 0.57 s on a macOS runner.
    assert time.monotonic() - started < 0.9


@pytest.mark.skipif(os.name == "nt", reason="distinct NFC-equivalent names require POSIX")
def test_manifest_rejects_unicode_normalization_collisions(tmp_path: Path) -> None:
    root = tmp_path / "plain"
    root.mkdir()
    composed = "caf\u00e9.py"
    decomposed = unicodedata.normalize("NFD", composed)
    (root / composed).write_text("one", encoding="utf-8")
    (root / decomposed).write_text("two", encoding="utf-8")
    if len(list(root.iterdir())) < 2:
        pytest.skip(
            "this filesystem is Unicode-normalization-insensitive, so the two "
            "names are one file and the manifest has no collision to reject"
        )

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


def test_a_cancel_during_the_ignored_folder_query_stops_the_revision(tmp_path: Path) -> None:
    """A stop is a TimeoutError, an OSError; it was taken for a Git failure.

    docs/research/2026-09-25-a-cancel-is-not-a-git-failure.md
    """
    import workspace_revision

    root = tmp_path / "repository"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)

    with pytest.raises(TimeoutError, match="cancel"):
        workspace_revision.ignored_directories(root, deadline=None, cancelled=lambda: True)
