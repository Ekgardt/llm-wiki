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
    * branch is fully merged into main
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
    def can_auto_delete(self) -> bool:
        return (
            not self.is_main
            and not self.is_locked
            and not self.is_prunable
            and self.is_clean
            and self.is_merged
        )


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


def parse_worktree_porcelain(raw: bytes) -> list[dict[str, str | bool]]:
    records: list[dict[str, str | bool]] = []
    current: dict[str, str | bool] = {}
    for field in raw.split(b"\0"):
        if not field:
            if current:
                records.append(current)
                current = {}
            continue
        text = field.decode("utf-8", errors="surrogateescape")
        key, separator, value = text.partition(" ")
        current[key] = value if separator else True
    if current:
        records.append(current)
    return records


def approved_roots(primary: Path) -> tuple[Path, ...]:
    return tuple(
        (primary / name / "worktrees").resolve(strict=False)
        for name in (".claude", ".codex", ".opencode")
    )


def in_approved_root(path: Path, roots: tuple[Path, ...]) -> bool:
    resolved = path.resolve(strict=False)
    return any(resolved != root and resolved.is_relative_to(root) for root in roots)


def list_worktrees(repo_root: Path) -> list[WorktreeInfo]:
    raw = _run_bytes(
        ["git", "worktree", "list", "--porcelain", "-z"], cwd=repo_root
    )
    worktrees: list[WorktreeInfo] = []
    for index, record in enumerate(parse_worktree_porcelain(raw)):
        path_value = record.get("worktree")
        if not isinstance(path_value, str):
            continue
        branch_value = record.get("branch")
        branch = branch_value if isinstance(branch_value, str) else None
        if branch and branch.startswith("refs/heads/"):
            branch = branch[len("refs/heads/") :]
        worktrees.append(
            WorktreeInfo(
                path=Path(path_value),
                branch=branch,
                is_main=index == 0,
                is_clean=True,
                is_merged=False,
                is_locked="locked" in record,
                is_prunable="prunable" in record,
            )
        )
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


def main(argv: list[str] | None = None) -> int:
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
    parser.add_argument(
        "--prune-stale-metadata",
        action="store_true",
        help="report stale Git worktree metadata; requires --apply to prune",
    )
    args = parser.parse_args(argv)

    repo_root_str = _try_run(["git", "rev-parse", "--show-toplevel"])
    if repo_root_str is None:
        print("error: not inside a git repository", file=sys.stderr)
        return 2
    caller_root = Path(repo_root_str).resolve()
    discovered = list_worktrees(caller_root)
    if not discovered or not discovered[0].is_main:
        print("error: Git did not report a primary worktree", file=sys.stderr)
        return 2
    repo_root = discovered[0].path.resolve(strict=False)
    roots = approved_roots(repo_root)
    worktrees = [
        worktree
        for worktree in discovered
        if worktree.is_main or in_approved_root(worktree.path, roots)
    ]

    for w in worktrees:
        if w.is_main:
            continue
        if w.is_locked:
            w.reason_kept = "worktree is locked"
            continue
        if w.is_prunable:
            w.reason_kept = "worktree path is missing; use --prune-stale-metadata"
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
                if w.is_main or w.can_auto_delete or w.is_locked or w.is_prunable:
                    continue
                print(f"\nWorktree: {w.path}")
                print(f"  branch: {w.branch}")
                print(f"  reason kept: {w.reason_kept}")
                if prompt_yes_no("  delete anyway (destroys unmerged work)?"):
                    _run(
                        ["git", "worktree", "remove", "--force", "--", str(w.path)],
                        cwd=repo_root,
                    )
                    if w.branch:
                        _run(["git", "branch", "-D", "--", w.branch], cwd=repo_root)
                        actions.append(
                            f"force-removed {w.path} and branch {w.branch}"
                        )
                    else:
                        actions.append(f"force-removed {w.path}")

    if args.prune_stale_metadata:
        command = ["git", "worktree", "prune"]
        if not args.apply:
            command.extend(("--dry-run", "--verbose"))
            action = "reported stale worktree metadata"
        else:
            command.append("--verbose")
            action = "pruned stale worktree metadata"
        output = _run(command, cwd=repo_root)
        actions.append(f"{action}: {output}" if output else action)

    print(format_report(worktrees, actions))
    if not args.apply and any(w.can_auto_delete for w in worktrees):
        print("(dry-run — pass --apply to actually delete)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
