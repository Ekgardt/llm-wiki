"""Indexes that follow a repository's worktrees (#24, section D1).

Several agents open a worktree per task; the index must exist without anyone
running an indexer. A *registered* repository is one with at least one
generation; every other worktree of it that has none is indexed here, with the
code roots of its newest sibling, under the same per-repository fence a
refresh takes. The nightly `refresh-all` does it for every registered
repository, and the MCP server does it once per checkout when a structural
answer finds no generation for a worktree whose repository is registered.

A worktree or branch opts out through Git's own configuration, which this
product only reads: `llmwiki.index=false` for the repository (or for one
worktree, with `git config --worktree` under `extensions.worktreeConfig`), or
`branch.<name>.llmwikiIndex=false` for one branch. The first key set decides,
the branch key first. Nothing is written into a foreign repository. Research:
`docs/research/2026-09-11-the-graph-meets-the-agent-where-it-searches.md`.
"""

from __future__ import annotations

import subprocess
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

import repository_index as index

INDEX_KEY = "llmwiki.index"
BRANCH_KEY = "llmwikiIndex"
MAX_WORKTREES_PER_REPOSITORY = 64
# A worktree's first build is a full one (reuse is per checkout), so one pass
# starts at most this many; the rest wait for the next pass or first use.
MAX_FOLLOWED_PER_PASS = 8


@dataclass(frozen=True)
class Worktree:
    path: Path
    branch: str | None
    bare: bool
    prunable: bool


# --------------------------------------------------------------------------
# listing
# --------------------------------------------------------------------------


def _attribute(field: str) -> tuple[str, str]:
    name, _separator, value = field.partition(" ")
    return name, value


def _short_branch(reference: str | None) -> str | None:
    if not reference:
        return None
    return reference.removeprefix("refs/heads/")


def _worktree(fields: Iterable[str]) -> Worktree:
    attributes = dict(_attribute(field) for field in fields if field)
    return Worktree(
        path=Path(attributes.get("worktree", "")),
        branch=_short_branch(attributes.get("branch")),
        bare="bare" in attributes,
        prunable="prunable" in attributes,
    )


def parse_worktrees(listing: str) -> list[Worktree]:
    """`git worktree list --porcelain -z`: NUL-ended lines, an empty one between records."""
    records = [record for record in listing.split("\0\0") if record.strip("\0")]
    return [_worktree(record.split("\0")) for record in records]


def list_worktrees(checkout: Path) -> list[Worktree]:
    listing = index._git_text(checkout, "worktree", "list", "--porcelain", "-z")  # noqa: SLF001
    return parse_worktrees(listing)[:MAX_WORKTREES_PER_REPOSITORY]


def _indexable(worktree: Worktree) -> bool:
    if worktree.bare or worktree.prunable:
        return False
    return worktree.path.is_absolute() and worktree.path.is_dir()


# --------------------------------------------------------------------------
# the opt-out mark
# --------------------------------------------------------------------------


def _git(root: Path, *arguments: str) -> subprocess.CompletedProcess:
    from repository_scope import sanitized_git_environment

    return subprocess.run(  # noqa: S603 - fixed argv, no shell
        ["git", "-C", str(root), *arguments],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        shell=False,
        env=sanitized_git_environment(),
        timeout=index.GIT_TIMEOUT_SECONDS,
        check=False,
    )


def _config_bool(root: Path, key: str) -> bool | None:
    """The key's boolean value; None when it is unset or not a boolean."""
    completed = _git(root, "config", "--type=bool", "--get", key)
    if completed.returncode != 0:
        return None
    return completed.stdout.strip() == b"true"


def _current_branch(root: Path) -> str | None:
    completed = _git(root, "symbolic-ref", "--quiet", "--short", "HEAD")
    if completed.returncode != 0:
        return None
    return completed.stdout.decode("utf-8", errors="replace").strip() or None


def _mark_keys(root: Path) -> list[str]:
    branch = _current_branch(root)
    if branch is None:
        return [INDEX_KEY]
    return [f"branch.{branch}.{BRANCH_KEY}", INDEX_KEY]


def indexing_marked_off(root: Path) -> str | None:
    """The configuration key that turned indexing off here, or None."""
    for key in _mark_keys(root):
        value = _config_bool(root, key)
        if value is not None:
            return None if value else key
    return None


def require_indexing_wanted(root: Path) -> None:
    key = indexing_marked_off(root)
    if key is None:
        return
    raise index._refuse(  # noqa: SLF001
        "repository_marked_not_indexed",
        f"indexing is turned off here by git config {key}=false; "
        f"to index it, run: git -C {root} config --unset {key}",
        directory=str(root),
        config_key=key,
    )


# --------------------------------------------------------------------------
# following
# --------------------------------------------------------------------------


def _existing_root(row: Mapping) -> Path | None:
    checkout = row.get("checkout_root")
    if not checkout or not Path(str(checkout)).is_dir():
        return None
    return Path(str(checkout))


def _registered_roots(rows: Iterable[Mapping]) -> set[Path]:
    return {Path(str(row["checkout_root"])) for row in rows if row.get("checkout_root")}


def _vault_root() -> Path:
    from memory_state import ROOT

    return Path(ROOT).resolve()


