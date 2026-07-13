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
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
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


def test_session_end_ephemeral_transcript_is_cleaned_when_spawn_fails(
    monkeypatch, tmp_path, capsys
):
    import session_end_capture

    state_root = tmp_path / "state"
    transient = state_root / "cache" / "transient-transcripts" / "transient.txt"
    transient.parent.mkdir(parents=True)
    transient.write_text("redacted transcript", encoding="utf-8")
    monkeypatch.setattr(session_end_capture, "STATE_ROOT", state_root, raising=False)
    spawned = []
    monkeypatch.setattr(
        session_end_capture,
        "spawn_detached",
        lambda args: spawned.append(args) or None,
    )

    rc = _run_capture_with_stdin(
        "session_end_capture",
        {
            "session_id": "session-1",
            "transcript_path": str(transient),
            "ephemeral_transcript": True,
        },
    )

    assert rc == 0
    assert "--ephemeral-transcript" in spawned[0]
    assert not transient.exists()
    assert json.loads(capsys.readouterr().out) == {"flush_started": False}


def test_session_end_confirms_detached_flush_start(monkeypatch, capsys):
    import session_end_capture

    monkeypatch.setattr(session_end_capture, "spawn_detached", lambda args: 1234)

    assert _run_capture_with_stdin(
        "session_end_capture", {"transcript_path": "session.jsonl"}
    ) == 0
    assert json.loads(capsys.readouterr().out) == {"flush_started": True}


def test_session_end_passes_explicit_sanitized_trigger(monkeypatch, capsys):
    import session_end_capture

    spawned = []
    monkeypatch.setattr(
        session_end_capture, "spawn_detached", lambda args: spawned.append(args) or 1234
    )

    assert _run_capture_with_stdin(
        "session_end_capture",
        {"reason": "reason", "trigger": "sanitized-trigger"},
    ) == 0
    trigger_index = spawned[0].index("--trigger")
    assert spawned[0][trigger_index + 1] == "sanitized-trigger"
    assert json.loads(capsys.readouterr().out) == {"flush_started": True}


def test_session_end_failed_spawn_does_not_delete_untrusted_path(monkeypatch, tmp_path):
    import session_end_capture

    protected = tmp_path / "protected.txt"
    protected.write_text("keep", encoding="utf-8")
    monkeypatch.setattr(session_end_capture, "STATE_ROOT", tmp_path / "state", raising=False)
    monkeypatch.setattr(session_end_capture, "spawn_detached", lambda args: None)

    rc = _run_capture_with_stdin(
        "session_end_capture",
        {"transcript_path": str(protected), "ephemeral_transcript": True},
    )

    assert rc == 0
    assert protected.exists()


def test_precompact_ephemeral_transcript_propagates_and_cleans_failed_spawn(
    monkeypatch, tmp_path, capsys
):
    import precompact_capture

    state_root = tmp_path / "state"
    transient = state_root / "cache" / "transient-transcripts" / "compact.txt"
    transient.parent.mkdir(parents=True)
    transient.write_text("safe", encoding="utf-8")
    spawned = []
    monkeypatch.setattr(precompact_capture, "STATE_ROOT", state_root, raising=False)
    monkeypatch.setattr(
        precompact_capture, "spawn_detached", lambda args: spawned.append(args) or None
    )

    assert _run_capture_with_stdin(
        "precompact_capture",
        {
            "transcript_path": str(transient),
            "ephemeral_transcript": True,
        },
    ) == 0
    assert "--ephemeral-transcript" in spawned[0]
    assert not transient.exists()
    assert json.loads(capsys.readouterr().out) == {"flush_started": False}


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
    monkeypatch.setattr(user_prompt_capture, "_claim_prompt_dedupe", lambda *a: True)
    monkeypatch.setattr(user_prompt_capture, "_increment_prompt_count", lambda *args: 1)

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


