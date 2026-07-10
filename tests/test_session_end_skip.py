"""Regression test: session_end_project_tag skip semantics.

SessionEnd hook must:
  - SKIP when cwd is inside the vault (vault's own project-level
    session_end_capture.py handles it with richer content).
  - SKIP when cwd is $HOME (HOME is not a project; the .claude/
    marker matches ~/.claude/ not a project .claude/).
  - WRITE a tagged entry for normal non-vault cwd.

Hermetic: all tests use a tmp_path vault mirror — never the real checkout.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "session_end_project_tag.py"


def _invoke(cwd_path: str, vault_root: str, payload: dict) -> int:
    """Run the SessionEnd hook with given CLAUDE_PROJECT_DIR and stdin payload."""
    env = dict(os.environ)
    env["CLAUDE_PROJECT_DIR"] = cwd_path
    env["LLM_WIKI_ROOT"] = vault_root
    result = subprocess.run(
        [sys.executable, str(SCRIPT)],
        input=json.dumps(payload),
        env=env,
        text=True,
        capture_output=True,
    )
    return result.returncode


@pytest.fixture
def fake_vault(tmp_path):
    """Create a minimal vault stub in tmp_path with knowledge/daily/."""
    vault = tmp_path / "vault"
    daily_dir = vault / "knowledge" / "daily"
    daily_dir.mkdir(parents=True, exist_ok=True)
    projects_dir = vault / "knowledge" / "projects"
    projects_dir.mkdir(parents=True, exist_ok=True)
    # Create a project state dir so the hook can resolve slug
    template = projects_dir / "_template"
    template.mkdir(parents=True, exist_ok=True)
    yield vault


def _today_daily(vault: Path) -> Path:
    return vault / "knowledge" / "daily" / f"{datetime.now().strftime('%Y-%m-%d')}.md"


def test_skip_vault_cwd(fake_vault):
    """Vault cwd → hook must skip (project-level hook handles it)."""
    rc = _invoke(str(fake_vault), str(fake_vault), {"session_id": "reg-vault", "reason": "other"})
    assert rc == 0
    daily = _today_daily(fake_vault)
    assert not daily.exists() or daily.read_text(encoding="utf-8") == "", (
        "vault cwd session-end wrote to daily log (should skip)"
    )


def test_skip_home_cwd(fake_vault):
    """HOME cwd → hook must skip."""
    home = str(Path.home().resolve())
    rc = _invoke(home, str(fake_vault), {"session_id": "reg-home", "reason": "other"})
    assert rc == 0
    daily = _today_daily(fake_vault)
    assert not daily.exists() or daily.read_text(encoding="utf-8") == "", (
        "HOME cwd session-end wrote to daily log (should skip)"
    )


def test_write_non_vault_cwd(fake_vault):
    """Normal non-vault cwd → tagged entry appended to today's daily log."""
    with tempfile.TemporaryDirectory() as tmp:
        rc = _invoke(tmp, str(fake_vault), {"session_id": "reg-nonvault", "reason": "other"})
    assert rc == 0
    daily = _today_daily(fake_vault)
    assert daily.exists(), "daily log was not created"
    content = daily.read_text(encoding="utf-8")
    assert "reg-nonvault" in content, "non-vault session-end did not append to daily log"
    assert "Project slug:" in content, "appended entry missing `Project slug:` line"
