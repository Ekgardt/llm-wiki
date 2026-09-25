"""Navigation keeps its word: dead-code narrowing and the caller's Git budget (audit C-44).

docs/research/2026-09-25-navigation-dead-code-and-stale-words.md
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import code_graph  # noqa: E402
import workspace_revision  # noqa: E402


def test_a_git_run_gets_the_whole_budget_its_caller_gave():
    budget = time.monotonic() + 10 * workspace_revision.GIT_STATUS_TIMEOUT_SECONDS

    assert workspace_revision._git_run_deadline(budget) == budget


def test_a_git_run_without_a_budget_is_still_bounded():
    bound = workspace_revision._git_run_deadline(None) - time.monotonic()

    assert 0 < bound <= workspace_revision.GIT_STATUS_TIMEOUT_SECONDS


def test_the_live_dead_code_answer_is_only_about_the_named_symbol(tmp_path):
    (tmp_path / "a.py").write_text(
        "def orphan_one():\n    return 1\n\n\ndef orphan_two():\n    return 2\n",
        encoding="utf-8",
    )

    answer = code_graph.find_dead_code(tmp_path, live=True, symbol="orphan_two")

    assert [item["name"] for item in answer] == ["orphan_two"]
