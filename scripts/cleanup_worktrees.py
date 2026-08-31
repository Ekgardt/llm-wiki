"""Periodic hygiene for Git worktrees under approved agent-owned roots.

Claude Code Desktop creates a fresh worktree per session (see `.claude/worktrees/`).
Worktrees exiting cleanly are auto-removed by Claude; manually kept worktrees,
orphans from crashed sessions, and anything created via `claude --worktree NAME`
accumulate until cleaned.

Default behavior is DRY-RUN — show what would happen, change nothing. Pass
`--apply` to actually delete.

Safety rules:
- The main worktree (Git's first porcelain record) is ALWAYS kept.
- Only descendants of .claude/worktrees, .codex/worktrees, or
  .opencode/worktrees under the main worktree are candidates.
- A worktree is eligible for deletion only if ALL hold:
    * working tree is clean (no modified, staged, or untracked files)
    * branch is fully merged into main or into the integration branch
- Anything that doesn't meet those criteria is kept and flagged in the report.
- With `--interactive`, unmerged/dirty worktrees trigger a per-item prompt
  (requires --apply too). Default flow is non-interactive, safe, and boring.

Usage:
    python scripts/cleanup_worktrees.py                 # dry-run, default
    python scripts/cleanup_worktrees.py --apply         # delete merged+clean
    python scripts/cleanup_worktrees.py --apply --interactive  # ask about unmerged
    python scripts/cleanup_worktrees.py --prune-stale-metadata # report stale metadata
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

MAIN_BRANCH = os.environ.get("LLM_WIKI_MAIN_BRANCH", "main")

# Where an agent's work actually lands. Asking only about `main` kept 13
# worktrees on 2026-08-29 whose every commit was already in `work` — the owner
# merges `work` into `main` in batches, so nothing had reached `main` since
# PR 12 and the question the tool asked could not become true for weeks. A
# branch is merged when either branch already contains it; measured, that
# turned 743 MB of kept worktrees into 4 KB without losing a commit.
INTEGRATION_BRANCH = os.environ.get("LLM_WIKI_INTEGRATION_BRANCH", "work")


@dataclass
class WorktreeInfo:
    path: Path
    branch: str | None
    is_main: bool
    is_clean: bool
    is_merged: bool
    is_locked: bool = False
    is_prunable: bool = False
    reason_kept: str | None = None

    @property
    def _git_permits_removal(self) -> bool:
        return not (self.is_main or self.is_locked or self.is_prunable)

    @property
    def can_auto_delete(self) -> bool:
        return self._git_permits_removal and self.is_clean and self.is_merged


def _run(cmd: list[str], cwd: Path | None = None) -> str:
    result = subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"command failed: {' '.join(cmd)}\n"
            f"stderr: {result.stderr.strip()}"
        )
    return result.stdout.rstrip("\n")


def _run_bytes(cmd: list[str], cwd: Path | None = None) -> bytes:
    result = subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        error = result.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"command failed: {' '.join(cmd)}\nstderr: {error}")
    return result.stdout


def _try_run(cmd: list[str], cwd: Path | None = None) -> str | None:
    try:
        return _run(cmd, cwd=cwd)
    except RuntimeError:
        return None


def _porcelain_field(current: dict[str, str | bool], field: bytes) -> None:
    text = field.decode("utf-8", errors="surrogateescape")
    key, separator, value = text.partition(" ")
    current[key] = value if separator else True


def _flushed(records: list[dict[str, str | bool]], current: dict) -> dict:
    """A blank field ends one record; an empty one is not a record."""
    if current:
        records.append(current)
    return {}


def parse_worktree_porcelain(raw: bytes) -> list[dict[str, str | bool]]:
    records: list[dict[str, str | bool]] = []
    current: dict[str, str | bool] = {}
    for field in raw.split(b"\0"):
        if not field:
            current = _flushed(records, current)
            continue
        _porcelain_field(current, field)
    _flushed(records, current)
    return records


def approved_roots(primary: Path) -> tuple[Path, ...]:
    return tuple(
        (primary / name / "worktrees").resolve(strict=False)
        for name in (".claude", ".codex", ".opencode")
    )


def in_approved_root(path: Path, roots: tuple[Path, ...]) -> bool:
    resolved = path.resolve(strict=False)
    return any(resolved != root and resolved.is_relative_to(root) for root in roots)


def _record_branch(record: dict[str, str | bool]) -> str | None:
    value = record.get("branch")
    if not isinstance(value, str):
        return None
    prefix = "refs/heads/"
    return value[len(prefix):] if value.startswith(prefix) else value


def _worktree_of(record: dict[str, str | bool], index: int) -> WorktreeInfo | None:
    path_value = record.get("worktree")
    if not isinstance(path_value, str):
        return None
    return WorktreeInfo(
        path=Path(path_value),
        branch=_record_branch(record),
        is_main=index == 0,
        is_clean=True,
        is_merged=False,
        is_locked="locked" in record,
        is_prunable="prunable" in record,
    )


def list_worktrees(repo_root: Path) -> list[WorktreeInfo]:
    raw = _run_bytes(
        ["git", "worktree", "list", "--porcelain", "-z"], cwd=repo_root
    )
    found = [
        _worktree_of(record, index)
        for index, record in enumerate(parse_worktree_porcelain(raw))
    ]
    return [worktree for worktree in found if worktree is not None]


def check_clean(wt: WorktreeInfo) -> bool:
    status = _try_run(["git", "status", "--porcelain"], cwd=wt.path)
    if status is None:
        return False
    return status == ""


def _contained_by(branch: str, target: str, repo_root: Path) -> bool:
    """Whether `target` already holds every commit of `branch`."""
    return (
        _try_run(
            ["git", "merge-base", "--is-ancestor", branch, target], cwd=repo_root
        )
        is not None
    )


def check_merged(wt: WorktreeInfo, repo_root: Path) -> bool:
    """Merged into whichever branch integrates work here — `main` or `work`.

    A worktree is safe to remove once its commits live somewhere else, and on
    this machine that somewhere is usually the integration branch, not `main`.
    Both are consulted so the answer does not depend on when the owner last
    merged a batch.
    """
    if not wt.branch:
        return False
    return any(
        _contained_by(wt.branch, target, repo_root)
        for target in (MAIN_BRANCH, INTEGRATION_BRANCH)
    )


def _removal_lines(to_delete: list[WorktreeInfo]) -> list[str]:
    if not to_delete:
        return []
    rows = [f"  - {w.path}  [{w.branch}]" for w in to_delete]
    return [f"Would remove ({len(to_delete)}):", *rows, ""]


def _kept_lines(kept_other: list[WorktreeInfo]) -> list[str]:
    if not kept_other:
        return []
    rows: list[str] = []
    for worktree in kept_other:
        rows.append(f"  - {worktree.path}  [{worktree.branch}]")
        rows.append(f"    reason: {worktree.reason_kept}")
    return [f"Kept ({len(kept_other)}):", *rows, ""]


def _main_lines(kept_main: list[WorktreeInfo]) -> list[str]:
    if not kept_main:
        return []
    return [f"Main (always kept): {w.path}" for w in kept_main] + [""]


def _action_lines(actions: list[str]) -> list[str]:
    if not actions:
        return []
    return ["Actions:", *(f"  {action}" for action in actions), ""]


def _grouped(worktrees: list[WorktreeInfo]) -> dict[str, list[WorktreeInfo]]:
    groups: dict[str, list[WorktreeInfo]] = {"remove": [], "keep": [], "main": []}
    for worktree in worktrees:
        groups[_report_group(worktree)].append(worktree)
    return groups


def _report_group(worktree: WorktreeInfo) -> str:
    if worktree.is_main:
        return "main"
    return "remove" if worktree.can_auto_delete else "keep"


def format_report(worktrees: list[WorktreeInfo], actions: list[str]) -> str:
    groups = _grouped(worktrees)
    lines = [
        "=== Worktree cleanup report ===",
        "",
        *_removal_lines(groups["remove"]),
        *_kept_lines(groups["keep"]),
        *_main_lines(groups["main"]),
        *_action_lines(actions),
    ]
    return "\n".join(lines)

def remove_worktree(wt: WorktreeInfo, repo_root: Path) -> str:
    _run(["git", "worktree", "remove", "--", str(wt.path)], cwd=repo_root)
    if wt.branch:
        _run(["git", "branch", "-d", "--", wt.branch], cwd=repo_root)
        return f"removed {wt.path} and branch {wt.branch}"
    return f"removed {wt.path} (no branch)"


def prompt_yes_no(question: str) -> bool:
    while True:
        try:
            answer = input(f"{question} [y/N]: ").strip().lower()
        except EOFError:
            return False
        if answer in ("", "n", "no"):
            return False
        if answer in ("y", "yes"):
            return True
        print("please answer 'y' or 'n'")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Hygiene for git worktrees under .claude/worktrees/",
    )
    parser.add_argument(
        "--apply", action="store_true", help="actually delete (default is dry-run)"
    )
    parser.add_argument(
        "--interactive",
        action="store_true",
        help="prompt per-worktree for unmerged/dirty ones; ignored without --apply",
    )
    parser.add_argument(
        "--prune-stale-metadata",
        action="store_true",
        help="report stale Git worktree metadata; requires --apply to prune",
    )
    return parser


def _blocked_reason(worktree: WorktreeInfo) -> str | None:
    """Why Git itself forbids touching this one, before any branch question."""
    if worktree.is_locked:
        return "worktree is locked"
    if worktree.is_prunable:
        return "worktree path is missing; use --prune-stale-metadata"
    return None


def _keep_reason(worktree: WorktreeInfo) -> str:
    if not worktree.is_clean and not worktree.is_merged:
        return "dirty working tree + branch not merged"
    if not worktree.is_clean:
        return "dirty working tree (untracked or modified files)"
    return f"branch not merged into {MAIN_BRANCH} or {INTEGRATION_BRANCH}"


def _classify(worktree: WorktreeInfo, repo_root: Path) -> None:
    blocked = _blocked_reason(worktree)
    if blocked is not None:
        worktree.reason_kept = blocked
        return
    worktree.is_clean = check_clean(worktree)
    worktree.is_merged = check_merged(worktree, repo_root)
    if worktree.can_auto_delete:
        return
    worktree.reason_kept = _keep_reason(worktree)


def _force_remove(worktree: WorktreeInfo, repo_root: Path) -> str:
    _run(
        ["git", "worktree", "remove", "--force", "--", str(worktree.path)],
        cwd=repo_root,
    )
    if not worktree.branch:
        return f"force-removed {worktree.path}"
    _run(["git", "branch", "-D", "--", worktree.branch], cwd=repo_root)
    return f"force-removed {worktree.path} and branch {worktree.branch}"


def _offered(worktree: WorktreeInfo) -> bool:
    """Only a worktree the automatic rule kept is worth asking about."""
    return not (
        worktree.is_main
        or worktree.can_auto_delete
        or worktree.is_locked
        or worktree.is_prunable
    )


def _asked_removals(worktrees: list[WorktreeInfo], repo_root: Path) -> list[str]:
    actions: list[str] = []
    for worktree in filter(_offered, worktrees):
        print(f"\nWorktree: {worktree.path}")
        print(f"  branch: {worktree.branch}")
        print(f"  reason kept: {worktree.reason_kept}")
        if prompt_yes_no("  delete anyway (destroys unmerged work)?"):
            actions.append(_force_remove(worktree, repo_root))
    return actions


def _prune_metadata(repo_root: Path, apply: bool) -> str:
    command = ["git", "worktree", "prune", "--verbose"]
    action = "pruned stale worktree metadata"
    if not apply:
        command.insert(3, "--dry-run")
        action = "reported stale worktree metadata"
    output = _run(command, cwd=repo_root)
    return f"{action}: {output}" if output else action


def _applied(worktrees: list[WorktreeInfo], repo_root: Path, args) -> list[str]:
    if not args.apply:
        return []
    actions = [
        remove_worktree(worktree, repo_root)
        for worktree in worktrees
        if worktree.can_auto_delete
    ]
    if args.interactive:
        actions.extend(_asked_removals(worktrees, repo_root))
    return actions


def _discovered_worktrees(caller_root: Path) -> list[WorktreeInfo] | None:
    discovered = list_worktrees(caller_root)
    if not discovered or not discovered[0].is_main:
        return None
    return discovered


def _in_scope(discovered: list[WorktreeInfo], repo_root: Path) -> list[WorktreeInfo]:
    roots = approved_roots(repo_root)
    return [
        worktree
        for worktree in discovered
        if worktree.is_main or in_approved_root(worktree.path, roots)
    ]


def _classified(discovered: list[WorktreeInfo], repo_root: Path) -> list[WorktreeInfo]:
    worktrees = _in_scope(discovered, repo_root)
    for worktree in filter(lambda item: not item.is_main, worktrees):
        _classify(worktree, repo_root)
    return worktrees


def _collected_actions(worktrees, repo_root: Path, args) -> list[str]:
    actions = _applied(worktrees, repo_root, args)
    if args.prune_stale_metadata:
        actions.append(_prune_metadata(repo_root, args.apply))
    return actions


def _reported(worktrees: list[WorktreeInfo], actions: list[str], apply: bool) -> int:
    print(format_report(worktrees, actions))
    if not apply and any(worktree.can_auto_delete for worktree in worktrees):
        print("(dry-run — pass --apply to actually delete)")
    return 0


def _repository_root() -> Path | None:
    value = _try_run(["git", "rev-parse", "--show-toplevel"])
    return None if value is None else Path(value).resolve()


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    caller_root = _repository_root()
    if caller_root is None:
        print("error: not inside a git repository", file=sys.stderr)
        return 2
    discovered = _discovered_worktrees(caller_root)
    if discovered is None:
        print("error: Git did not report a primary worktree", file=sys.stderr)
        return 2
    repo_root = discovered[0].path.resolve(strict=False)
    worktrees = _classified(discovered, repo_root)
    actions = _collected_actions(worktrees, repo_root, args)
    return _reported(worktrees, actions, args.apply)


if __name__ == "__main__":
    raise SystemExit(main())