def test_prompt_capture_redacts_and_builds_envelope_before_append(monkeypatch, tmp_path):
    import user_prompt_capture
    from event_envelope import build_event_envelope

    fake_root = tmp_path / "vault"
    fake_root.mkdir()
    project_cwd = tmp_path / "project"
    project_cwd.mkdir()
    secret = "sk-abcdefghijklmnopqrstuvwxyz012345"
    calls = []

    def observed_build(**kwargs):
        calls.append(("build", kwargs))
        return build_event_envelope(**kwargs)

    def observed_append(slug, session_id, preview):
        calls.append(("append", {"slug": slug, "session": session_id, "preview": preview}))

    monkeypatch.setattr(user_prompt_capture, "ROOT", fake_root)
    monkeypatch.setattr(user_prompt_capture, "_compute_slug_from_cwd", lambda cwd: "test-slug")
    monkeypatch.setattr(user_prompt_capture, "_increment_prompt_count", lambda *args: 1)
    monkeypatch.setattr(user_prompt_capture, "_claim_prompt_dedupe", lambda *args: True)
    monkeypatch.setattr(user_prompt_capture, "build_event_envelope", observed_build)
    monkeypatch.setattr(user_prompt_capture, "_append_prompt_tag", observed_append)

    rc = _run_capture_with_stdin(
        "user_prompt_capture",
        {
            "prompt": f"Authorization: Bearer {secret}",
            "session_id": "session-1",
            "cwd": str(project_cwd),
        },
    )

    assert rc == 0
    assert [name for name, _ in calls] == ["build", "append"]
    assert calls[0][1]["event_type"] == "user_prompt"
    assert secret not in calls[0][1]["payload"]["prompt"]
    assert secret not in calls[1][1]["preview"]


def test_prompt_capture_rejection_has_no_side_effects(monkeypatch, tmp_path):
    import user_prompt_capture

    fake_root = tmp_path / "vault"
    fake_root.mkdir()
    project_cwd = tmp_path / "project"
    project_cwd.mkdir()
    calls = []

    def reject_envelope(**kwargs):
        raise ValueError("invalid event payload")

    monkeypatch.setattr(user_prompt_capture, "ROOT", fake_root)
    monkeypatch.setattr(user_prompt_capture, "_compute_slug_from_cwd", lambda cwd: "test-slug")
    monkeypatch.setattr(user_prompt_capture, "build_event_envelope", reject_envelope)
    monkeypatch.setattr(
        user_prompt_capture,
        "_increment_prompt_count",
        lambda *args: calls.append("counter") or 20,
    )
    monkeypatch.setattr(user_prompt_capture, "_build_advisory_refresh", lambda: "refresh")
    monkeypatch.setattr(
        user_prompt_capture,
        "_write_advisory_output",
        lambda *args: calls.append("advisory"),
    )
    monkeypatch.setattr(
        user_prompt_capture,
        "_spawn_periodic_flush",
        lambda *args: calls.append("flush"),
    )
    monkeypatch.setattr(
        user_prompt_capture,
        "_claim_prompt_dedupe",
        lambda *args: calls.append("dedupe") or True,
    )
    monkeypatch.setattr(
        user_prompt_capture,
        "_append_prompt_tag",
        lambda *args: calls.append("append"),
    )

    rc = _run_capture_with_stdin(
        "user_prompt_capture",
        {
            "prompt": "Reject this otherwise valid prompt",
            "session_id": "session-1",
            "cwd": str(project_cwd),
        },
    )

    assert rc == 0
    assert calls == []


def test_prompt_counter_is_durable_and_concurrency_safe(tmp_path, monkeypatch):
    import memory_state
    import user_prompt_capture

    monkeypatch.setattr(memory_state, "STATE_DIR", tmp_path / "run")
    monkeypatch.setattr(memory_state, "STATE_FILE", tmp_path / "run" / "state.json")
    monkeypatch.setattr(memory_state, "LOCK_FILE", tmp_path / "run" / "state.json.lock")
    monkeypatch.setattr(user_prompt_capture, "HOOK_STATE_LOCK_TIMEOUT", 10.0)
    errors = []

    def observed_update(mutator, **kwargs):
        try:
            return memory_state.update_state(mutator, **kwargs)
        except Exception as exc:
            errors.append(exc)
            raise

    monkeypatch.setattr(user_prompt_capture, "update_state", observed_update)

    with ThreadPoolExecutor(max_workers=20) as pool:
        thresholds = list(
            pool.map(
                lambda _: user_prompt_capture._increment_prompt_count("session-a", "project-a"),
                range(100),
            )
        )

    state = json.loads(memory_state.STATE_FILE.read_text(encoding="utf-8"))
    assert errors == []
    assert state["user_prompt_counts"]["session-a"] == 100
    assert sum(count % 20 == 0 for count in thresholds) == 5


