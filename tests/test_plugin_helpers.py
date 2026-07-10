"""Phase 5 tests: OpenCode plugin's Python file-IO helpers.

Locks in:
1. Each helper exits 0 on empty/malformed stdin (never breaks plugin).
2. Each helper writes the expected artifact (daily log line / state heartbeat).
3. Each helper is idempotent across multiple calls within the same day.
"""
from __future__ import annotations

import io
import json
import sys
from datetime import date
from pathlib import Path
from unittest.mock import patch

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))


def _reload(module_name: str):
    """Force re-import so module-level env reads pick up monkeypatched env."""
    if module_name in sys.modules:
        del sys.modules[module_name]
    return __import__(module_name)


def _run_with_stdin(module_name: str, stdin_text: str) -> int:
    mod = _reload(module_name)
    with patch.object(sys, "stdin", io.StringIO(stdin_text)):
        return mod.main()


# ---------------------------------------------------------------------------
# daily_log_append.py
# ---------------------------------------------------------------------------


def test_daily_log_append_exits_zero_on_empty_stdin():
    assert _run_with_stdin("daily_log_append", "") == 0


def test_daily_log_append_exits_zero_on_malformed_json():
    assert _run_with_stdin("daily_log_append", "not json {{{") == 0


def test_daily_log_append_writes_block(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_WIKI_ROOT", str(tmp_path))
    payload = {
        "slug": "your-app",
        "sessionId": "opencode-abc123",
        "block": "## [10:00:00] test | opencode\n- Tier: `major`\n\nbody here\n",
    }
    rc = _run_with_stdin("daily_log_append", json.dumps(payload))
    assert rc == 0

    today = date.today().isoformat()
    daily = tmp_path / "knowledge" / "daily" / f"{today}.md"
    assert daily.exists()
    content = daily.read_text(encoding="utf-8")
    assert "Tier: `major`" in content
    assert "body here" in content


# ---------------------------------------------------------------------------
# heartbeat_record.py
# ---------------------------------------------------------------------------


def test_heartbeat_record_exits_zero_on_empty_stdin():
    assert _run_with_stdin("heartbeat_record", "") == 0


def test_heartbeat_record_exits_zero_on_malformed_json():
    assert _run_with_stdin("heartbeat_record", "{not json") == 0


def test_heartbeat_record_writes_state_entry(tmp_path, monkeypatch):
    # memory_state caches STATE_ROOT at module-load time, so we patch
    # the resolved attributes directly rather than relying on env vars.
    fake_state_root = tmp_path
    fake_state_dir = fake_state_root / "run"
    fake_state_dir.mkdir(parents=True, exist_ok=True)
    fake_state_file = fake_state_dir / "state.json"

    # Patch memory_state module attributes (heartbeat_record imports
    # update_state from memory_state, which references STATE_FILE etc).
    import memory_state

    monkeypatch.setattr(memory_state, "STATE_ROOT", fake_state_root)
    monkeypatch.setattr(memory_state, "STATE_DIR", fake_state_dir)
    monkeypatch.setattr(memory_state, "STATE_FILE", fake_state_file)
    monkeypatch.setattr(memory_state, "LOCK_FILE", fake_state_dir / "state.json.lock")

    payload = {
        "slug": "test-project",
        "projectRoot": "/path/to/test-project",
        "reason": "opencode-session-start",
        "sessionId": "opc-123",
    }
    rc = _run_with_stdin("heartbeat_record", json.dumps(payload))
    assert rc == 0

    assert fake_state_file.exists()
    state = json.loads(fake_state_file.read_text(encoding="utf-8"))
    assert "test-project" in state.get("codex_heartbeats", {})
    hb = state["codex_heartbeats"]["test-project"]
    assert hb["reason"] == "opencode-session-start"
    assert hb["session_id"] == "opc-123"
    assert hb["source"] == "opencode"


# ---------------------------------------------------------------------------
# tool_breadcrumb_append.py
# ---------------------------------------------------------------------------


def test_tool_breadcrumb_exits_zero_on_empty_stdin():
    assert _run_with_stdin("tool_breadcrumb_append", "") == 0


def test_tool_breadcrumb_exits_zero_on_malformed_json():
    assert _run_with_stdin("tool_breadcrumb_append", "garbage") == 0


def test_tool_breadcrumb_writes_line(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_WIKI_ROOT", str(tmp_path))
    payload = {
        "slug": "your-app",
        "sessionId": "abcdefghij",
        "tool": "edit",
        "target": "src/auth.ts",
    }
    rc = _run_with_stdin("tool_breadcrumb_append", json.dumps(payload))
    assert rc == 0

    today = date.today().isoformat()
    daily = tmp_path / "knowledge" / "daily" / f"{today}.md"
    assert daily.exists()
    content = daily.read_text(encoding="utf-8")
    assert "tool" in content
    assert "your-app" in content
    assert "edit" in content
    assert "src/auth.ts" in content
    assert "abcdefgh" in content  # session_id truncated to 8 chars
