"""Run as a script, the index answers a sibling's refusal as JSON (audit 2026-09-26 A-5).

docs/research/2026-09-26-a-refusal-class-lives-outside-the-script.md
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from tests.slow_machine import LONG_TIMEOUT

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "repository_index.py"


def _git(root: Path, *arguments: str) -> None:
    subprocess.run(["git", "-C", str(root), *arguments], check=True, capture_output=True)


def test_following_a_non_git_directory_is_a_json_refusal(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    env = {**os.environ, "LLM_WIKI_STATE_ROOT": str(tmp_path / "state"), "MEMORY_LLM_PROVIDER": "fake"}

    run = subprocess.run(
        [sys.executable, str(SCRIPT), "follow", str(plain), "--state-root", str(tmp_path / "state")],
        capture_output=True, text=True, timeout=LONG_TIMEOUT, env=env,
    )

    assert (run.returncode, json.loads(run.stdout)["status"]) == (2, "refused")