def test_prompt_counters_are_isolated_across_parallel_sessions(tmp_path, monkeypatch):
    import memory_state
    import user_prompt_capture

    monkeypatch.setattr(memory_state, "STATE_DIR", tmp_path / "run")
    monkeypatch.setattr(memory_state, "STATE_FILE", tmp_path / "run" / "state.json")
    monkeypatch.setattr(memory_state, "LOCK_FILE", tmp_path / "run" / "state.json.lock")
    monkeypatch.setattr(user_prompt_capture, "update_state", memory_state.update_state)
    monkeypatch.setattr(user_prompt_capture, "HOOK_STATE_LOCK_TIMEOUT", 10.0)

    work = [(session, project) for session, project in (("s-a", "p-a"), ("s-b", "p-b")) for _ in range(20)]
    with ThreadPoolExecutor(max_workers=20) as pool:
        counts = list(pool.map(lambda item: user_prompt_capture._increment_prompt_count(*item), work))

    state = json.loads(memory_state.STATE_FILE.read_text(encoding="utf-8"))
    assert state["user_prompt_counts"] == {"s-a": 20, "s-b": 20}
    assert counts.count(20) == 2


def test_prompt_counter_uses_project_fallback_for_missing_session(tmp_path, monkeypatch):
    import memory_state
    import user_prompt_capture

    monkeypatch.setattr(memory_state, "STATE_DIR", tmp_path / "run")
    monkeypatch.setattr(memory_state, "STATE_FILE", tmp_path / "run" / "state.json")
    monkeypatch.setattr(memory_state, "LOCK_FILE", tmp_path / "run" / "state.json.lock")
    monkeypatch.setattr(user_prompt_capture, "update_state", memory_state.update_state)

    assert user_prompt_capture._increment_prompt_count("", "project-a") == 1
    assert user_prompt_capture._increment_prompt_count("unknown", "project-a") == 2
    state = json.loads(memory_state.STATE_FILE.read_text(encoding="utf-8"))
    assert state["user_prompt_counts"] == {"project:project-a": 2}


def test_prompt_bookkeeping_fails_open_quickly_when_state_lock_is_held(
    tmp_path, monkeypatch
):
    import memory_state
    import user_prompt_capture

    state_dir = tmp_path / "run"
    lock_file = state_dir / "state.json.lock"
    state_dir.mkdir()
    lock_file.write_text(str(os.getpid()), encoding="utf-8")
    monkeypatch.setattr(memory_state, "STATE_DIR", state_dir)
    monkeypatch.setattr(memory_state, "STATE_FILE", state_dir / "state.json")
    monkeypatch.setattr(memory_state, "LOCK_FILE", lock_file)
    monkeypatch.setattr(user_prompt_capture, "update_state", memory_state.update_state)

    started = time.perf_counter()
    count = user_prompt_capture._increment_prompt_count("session-a", "project-a")
    count_elapsed = time.perf_counter() - started

    started = time.perf_counter()
    claimed = user_prompt_capture._claim_prompt_dedupe("project-a", "hash-a")
    dedupe_elapsed = time.perf_counter() - started

    assert count == 0
    assert claimed is True
    assert count_elapsed < 0.75
    assert dedupe_elapsed < 0.75


def test_twentieth_prompt_spawns_nonblocking_flush(monkeypatch, tmp_path):
    import user_prompt_capture

    fake_root = tmp_path / "vault"
    fake_root.mkdir()
    project = tmp_path / "project"
    project.mkdir()
    spawned = []
    monkeypatch.setattr(user_prompt_capture, "ROOT", fake_root)
    monkeypatch.setattr(user_prompt_capture, "_increment_prompt_count", lambda *args: 20)
    monkeypatch.setattr(user_prompt_capture, "_rate_limited", lambda *a: False)
    monkeypatch.setattr(user_prompt_capture, "_append_prompt_tag", lambda *a: None)
    monkeypatch.setattr(user_prompt_capture, "_claim_prompt_dedupe", lambda *a: True)
    monkeypatch.setattr(
        user_prompt_capture, "spawn_detached", lambda args: spawned.append(args) or 123
    )

    rc = _run_capture_with_stdin(
        "user_prompt_capture",
        {
            "prompt": "twentieth meaningful prompt",
            "session_id": "session-20",
            "cwd": str(project),
            "transcript_path": str(tmp_path / "session.jsonl"),
        },
    )

    assert rc == 0
    assert len(spawned) == 1
    assert spawned[0][0] == sys.executable
    assert spawned[0][1] == str(fake_root / "scripts" / "flush_memory.py")
    assert spawned[0][2:] == [
        "--event", "pre-compact", "--session-id", "session-20",
        "--transcript", str(tmp_path / "session.jsonl"),
        "--trigger", "prompt-count-20",
    ]


