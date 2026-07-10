"""Contract tests for audit-flagged runtime paths.

Covers:
- compile_memory has module-level `re` (contradiction path)
- feedback_capture stdin JSON (OpenCode plugin)
- loop_detector matches real breadcrumb format
- MEMORY_LLM_PROVIDER=fake smoke for compile plan apply
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))


def test_compile_memory_imports_re():
    import compile_memory

    assert hasattr(compile_memory, "re")
    # Contradiction path must not NameError
    result = compile_memory._check_contradictions_pre_write(
        "patterns",
        "new-page",
        "new title about auth",
        "we use JWT instead of sessions",
    )
    assert isinstance(result, list)


def test_feedback_capture_stdin_json(tmp_path, monkeypatch):
    import feedback_capture

    monkeypatch.setattr(feedback_capture, "ROOT", tmp_path)
    monkeypatch.setattr(feedback_capture, "FEEDBACK_DIR", tmp_path / "knowledge" / "feedback")

    payload = json.dumps(
        {
            "text": "No, always use Postgres instead of SQLite for production",
            "session_id": "sess-test",
            "slug": "demo",
            "trigger": "opencode-idle",
        }
    )
    env = dict(os.environ)
    env["LLM_WIKI_ROOT"] = str(tmp_path)
    result = subprocess.run(
        [sys.executable, str(SCRIPTS / "feedback_capture.py")],
        input=payload,
        text=True,
        capture_output=True,
        env=env,
        cwd=str(ROOT),
    )
    assert result.returncode == 0
    # Candidate files under vault feedback dir (module uses its ROOT from env at import
    # in subprocess — script resolves ROOT from memory_state). Check via capture_from_text
    # unit path as well:
    cid = feedback_capture.capture_from_text(
        "No, always use Postgres instead of SQLite for production",
        session_id="sess-test",
        slug="demo",
        trigger="opencode-idle",
    )
    assert cid is not None
    assert (tmp_path / "knowledge" / "feedback" / f"{cid}.json").exists()


def test_loop_detector_matches_breadcrumb_format(tmp_path, monkeypatch):
    import loop_detector

    daily = tmp_path / "knowledge" / "daily"
    daily.mkdir(parents=True)
    day = daily / "2026-07-09.md"
    # Exact writer format from tool_breadcrumb_append.py
    day.write_text(
        "# Daily\n"
        "- `[10:00:01] tool | abcd1234 | demo | edit` src/app.py\n"
        "- `[10:05:02] tool | abcd1234 | demo | write` src/app.py\n"
        "- `[10:10:03] tool | efgh5678 | demo | Edit` src/app.py\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(loop_detector, "DAILY_DIR", daily)
    monkeypatch.setattr(loop_detector, "ROOT", tmp_path)
    loops = loop_detector.detect_file_edit_loops("demo", days=30, threshold=3)
    assert loops
    assert loops[0]["target"] == "src/app.py"
    assert loops[0]["edit_count"] >= 3


def test_fake_llm_provider_returns_canned_json(monkeypatch):
    import llm_client

    monkeypatch.setenv("MEMORY_LLM_PROVIDER", "fake")
    monkeypatch.setenv(
        "MEMORY_LLM_FAKE_RESPONSE",
        '{"operations": [], "audit": {}}\nCOMPILE_AUDIT: verified 0 evidence citations; 0 dedup checks performed; 0 stubs skipped; 0 contradictions handled; 0 pages rejected as below-threshold',
    )
    out = llm_client.call_llm("hello", system_prompt="sys")
    assert "operations" in out
    assert "COMPILE_AUDIT" in out


def test_flush_memory_uses_maybe_compile(monkeypatch, tmp_path):
    """maybe_trigger_compile must call spawn_compile_if_idle, not spawn_detached."""
    import flush_memory

    calls = []

    def fake_spawn(force=False):
        calls.append(force)
        return True, "spawned compile pid=1"

    monkeypatch.setattr(flush_memory, "spawn_compile_if_idle", fake_spawn)
    monkeypatch.setattr(flush_memory, "file_hash", lambda p: "abc")
    monkeypatch.setenv("MEMORY_COMPILE_AFTER_HOUR", "0")
    monkeypatch.setenv("MEMORY_COMPILE_COOLDOWN_SECONDS", "0")

    daily = tmp_path / "2026-07-09.md"
    daily.write_text("# d\n", encoding="utf-8")
    state: dict = {"compiled_daily_hashes": {}}
    flush_memory.maybe_trigger_compile(state, daily, "major")
    assert calls == [False]
    assert state["last_compile_spawned_reason"] == "spawned compile pid=1"
