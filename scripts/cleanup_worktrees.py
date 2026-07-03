"""Periodic hygiene for git worktrees under .claude/worktrees/.

Claude Code Desktop creates a fresh worktree per session (see `.claude/worktrees/`).
Worktrees exiting cleanly are auto-removed by Claude; manually kept worktrees,
orphans from crashed sessions, and anything created via `claude --worktree NAME`
accumulate until cleaned.

Default behavior is DRY-RUN — show what would happen, change nothing. Pass
`--apply` to actually delete.

Safety rules:
- The main worktree (current repo root) is ALWAYS kept.
- A worktree is eligible for deletion only if ALL hold:
    * working tree is clean (no modified, staged, or untracked files)
    * branch is fully merged into main
- Anything that doesn''t meet those criteria is kept and flagged in the report.
- With `--interactive`, unmerged/dirty worktrees trigger a per-item prompt
  (requires --apply too). Default flow is non-interactive, safe, and boring.

Usage:
    python scripts/cleanup_worktrees.py                 # dry-run, default
    python scripts/cleanup_worktrees.py --apply         # delete merged+clean
    python scripts/cleanup_worktrees.py --apply --interactive  # ask about unmerged
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

MAIN_BRANCH = "main"


@dataclass
class WorktreeInfo:
    path: Path
    branch: str | None
    is_main: bool
    is_clean: bool
    is_merged: bool
    reason_kept: str | None = None

    @property
    def can_auto_delete(self) -> bool:
        return not self.is_main and self.is_clean and self.is_merged


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


def _try_run(cmd: list[str], cwd: Path | None = None) -> str | None:
    try:
        return _run(cmd, cwd=cwd)
    except RuntimeError:
        return None


def list_worktrees(repo_root: Path) -> list[WorktreeInfo]:
    raw = _run(["git", "worktree", "list", "--porcelain"], cwd=repo_root)
    worktrees: list[WorktreeInfo] = []
    current: dict[str, str] = {}
    for line in raw.splitlines() + [""]:
        if not line:
            if current:
                path = Path(current["worktree"])
                branch = current.get("branch")
                if branch and branch.startswith("refs/heads/"):
                    branch = branch[len("refs/heads/") :]
                is_main = path.resolve() == repo_root.resolve()
                worktrees.append(
                    WorktreeInfo(
                        path=path,
                        branch=branch,
                        is_main=is_main,
                        is_clean=True,
                        is_merged=False,
                    )
                )
                current = {}
            continue
        key, _, value = line.partition(" ")
        current[key] = value
    return worktrees


def check_clean(wt: WorktreeInfo) -> bool:
    status = _try_run(["git", "status", "--porcelain"], cwd=wt.path)
    if status is None:
        return False
    return status == ""


def check_merged(wt: WorktreeInfo, repo_root: Path) -> bool:
    if not wt.branch:
        return False
    merge_base = _try_run(
        ["git", "merge-base", "--is-ancestor", wt.branch, MAIN_BRANCH],
        cwd=repo_root,
    )
    return merge_base is not None


def format_report(worktrees: list[WorktreeInfo], actions: list[str]) -> str:
    lines: list[str] = []
    lines.append("=== Worktree cleanup report ===")
    lines.append("")
    to_delete = [w for w in worktrees if w.can_auto_delete]
    kept_main = [w for w in worktrees if w.is_main]
    kept_other = [
        w for w in worktrees if not w.is_main and not w.can_auto_delete
    ]

    if to_delete:
        lines.append(f"Would remove ({len(to_delete)}):")
        for w in to_delete:
            lines.append(f"  - {w.path}  [{w.branch}]")
        lines.append("")

    if kept_other:
        lines.append(f"Kept ({len(kept_other)}):")
        for w in kept_other:
            lines.append(f"  - {w.path}  [{w.branch}]")
            lines.append(f"    reason: {w.reason_kept}")
        lines.append("")

    if kept_main:
        for w in kept_main:
            lines.append(f"Main (always kept): {w.path}")
        lines.append("")

    if actions:
        lines.append("Actions:")
        for a in actions:
            lines.append(f"  {a}")
        lines.append("")

    return "\n".join(lines)


def remove_worktree(wt: WorktreeInfo, repo_root: Path) -> str:
    _run(["git", "worktree", "remove", str(wt.path)], cwd=repo_root)
    if wt.branch:
        _run(["git", "branch", "-d", wt.branch], cwd=repo_root)
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


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Hygiene for git worktrees under .claude/worktrees/",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="actually delete (default is dry-run)",
    )
    parser.add_argument(
        "--interactive",
        action="store_true",
        help="prompt per-worktree for unmerged/dirty ones; ignored without --apply",
    )
    args = parser.parse_args()

    repo_root_str = _try_run(["git", "rev-parse", "--show-toplevel"])
    if repo_root_str is None:
        print("error: not inside a git repository", file=sys.stderr)
        return 2
    repo_root = Path(repo_root_str).resolve()

    worktrees = list_worktrees(repo_root)

    for w in worktrees:
        if w.is_main:
            continue
        w.is_clean = check_clean(w)
        w.is_merged = check_merged(w, repo_root)
        if not w.is_clean and not w.is_merged:
            w.reason_kept = "dirty working tree + branch not merged"
        elif not w.is_clean:
            w.reason_kept = "dirty working tree (untracked or modified files)"
        elif not w.is_merged:
            w.reason_kept = "branch not merged into " + MAIN_BRANCH

    actions: list[str] = []

    if args.apply:
        for w in worktrees:
            if w.can_auto_delete:
                actions.append(remove_worktree(w, repo_root))

        if args.interactive:
            for w in worktrees:
                if w.is_main or w.can_auto_delete:
                    continue
                print(f"\nWorktree: {w.path}")
                print(f"  branch: {w.branch}")
                print(f"  reason kept: {w.reason_kept}")
                if prompt_yes_no("  delete anyway (destroys unmerged work)?"):
                    _run(
                        ["git", "worktree", "remove", "--force", str(w.path)],
                        cwd=repo_root,
                    )
                    if w.branch:
                        _run(["git", "branch", "-D", w.branch], cwd=repo_root)
                        actions.append(
                            f"force-removed {w.path} and branch {w.branch}"
                        )
                    else:
                        actions.append(f"force-removed {w.path}")

        _run(["git", "worktree", "prune"], cwd=repo_root)
        actions.append("pruned stale worktree metadata")

    print(format_report(worktrees, actions))
    if not args.apply and any(w.can_auto_delete for w in worktrees):
        print("(dry-run — pass --apply to actually delete)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
