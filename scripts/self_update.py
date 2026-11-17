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

import argparse
import json
import os
import re
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

from llm_wiki.step_budgets import (
    FETCH_TIMEOUT_SECONDS,
    GIT_TIMEOUT_SECONDS,
    PYTHON_PROBE_TIMEOUT_SECONDS,
    SYNC_TIMEOUT_SECONDS,
)
from secret_redact import describe_error

# The update's sync, before the selection it names. It is exact: every extra and
# group the vault has is named (`sync_selection`), so what the lock no longer names
# is removed — 2.7 GB of CUDA wheels stayed behind on the owner's vault after torch
# moved to its CPU build. Inexact was right while the update named no extra
# (docs/research/2026-09-14-an-update-that-keeps-what-is-installed.md); see
# docs/research/2026-09-29-a-sync-removes-what-the-lock-no-longer-names.md.
BASELINE_SYNC_COMMAND = (
    "uv", "sync", "--locked", "--no-default-groups", "--no-python-downloads", "--quiet",
)
FETCH_DETAIL_CHARS = 300

# The extras every install brings: all of them (`full` names semantic, hybrid,
# code-graph, mcp-server and reranker), by the owner's requirement of 2026-09-29.
# The models step fetches every pinned model whose runtime is present, so both the
# e5 encoder and the reranker arrive; Linux takes torch's CPU build (pyproject.toml).
# Language servers other than Pyright stay one explicit command per language.
# `tests/test_the_installer_brings_the_runtime_of_every_model_it_fetches.py`
# holds this to `install_models.pinned_models`; the nightly update keeps it
# through `self_update.chosen_extras`.
# See docs/research/2026-09-29-every-install-brings-every-component.md.
DEFAULT_EXTRAS = ("full",)


class SelfUpdateError(RuntimeError):
    """A git command failed in a way the caller must not paper over."""


