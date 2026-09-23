"""The memory removes the transcripts its own provider calls left; a held session stays.

See `docs/research/2026-09-23-the-memory-retires-its-own-residue.md`.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import retire_own_call_transcripts as retirer  # noqa: E402
import scheduled_nightly  # noqa: E402


def _transcript(path: Path, entrypoint: str, cwd: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    first = {"type": "attachment", "cwd": cwd, "entrypoint": entrypoint, "sessionId": "s"}
    second = {"type": "user", "message": {"role": "user", "content": "hello"}}
    path.write_text(json.dumps(first) + "\n" + json.dumps(second) + "\n", encoding="utf-8")
    return path


def test_only_the_memory_s_own_calls_are_removed(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    projects = tmp_path / "projects"
    own_vault = _transcript(projects / "-vault" / "a.jsonl", "sdk-cli", str(vault))
    own_temp = _transcript(projects / "-tmp-x" / "b.jsonl", "sdk-cli", tempfile.gettempdir() + "/llm-wiki-provider-x")
    foreign = _transcript(projects / "-elsewhere" / "c.jsonl", "sdk-cli", "/srv/elsewhere")
    held = _transcript(projects / "-vault" / "d.jsonl", "cli", str(vault))
    unreadable = projects / "-vault" / "e.jsonl"
    unreadable.write_text("not json\n", encoding="utf-8")

    removed, emptied = retirer.retire(projects, vault)

    # the temporary directory's project folder is emptied and goes; the vault's keeps its held session
    assert (removed, emptied) == (2, 1)
    assert [p.exists() for p in (own_vault, own_temp, foreign, held, unreadable)] == [False, False, True, True, True]
    assert not own_temp.parent.exists() and held.parent.exists()


def test_an_emptied_project_directory_of_the_memory_s_own_goes_with_its_transcripts(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    projects = tmp_path / "projects"
    own = _transcript(projects / (retirer._encoded(vault) + "-worktree") / "a.jsonl", "sdk-cli", str(vault))
    stranger = projects / "-somewhere-else"
    stranger.mkdir()

    assert retirer.retire(projects, vault) == (1, 1)
    assert not own.parent.exists() and stranger.exists()


def test_a_missing_projects_directory_is_nothing_to_do(tmp_path: Path) -> None:
    assert retirer.retire(tmp_path / "absent", tmp_path) == (0, 0)


def test_the_nightly_runs_the_retirer_after_the_lsp_evidence_step() -> None:
    labels = [step.label for step in scheduled_nightly._post_compile_steps()]
    step = next(step for step in scheduled_nightly._post_compile_steps() if step.label == "own_calls")

    assert labels.index("own_calls") == labels.index("lsp_evidence") + 1
    assert step.command[-1].endswith("retire_own_call_transcripts.py")
