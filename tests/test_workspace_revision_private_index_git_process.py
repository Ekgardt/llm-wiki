"""The private-index fast path fences git's semantic inputs through its final proof and runs git on a sealed descriptor, bounded and leak-free."""


from __future__ import annotations

import os
import shutil
import signal
import subprocess
import time
from pathlib import Path

import pytest
import workspace_revision
from repository_scope import resolve_repository_scope
from workspace_revision import (
    compute_workspace_revision,
)

from tests.workspace_revision_helpers import (
    _isolated_global_git_configuration,  # noqa: F401
    _linux_memfd_available,
    _private_xdg_git_path,
    _run_git,
    _verification_git_call_counts,
)
from tests.workspace_revision_race_helpers import (
    _qualify_private_git_wrapper,
)


def _process_is_gone_or_zombie(pid: int) -> bool:
    try:
        state = Path(f"/proc/{pid}/stat").read_text(encoding="ascii").split()[2]
    except FileNotFoundError:
        return True
    return state == "Z"


def _await_descendant_reaped(pid: int, attempts: int = 100) -> None:
    for _attempt in range(attempts):
        if _process_is_gone_or_zombie(pid):
            return
        time.sleep(0.01)
    pytest.fail("successful private Git descendant remained alive")


def _stop_condition(stop: str, started: float):
    """Either an absolute deadline or a cancellation callback, never both."""
    if stop == "deadline":
        return started + 0.1, None
    return started + 1.0, lambda: time.monotonic() >= started + 0.05


def _await_process_gone(pid: int, attempts: int = 100) -> None:
    for _attempt in range(attempts):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.01)
    pytest.fail("timed-out Git process remained alive")


def _local_config_semantics(
    repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, str]:
    """The repository's own config, to be given a clean filter after status."""
    target = repository / ".git/config"
    return target, target.read_text(encoding="utf-8") + '[filter "late"]\nclean = cat\n'


def _global_config_semantics(
    repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, str]:
    """An existing but inert global config, to be given core.autocrlf after status."""
    target = _private_xdg_git_path(tmp_path, monkeypatch, "config")
    target.write_text("[user]\nname = Inert Identity\n", encoding="ascii")
    return target, "[core]\nautocrlf = false\n"


def _absent_global_config_semantics(
    repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, str]:
    """A global config that does not exist yet and is created only after status."""
    return _private_xdg_git_path(tmp_path, monkeypatch, "config"), "[core]\nautocrlf = false\n"


def _info_attributes_semantics(
    repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, str]:
    """The repository's own info attributes, inert until they are rewritten."""
    target = repository / ".git/info/attributes"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("# initially inert\n", encoding="ascii")
    return target, "*.py text\n"


# One setup per parametrize id, each returning the semantics file to watch and the
# content written into it once the private status has already been taken.
_FENCED_SEMANTICS_SOURCES = {
    "absent-global-config": _absent_global_config_semantics,
    "global-config": _global_config_semantics,
    "info-attributes": _info_attributes_semantics,
    "local-config": _local_config_semantics,
}


@pytest.mark.skipif(not _linux_memfd_available(), reason="Linux memfd optimization")
@pytest.mark.parametrize("source", sorted(_FENCED_SEMANTICS_SOURCES))
def test_private_index_semantics_files_are_fenced_after_status(
    repository: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source: str,
) -> None:
    target, replacement = _FENCED_SEMANTICS_SOURCES[source](repository, tmp_path, monkeypatch)
    scope = resolve_repository_scope(repository)
    expected = compute_workspace_revision(scope)
    real_private = workspace_revision.git_state_with_private_index
    mutated = False

    def mutate_after_private_status(*args, **kwargs):
        nonlocal mutated
        result = real_private(*args, **kwargs)
        target.write_text(replacement, encoding="utf-8")
        mutated = True
        return result

    monkeypatch.setattr(
        workspace_revision,
        "git_state_with_private_index",
        mutate_after_private_status,
    )
    result, exact_calls, private_calls = _verification_git_call_counts(
        scope, expected, monkeypatch
    )

    assert mutated
    assert result is True
    assert exact_calls == 1
    assert private_calls == 1


