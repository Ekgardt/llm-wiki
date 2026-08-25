"""Advance the vault's own checkout, fast-forward only, or say why not.

The owner's requirement is that the product improves without him typing
`git pull`. The danger is that this working tree holds both the product's source
and his knowledge, and the runtime keeps it dirty by rewriting the tracked index
and log on every compile. So the rule is not "clean tree required" — that would
be an off switch — but "no file this update would change may be modified here".

Nothing destructive lives in this module: no reset, no clean, no stash, no
conflict resolution, no push. The merge is `--ff-only`, which either advances the
branch pointer or fails leaving the tree exactly as it was.

See knowledge/notes/automatic-code-update-decision.md.
"""
from __future__ import annotations

import subprocess
from collections.abc import Sequence
from pathlib import Path

FETCH_TIMEOUT_SECONDS = 120.0
GIT_TIMEOUT_SECONDS = 60.0
SYNC_TIMEOUT_SECONDS = 600.0


class SelfUpdateError(RuntimeError):
    """A git command failed in a way the caller must not paper over."""


def _run(
    command: Sequence[str], *, cwd: Path, timeout: float
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(command),
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _git(root: Path, *arguments: str, timeout: float = GIT_TIMEOUT_SECONDS) -> str:
    completed = _run(("git", *arguments), cwd=root, timeout=timeout)
    if completed.returncode != 0:
        raise SelfUpdateError(f"git {arguments[0]} failed")
    return completed.stdout.strip()


def _outcome(status: str, reason: str | None = None, **fields: object) -> dict:
    return {"status": status, "reason": reason, **fields}


def _current_branch(root: Path) -> str | None:
    """The checked-out branch, or None on a detached head."""
    branch = _git(root, "rev-parse", "--abbrev-ref", "HEAD")
    if branch == "HEAD":
        return None
    return branch


def _remote_for(root: Path, branch: str) -> str | None:
    completed = _run(
        ("git", "config", "--get", f"branch.{branch}.remote"),
        cwd=root,
        timeout=GIT_TIMEOUT_SECONDS,
    )
    if completed.returncode != 0:
        return None
    return completed.stdout.strip() or None


def _is_ancestor(root: Path, ancestor: str, descendant: str) -> bool:
    completed = _run(
        ("git", "merge-base", "--is-ancestor", ancestor, descendant),
        cwd=root,
        timeout=GIT_TIMEOUT_SECONDS,
    )
    return completed.returncode == 0


def _diff_paths(root: Path, *arguments: str) -> set[str]:
    """`-z` output: no status column to slice past and no C-quoted names."""
    output = _git(root, "diff", "--name-only", "-z", *arguments)
    return {item for item in output.split("\0") if item}


def _changed_paths(root: Path, base: str, head: str) -> set[str]:
    return _diff_paths(root, f"{base}..{head}")


def _modified_paths(root: Path) -> set[str]:
    """Tracked paths the working tree or the index has changed."""
    return _diff_paths(root) | _diff_paths(root, "--cached")


def _synced_dependencies(root: Path) -> bool:
    completed = _run(
        ("uv", "sync", "--locked", "--no-dev"), cwd=root, timeout=SYNC_TIMEOUT_SECONDS
    )
    return completed.returncode == 0


def _fetched(root: Path, remote: str, branch: str) -> bool:
    completed = _run(
        ("git", "fetch", "--quiet", remote, branch),
        cwd=root,
        timeout=FETCH_TIMEOUT_SECONDS,
    )
    return completed.returncode == 0


def _update_target(root: Path) -> tuple[str, str] | dict:
    """The branch and remote to update from, or the outcome that stops us."""
    branch = _current_branch(root)
    if branch is None:
        return _outcome("skipped", "detached_head")
    remote = _remote_for(root, branch)
    if remote is None:
        return _outcome("skipped", "no_tracking_remote")
    return branch, remote


def _fast_forward_block(root: Path, head: str, fetched: str) -> dict | None:
    if head == fetched:
        return _outcome("current", None, commit=head)
    if not _is_ancestor(root, head, fetched):
        return _outcome("skipped", "diverged_branch", commit=head)
    return None


def _conflicting_paths(root: Path, head: str, fetched: str) -> dict | None:
    """The owner and the update reaching for the same file stops the update."""
    conflicts = _changed_paths(root, head, fetched) & _modified_paths(root)
    if not conflicts:
        return None
    return _outcome("skipped", "local_changes_conflict", paths=sorted(conflicts)[:20])


def _fast_forward(root: Path, head: str, fetched: str) -> dict | None:
    """The outcome that stops a fast-forward, or None when it may proceed."""
    blocked = _fast_forward_block(root, head, fetched)
    if blocked is not None:
        return blocked
    return _conflicting_paths(root, head, fetched)


def update_checkout(root: Path | str) -> dict:
    """Advance this checkout to its remote branch when that is safe.

    Returns an outcome naming what happened and why. Never raises for an
    ordinary refusal: a diverged branch, an offline machine and a file the owner
    is editing are all normal states, not failures of the vault.
    """
    root = Path(root)
    try:
        return _attempted_update(root)
    except (OSError, subprocess.TimeoutExpired, SelfUpdateError) as error:
        return _outcome("error", type(error).__name__)


def _prepared_update(root: Path) -> tuple[str, str] | dict:
    """The current and fetched heads, or the outcome that stops us first."""
    target = _update_target(root)
    if isinstance(target, dict):
        return target
    branch, remote = target
    if not _fetched(root, remote, branch):
        return _outcome("skipped", "fetch_failed")
    return _git(root, "rev-parse", "HEAD"), _git(root, "rev-parse", "FETCH_HEAD")


def _dependency_state(root: Path) -> str:
    if _synced_dependencies(root):
        return "synced"
    return "stale"


def _merged_update(root: Path, head: str) -> dict:
    _git(root, "merge", "--ff-only", "FETCH_HEAD")
    return _outcome(
        "updated",
        None,
        commit=_git(root, "rev-parse", "HEAD"),
        previous=head,
        dependencies=_dependency_state(root),
    )


def _attempted_update(root: Path) -> dict:
    prepared = _prepared_update(root)
    if isinstance(prepared, dict):
        return prepared
    head, fetched = prepared
    stopped = _fast_forward(root, head, fetched)
    if stopped is not None:
        return stopped
    return _merged_update(root, head)
