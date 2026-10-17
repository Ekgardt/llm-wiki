from pathlib import Path

import pytest
from corpus_snapshot import collect_corpus


@pytest.fixture
def ignored_vault(tmp_path: Path) -> Path:
    notes = tmp_path / "knowledge/notes"
    notes.mkdir(parents=True)
    for index in range(50_001):
        (notes / f"ignored-{index}.txt").touch()
    return tmp_path


def _configure(vault: Path, maximum: int) -> None:
    (vault / "llm-wiki.toml").write_text(f"[corpus]\nmax_files = {maximum}\n")


def test_configured_budget_accepts_real_ignored_entries(ignored_vault: Path):
    _configure(ignored_vault, 50_100)
    snapshot = collect_corpus(ignored_vault)
    assert snapshot.sources == ()
    assert snapshot.policy.max_entries == 50_100


def test_environment_budget_accepts_real_ignored_entries(ignored_vault, monkeypatch):
    _configure(ignored_vault, 1)
    monkeypatch.setenv("LLM_WIKI_CORPUS_MAX_FILES", "50100")
    assert collect_corpus(ignored_vault).policy.max_entries == 50_100


def test_small_configured_budget_refuses_ignored_entries(tmp_path):
    notes = tmp_path / "knowledge/notes"
    notes.mkdir(parents=True)
    (notes / "ignored.txt").touch()
    (notes / "also-ignored.txt").touch()
    _configure(tmp_path, 1)
    with pytest.raises(ValueError, match="entry limit"):
        collect_corpus(tmp_path)


def test_explicit_entry_budget_still_refuses(ignored_vault):
    _configure(ignored_vault, 50_100)
    with pytest.raises(ValueError, match="entry limit"):
        collect_corpus(ignored_vault, max_entries=50_000)


def test_explicit_entry_budget_overrides_small_config(ignored_vault):
    _configure(ignored_vault, 1)
    assert collect_corpus(ignored_vault, max_entries=50_100).sources == ()
