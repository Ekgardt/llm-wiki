"""Helpers shared by test_episode_consolidation.py and the test files importing them."""
from __future__ import annotations

from pathlib import Path

import pytest

RECORD = """---
type: raw-source
session: abc123
---

# Session abc123

**user:** why systemd and not cron?

**assistant:** systemd user timers survive a reboot and cron does not report failures
"""


@pytest.fixture()
def vault(tmp_path: Path) -> Path:
    directory = tmp_path / "knowledge/raw/sessions/2026-08-23"
    directory.mkdir(parents=True)
    (directory / "abc123.md").write_text(RECORD, encoding="utf-8")
    return tmp_path
