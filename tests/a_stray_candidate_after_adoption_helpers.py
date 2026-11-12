"""Helpers shared by test_a_stray_candidate_after_adoption.py and the test files importing them."""
from __future__ import annotations

from pathlib import Path

import markdown_transaction

from tests.doctor_helpers import _adopt, _build_root

CANDIDATE = "run/markdown-transactions-v3.candidate.sqlite3"


def _adopted_vault(tmp_path: Path) -> tuple[Path, Path, Path]:
    root, state_root, home = _build_root(tmp_path)
    _adopt(root, state_root)
    return root, state_root, home


def _stray(state_root: Path) -> Path:
    candidate = state_root / CANDIDATE
    markdown_transaction.initialize_coordinator_v3_candidate(candidate, source_v2=None)
    return candidate
