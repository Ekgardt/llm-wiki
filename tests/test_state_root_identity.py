"""Every runtime actor must name the same state root.

A symlinked or relative `LLM_WIKI_STATE_ROOT` is one directory, but two path
strings. Containment checks and the ownership registry compare strings, so an
actor that skips `resolve()` disagrees with every actor that does not — and the
disagreement only shows up on machines whose temporary or home directories are
reached through a link, which is every macOS machine.
"""

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))


@pytest.fixture()
def linked_state_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A state root reached through a symlink, plus a vault to go with it."""
    real = tmp_path / "real-state"
    real.mkdir()
    link = tmp_path / "linked-state"
    link.symlink_to(real, target_is_directory=True)
    vault = tmp_path / "vault"
    (vault / "knowledge").mkdir(parents=True)
    monkeypatch.setenv("LLM_WIKI_ROOT", str(vault))
    monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(link))
    return real.resolve()


def test_the_coordinator_resolves_its_state_root(linked_state_root: Path) -> None:
    import markdown_transaction

    coordinator = markdown_transaction._default_coordinator()
    assert coordinator.state_root == linked_state_root


def test_the_queue_resolves_its_state_root(linked_state_root: Path) -> None:
    import memory_queue

    assert memory_queue._state_root() == linked_state_root


def test_the_blackboard_agrees_with_the_coordinator(linked_state_root: Path) -> None:
    """The two actors that share the coordinator database must share its path."""
    import markdown_transaction

    blackboard_root = Path(os.environ["LLM_WIKI_STATE_ROOT"]).resolve()
    assert markdown_transaction._default_coordinator().state_root == blackboard_root


def test_a_relative_state_root_is_made_absolute(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = tmp_path / "state"
    state.mkdir()
    vault = tmp_path / "vault"
    (vault / "knowledge").mkdir(parents=True)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LLM_WIKI_ROOT", str(vault))
    monkeypatch.setenv("LLM_WIKI_STATE_ROOT", "state")

    import markdown_transaction
    import memory_queue

    assert markdown_transaction._default_coordinator().state_root == state.resolve()
    assert memory_queue._state_root() == state.resolve()