def test_tenth_prompt_injects_short_advisory_with_hook_output_contract(
    monkeypatch, tmp_path, capsys
):
    import user_prompt_capture

    fake_root = tmp_path / "vault"
    fake_root.mkdir()
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setattr(user_prompt_capture, "ROOT", fake_root)
    monkeypatch.setattr(user_prompt_capture, "_increment_prompt_count", lambda *args: 10)
    monkeypatch.setattr(user_prompt_capture, "_build_advisory_refresh", lambda: "42 pages; 3 stale.")
    monkeypatch.setattr(user_prompt_capture, "_rate_limited", lambda *a: False)
    monkeypatch.setattr(user_prompt_capture, "_append_prompt_tag", lambda *a: None)
    monkeypatch.setattr(user_prompt_capture, "_claim_prompt_dedupe", lambda *a: True)

    _run_capture_with_stdin(
        "user_prompt_capture",
        {"prompt": "refresh my context now", "session_id": "s10", "cwd": str(project)},
    )

    assert json.loads(capsys.readouterr().out) == {
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": "42 pages; 3 stale.",
        }
    }


def test_short_advisory_refresh_includes_page_and_stale_counts(tmp_path, monkeypatch):
    import build_advisory

    notes = tmp_path / "notes"
    notes.mkdir()
    for index in range(4):
        (notes / f"page-{index}.md").write_text("---\ntype: concept\n---\n", encoding="utf-8")
    monkeypatch.setattr(build_advisory, "KNOWLEDGE", notes)
    monkeypatch.setattr(build_advisory, "_find_stale_pages", lambda: 2)

    refresh = build_advisory.build_advisory_refresh()

    assert "4 pages" in refresh
    assert "2 stale" in refresh
    assert len(refresh.split()) <= 50


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


def test_tool_capture_redacts_and_builds_envelope_before_append(monkeypatch, tmp_path):
    import post_tool_capture
    from event_envelope import build_event_envelope

    fake_root = tmp_path / "vault"
    fake_root.mkdir()
    project_cwd = tmp_path / "project"
    project_cwd.mkdir()
    secret = "sk-abcdefghijklmnopqrstuvwxyz012345"
    calls = []

    def observed_build(**kwargs):
        calls.append(("build", kwargs))
        return build_event_envelope(**kwargs)

    def observed_append(slug, session_id, tool, target):
        calls.append(("append", {"slug": slug, "session": session_id, "tool": tool, "target": target}))

    monkeypatch.setattr(post_tool_capture, "ROOT", fake_root)
    monkeypatch.setattr(post_tool_capture, "_compute_slug_from_cwd", lambda cwd: "test-slug")
    monkeypatch.setattr(post_tool_capture, "_rate_limited", lambda *args: False)
    monkeypatch.setattr(post_tool_capture, "_record_dedupe", lambda *args: None)
    monkeypatch.setattr(post_tool_capture, "build_event_envelope", observed_build)
    monkeypatch.setattr(post_tool_capture, "_append_tool_tag", observed_append)

    rc = _run_capture_with_stdin(
        "post_tool_capture",
        {
            "tool_name": "Bash",
            "tool_input": {"command": f"curl -H 'Authorization: Bearer {secret}' example.com"},
            "session_id": "session-1",
            "cwd": str(project_cwd),
        },
    )

    assert rc == 0
    assert [name for name, _ in calls] == ["build", "append"]
    assert calls[0][1]["event_type"] == "post_tool_use"
    assert secret not in calls[0][1]["payload"]["target"]
    assert secret not in calls[1][1]["target"]


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


def test_prompt_capture_dedupe_claim_is_atomic_under_concurrency(tmp_path, monkeypatch):
    import memory_state
    import user_prompt_capture

    state_dir = tmp_path / "run"
    monkeypatch.setattr(memory_state, "STATE_ROOT", tmp_path)
    monkeypatch.setattr(memory_state, "STATE_DIR", state_dir)
    monkeypatch.setattr(memory_state, "STATE_FILE", state_dir / "state.json")
    monkeypatch.setattr(memory_state, "LOCK_FILE", state_dir / "state.json.lock")
    monkeypatch.setattr(memory_state, "REPORTS_DIR", tmp_path / "logs")
    monkeypatch.setattr(user_prompt_capture, "STATE_ROOT", tmp_path)
    monkeypatch.setattr(user_prompt_capture, "HOOK_STATE_LOCK_TIMEOUT", 10.0)

    with ThreadPoolExecutor(max_workers=16) as pool:
        claims = list(
            pool.map(
                lambda _: user_prompt_capture._claim_prompt_dedupe("slug", "same-hash"),
                range(64),
            )
        )

    assert sum(claims) == 1


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
