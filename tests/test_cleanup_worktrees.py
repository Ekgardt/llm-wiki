from __future__ import annotations

import subprocess
from pathlib import Path


def _record(path: str, branch: str | None = "topic") -> bytes:
    fields = [f"worktree {path}", "HEAD " + "a" * 40]
    fields.append(f"branch refs/heads/{branch}" if branch else "detached")
    return b"\0".join(field.encode("utf-8") for field in fields) + b"\0\0"


def test_nul_porcelain_parser_preserves_unusual_paths() -> None:
    import cleanup_worktrees

    unusual = "/repo/.claude/worktrees/path with spaces\nand newline"

    records = cleanup_worktrees.parse_worktree_porcelain(
        _record("/repo", "main") + _record(unusual)
    )

    assert records[1]["worktree"] == unusual
    assert records[1]["branch"] == "refs/heads/topic"


def test_primary_worktree_is_first_git_record_not_caller_cwd(
    tmp_path: Path, monkeypatch
) -> None:
    import cleanup_worktrees

    primary = tmp_path / "primary"
    linked = tmp_path / "linked"
    raw = _record(str(primary), "main") + _record(str(linked))
    monkeypatch.setattr(cleanup_worktrees, "_run_bytes", lambda *args, **kwargs: raw)

    worktrees = cleanup_worktrees.list_worktrees(linked)

    assert [worktree.is_main for worktree in worktrees] == [True, False]
    assert worktrees[0].path == primary


def test_only_resolved_descendants_of_exact_agent_roots_are_reported(
    tmp_path: Path,
) -> None:
    import cleanup_worktrees

    primary = tmp_path / "repo"
    roots = cleanup_worktrees.approved_roots(primary)

    assert cleanup_worktrees.in_approved_root(
        primary / ".claude" / "worktrees" / "session", roots
    )
    assert not cleanup_worktrees.in_approved_root(
        primary / ".claude" / "worktrees-lookalike" / "session", roots
    )
    assert not cleanup_worktrees.in_approved_root(
        primary / "sibling-worktrees" / ".claude-session", roots
    )
    assert not cleanup_worktrees.in_approved_root(roots[0], roots)


def _worktree(cleanup_worktrees, path: Path, *, main: bool = False):
    return cleanup_worktrees.WorktreeInfo(
        path=path,
        branch="main" if main else "topic",
        is_main=main,
        is_clean=not main,
        is_merged=not main,
    )


def _main_fixture(tmp_path: Path, monkeypatch):
    import cleanup_worktrees

    primary = tmp_path / "repo"
    candidate = primary / ".claude" / "worktrees" / "candidate"
    primary.mkdir(parents=True)
    candidate.mkdir(parents=True)
    worktrees = [
        _worktree(cleanup_worktrees, primary, main=True),
        _worktree(cleanup_worktrees, candidate),
    ]
    calls: list[list[str]] = []
    monkeypatch.setattr(
        cleanup_worktrees,
        "_try_run",
        lambda command, cwd=None: str(primary)
        if command[:3] == ["git", "rev-parse", "--show-toplevel"]
        else "",
    )
    monkeypatch.setattr(cleanup_worktrees, "list_worktrees", lambda root: worktrees)
    monkeypatch.setattr(cleanup_worktrees, "check_clean", lambda worktree: worktree.is_clean)
    monkeypatch.setattr(
        cleanup_worktrees,
        "check_merged",
        lambda worktree, repo_root: worktree.is_merged,
    )

    def run(command, cwd=None):
        calls.append(command)
        return ""

    monkeypatch.setattr(cleanup_worktrees, "_run", run)
    return cleanup_worktrees, primary, candidate, calls


def test_normal_apply_never_invokes_global_prune(tmp_path: Path, monkeypatch) -> None:
    cleanup_worktrees, _primary, candidate, calls = _main_fixture(tmp_path, monkeypatch)

    assert cleanup_worktrees.main(["--apply"]) == 0

    assert ["git", "worktree", "remove", "--", str(candidate)] in calls
    assert not any(command[:3] == ["git", "worktree", "prune"] for command in calls)


def test_prune_requires_separate_named_action_and_apply(
    tmp_path: Path, monkeypatch
) -> None:
    cleanup_worktrees, _primary, _candidate, calls = _main_fixture(tmp_path, monkeypatch)

    assert cleanup_worktrees.main(["--prune-stale-metadata"]) == 0
    assert ["git", "worktree", "prune", "--dry-run", "--verbose"] in calls

    calls.clear()
    assert cleanup_worktrees.main(["--prune-stale-metadata", "--apply"]) == 0
    assert ["git", "worktree", "prune", "--verbose"] in calls


def test_interactive_force_delete_cannot_escape_approved_roots(
    tmp_path: Path, monkeypatch
) -> None:
    cleanup_worktrees, primary, candidate, calls = _main_fixture(tmp_path, monkeypatch)
    outside = tmp_path / "repo-lookalike" / ".claude" / "worktrees" / "outside"
    outside.mkdir(parents=True)
    inside = _worktree(cleanup_worktrees, candidate)
    inside.is_clean = False
    inside.is_merged = False
    out_of_scope = _worktree(cleanup_worktrees, outside)
    out_of_scope.is_clean = False
    out_of_scope.is_merged = False
    monkeypatch.setattr(
        cleanup_worktrees,
        "list_worktrees",
        lambda root: [
            _worktree(cleanup_worktrees, primary, main=True),
            inside,
            out_of_scope,
        ],
    )
    monkeypatch.setattr(cleanup_worktrees, "prompt_yes_no", lambda question: True)

    assert cleanup_worktrees.main(["--apply", "--interactive"]) == 0

    assert ["git", "worktree", "remove", "--force", "--", str(candidate)] in calls
    assert not any(str(outside) in command for command in calls)


def test_real_linked_caller_reports_only_approved_clean_merged_worktree(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    import cleanup_worktrees

    primary = tmp_path / "primary"
    primary.mkdir()

    def git(*arguments: str, cwd: Path = primary) -> None:
        subprocess.run(
            ["git", *arguments],
            cwd=cwd,
            capture_output=True,
            text=True,
            check=True,
        )

    git("init", "-b", "main")
    git("config", "user.email", "test@example.invalid")
    git("config", "user.name", "Test User")
    (primary / "tracked.txt").write_text("fixture\n", encoding="utf-8")
    git("add", "tracked.txt")
    git("commit", "-m", "fixture")
    approved = primary / ".claude" / "worktrees" / "approved with spaces"
    outside = tmp_path / "outside worktree"
    git("worktree", "add", "-b", "approved-topic", str(approved), "main")
    git("worktree", "add", "-b", "outside-topic", str(outside), "main")
    monkeypatch.chdir(outside)

    assert cleanup_worktrees.main([]) == 0

    output = capsys.readouterr()
    assert str(approved) in output.out
    assert str(primary) in output.out
    assert str(outside) not in output.out
    assert "Would remove (1)" in output.out
