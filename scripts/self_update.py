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

from secret_redact import describe_error

FETCH_TIMEOUT_SECONDS = 120.0
GIT_TIMEOUT_SECONDS = 60.0
SYNC_TIMEOUT_SECONDS = 600.0
# The project's baseline sync, as `sync_memory` runs it. Without `--inexact` an
# exact sync removes every package the lock selection does not name: dry-run on
# the live environment said "Would uninstall 93 packages", torch and the models
# among them. See `docs/research/2026-09-14-an-update-that-keeps-what-is-installed.md`.
BASELINE_SYNC_COMMAND = (
    "uv", "sync", "--locked", "--inexact", "--no-default-groups", "--no-python-downloads", "--quiet",
)
FETCH_DETAIL_CHARS = 300

# What one update may cost the pass that calls it, by its own timeouts: two
# fetches (the default branch and the tracked one), the baseline sync, and the
# thirteen ordinary git calls of a full update — `rev-parse --abbrev-ref`,
# `config --get`, `symbolic-ref`, `rev-parse FETCH_HEAD` twice, `rev-parse HEAD`
# twice, `merge-base --is-ancestor` twice, three `diff`s and the `merge`. The
# nightly counts this in its own bound instead of leaving the step out of the
# sum. Research: docs/research/2026-09-18-a-pass-that-knows-how-long-it-can-be.md
GIT_CALLS_PER_UPDATE = 13
WORST_CASE_SECONDS = (
    2 * FETCH_TIMEOUT_SECONDS
    + SYNC_TIMEOUT_SECONDS
    + GIT_CALLS_PER_UPDATE * GIT_TIMEOUT_SECONDS
)


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
    completed = _run(BASELINE_SYNC_COMMAND, cwd=root, timeout=SYNC_TIMEOUT_SECONDS)
    return completed.returncode == 0


def _fetch_failure(root: Path, remote: str, branch: str) -> str | None:
    """None when the fetch worked; otherwise what git said, redacted and bounded."""
    from secret_redact import redact_secrets

    completed = _run(
        ("git", "fetch", "--quiet", remote, branch),
        cwd=root,
        timeout=FETCH_TIMEOUT_SECONDS,
    )
    if completed.returncode == 0:
        return None
    said = " ".join(redact_secrets(completed.stderr or "").split())
    return said[-FETCH_DETAIL_CHARS:] or f"exit {completed.returncode}"


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


def _outside_default_branch(root: Path, fetched: str, default_tip: str) -> dict | None:
    """Only what the default branch holds has passed its checks (a merged pull request).

    See `docs/research/2026-09-14-an-update-only-to-what-main-holds.md`.
    """
    if _is_ancestor(root, fetched, default_tip):
        return None
    return _outcome("skipped", "not_in_default_branch", commit=fetched)


def _fast_forward(root: Path, head: str, fetched: str, default_tip: str) -> dict | None:
    """The outcome that stops a fast-forward, or None when it may proceed."""
    blocked = _fast_forward_block(root, head, fetched) or _outside_default_branch(root, fetched, default_tip)
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
        return _outcome("error", describe_error(error))


def _default_branch(root: Path, remote: str) -> str:
    """The remote's default branch as `refs/remotes/<remote>/HEAD` names it, else `main`."""
    completed = _run(
        ("git", "symbolic-ref", "--short", f"refs/remotes/{remote}/HEAD"),
        cwd=root,
        timeout=GIT_TIMEOUT_SECONDS,
    )
    name = completed.stdout.strip()
    if completed.returncode != 0 or not name.startswith(f"{remote}/"):
        return "main"
    return name[len(remote) + 1 :]


def _fetched_tip(root: Path, remote: str, branch: str) -> str | dict:
    """The fetched commit of one remote branch, or the outcome that stops us."""
    failure = _fetch_failure(root, remote, branch)
    if failure is not None:
        return _outcome("skipped", "fetch_failed", detail=failure)
    return _git(root, "rev-parse", "FETCH_HEAD")


def _prepared_update(root: Path) -> tuple[str, str, str] | dict:
    """The current head, the fetched head and the default branch's tip, or what stops us."""
    target = _update_target(root)
    if isinstance(target, dict):
        return target
    return _fetched_heads(root, *target)


def _fetched_heads(root: Path, branch: str, remote: str) -> tuple[str, str, str] | dict:
    """The default branch is fetched first, so the tracked branch is what FETCH_HEAD names last."""
    default_tip = _fetched_tip(root, remote, _default_branch(root, remote))
    if isinstance(default_tip, dict):
        return default_tip
    fetched = _fetched_tip(root, remote, branch)
    if isinstance(fetched, dict):
        return fetched
    return _git(root, "rev-parse", "HEAD"), fetched, default_tip


def _dependency_state(root: Path) -> str:
    if _synced_dependencies(root):
        return "synced"
    return "stale"


def _merged_update(root: Path, head: str, fetched: str) -> dict:
    _git(root, "merge", "--ff-only", fetched)
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
    head, fetched, default_tip = prepared
    stopped = _fast_forward(root, head, fetched, default_tip)
    if stopped is not None:
        return stopped
    return _merged_update(root, head, fetched)