def run(
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


def git(root: Path, *arguments: str, stdin: str | None = None) -> str:
    """One git call; a failure carries git's own words (audit 2026-09-26 A-9)."""
    completed = run(("git", *arguments), cwd=root, timeout=GIT_TIMEOUT_SECONDS, stdin=stdin)
    if completed.returncode != 0:
        raise SelfUpdateError(f"git {arguments[0]} failed: {_git_detail(completed)}")
    return completed.stdout.strip()


def _outcome(status: str, reason: str | None = None, **fields: object) -> dict:
    return {"status": status, "reason": reason, **fields}


def _current_branch(root: Path) -> str | None:
    """The checked-out branch, or None on a detached head."""
    branch = git(root, "rev-parse", "--abbrev-ref", "HEAD")
    if branch == "HEAD":
        return None
    return branch


def _remote_for(root: Path, branch: str) -> str | None:
    completed = run(
        ("git", "config", "--get", f"branch.{branch}.remote"),
        cwd=root,
        timeout=GIT_TIMEOUT_SECONDS,
    )
    if completed.returncode != 0:
        return None
    return completed.stdout.strip() or None


def _is_ancestor(root: Path, ancestor: str, descendant: str) -> bool:
    completed = run(
        ("git", "merge-base", "--is-ancestor", ancestor, descendant),
        cwd=root,
        timeout=GIT_TIMEOUT_SECONDS,
    )
    return completed.returncode == 0


def _diff_paths(root: Path, *arguments: str) -> set[str]:
    """`-z` output: no status column to slice past and no C-quoted names."""
    output = git(root, "diff", "--name-only", "-z", *arguments)
    return {item for item in output.split("\0") if item}


def _changed_paths(root: Path, base: str, head: str) -> set[str]:
    return _diff_paths(root, f"{base}..{head}")


def _modified_paths(root: Path) -> set[str]:
    """Tracked paths the working tree or the index has changed."""
    return _diff_paths(root) | _diff_paths(root, "--cached")


def selection_arguments(extras: Sequence[str], groups: Sequence[str] = ()) -> tuple[str, ...]:
    """`--extra` for each extra and `--group` for each group, in that order."""
    named = [("--extra", extra) for extra in extras] + [("--group", group) for group in groups]
    return tuple(argument for pair in named for argument in pair)


def sync_command(extras: Sequence[str], groups: Sequence[str] = ()) -> tuple[str, ...]:
    """The exact sync of the baseline and every extra and group the vault has."""
    return (*BASELINE_SYNC_COMMAND, *selection_arguments(extras, groups))


def synced_dependencies(root: Path, extras: Sequence[str]) -> bool:
    command = sync_command(extras, chosen_groups(root))
    completed = run(command, cwd=root, timeout=SYNC_TIMEOUT_SECONDS)
    return completed.returncode == 0


def fetch_failure(root: Path, remote: str, branch: str) -> str | None:
    """None when the fetch worked; otherwise what git said, redacted and bounded."""
    completed = run(
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
    blocked = _fast_forward_block(root, head, fetched) or _python_requirement_block(root, fetched)
    if blocked is not None:
        return blocked
    return _conflicting_paths(root, head, fetched)


def _required_python(root: Path, fetched: str) -> str | None:
    """The next commit's `requires-python`; a commit without one asks for nothing."""
    command = ("git", "show", f"{fetched}:pyproject.toml")
    shown = run(command, cwd=root, timeout=GIT_TIMEOUT_SECONDS)
    if shown.returncode != 0:
        return None
    import tomllib

    try:
        return tomllib.loads(shown.stdout).get("project", {}).get("requires-python")
    except ValueError as error:
        raise SelfUpdateError(f"the fetched pyproject.toml is unreadable: {error}") from error


def _python_requirement_block(root: Path, fetched: str) -> dict | None:
    """Stay on working code when no interpreter here can run the next commit.

    The sync comes after the merge and may not download an interpreter, so a commit
    that moved past this machine's Python would land and could not start. See
    `docs/research/2026-11-09-an-update-names-the-python-it-needs.md`.
    """
    required = _required_python(root, fetched)
    if required is None:
        return None
    found = run(("uv", "python", "find", required), cwd=root, timeout=PYTHON_PROBE_TIMEOUT_SECONDS)
    if found.returncode == 0:
        return None
    return _outcome("skipped", "python_requirement_unmet", required=required)


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
    completed = run(
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
    failure = fetch_failure(root, remote, branch)
    if failure is not None:
        return _outcome("skipped", "fetch_failed", detail=failure)
    return git(root, "rev-parse", "FETCH_HEAD")


def _prepared_update(root: Path) -> tuple[str, str] | dict:
    """The current head and the fetched default-branch head, or what stops us."""
    target = _update_target(root)
    if isinstance(target, dict):
        return target
    branch, remote = target
    fetched = _fetched_tip(root, remote, branch)
    if isinstance(fetched, dict):
        return fetched
    return git(root, "rev-parse", "HEAD"), fetched


def _dependency_state(root: Path, extras: Sequence[str]) -> str:
    if synced_dependencies(root, extras):
        return "synced"
    return "stale"


def sync_dependencies(root: Path | str) -> str:
    """Sync the baseline and the chosen extras again: `synced` or `stale`.

    The nightly calls it when an earlier update left the dependencies stale; the
    next night's update found nothing to fetch and the warning simply vanished
    (audit 2026-09-26 B-24).
    """
    root = Path(root)
    return _dependency_state(root, update_extras(root))


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


def _unconditional(requirement: str) -> bool:
    """A requirement the distribution always brings, not one of its own extras'."""
    return "extra" not in requirement.partition(";")[2]


def _distributions_under(site_paths: Sequence[str] | None):
    from importlib.metadata import distributions

    if site_paths is None:
        return distributions()
    return distributions(path=list(site_paths))


def installed_distributions(site_paths: Sequence[str] | None = None) -> dict[str, set[str]]:
    """Each distribution installed here, or under `site_paths`, and what it always requires."""
    installed: dict[str, set[str]] = {}
    for distribution in _distributions_under(site_paths):
        name = distribution.metadata["Name"]
        if name:
            requires = filter(_unconditional, distribution.requires or ())
            installed[canonical_name(name)] = {_requirement_name(value) for value in requires}
    return installed


def _chosen_by_itself(
    exclusive: set[str], own: set[str], installed: dict[str, set[str]]
) -> bool:
    """An exclusive distribution is installed and nothing outside the extra pulled it in.

    `transformers` (reranker) requires `huggingface-hub` (semantic's own), so the
    reranker alone read as a semantic choice (audit 2026-09-26, regress 9,
    docs/research/2026-09-26-an-extra-is-chosen-by-what-nothing-else-pulled-in.md).
    """
    pulled_in = set().union(*(needs for name, needs in installed.items() if name not in own))
    return bool((exclusive & installed.keys()) - pulled_in)


def _pyproject(root: Path) -> dict:
    """The whole `pyproject.toml`; a checkout without one declares nothing to keep."""
    path = root / "pyproject.toml"
    if not path.is_file():
        return {}
    import tomllib
    return tomllib.loads(path.read_text(encoding="utf-8"))


def _project(root: Path) -> dict:
    """The `[project]` table."""
    return _pyproject(root).get("project", {})


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
    return _exclusive(_own_names(project), base)


def _exclusive(owned: dict[str, set[str]], shared: set[str]) -> dict[str, set[str]]:
    """What each set names that no other set and nothing in `shared` names."""
    exclusive: dict[str, set[str]] = {}
    for key, names in owned.items():
        others = set().union(*(other for name, other in owned.items() if name != key))
        exclusive[key] = names - others - shared
    return exclusive


def _group_names(document: dict) -> dict[str, set[str]]:
    """Each dependency group and the distributions it names (an include names none)."""
    groups = document.get("dependency-groups", {})
    return {
        canonical_name(group): {_requirement_name(value) for value in values if isinstance(value, str)} - {""}
        for group, values in groups.items()
    }


def chosen_groups(root: Path, installed: dict[str, set[str]] | None = None) -> tuple[str, ...]:
    """The dependency groups the operator installed, found as chosen extras are.

    A group is chosen when a distribution only it brings is installed and nothing
    else pulled it in: `pytest` means `dev`. An exact sync that did not name it
    would uninstall the operator's test tools.
    """
    document = _pyproject(root)
    project = document.get("project", {})
    groups = _group_names(document)
    shared = {_requirement_name(value) for value in project.get("dependencies", [])}
    shared |= set().union(*_own_names(project).values())
    present = installed_distributions() if installed is None else installed
    exclusive = _exclusive(groups, shared)
    return tuple(sorted(group for group, names in exclusive.items() if _chosen_by_itself(names, groups[group], present)))


def chosen_extras(root: Path, installed: dict[str, set[str]] | None = None) -> tuple[str, ...]:
    """The extras the operator chose: those with a distribution only they bring installed.

    Nothing records the choice, so the environment is the record. The update syncs
    them with the baseline, so a package added to a chosen extra arrives with the
    code that needs it. See
    `docs/research/2026-09-25-an-update-brings-the-extras-the-operator-chose.md`.
    """
    installed = installed_distributions() if installed is None else installed
    project = _project(root)
    own = _own_names(project)
    exclusive = _exclusive_names(project)
    return tuple(
        sorted(extra for extra, names in exclusive.items() if _chosen_by_itself(names, own[extra], installed))
    )


def update_extras(root: Path, installed: dict[str, set[str]] | None = None) -> tuple[str, ...]:
    """What an update syncs: every extra an install brings, and any the operator added.

    Syncing only what was installed kept a vault updated by the nightly alone on the
    components of the release it was first installed from; the default set grows
    with the product (docs/research/2026-09-29-every-install-brings-every-component.md).
    """
    return tuple(sorted(set(DEFAULT_EXTRAS) | set(chosen_extras(root, installed))))


def sync_selection(root: Path, installed: dict[str, set[str]] | None = None) -> dict[str, list[str]]:
    """What an exact sync of this vault must name: the extras and the groups.

    One rule for the nightly update and the installer; the installer asks the
    environment's own interpreter for its distribution inventory, because only
    that one sees what is installed there.
    """
    extras, groups = update_extras(root, installed), chosen_groups(root, installed)
    return {"extras": list(extras), "groups": list(groups), "arguments": list(selection_arguments(extras, groups))}


def repair_environment(root: Path, site_paths: Sequence[str]) -> str | None:
    """Move the environment onto the checkout's Python: None, or why it could not move.

    Runs on a supported interpreter outside the environment, on behalf of the
    `python_series` gate. `site_paths` is the old environment, read first, because
    it is the only record of what the operator chose and the sync replaces it.
    """
    selection = sync_selection(root, installed_distributions(site_paths))
    completed = run((*BASELINE_SYNC_COMMAND, *selection["arguments"]), cwd=root, timeout=SYNC_TIMEOUT_SECONDS)
    if completed.returncode == 0:
        return None
    return _git_detail(completed)


# What the installer renders owned resources from — units, plists, task settings,
# agent hook blocks, the OpenCode plugin. A change here reaches an installed vault
# only when the operator reruns the installer; a maintenance pass must not write
# the operator's shell profile or agent configuration by itself.
_OWNED_RESOURCE_SOURCES = (
    "scripts/install_control.py",
    "scripts/llm_wiki/install_",
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
    ids = git(root, "hash-object", "--stdin-paths", stdin="\n".join(regular) + "\n").splitlines()
    known = dict(zip(regular, ids))
    return [known.get(path) for path in paths]


def _is_regular_file(path: Path) -> bool:
    return path.is_file() and not path.is_symlink()


def _fetched_blobs(root: Path, fetched: str, paths: list[str]) -> list[str]:
    """Each path's object id in the fetched commit, from one `cat-file --batch-check`."""
    requests = "".join(f"{fetched}:{path}\n" for path in paths)
    lines = git(root, "cat-file", "--batch-check=%(objectname)", stdin=requests).splitlines()
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
    """A failed or interrupted merge leaves the tree as it was: each copy set aside returns."""
    for path, (content, mode) in saved.items():
        target = root / path
        if not os.path.lexists(target):
            target.write_bytes(content)
            os.chmod(target, mode)


def _fast_forward_over(root: Path, fetched: str, copies: list[str]) -> None:
    saved = _set_aside(root, copies)
    try:
        git(root, "merge", "--ff-only", fetched)
    except BaseException:
        # A timeout or an OSError skipped the put-back too (audit 2026-09-27 C-13);
        # only a missing path is written, so a merge that got as far as the file
        # is left alone.
        _put_back(root, saved)
        raise


def _merged_update(root: Path, head: str, fetched: str, copies: list[str]) -> dict:
    changed = _changed_paths(root, head, fetched)
    _fast_forward_over(root, fetched, copies)
    extras = update_extras(root)
    return _outcome(
        "updated",
        None,
        commit=git(root, "rev-parse", "HEAD"),
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


# What the installers print for the checkout they leave, by the reason
# `_update_target` stops with: they used to promise a fast-forward of whatever
# branch was checked out (audit 2026-09-27 C-12,
# docs/research/2026-09-27-the-installers-say-which-branch-and-which-warning.md).
_TARGET_NOTES = {
    "detached_head": (
        "none - this checkout is pinned to one commit, which the nightly update skips; "
        "update it by hand with git"
    ),
    "no_tracking_remote": "none - the checked-out branch tracks no remote, so the nightly update skips it",
    "not_on_default_branch": (
        "none while {branch} is checked out - the nightly update follows only the default "
        "branch; switch to it with git"
    ),
}


def update_target_note(root: Path | str) -> str:
    """What the nightly update will do with this checkout, in the installers' words."""
    try:
        target = _update_target(Path(root))
    except (OSError, subprocess.TimeoutExpired, SelfUpdateError) as error:
        return f"unknown - {describe_error(error)}"
    if isinstance(target, dict):
        return _TARGET_NOTES[target["reason"]].format(**target)
    branch, remote = target
    return f"nightly fast-forward of {branch}, the default branch of {remote}"


def _repair_exit_code(root: Path, site_paths: Sequence[str]) -> int:
    """0 when the environment moved; otherwise why not, on stderr, redacted."""
    try:
        failure = repair_environment(root, site_paths)
    except (OSError, ValueError, subprocess.TimeoutExpired) as error:
        failure = describe_error(error)
    if failure is None:
        return 0
    print(failure, file=sys.stderr)
    return 1


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Say what the nightly code update does with a checkout.")
    chosen = parser.add_mutually_exclusive_group(required=True)
    chosen.add_argument("--note", type=Path, metavar="ROOT", help="the checkout to describe")
    chosen.add_argument(
        "--selection", type=Path, metavar="ROOT", help="print the extras and groups an exact sync names, as JSON"
    )
    chosen.add_argument(
        "--repair-environment", type=Path, metavar="ROOT", help="move the environment onto the checkout's Python"
    )
    parser.add_argument("--site", action="append", default=[], help="a site-packages directory of the old environment")
    args = parser.parse_args(argv)
    if args.repair_environment is not None:
        return _repair_exit_code(args.repair_environment, args.site)
    if args.selection is not None:
        print(json.dumps(sync_selection(args.selection), sort_keys=True))
        return 0
    print(update_target_note(args.note))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
