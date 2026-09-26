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

import os
import re
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - 3.10 reads the same documents through tomli
    import tomli as tomllib

from secret_redact import describe_error

FETCH_TIMEOUT_SECONDS = 120.0
# One git call of the nightly update, which fetches over the network; the local-only git calls elsewhere allow 10-20 s.
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
# sixteen ordinary git calls of a full update — `rev-parse --abbrev-ref`,
# `config --get`, `symbolic-ref`, `rev-parse FETCH_HEAD` twice, `rev-parse HEAD`
# twice, `merge-base --is-ancestor` twice, four `diff`s, `hash-object`,
# `cat-file` and the `merge`. The
# nightly counts this in its own bound instead of leaving the step out of the
# sum. Research: docs/research/2026-09-18-a-pass-that-knows-how-long-it-can-be.md
GIT_CALLS_PER_UPDATE = 16
WORST_CASE_SECONDS = (
    2 * FETCH_TIMEOUT_SECONDS
    + SYNC_TIMEOUT_SECONDS
    + GIT_CALLS_PER_UPDATE * GIT_TIMEOUT_SECONDS
)


class SelfUpdateError(RuntimeError):
    """A git command failed in a way the caller must not paper over."""


def _run(
    command: Sequence[str], *, cwd: Path, timeout: float, stdin: str | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(command),
        cwd=str(cwd),
        input=stdin,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _git_detail(completed: subprocess.CompletedProcess[str]) -> str:
    """What git said on stderr, redacted and bounded; its exit code when it said nothing."""
    from secret_redact import redact_secrets

    said = " ".join(redact_secrets(completed.stderr or "").split())
    return said[-FETCH_DETAIL_CHARS:] or f"exit {completed.returncode}"


def _git(root: Path, *arguments: str, stdin: str | None = None) -> str:
    """One git call; a failure carries git's own words (audit 2026-09-26 A-9)."""
    completed = _run(("git", *arguments), cwd=root, timeout=GIT_TIMEOUT_SECONDS, stdin=stdin)
    if completed.returncode != 0:
        raise SelfUpdateError(f"git {arguments[0]} failed: {_git_detail(completed)}")
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


def _sync_command(extras: Sequence[str]) -> tuple[str, ...]:
    """The baseline sync plus every extra the operator chose, in one inexact call."""
    chosen = tuple(argument for extra in extras for argument in ("--extra", extra))
    return (*BASELINE_SYNC_COMMAND, *chosen)


def _synced_dependencies(root: Path, extras: Sequence[str]) -> bool:
    completed = _run(_sync_command(extras), cwd=root, timeout=SYNC_TIMEOUT_SECONDS)
    return completed.returncode == 0


def _fetch_failure(root: Path, remote: str, branch: str) -> str | None:
    """None when the fetch worked; otherwise what git said, redacted and bounded."""
    completed = _run(
        ("git", "fetch", "--quiet", remote, branch),
        cwd=root,
        timeout=FETCH_TIMEOUT_SECONDS,
    )
    if completed.returncode == 0:
        return None
    return _git_detail(completed)


def _update_target(root: Path) -> tuple[str, str] | dict:
    """The default branch and its remote, or the outcome that stops us.

    Only the default branch is followed: it is what a merged pull request lands
    on. A checkout on any other branch is named, never reported `current`; on
    2026-09-24 the vault sat on `work` and missed what reached `main` from other
    branches. See `docs/research/2026-09-24-the-vault-follows-its-default-branch.md`.
    """
    branch = _current_branch(root)
    if branch is None:
        return _outcome("skipped", "detached_head")
    remote = _remote_for(root, branch)
    if remote is None:
        return _outcome("skipped", "no_tracking_remote")
    return _on_default_branch(root, branch, remote)


def _on_default_branch(root: Path, branch: str, remote: str) -> tuple[str, str] | dict:
    if branch != _default_branch(root, remote):
        return _outcome("skipped", "not_on_default_branch", branch=branch)
    return branch, remote


def _fast_forward_block(root: Path, head: str, fetched: str) -> dict | None:
    if head == fetched:
        return _outcome("current", None, commit=head)
    return _ancestry_block(root, head, fetched)


def _ancestry_block(root: Path, head: str, fetched: str) -> dict | None:
    """A checkout ahead of the remote or diverged from it is not fast-forwarded."""
    if _is_ancestor(root, fetched, head):
        return _outcome("skipped", "ahead_of_remote", commit=head)
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
    """The outcome that stops a fast-forward, or None when it may proceed.

    Only the default branch is fetched (`_update_target`), so what it holds has
    passed its checks as a merged pull request; see
    `docs/research/2026-09-14-an-update-only-to-what-main-holds.md`.
    """
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


def _prepared_update(root: Path) -> tuple[str, str] | dict:
    """The current head and the fetched default-branch head, or what stops us."""
    target = _update_target(root)
    if isinstance(target, dict):
        return target
    branch, remote = target
    fetched = _fetched_tip(root, remote, branch)
    if isinstance(fetched, dict):
        return fetched
    return _git(root, "rev-parse", "HEAD"), fetched


def _dependency_state(root: Path, extras: Sequence[str]) -> str:
    if _synced_dependencies(root, extras):
        return "synced"
    return "stale"


def canonical_name(name: str) -> str:
    """A distribution name as the packaging specification compares it."""
    return re.sub(r"[-_.]+", "-", name).lower()


_REQUIREMENT_HEAD = re.compile(r"\s*([A-Za-z0-9][A-Za-z0-9._-]*)")


def _requirement_name(requirement: object) -> str:
    """The distribution a requirement names, without extras, version or marker."""
    match = _REQUIREMENT_HEAD.match(str(requirement).split(";")[0])
    if match is None:
        return ""
    return canonical_name(match.group(1))


def _installed_distributions() -> set[str]:
    from importlib.metadata import distributions

    named = (distribution.metadata["Name"] for distribution in distributions())
    return {canonical_name(name) for name in named if name}


def _project(root: Path) -> dict:
    """The `[project]` table; a checkout without one declares no extras to keep."""
    path = root / "pyproject.toml"
    if not path.is_file():
        return {}
    return tomllib.loads(path.read_text(encoding="utf-8")).get("project", {})


def _own_names(project: dict) -> dict[str, set[str]]:
    """Each extra and the distributions it names itself; a self-reference names none."""
    own = canonical_name(str(project.get("name", "")))
    extras = project.get("optional-dependencies", {})
    return {
        canonical_name(extra): {_requirement_name(value) for value in values} - {own, ""}
        for extra, values in extras.items()
    }


def _exclusive_names(project: dict) -> dict[str, set[str]]:
    """What only one extra brings: not the base, not any other extra.

    An aggregate (`hybrid`, `full`) brings nothing of its own, so it is never
    chosen by itself; its parts are.
    """
    base = {_requirement_name(value) for value in project.get("dependencies", [])}
    own = _own_names(project)
    exclusive: dict[str, set[str]] = {}
    for extra, names in own.items():
        others = set().union(*(other for name, other in own.items() if name != extra))
        exclusive[extra] = names - others - base
    return exclusive


def chosen_extras(root: Path) -> tuple[str, ...]:
    """The extras the operator chose: those with a distribution only they bring installed.

    Nothing records the choice, so the environment is the record. The update syncs
    them with the baseline, so a package added to a chosen extra arrives with the
    code that needs it. See
    `docs/research/2026-09-25-an-update-brings-the-extras-the-operator-chose.md`.
    """
    present = _installed_distributions()
    exclusive = _exclusive_names(_project(root))
    return tuple(sorted(extra for extra, names in exclusive.items() if names & present))


# What the installer renders owned resources from — units, plists, task settings,
# agent hook blocks, the OpenCode plugin. A change here reaches an installed vault
# only when the operator reruns the installer; a maintenance pass must not write
# the operator's shell profile or agent configuration by itself.
_OWNED_RESOURCE_SOURCES = (
    "scripts/install_control.py",
    "scripts/installer_config.py",
    "scripts/integration_hook_config.py",
    "scripts/install-scheduled-tasks.ps1",
    "integrations/",
)


def _resource_state(changed: set[str]) -> str:
    if any(path.startswith(_OWNED_RESOURCE_SOURCES) for path in changed):
        return "rerun_installer"
    return "current"


def _present_additions(root: Path, head: str, fetched: str) -> list[str]:
    """Paths the update adds that already exist, untracked, in the working tree."""
    added = _diff_paths(root, "--diff-filter=A", f"{head}..{fetched}")
    return sorted(path for path in added if os.path.lexists(root / path))


def _local_blobs(root: Path, paths: list[str]) -> list[str | None]:
    """Each path's blob id as `git add` would store it; a link or directory is None."""
    regular = [path for path in paths if _is_regular_file(root / path)]
    ids = _git(root, "hash-object", "--stdin-paths", stdin="\n".join(regular) + "\n").splitlines()
    known = dict(zip(regular, ids))
    return [known.get(path) for path in paths]


def _is_regular_file(path: Path) -> bool:
    return path.is_file() and not path.is_symlink()


def _fetched_blobs(root: Path, fetched: str, paths: list[str]) -> list[str]:
    """Each path's object id in the fetched commit, from one `cat-file --batch-check`."""
    requests = "".join(f"{fetched}:{path}\n" for path in paths)
    lines = _git(root, "cat-file", "--batch-check=%(objectname)", stdin=requests).splitlines()
    return [line.strip() for line in lines]


def _untracked_copies(root: Path, head: str, fetched: str) -> list[str] | dict:
    """Untracked files identical to what the update adds, or the outcome that stops it.

    Git refuses a fast-forward that would overwrite an untracked file. A research
    note written in the vault and merged through a pull request is such a file,
    byte for byte what the update brings; a file that differs is the owner's and
    stops the update. See
    `docs/research/2026-09-26-an-untracked-copy-of-the-update-does-not-stop-it.md`.
    """
    present = _present_additions(root, head, fetched)
    if not present:
        return []
    pairs = zip(present, _local_blobs(root, present), _fetched_blobs(root, fetched, present))
    differing = [path for path, local, incoming in pairs if local != incoming]
    if differing:
        return _outcome("skipped", "untracked_files_conflict", paths=differing[:20])
    return present


def _set_aside(root: Path, paths: list[str]) -> dict[str, tuple[bytes, int]]:
    """Read each identical copy and remove it, keeping what puts it back."""
    saved = {path: ((root / path).read_bytes(), (root / path).stat().st_mode) for path in paths}
    for path in paths:
        (root / path).unlink()
    return saved


def _put_back(root: Path, saved: dict[str, tuple[bytes, int]]) -> None:
    """A failed merge leaves the tree as it was: each copy set aside returns."""
    for path, (content, mode) in saved.items():
        target = root / path
        if not os.path.lexists(target):
            target.write_bytes(content)
            os.chmod(target, mode)


def _fast_forward_over(root: Path, fetched: str, copies: list[str]) -> None:
    saved = _set_aside(root, copies)
    try:
        _git(root, "merge", "--ff-only", fetched)
    except SelfUpdateError:
        _put_back(root, saved)
        raise


def _merged_update(root: Path, head: str, fetched: str, copies: list[str]) -> dict:
    changed = _changed_paths(root, head, fetched)
    _fast_forward_over(root, fetched, copies)
    extras = chosen_extras(root)
    return _outcome(
        "updated",
        None,
        commit=_git(root, "rev-parse", "HEAD"),
        previous=head,
        dependencies=_dependency_state(root, extras),
        extras=extras,
        resources=_resource_state(changed),
    )


def _attempted_update(root: Path) -> dict:
    prepared = _prepared_update(root)
    if isinstance(prepared, dict):
        return prepared
    head, fetched = prepared
    stopped = _fast_forward(root, head, fetched)
    if stopped is not None:
        return stopped
    return _update_over_copies(root, head, fetched)


def _update_over_copies(root: Path, head: str, fetched: str) -> dict:
    copies = _untracked_copies(root, head, fetched)
    if isinstance(copies, dict):
        return copies
    return _merged_update(root, head, fetched, copies)
