"""Phase 1 regression tests: UserPromptSubmit + PostToolUse capture hooks.

Locks in:
1. Capture hooks never fail (always exit 0) — even on malformed input,
   missing stdin, or upstream state corruption. A logging hook MUST
   NOT break the user's session.
2. Prompts below MIN_PROMPT_CHARS are skipped (autocomplete noise).
3. Tool capture filters to SIGNIFICANT_TOOLS only — Read/Glob/Grep
   do not produce memory breadcrumb lines.
4. Rate limiting kicks in within the dedupe window for both hooks.
5. Sessions inside the vault itself (cwd = ROOT) are skipped to avoid
   feedback loops (e.g. flush_memory sub-sessions writing daily-log
   tags about themselves).
"""
from __future__ import annotations

import io
import json
import sys
from pathlib import Path
from unittest.mock import patch

# ---------------------------------------------------------------------------
# UserPromptSubmit capture — user_prompt_capture.py
# ---------------------------------------------------------------------------


def _run_capture_with_stdin(module_name: str, stdin_payload: dict | str) -> int:
    """Helper: invoke capture script's main() with simulated stdin."""
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
    mod = __import__(module_name)

    # Simulate stdin
    if isinstance(stdin_payload, dict):
        stdin_text = json.dumps(stdin_payload)
    else:
        stdin_text = stdin_payload

    with patch.object(sys, "stdin", io.StringIO(stdin_text)):
        return mod.main()


def test_prompt_capture_exits_zero_on_empty_stdin():
    """No stdin → no crash, exit 0."""
    rc = _run_capture_with_stdin("user_prompt_capture", "")
    assert rc == 0


def test_prompt_capture_exits_zero_on_malformed_json():
    """Garbage stdin → no crash, exit 0."""
    rc = _run_capture_with_stdin("user_prompt_capture", "not even json {{{")
    assert rc == 0


def test_prompt_capture_skips_short_prompts(tmp_path, monkeypatch):
    """Prompts below MIN_PROMPT_CHARS (autocomplete noise) are skipped."""
    import user_prompt_capture  # noqa: WPS433

    monkeypatch.setattr(user_prompt_capture, "DAILY_DIR", tmp_path)
    monkeypatch.setattr(user_prompt_capture, "ROOT", tmp_path.parent)  # not equal to cwd
    rc = _run_capture_with_stdin(
        "user_prompt_capture",
        {"prompt": "hi", "session_id": "s1", "cwd": str(tmp_path)},
    )
    assert rc == 0
    # No file should have been written
    assert list(tmp_path.glob("*.md")) == []


def test_prompt_capture_skips_vault_internal_sessions(monkeypatch, tmp_path):
    """Sessions where cwd = ROOT must be skipped (feedback loop guard)."""
    import user_prompt_capture  # noqa: WPS433

    fake_root = tmp_path / "vault"
    fake_root.mkdir()
    monkeypatch.setattr(user_prompt_capture, "ROOT", fake_root)
    monkeypatch.setattr(user_prompt_capture, "DAILY_DIR", fake_root / "knowledge" / "daily")

    rc = _run_capture_with_stdin(
        "user_prompt_capture",
        {"prompt": "this is a long enough prompt", "session_id": "s1", "cwd": str(fake_root)},
    )
    assert rc == 0
    # No daily log written because cwd == ROOT.
    daily_dir = fake_root / "knowledge" / "daily"
    assert not daily_dir.exists() or list(daily_dir.glob("*.md")) == []


def test_prompt_capture_writes_line_for_real_prompt(monkeypatch, tmp_path):
    """Long-enough prompt from a non-vault cwd writes one line."""
    import user_prompt_capture  # noqa: WPS433

    fake_root = tmp_path / "vault"
    fake_root.mkdir()
    daily_dir = fake_root / "knowledge" / "daily"
    monkeypatch.setattr(user_prompt_capture, "ROOT", fake_root)
    monkeypatch.setattr(user_prompt_capture, "DAILY_DIR", daily_dir)
    monkeypatch.setenv("LLM_WIKI_ROOT", str(fake_root))
    monkeypatch.setattr(
        user_prompt_capture, "_compute_slug_from_cwd", lambda cwd: "test-slug"
    )
    monkeypatch.setattr(user_prompt_capture, "_rate_limited", lambda *a: False)
    monkeypatch.setattr(user_prompt_capture, "_record_dedupe", lambda *a: None)

    # Use a cwd that's NOT the fake_root (so it's not skipped as vault-internal).
    project_cwd = tmp_path / "project"
    project_cwd.mkdir()
    rc = _run_capture_with_stdin(
        "user_prompt_capture",
        {
            "prompt": "Help me refactor the auth module",
            "session_id": "abc123def456",
            "cwd": str(project_cwd),
        },
    )
    assert rc == 0
    # Verify daily log was written
    today = __import__("datetime").date.today().isoformat()
    daily = daily_dir / f"{today}.md"
    assert daily.exists()
    content = daily.read_text(encoding="utf-8")
    assert "prompt" in content
    assert "test-slug" in content
    assert "abc123de" in content  # session_id[:8]
    assert "Help me refactor" in content


# ---------------------------------------------------------------------------
# PostToolUse capture — post_tool_capture.py
# ---------------------------------------------------------------------------


def test_tool_capture_exits_zero_on_empty_stdin():
    rc = _run_capture_with_stdin("post_tool_capture", "")
    assert rc == 0


