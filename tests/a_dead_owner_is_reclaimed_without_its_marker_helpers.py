"""Helpers shared by test_a_dead_owner_is_reclaimed_without_its_marker.py and the test files importing them."""
from __future__ import annotations

from pathlib import Path

import markdown_transaction

MARKER = "run/maintenance.lock"


def _candidate(state_root: Path) -> Path:
    path = state_root / "run/markdown-transactions-v3.candidate.sqlite3"
    markdown_transaction.initialize_coordinator_v3_candidate(path, source_v2=None)
    return path