def _worktree_ignore_source(
    repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Path:
    """A committed `.gitignore` in the working tree that hides `hidden.py`."""
    target = repository / ".gitignore"
    target.write_text("hidden.py\n", encoding="ascii")
    _run_git(repository, "add", ".gitignore")
    _run_git(repository, "commit", "-m", "ignore hidden source")
    return target


def _info_ignore_source(
    repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Path:
    """The repository's own `.git/info/exclude`, hiding `hidden.py`."""
    target = repository / ".git/info/exclude"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("hidden.py\n", encoding="ascii")
    return target


def _global_ignore_source(
    repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Path:
    """A global ignore file, reached through `XDG_CONFIG_HOME`, hiding `hidden.py`."""
    target = _private_xdg_git_path(tmp_path, monkeypatch, "ignore")
    target.write_text("hidden.py\n", encoding="ascii")
    return target


# One setup per parametrize id, each returning the ignore file that is emptied out
# just before the final proof runs.
_FENCED_IGNORE_SOURCES = {
    "global": _global_ignore_source,
    "info": _info_ignore_source,
    "worktree": _worktree_ignore_source,
}


@pytest.mark.skipif(not _linux_memfd_available(), reason="Linux Git ignore semantics")
@pytest.mark.parametrize("source", sorted(_FENCED_IGNORE_SOURCES))
def test_private_index_ignore_sources_are_fenced_through_final_proof(
    repository: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source: str,
) -> None:
    target = _FENCED_IGNORE_SOURCES[source](repository, tmp_path, monkeypatch)
    (repository / "hidden.py").write_text("hidden = True\n", encoding="ascii")
    scope = resolve_repository_scope(repository)
    expected = compute_workspace_revision(scope)
    real_validate = workspace_revision.validate_private_git_proof
    mutated = False

    def mutate_before_final_proof(*args, **kwargs):
        nonlocal mutated
        target.write_text("", encoding="ascii")
        mutated = True
        return real_validate(*args, **kwargs)

    monkeypatch.setattr(
        workspace_revision,
        "validate_private_git_proof",
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
    installation = workspace_revision.private_git_installation()
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

    # The verification runs first inside the tuple, so the counts read after it are
    # the counts it produced; the tuple only spares the gate one branch per fact.
    assert (
        workspace_revision.verify_workspace_revision_unchanged(scope, expected),
        len(created),
        len(calls),
    ) == (True, 1, 1)
    descriptor, flags = created[0]
    command, options = calls[0]
    # `bool(flags & BIT)` keeps the original truthiness check on the masked bit.
    assert (bool(flags & os.MFD_CLOEXEC), bool(flags & os.MFD_ALLOW_SEALING), command) == (
        True,
        True,
        [
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
        ],
    )
    assert (
        options["pass_fds"],
        options["close_fds"],
        options["start_new_session"],
        options["shell"],
        options["env"]["GIT_INDEX_FILE"],
        options["env"]["GIT_OPTIONAL_LOCKS"],
        options["env"]["GIT_TERMINAL_PROMPT"],
        "GIT_DIR" in options["env"],
        "GIT_CONFIG_COUNT" in options["env"],
        (repository / ".git/index.lock").exists(),
    ) == (
        (descriptor,),
        True,
        True,
        False,
        f"/proc/self/fd/{descriptor}",
        "0",
        "0",
        False,
        False,
        False,
    )


@pytest.mark.skipif(not _linux_memfd_available(), reason="Linux memfd optimization")
def test_private_index_memfd_creation_failure_uses_exact_fallback(
    repository: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scope = resolve_repository_scope(repository)
    expected = compute_workspace_revision(scope)
    exact_calls = 0
    real_exact = workspace_revision.git_state

    def recording_exact(*args, **kwargs):
        nonlocal exact_calls
        exact_calls += 1
        return real_exact(*args, **kwargs)

    monkeypatch.setattr(workspace_revision, "git_state", recording_exact)
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
    deadline, cancelled = _stop_condition(stop, started)

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
    _await_process_gone(int(pid_path.read_text(encoding="ascii")))
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
        _await_descendant_reaped(descendant_pid)
    finally:
        _kill_descendant_if_recorded(descendant_pid)


def _kill_descendant_if_recorded(descendant_pid: int | None) -> None:
    if descendant_pid is None:
        return
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
    real_private = workspace_revision.git_state_with_private_index

    def inspect_private(root: Path, descriptor: int, **kwargs):
        nonlocal private_calls
        private_calls += 1
        private_content = os.pread(descriptor, len(original_index) + 1, 0)
        assert private_content != original_index
        return real_private(root, descriptor, **kwargs)

    monkeypatch.setattr(workspace_revision, "git_state_with_private_index", inspect_private)
    monkeypatch.setattr(
        workspace_revision,
        "git_state",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("real-index status refresh used")
        ),
    )

    assert (
        workspace_revision.verify_workspace_revision_unchanged(scope, expected),
        private_calls,
        index_path.read_bytes() == original_index,
        (repository / ".git/index.lock").exists(),
    ) == (True, 1, True, False)