def test_tool_capture_filters_non_significant_tools(monkeypatch, tmp_path):
    """Read / Glob / Grep / LS must NOT produce breadcrumbs."""
    import post_tool_capture  # noqa: WPS433

    daily_dir = tmp_path / "daily"
    monkeypatch.setattr(post_tool_capture, "DAILY_DIR", daily_dir)
    monkeypatch.setattr(post_tool_capture, "ROOT", tmp_path / "vault")

    for noisy_tool in ["Read", "Glob", "Grep", "LS", "TodoWrite"]:
        rc = _run_capture_with_stdin(
            "post_tool_capture",
            {"tool_name": noisy_tool, "tool_input": {}, "session_id": "s1", "cwd": str(tmp_path)},
        )
        assert rc == 0
    # No files written for filtered tools.
    assert not daily_dir.exists() or list(daily_dir.glob("*.md")) == []


def test_tool_capture_logs_significant_tools(monkeypatch, tmp_path):
    """Edit / Write / MultiEdit / Bash produce breadcrumbs."""
    import post_tool_capture  # noqa: WPS433

    fake_root = tmp_path / "vault"
    fake_root.mkdir()
    daily_dir = fake_root / "knowledge" / "daily"
    monkeypatch.setattr(post_tool_capture, "ROOT", fake_root)
    monkeypatch.setattr(post_tool_capture, "DAILY_DIR", daily_dir)
    monkeypatch.setenv("LLM_WIKI_ROOT", str(fake_root))
    monkeypatch.setattr(
        post_tool_capture, "_compute_slug_from_cwd", lambda cwd: "test-slug"
    )
    monkeypatch.setattr(post_tool_capture, "_rate_limited", lambda *a: False)
    monkeypatch.setattr(post_tool_capture, "_record_dedupe", lambda *a: None)

    project_cwd = tmp_path / "project"
    project_cwd.mkdir()
    rc = _run_capture_with_stdin(
        "post_tool_capture",
        {
            "tool_name": "Edit",
            "tool_input": {"filePath": "src/auth.py"},
            "session_id": "abc123def456",
            "cwd": str(project_cwd),
        },
    )
    assert rc == 0
    today = __import__("datetime").date.today().isoformat()
    daily = daily_dir / f"{today}.md"
    assert daily.exists()
    content = daily.read_text(encoding="utf-8")
    assert "Edit" in content
    assert "src/auth.py" in content
    assert "test-slug" in content


def test_tool_capture_bash_filters_short_commands(monkeypatch, tmp_path):
    """Short Bash commands (cd, pwd, ls) are noise — skip them."""
    import post_tool_capture  # noqa: WPS433

    monkeypatch.setattr(post_tool_capture, "DAILY_DIR", tmp_path / "daily")
    monkeypatch.setattr(post_tool_capture, "ROOT", tmp_path / "vault")
    monkeypatch.setattr(post_tool_capture, "_rate_limited", lambda *a: False)
    monkeypatch.setattr(post_tool_capture, "_record_dedupe", lambda *a: None)

    # "pwd" is below MIN_BASH_CMD_CHARS — should be skipped.
    rc = _run_capture_with_stdin(
        "post_tool_capture",
        {
            "tool_name": "Bash",
            "tool_input": {"command": "pwd"},
            "session_id": "s1",
            "cwd": str(tmp_path / "project"),
        },
    )
    assert rc == 0
    # No file should be written.
    assert list((tmp_path / "daily").glob("*.md")) == [] if (tmp_path / "daily").exists() else True


def test_tool_capture_skips_vault_internal_sessions(monkeypatch, tmp_path):
    """Tool calls where cwd = ROOT must be skipped."""
    import post_tool_capture  # noqa: WPS433

    fake_root = tmp_path / "vault"
    fake_root.mkdir()
    monkeypatch.setattr(post_tool_capture, "ROOT", fake_root)
    monkeypatch.setattr(post_tool_capture, "DAILY_DIR", fake_root / "knowledge" / "daily")

    rc = _run_capture_with_stdin(
        "post_tool_capture",
        {
            "tool_name": "Edit",
            "tool_input": {"filePath": "foo.py"},
            "session_id": "s1",
            "cwd": str(fake_root),
        },
    )
    assert rc == 0
    daily_dir = fake_root / "knowledge" / "daily"
    assert not daily_dir.exists() or list(daily_dir.glob("*.md")) == []


# ---------------------------------------------------------------------------
# Rate-limit helpers
# ---------------------------------------------------------------------------


def test_prompt_capture_rate_limit_window(tmp_path, monkeypatch):
    """Verify rate-limit check returns True within window, False outside."""
    from datetime import datetime, timedelta

    import user_prompt_capture  # noqa: WPS433

    state_file = tmp_path_state(tmp_path, monkeypatch, user_prompt_capture)
    # Pre-populate dedupe with an entry 5 seconds ago (within 30s window).
    recent = (datetime.now() - timedelta(seconds=5)).isoformat(timespec="seconds")
    state = {"prompt_capture_dedupe": {"slug::abc": recent}}
    state_file.write_text(json.dumps(state), encoding="utf-8")
    assert user_prompt_capture._rate_limited("slug", "abc") is True

    # Old entry — outside window.
    old = (datetime.now() - timedelta(seconds=120)).isoformat(timespec="seconds")
    state = {"prompt_capture_dedupe": {"slug::xyz": old}}
    state_file.write_text(json.dumps(state), encoding="utf-8")
    assert user_prompt_capture._rate_limited("slug", "xyz") is False


def tmp_path_state(tmp_path: Path, monkeypatch, module):
    """Point a capture module's STATE_ROOT at pytest's tmp_path, return state_file.

    Uses pytest's built-in tmp_path fixture (auto-cleaned per test) instead
    of a sibling directory under tests/ — that older variant left a
    `_tmp_state_dir/` artifact in the repo after the suite ran.
    """
    state_dir = tmp_path / "run"
    state_dir.mkdir()
    monkeypatch.setattr(module, "STATE_ROOT", tmp_path)
    return state_dir / "state.json"
