"""Helpers shared by test_repair_installed_memory.py and the test files importing them."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).parents[1] / "scripts" / "repair_installed_memory.py"


def _run(*arguments: str) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment.pop("LLM_WIKI_ROOT", None)
    environment.pop("LLM_WIKI_STATE_ROOT", None)
    return subprocess.run(
        [sys.executable, str(SCRIPT), *arguments],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )
