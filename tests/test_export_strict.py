"""What `--strict` asks: does the archive match the working tree?"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))


def _isolated_git(repo: Path):
    environment = {
        **os.environ,
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0",
    }

    def git(*arguments: str) -> None:
        subprocess.run(
            [
                "git",
                "-c",
                "user.email=export@test",
                "-c",
                "user.name=export test",
                "-c",
                "commit.gpgSign=false",
                "-c",
                f"core.hooksPath={repo / '.nohooks'}",
                *arguments,
            ],
            cwd=repo,
            check=True,
            capture_output=True,
            timeout=30,
            env=environment,
        )

    return git


def _repository_with_one_commit(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    git = _isolated_git(repo)
    git("init", "-q")
    (repo / "tracked.md").write_text("original\n", encoding="utf-8")
    git("add", "tracked.md")
    git("commit", "-qm", "initial")
    return repo


def test_strict_names_the_archived_paths_that_differ_from_the_working_tree(
    tmp_path, monkeypatch
):
    """`git status --porcelain` answered a wider question than the flag means.

    An untracked file cannot reach the archive, so it is not a reason to refuse
    one; a modified tracked file is, and the operator needs its name.
    """
    import export_vault

    repo = _repository_with_one_commit(tmp_path)
    monkeypatch.setattr(export_vault, "ROOT", repo)

    assert export_vault._paths_differing_from_ref("HEAD") == ()

    (repo / "untracked.md").write_text("never archived\n", encoding="utf-8")
    assert export_vault._paths_differing_from_ref("HEAD") == ()

    (repo / "tracked.md").write_text("edited after the commit\n", encoding="utf-8")
    assert export_vault._paths_differing_from_ref("HEAD") == ("tracked.md",)


def test_strict_refuses_and_names_the_path(tmp_path, monkeypatch, capsys):
    """The refusal has to say which file, or it cannot be acted on."""
    import export_vault

    repo = _repository_with_one_commit(tmp_path)
    monkeypatch.setattr(export_vault, "ROOT", repo)
    (repo / "tracked.md").write_text("edited after the commit\n", encoding="utf-8")
    monkeypatch.setattr(
        sys, "argv", ["export_vault.py", "--strict", "--output", str(tmp_path / "out.zip")]
    )

    assert export_vault.main() == 2
    assert "tracked.md" in capsys.readouterr().err
