"""The transaction CLI must open the database the vault actually has.

Reliability V3 adoption replaces the pre-adoption
`run/markdown-transactions.sqlite3` with a JSON tombstone, so opening that
path directly raises `file is not a database`. `markdown_transaction.py`'s own
CLI constructed a coordinator on the raw paths, so every one of its commands
had been dead since adoption: `prune`, which is the only thing that enforces
the two-day undo retention — the trail had grown to 5.5 GB — and `undo` and
`recover`, which are the operator's only hands on a transaction.
`reclaim_runtime_state.py` learned this on 2026-09-02; the CLI never did.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import markdown_transaction  # noqa: E402


def test_the_cli_asks_which_coordinator_adoption_left_behind(tmp_path, monkeypatch):
    """The one rule that decides this, rather than a constructor call."""
    (tmp_path / "run").mkdir(parents=True)
    monkeypatch.setenv("LLM_WIKI_ROOT", str(tmp_path))
    monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(tmp_path))
    asked = {}

    def _record(vault, state_root):
        asked["vault"] = Path(vault)
        asked["state_root"] = Path(state_root)
        return "the coordinator this vault has"

    monkeypatch.setattr(
        markdown_transaction, "active_or_legacy_coordinator", _record
    )

    assert markdown_transaction._cli_coordinator() == "the coordinator this vault has"
    assert asked["vault"] == tmp_path.resolve()
    assert asked["state_root"] == tmp_path.resolve()


def test_the_same_paths_without_adoption_still_open_the_legacy_pair(
    tmp_path, monkeypatch
):
    (tmp_path / "run").mkdir(parents=True)
    monkeypatch.setenv("LLM_WIKI_ROOT", str(tmp_path))
    monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(tmp_path))

    coordinator = markdown_transaction._cli_coordinator()

    assert coordinator.database_path.name == "markdown-transactions.sqlite3"
