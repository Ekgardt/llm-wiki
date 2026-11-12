"""Fixtures and git helpers the workspace revision test files share."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
import workspace_revision
from workspace_revision import WorkspaceRevision


def _indexed_git_config_variables() -> list[str]:
    """The `GIT_CONFIG_KEY_n` / `GIT_CONFIG_VALUE_n` names this process carries."""
    prefixes = ("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_")
    return [name for name in os.environ if name.startswith(prefixes)]


def _neutralize_git_environment(monkeypatch) -> None:
    """Remove every variable that could steer git's configuration.

    `_private_git_environment_overridden` declines the private path outright
    when one of these is set, so a host that exports one moves every
    optimization assertion here onto the exact fallback. The names come from the
    product, so one added there is covered here without being copied.
    """
    selectors = workspace_revision.PRIVATE_GIT_SELECTOR_ENVIRONMENT
    for name in (*selectors, *_indexed_git_config_variables()):
        monkeypatch.delenv(name, raising=False)


def _absent_system_git_files(monkeypatch, home: Path) -> None:
    """Point the system config and attributes at files that do not exist.

    A hosted runner's `/etc/gitconfig` is not empty. Installing the `git-lfs`
    package runs `git lfs install --skip-repo --system`, which writes a
    `[filter "lfs"]` clean/smudge section there, and a clean/smudge filter is
    the most direct way a configuration can change what git reads of a working
    tree. The product is right to decline its fast path on such a machine, so
    without this these tests assert the host rather than the product - which is
    what they were doing on every Linux runner. The fence's strictness is pinned
    by `tests/test_a_system_config_that_only_says_where_git_may_work.py`
    instead of by whatever `/etc` happens to hold. Research:
    `docs/research/2026-09-18-a-test-of-an-optimisation-must-own-the-environment-it-asserts.md`.
    """
    system = home / "system-git"
    system.mkdir()
    monkeypatch.setattr(
        workspace_revision, "SYSTEM_GIT_CONFIG_PATHS", (system / "gitconfig",)
    )
    monkeypatch.setattr(
        workspace_revision, "SYSTEM_GIT_ATTRIBUTE_PATHS", (system / "gitattributes",)
    )


@pytest.fixture(autouse=True)
def _isolated_global_git_configuration(tmp_path_factory, monkeypatch) -> None:
    """Keep the machine's own Git configuration out of these contracts.

    `workspace_revision` deliberately declines its private-index fast path when
    a Git configuration could change ignore, attribute or index semantics, and
    it only tolerates sections that cannot. Any ordinary `~/.gitconfig` — a
    credential helper, an alias, `init.defaultBranch` — is therefore enough to
    move every verifier here onto the exact fallback, so the optimization
    assertions passed only on a machine whose configuration happened to be
    empty. The system configuration, the system attributes file and the `GIT_*`
    selector environment are the same class of host input, and are pinned too.
    """
    home = tmp_path_factory.mktemp("git-home")
    (home / ".config").mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    _neutralize_git_environment(monkeypatch)
    _absent_system_git_files(monkeypatch, home)


def _symlink_or_skip(link: Path, target: Path, *, directory: bool = False) -> None:
    try:
        os.symlink(target, link, target_is_directory=directory)
    except OSError:
        pytest.skip("symlink creation is unavailable")


def _linux_memfd_available() -> bool:
    return workspace_revision.private_index_platform_supported()


def _run_git(root: Path, *arguments: str, input_bytes: bytes | None = None) -> bytes:
    return subprocess.run(
        ["git", *arguments],
        cwd=root,
        input=input_bytes,
        check=True,
        capture_output=True,
        timeout=10,
    ).stdout


def _verification_git_call_counts(
    scope,
    expected: WorkspaceRevision,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[bool, int, int]:
    real_exact = workspace_revision.git_state
    real_private = workspace_revision.git_state_with_private_index
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

    monkeypatch.setattr(workspace_revision, "git_state", recording_exact)
    monkeypatch.setattr(workspace_revision, "git_state_with_private_index", recording_private)
    result = workspace_revision.verify_workspace_revision_unchanged(scope, expected)
    return result, exact_calls, private_calls


def _private_xdg_git_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str) -> Path:
    """The path of `git/<name>` under a private `XDG_CONFIG_HOME`, parents created.

    The file itself is not written, so a caller can also exercise the case where
    Git's global file does not exist.
    """
    config_home = tmp_path / "xdg"
    target = config_home / "git" / name
    target.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    return target