def _followable(row: Mapping, vault: Path) -> bool:
    """A foreign checkout that still exists. The vault's own generation is
    active and covers memory only; its worktrees are not followed for it."""
    root = _existing_root(row)
    if root is None or row.get("active"):
        return False
    return root.resolve() != vault


def _representatives(rows: Iterable[Mapping]) -> dict[str, Mapping]:
    """One followable row per repository, newest first."""
    vault = _vault_root()
    chosen: dict[str, Mapping] = {}
    for row in rows:
        if _followable(row, vault):
            chosen.setdefault(str(row["repository_id"]), row)
    return chosen


def _candidates_of(row: Mapping, registered: set[Path]) -> list[tuple[Path, list[str] | None]]:
    worktrees = list_worktrees(_existing_root(row))
    roots = row.get("code_roots") or None
    return [
        (worktree.path, roots)
        for worktree in worktrees
        if _indexable(worktree) and worktree.path not in registered
    ]


def _safe_candidates(row: Mapping, registered: set[Path]) -> list[tuple[Path, list[str] | None]]:
    try:
        return _candidates_of(row, registered)
    except (index.RepositoryIndexRefused, OSError, subprocess.SubprocessError):
        return []


def unindexed_worktrees(rows: list[Mapping]) -> list[tuple[Path, list[str] | None]]:
    """(worktree, code roots of its newest sibling) for every worktree still without a generation."""
    registered = _registered_roots(rows)
    found: list[tuple[Path, list[str] | None]] = []
    for row in _representatives(rows).values():
        found.extend(_safe_candidates(row, registered))
    return found


def _fenced_index(admission, roots, state_root, deadline, cancelled) -> dict:
    def work(bound: float, stop: Callable[[], bool]) -> dict:
        receipt = index.index_repository(
            admission.root, roots=roots, state_root=state_root, deadline=bound, cancelled=stop
        )
        return {**receipt, "status": "followed"}

    root = index.state_root_path(state_root)
    outcome = index.run_fenced(admission.scope.repository_id, root, deadline, cancelled, work)
    return {"directory": str(admission.root), **outcome}


def follow_worktree(
    directory: Path | str,
    *,
    roots: list[str] | None = None,
    state_root: Path | None = None,
    deadline: float | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> dict[str, object]:
    """Index one worktree of a registered repository, fenced; refusals are named."""
    admission = index.admit_repository(directory, state_root=state_root, deadline=deadline)
    require_indexing_wanted(admission.root)
    sibling = _registered_sibling(admission, state_root, deadline)
    if sibling is None:
        raise index._refuse(  # noqa: SLF001
            "repository_not_registered",
            "no worktree of this repository is indexed yet; index one with `index` first",
            directory=str(admission.root),
        )
    if sibling.get("checkout_id") == admission.scope.checkout_id:
        return {"directory": str(admission.root), "status": "already_indexed"}
    return _fenced_index(admission, _roots_for(roots, sibling), state_root, deadline, cancelled)


def _roots_for(requested: list[str] | None, sibling: Mapping) -> list[str] | None:
    """What was asked for, else what the sibling covered, else discovery."""
    if requested is not None:
        return requested
    return sibling.get("code_roots") or None


def _rows_matching(rows: Iterable[Mapping], key: str, value: str) -> list[Mapping]:
    return [row for row in rows if row.get(key) == value]


def _registered_sibling(admission, state_root, deadline) -> Mapping | None:
    """This checkout's own row when it has one, else the repository's newest."""
    rows = index.list_repositories(state_root=state_root, deadline=deadline)["repositories"]
    foreign = _rows_matching(rows, "active", False)
    same = _rows_matching(foreign, "repository_id", admission.scope.repository_id)
    own = _rows_matching(same, "checkout_id", admission.scope.checkout_id)
    return next(iter(own or same), None)


def _followable_scope(directory: Path, deadline: float | None):
    from repository_scope import resolve_repository_scope

    scope = resolve_repository_scope(directory, deadline=deadline)
    if scope.git_common_dir is None:
        return None
    if indexing_marked_off(Path(scope.checkout_root)) is not None:
        return None
    return scope


def unindexed_worktree_root(
    directory: Path, *, state_root: Path | None = None, deadline: float | None = None
) -> Path | None:
    """The checkout root when it has no generation but a sibling worktree does."""
    scope = _followable_scope(Path(directory), deadline)
    if scope is None:
        return None
    rows = index.list_repositories(state_root=state_root, deadline=deadline)["repositories"]
    same = _rows_matching(_rows_matching(rows, "active", False), "repository_id", scope.repository_id)
    if not same or _rows_matching(same, "checkout_id", scope.checkout_id):
        return None
    return Path(scope.checkout_root)


def _followed(path: Path, roots, state_root, deadline) -> dict:
    try:
        return follow_worktree(path, roots=roots, state_root=state_root, deadline=deadline)
    except index.RepositoryIndexRefused as refusal:
        return {"directory": str(path), **refusal.as_dict()}


def follow_worktrees(
    rows: list[Mapping], *, state_root: Path | None = None, deadline: float
) -> list[dict]:
    """The timer's half: index up to `MAX_FOLLOWED_PER_PASS` new worktrees."""
    outcomes = []
    for path, roots in unindexed_worktrees(rows)[:MAX_FOLLOWED_PER_PASS]:
        if time.monotonic() >= deadline:
            break
        outcomes.append(_followed(path, roots, state_root, deadline))
    return outcomes
