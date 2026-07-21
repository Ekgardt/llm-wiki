"""Canonical repository and checkout identity contract tests."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

LOCAL_GIT_ENVIRONMENT = {
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_CEILING_DIRECTORIES",
    "GIT_COMMON_DIR",
    "GIT_CONFIG",
    "GIT_CONFIG_COUNT",
    "GIT_CONFIG_GLOBAL",
    "GIT_CONFIG_KEY_0",
    "GIT_CONFIG_NOSYSTEM",
    "GIT_CONFIG_PARAMETERS",
    "GIT_CONFIG_SYSTEM",
    "GIT_CONFIG_VALUE_0",
    "GIT_DIR",
    "GIT_DISCOVERY_ACROSS_FILESYSTEM",
    "GIT_GRAFT_FILE",
    "GIT_IMPLICIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_NAMESPACE",
    "GIT_NO_REPLACE_OBJECTS",
    "GIT_OBJECT_DIRECTORY",
    "GIT_PREFIX",
    "GIT_QUARANTINE_PATH",
    "GIT_REPLACE_REF_BASE",
    "GIT_SHALLOW_FILE",
    "GIT_WORK_TREE",
}


def _git(*args: str, cwd: Path) -> None:
    environment = os.environ.copy()
    for name in tuple(environment):
        if name in LOCAL_GIT_ENVIRONMENT or name == "GIT_TEMPLATE_DIR" or name.startswith(
            ("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_")
        ):
            environment.pop(name)
    isolation = cwd / ".git-test-isolation"
    hooks = isolation / "hooks"
    template = isolation / "template"
    hooks.mkdir(parents=True, exist_ok=True)
    template.mkdir(parents=True, exist_ok=True)
    environment.update(
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_CONFIG_NOSYSTEM="1",
        GIT_CONFIG_SYSTEM=os.devnull,
        GIT_TEMPLATE_DIR=str(template),
        GIT_TERMINAL_PROMPT="0",
    )
    command = [
        "git",
        "-c",
        f"core.hooksPath={hooks}",
        "-c",
        "commit.gpgSign=false",
        "-c",
        "tag.gpgSign=false",
        *args,
    ]
    if args and args[0] == "init":
        command.append(f"--template={template}")
    subprocess.run(
        command,
        cwd=cwd,
        check=True,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=10,
        env=environment,
    )


def _repository(path: Path) -> None:
    path.mkdir(parents=True)
    _git("init", cwd=path)
    _git("config", "user.email", "scope@example.test", cwd=path)
    _git("config", "user.name", "Repository Scope Test", cwd=path)
    (path / "tracked.txt").write_text("scope\n", encoding="utf-8")
    _git("add", "tracked.txt", cwd=path)
    _git("commit", "-m", "initial", cwd=path)


def test_equivalent_paths_resolve_to_the_same_scope(tmp_path):
    from repository_scope import resolve_repository_scope

    root = tmp_path / "repository"
    _repository(root)
    nested = root / "nested"
    nested.mkdir()

    direct = resolve_repository_scope(root)
    equivalent = resolve_repository_scope(nested / "..")
    from_nested = resolve_repository_scope(nested)

    assert direct == equivalent == from_nested
    assert Path(direct.checkout_root).is_absolute()
    assert Path(direct.git_common_dir).is_absolute()


def test_unrelated_git_toplevel_fails_closed_to_requested_directory(tmp_path, monkeypatch):
    import repository_scope

    requested = tmp_path / "requested"
    unrelated = tmp_path / "unrelated"
    requested.mkdir()
    _repository(unrelated)
    expected = repository_scope.resolve_repository_scope(requested)
    unrelated_scope = repository_scope.resolve_repository_scope(unrelated)
    calls = []

    def unrelated_git_value(_root, *arguments, **_kwargs):
        calls.append(arguments)
        if arguments[-1] == "--show-toplevel":
            return str(unrelated.resolve())
        if arguments[-1] == "--git-common-dir":
            return str((unrelated / ".git").resolve())
        return "a" * 40

    monkeypatch.setattr(repository_scope, "_git_value", unrelated_git_value)

    actual = repository_scope.resolve_repository_scope(requested)

    assert actual == expected
    assert actual.checkout_root != unrelated_scope.checkout_root
    assert actual.git_common_dir is None
    assert calls == [("--path-format=absolute", "--show-toplevel")]


def test_git_toplevel_may_contain_requested_subdirectory(tmp_path, monkeypatch):
    import repository_scope

    repository = tmp_path / "repository"
    _repository(repository)
    nested = repository / "nested" / "deeper"
    nested.mkdir(parents=True)
    expected = repository_scope.resolve_repository_scope(repository)

    def containing_git_value(_root, *arguments, **_kwargs):
        if arguments[-1] == "--show-toplevel":
            return str(repository.resolve())
        if arguments[-1] == "--git-common-dir":
            return str((repository / ".git").resolve())
        return str(expected.git_commit)

    monkeypatch.setattr(repository_scope, "_git_value", containing_git_value)

    assert repository_scope.resolve_repository_scope(nested) == expected


def test_unrelated_same_basename_roots_have_distinct_identities(tmp_path):
    from repository_scope import resolve_repository_scope

    first = tmp_path / "first" / "project"
    second = tmp_path / "second" / "project"
    _repository(first)
    _repository(second)

    first_scope = resolve_repository_scope(first)
    second_scope = resolve_repository_scope(second)

    assert first_scope.repository_id != second_scope.repository_id
    assert first_scope.checkout_id != second_scope.checkout_id


def test_worktrees_share_repository_id_but_not_checkout_id(tmp_path):
    from repository_scope import resolve_repository_scope

    root = tmp_path / "repository"
    worktree = tmp_path / "worktree"
    _repository(root)
    _git("worktree", "add", "--detach", str(worktree), cwd=root)

    main_scope = resolve_repository_scope(root)
    worktree_scope = resolve_repository_scope(worktree)

    assert main_scope.repository_id == worktree_scope.repository_id
    assert main_scope.checkout_id != worktree_scope.checkout_id
    assert main_scope.checkout_root != worktree_scope.checkout_root
    assert main_scope.git_common_dir == worktree_scope.git_common_dir
    assert main_scope.git_commit == worktree_scope.git_commit


def test_worktree_scope_ignores_hostile_ambient_git_repository(tmp_path, monkeypatch):
    from repository_scope import resolve_repository_scope

    repository = tmp_path / "repository"
    worktree = tmp_path / "worktree"
    hostile = tmp_path / "hostile"
    _repository(repository)
    _git("worktree", "add", "--detach", str(worktree), cwd=repository)
    _repository(hostile)
    expected = resolve_repository_scope(worktree)
    hostile_values = {
        "GIT_DIR": str(hostile / ".git"),
        "GIT_WORK_TREE": str(hostile),
        "GIT_COMMON_DIR": str(hostile / ".git"),
        "GIT_INDEX_FILE": str(hostile / ".git" / "index"),
        "GIT_OBJECT_DIRECTORY": str(hostile / ".git" / "objects"),
        "GIT_ALTERNATE_OBJECT_DIRECTORIES": str(repository / ".git" / "objects"),
        "GIT_CONFIG": str(hostile / ".git" / "config"),
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "core.worktree",
        "GIT_CONFIG_VALUE_0": str(hostile),
        "GIT_NAMESPACE": "hostile",
    }
    for name, value in hostile_values.items():
        monkeypatch.setenv(name, value)

    assert resolve_repository_scope(worktree) == expected


def test_non_git_directory_has_stable_local_identity(tmp_path):
    from repository_scope import resolve_repository_scope

    root = tmp_path / "plain"
    root.mkdir()

    first = resolve_repository_scope(root)
    second = resolve_repository_scope(root / ".")

    assert first == second
    assert first.repository_id.startswith("repository:")
    assert first.checkout_id.startswith("checkout:")
    assert first.git_common_dir is None
    assert first.git_commit is None


def test_scope_resolution_honors_expired_deadline_and_cancellation(tmp_path, monkeypatch):
    import repository_scope

    root = tmp_path / "plain"
    root.mkdir()
    probes = []
    monkeypatch.setattr(
        repository_scope.subprocess,
        "Popen",
        lambda *args, **kwargs: probes.append((args, kwargs)),
    )

    with pytest.raises(TimeoutError, match="deadline"):
        repository_scope.resolve_repository_scope(root, deadline=0.0)
    with pytest.raises(TimeoutError, match="cancel"):
        repository_scope.resolve_repository_scope(root, cancelled=lambda: True)

    assert probes == []


@pytest.mark.parametrize("checkout_kind", ["repository", "worktree"])
def test_git_marker_prevents_transient_probe_failure_from_becoming_non_git_scope(
    tmp_path, monkeypatch, checkout_kind
):
    import repository_scope

    repository = tmp_path / "repository"
    _repository(repository)
    checkout = repository
    if checkout_kind == "worktree":
        checkout = tmp_path / "worktree"
        _git("worktree", "add", "--detach", str(checkout), cwd=repository)
    assert (checkout / ".git").exists()

    def transient_failure(*_args, **_kwargs):
        raise subprocess.CalledProcessError(128, ["git", "rev-parse"])

    monkeypatch.setattr(repository_scope, "_git_value", transient_failure)

    with pytest.raises(repository_scope.RepositoryScopeUnavailable):
        repository_scope.resolve_repository_scope(checkout)


def test_non_git_scope_ignores_ambient_git_dir_and_work_tree(tmp_path, monkeypatch):
    from repository_scope import resolve_repository_scope

    plain = tmp_path / "plain"
    hostile = tmp_path / "hostile"
    plain.mkdir()
    _repository(hostile)
    expected = resolve_repository_scope(plain)
    monkeypatch.setenv("GIT_DIR", str(hostile / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(hostile))
    monkeypatch.setenv("GIT_COMMON_DIR", str(hostile / ".git"))

    assert resolve_repository_scope(plain) == expected


def test_unborn_git_repository_keeps_git_identity_only_after_bounded_empty_ref_probe(
    tmp_path, monkeypatch
):
    import repository_scope

    root = tmp_path / "unborn"
    root.mkdir()
    _git("init", cwd=root)
    commands = []
    real_popen = subprocess.Popen

    def recording_popen(command, **kwargs):
        commands.append(command)
        return real_popen(command, **kwargs)

    monkeypatch.setattr(repository_scope.subprocess, "Popen", recording_popen)

    scope = repository_scope.resolve_repository_scope(root)

    assert scope.git_common_dir is not None
    assert Path(scope.git_common_dir).name == ".git"
    assert scope.git_commit is None
    assert [
        command[3:]
        for command in commands
        if len(command) > 3 and command[3] == "for-each-ref"
    ] == [["for-each-ref", "--count=1", "--format=%(objectname)", "refs"]]


def test_failed_head_probe_with_existing_refs_is_uncertain_not_unborn(tmp_path):
    import repository_scope

    root = tmp_path / "repository"
    _repository(root)
    (root / ".git" / "HEAD").write_text(
        "ref: refs/heads/transiently-missing\n",
        encoding="ascii",
    )

    with pytest.raises(repository_scope.RepositoryScopeUnavailable, match="commit|ref"):
        repository_scope.resolve_repository_scope(root)


def test_scope_round_trips_as_one_closed_json_object(tmp_path):
    from repository_scope import RepositoryScope, resolve_repository_scope

    root = tmp_path / "plain"
    root.mkdir()
    scope = resolve_repository_scope(root)

    serialized = scope.as_dict()

    assert set(serialized) == {
        "schema_version",
        "repository_id",
        "checkout_id",
        "checkout_root",
        "git_common_dir",
        "git_commit",
    }
    assert RepositoryScope.from_dict(json.loads(json.dumps(serialized))) == scope


def test_from_dict_rejects_well_formed_ids_not_derived_from_paths(tmp_path):
    from repository_scope import RepositoryScope, resolve_repository_scope

    root = tmp_path / "plain"
    root.mkdir()
    value = resolve_repository_scope(root).as_dict()

    for field in ("repository_id", "checkout_id"):
        changed = dict(value)
        prefix = str(changed[field]).split(":", 1)[0]
        changed[field] = prefix + ":" + "f" * 64
        with pytest.raises(ValueError, match="derived|identity"):
            RepositoryScope.from_dict(changed)


@pytest.mark.parametrize(
    ("checkout_root", "git_common_dir"),
    [
        ("/srv/Repo/Checkout", "/srv/Repo/.git"),
        ("C:/Users/Example/Repo", "C:/Users/Example/Repo/.git"),
        ("/srv/Standalone", None),
        ("D:/Standalone", None),
    ],
)
def test_serialized_scope_paths_validate_independently_of_host(
    checkout_root, git_common_dir
):
    from repository_scope import (
        RepositoryScope,
        derive_checkout_id,
        derive_repository_id,
    )

    repository_id = derive_repository_id(
        checkout_root=checkout_root,
        git_common_dir=git_common_dir,
    )
    value = {
        "schema_version": "repository-scope/v1",
        "repository_id": repository_id,
        "checkout_id": derive_checkout_id(repository_id, checkout_root),
        "checkout_root": checkout_root,
        "git_common_dir": git_common_dir,
        "git_commit": "a" * 40 if git_common_dir else None,
    }

    assert RepositoryScope.from_dict(value).as_dict() == value


def test_windows_serialized_path_case_is_preserved_in_identity():
    from repository_scope import derive_checkout_id, derive_repository_id

    upper = "C:/Users/Example/Repo"
    lower_component = "C:/Users/example/Repo"
    repository_id = derive_repository_id(checkout_root=upper, git_common_dir=None)

    assert derive_checkout_id(repository_id, upper) != derive_checkout_id(
        repository_id, lower_component
    )


@pytest.mark.parametrize(
    "invalid",
    [
        "relative/path",
        "//server/share",
        "/srv//repo",
        "/srv/./repo",
        "/srv/../repo",
        "/srv/repo/",
        "c:/Users/Example/Repo",
        "C:\\Users\\Example\\Repo",
        "C:/Users//Repo",
        "C:/Users/./Repo",
        "C:/Users/../Repo",
        "C:/Users/Repo/",
        "/srv/Repo\x00other",
        "C:/Users/Repo\x00other",
    ],
)
def test_from_dict_rejects_noncanonical_serialized_paths(tmp_path, invalid):
    from repository_scope import RepositoryScope, resolve_repository_scope

    root = tmp_path / "plain"
    root.mkdir()
    value = resolve_repository_scope(root).as_dict()
    value["checkout_root"] = invalid

    with pytest.raises(ValueError, match="canonical|absolute|derived"):
        RepositoryScope.from_dict(value)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value.pop("repository_id"),
        lambda value: value.update(extra=True),
        lambda value: value.update(schema_version="other/v1"),
        lambda value: value.update(repository_id="repository:not-a-hash"),
        lambda value: value.update(checkout_id="checkout:" + "A" * 64),
        lambda value: value.update(checkout_root="relative/path"),
        lambda value: value.update(git_common_dir="relative/.git"),
        lambda value: value.update(checkout_root="C:/" + "x" * 4096),
        lambda value: value.update(git_commit="not-a-commit"),
        lambda value: value.update(git_common_dir="C:/repo/.git\x00other"),
        lambda value: value.update(git_commit="a" * 40),
    ],
)
def test_from_dict_rejects_open_or_malformed_values(tmp_path, mutate):
    from repository_scope import RepositoryScope, resolve_repository_scope

    root = tmp_path / "plain"
    root.mkdir()
    value = resolve_repository_scope(root).as_dict()
    mutate(value)

    with pytest.raises((TypeError, ValueError)):
        RepositoryScope.from_dict(value)


def test_git_probes_are_non_interactive_and_bounded(tmp_path, monkeypatch):
    import repository_scope

    root = tmp_path / "plain"
    root.mkdir()
    calls = []
    monkeypatch.setenv("SAFE_INHERITED_VALUE", "preserved")
    for name in LOCAL_GIT_ENVIRONMENT:
        monkeypatch.setenv(name, "hostile")

    class EmptyOutput:
        def read(self, size):
            return b""

    class UnavailableProcess:
        stdout = EmptyOutput()
        returncode = 1

        def wait(self, timeout=None):
            return self.returncode

        def poll(self):
            return self.returncode

        def kill(self):
            self.returncode = -9

    def unavailable(*args, **kwargs):
        calls.append((args, kwargs))
        return UnavailableProcess()

    monkeypatch.setattr(repository_scope.subprocess, "Popen", unavailable)

    scope = repository_scope.resolve_repository_scope(root)

    assert scope.git_common_dir is None
    assert calls
    for args, kwargs in calls:
        assert args[0][0:3] == ["git", "-C", str(root.resolve())]
        assert kwargs["stdin"] is subprocess.DEVNULL
        assert kwargs["stdout"] is subprocess.PIPE
        assert kwargs["stderr"] is subprocess.DEVNULL
        assert kwargs["shell"] is False
        assert kwargs["env"]["GIT_TERMINAL_PROMPT"] == "0"
        assert kwargs["env"]["SAFE_INHERITED_VALUE"] == "preserved"
        assert LOCAL_GIT_ENVIRONMENT.isdisjoint(kwargs["env"])


def test_git_probe_kills_process_when_stdout_exceeds_bound(tmp_path, monkeypatch):
    import repository_scope

    root = tmp_path / "plain"
    root.mkdir()
    reads = []

    class OverflowOutput:
        def read(self, size):
            reads.append(size)
            return b"x" * size

    class OverflowProcess:
        stdout = OverflowOutput()
        returncode = None
        killed = False

        def wait(self, timeout=None):
            if self.returncode is None:
                self.returncode = 0
            return self.returncode

        def poll(self):
            return self.returncode

        def kill(self):
            self.killed = True
            self.returncode = -9

    process = OverflowProcess()
    monkeypatch.setattr(repository_scope, "MAX_GIT_OUTPUT_BYTES", 8, raising=False)
    monkeypatch.setattr(repository_scope.subprocess, "Popen", lambda *args, **kwargs: process)

    with pytest.raises(ValueError, match="output|ceiling|bound"):
        repository_scope._git_value(root, "--show-toplevel")

    assert reads == [9]
    assert process.killed


def test_git_probe_kills_process_on_timeout(tmp_path, monkeypatch):
    import repository_scope

    root = tmp_path / "plain"
    root.mkdir()
    killed = threading.Event()

    class BlockingOutput:
        def read(self, size):
            killed.wait(1)
            return b""

    class BlockingProcess:
        stdout = BlockingOutput()
        returncode = None

        def wait(self, timeout=None):
            return self.returncode

        def poll(self):
            return self.returncode

        def kill(self):
            self.returncode = -9
            killed.set()

    process = BlockingProcess()
    monkeypatch.setattr(repository_scope, "GIT_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(repository_scope.subprocess, "Popen", lambda *args, **kwargs: process)

    with pytest.raises(TimeoutError):
        repository_scope._git_value(root, "--show-toplevel")

    assert killed.is_set()


def test_git_probe_uses_only_remaining_caller_budget(tmp_path, monkeypatch):
    import repository_scope

    root = tmp_path / "plain"
    root.mkdir()
    killed = threading.Event()

    class BlockingOutput:
        def read(self, size):
            killed.wait(1)
            return b""

    class BlockingProcess:
        stdout = BlockingOutput()
        returncode = None

        def wait(self, timeout=None):
            return self.returncode

        def poll(self):
            return self.returncode

        def kill(self):
            self.returncode = -9
            killed.set()

    process = BlockingProcess()
    monkeypatch.setattr(repository_scope.subprocess, "Popen", lambda *args, **kwargs: process)
    started = time.monotonic()

    with pytest.raises(TimeoutError):
        repository_scope._git_value(
            root,
            "--show-toplevel",
            deadline=started + 0.03,
        )

    assert time.monotonic() - started < 0.5
    assert killed.is_set()


def test_git_fixture_is_hermetic_and_bounded(tmp_path, monkeypatch):
    hostile_template = tmp_path / "hostile-template"
    hostile_hooks = hostile_template / "hooks"
    hostile_hooks.mkdir(parents=True)
    hook = hostile_hooks / "pre-commit"
    hook.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    hook.chmod(0o755)
    hostile_config = tmp_path / "hostile.gitconfig"
    hostile_config.write_text(
        "[commit]\n\tgpgSign = true\n[gpg]\n\tprogram = definitely-missing-gpg\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("GIT_TEMPLATE_DIR", str(hostile_template))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(hostile_config))
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", str(hostile_config))
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "commit.gpgSign")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "true")

    repository = tmp_path / "repository"
    _repository(repository)

    assert (repository / ".git").is_dir()
    assert not (repository / ".git" / "hooks" / "pre-commit").exists()


def test_git_fixture_invocation_sets_hermetic_options_and_timeout(tmp_path, monkeypatch):
    calls = []

    def record(*args, **kwargs):
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args[0], 0, b"", b"")

    monkeypatch.setattr(subprocess, "run", record)

    _git("status", cwd=tmp_path)

    command = calls[0][0][0]
    options = calls[0][1]
    assert "commit.gpgSign=false" in command
    assert any(item.startswith("core.hooksPath=") for item in command)
    assert options["timeout"] > 0
    assert options["env"]["GIT_CONFIG_NOSYSTEM"] == "1"
    assert options["env"]["GIT_CONFIG_GLOBAL"] == os.devnull
    assert options["env"]["GIT_CONFIG_SYSTEM"] == os.devnull
    assert not any(name.startswith("GIT_CONFIG_KEY_") for name in options["env"])
