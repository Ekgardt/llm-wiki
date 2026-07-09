"""Regression test: session_end_project_tag skip semantics.

SessionEnd hook must:
  - SKIP when cwd is inside the vault (vault's own project-level
    session_end_capture.py handles it with richer content).
  - SKIP when cwd is $HOME (HOME is not a project; the .claude/
    marker matches ~/.claude/ not a project .claude/).
  - WRITE a tagged entry for normal non-vault cwd.

Covers Round 2 (HOME skip) and Round 3.5 (vault skip).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "session_end_project_tag.py"
VAULT_ROOT = Path(__file__).resolve().parent.parent


def _invoke(cwd_path: str, payload: dict) -> int:
    """Run the SessionEnd hook with given CLAUDE_PROJECT_DIR and stdin payload.

    Returns exit code.

    Explicitly forwards `LLM_WIKI_ROOT` — conftest.py bootstraps it session-
    wide, but belt-and-suspenders here so this test is self-documenting:
    without LLM_WIKI_ROOT, the hook no-ops and the "write to daily log"
    assertion would fail silently on a clean clone.
    """
    env = dict(os.environ)
    env["CLAUDE_PROJECT_DIR"] = cwd_path
    # Always pin to this checkout — user/process env may point at an
    # installed clone (e.g. tools-agent/llm-wiki); the fixture asserts
    # against VAULT_ROOT/knowledge/daily/, so the hook must write there.
    env["LLM_WIKI_ROOT"] = str(VAULT_ROOT)
    result = subprocess.run(
        [sys.executable, str(SCRIPT)],
        input=json.dumps(payload),
        env=env,
        text=True,
        capture_output=True,
    )
    return result.returncode


@pytest.fixture
def daily_log_snapshot():
    """Snapshot today's daily log before/after to count appends."""
    from datetime import datetime
    daily = VAULT_ROOT / "knowledge" / "daily" / f"{datetime.now().strftime('%Y-%m-%d')}.md"
    before = daily.read_text(encoding="utf-8") if daily.exists() else ""
    yield {"path": daily, "before": before}
    # Restore (remove anything the test appended)
    if daily.exists():
        current = daily.read_text(encoding="utf-8")
        if current != before:
            daily.write_text(before, encoding="utf-8")


def test_skip_vault_cwd(daily_log_snapshot):
    rc = _invoke(str(VAULT_ROOT), {"session_id": "reg-vault", "reason": "other"})
    assert rc == 0
    # Daily log must be unchanged
    after = daily_log_snapshot["path"].read_text(encoding="utf-8") if daily_log_snapshot["path"].exists() else ""
    assert after == daily_log_snapshot["before"], (
        "vault cwd session-end wrote to daily log (should skip — project-level hook handles it)"
    )


def test_skip_home_cwd(daily_log_snapshot):
    home = str(Path.home().resolve())
    rc = _invoke(home, {"session_id": "reg-home", "reason": "other"})
    assert rc == 0
    after = daily_log_snapshot["path"].read_text(encoding="utf-8") if daily_log_snapshot["path"].exists() else ""
    assert after == daily_log_snapshot["before"], (
        "HOME cwd session-end wrote to daily log (should skip — HOME is not a project)"
    )


def test_write_non_vault_cwd(daily_log_snapshot):
    """Normal non-vault cwd — tagged entry appended to today's daily log."""
    with tempfile.TemporaryDirectory() as tmp:
        rc = _invoke(tmp, {"session_id": "reg-nonvault", "reason": "other"})
    assert rc == 0
    after = daily_log_snapshot["path"].read_text(encoding="utf-8") if daily_log_snapshot["path"].exists() else ""
    assert "reg-nonvault" in after, (
        "non-vault session-end did not append to daily log"
    )
    # Must carry the project-slug tag (key signal for compile pipeline)
    assert "Project slug:" in after[len(daily_log_snapshot["before"]):], (
        "appended entry missing `Project slug:` line"
    )
