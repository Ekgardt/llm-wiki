"""A project is a directory the owner could be working in; the corpus holds its claim pages.

The audit of 2026-09-23 found 85 project directories on the live vault, minted for a
benchmark run under `cache/`, a transaction directory under `run/`, the provider's temp
directory, a pytest temp directory, the home directory and the vault itself — and found
that their journals were 94 % of the search index's bytes. See
`docs/research/2026-09-23-the-corpus-is-the-claim-pages-and-a-project-is-a-project.md`.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import session_start_project_state as project_state  # noqa: E402
from corpus_snapshot import collect_corpus  # noqa: E402


def _vault(tmp_path: Path) -> Path:
    root = tmp_path / "vault"
    (root / "knowledge" / "projects").mkdir(parents=True)
    (root / "knowledge" / "notes").mkdir(parents=True)
    return root


@pytest.mark.parametrize(
    ("relative", "message"),
    [
        ("cache/benchmarks/full-run", "inside the vault"),
        ("run/transactions/abc123", "inside the vault"),
        ("knowledge/projects", "inside the vault"),
    ],
)
def test_a_directory_inside_the_vault_is_not_a_project(tmp_path: Path, relative: str, message: str) -> None:
    vault = _vault(tmp_path)
    inside = vault / relative
    inside.mkdir(parents=True, exist_ok=True)

    with pytest.raises(project_state.NotAProject, match=message):
        project_state._compute_slug(inside, vault / "knowledge" / "projects")


def test_a_direct_child_of_the_platform_temp_directory_is_not_a_project(tmp_path: Path, monkeypatch) -> None:
    """What `mkdtemp` makes — the provider's directory, a pytest session's `/tmp/tmp…`."""
    vault = _vault(tmp_path)
    fake_temp = tmp_path / "temp"
    provider = fake_temp / "llm-wiki-provider-abc123"
    provider.mkdir(parents=True)
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(fake_temp))

    with pytest.raises(project_state.NotAProject, match="temporary directory"):
        project_state._compute_slug(provider, vault / "knowledge" / "projects")


def test_a_deeper_temp_tree_is_left_alone(tmp_path: Path, monkeypatch) -> None:
    """pytest's `tmp_path` is a deliberate structure three levels down; it stays a project."""
    vault = _vault(tmp_path)
    fake_temp = tmp_path / "temp"
    deep = fake_temp / "pytest-of-owner" / "pytest-1" / "demo"
    deep.mkdir(parents=True)
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(fake_temp))

    assert project_state._compute_slug(deep, vault / "knowledge" / "projects") == "demo"


def test_the_home_directory_is_not_a_project(tmp_path: Path, monkeypatch) -> None:
    vault = _vault(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))

    with pytest.raises(project_state.NotAProject, match="home directory"):
        project_state._compute_slug(home, vault / "knowledge" / "projects")


def test_the_refusal_is_a_value_error_every_caller_already_catches() -> None:
    assert issubclass(project_state.NotAProject, ValueError)


def test_the_corpus_holds_the_claim_pages_and_not_the_journal(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    project = vault / "knowledge" / "projects" / "demo"
    project.mkdir()
    (project / "state.md").write_text("# State\nNow.\n", encoding="utf-8")
    (project / "context.md").write_text("# Context\nWhy.\n", encoding="utf-8")
    (project / "journal.md").write_text('{"event": 1}\n', encoding="utf-8")
    (project / "journal.001999-002936.md").write_text('{"event": 0}\n', encoding="utf-8")

    collected = {source.record.relative_path for source in collect_corpus(vault).sources}

    assert collected == {
        "knowledge/projects/demo/context.md",
        "knowledge/projects/demo/state.md",
    }
