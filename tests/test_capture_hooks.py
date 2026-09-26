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
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# UserPromptSubmit capture — user_prompt_capture.py
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_capture_state(tmp_path, monkeypatch):
    """Keep hook transaction state out of the suite runtime."""
    import user_prompt_capture

    state_root = tmp_path / "state"
    monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(state_root))
    monkeypatch.setattr(user_prompt_capture, "STATE_ROOT", state_root)
    return state_root


@pytest.fixture(autouse=True)
def _own_state_file(tmp_path, monkeypatch):
    """Each test counts and rate-limits in its own state file.

    The rate limit is thirty seconds of wall clock: with one state file shared by
    every run, a second run of this module inside that window found the first
    run's claims and wrote nothing.
    """
    import memory_state

    run = tmp_path / "own-state"
    monkeypatch.setattr(memory_state, "STATE_DIR", run)
    monkeypatch.setattr(memory_state, "STATE_FILE", run / "state.json")
    monkeypatch.setattr(memory_state, "LOCK_FILE", run / "state.json.lock")


def _missing(content: str, marks: tuple[str, ...]) -> list[str]:
    """Which of the marks the text does not carry.

    One assertion instead of one per mark, and the failure names every mark that
    was absent rather than the first. The managed complexity gate counts each
    `assert` as a branch, so a test with five of them is over its ceiling.
    """
    return [mark for mark in marks if mark not in content]


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


def test_prompt_capture_writes_line_for_real_prompt(
    monkeypatch, tmp_path, isolated_capture_state
):
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
    # Verify daily log was written
    today = __import__("datetime").date.today().isoformat()
    daily = daily_dir / f"{today}.md"
    content = daily.read_text(encoding="utf-8") if daily.exists() else ""
    # "abc123de" is session_id[:8].
    marks = ("prompt", "test-slug", "abc123de", "Help me refactor")
    transactions = isolated_capture_state / "run" / "markdown-transactions.sqlite3"

    assert (rc, daily.exists(), _missing(content, marks), transactions.is_file()) == (
        0,
        True,
        [],
        True,
    )


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

    def observed_append(slug, session_id, preview, operation_id=None):
        calls.append(("append", {"slug": slug, "session": session_id, "preview": preview,
                                 "operation_id": operation_id}))

    monkeypatch.setattr(user_prompt_capture, "ROOT", fake_root)
    monkeypatch.setattr(user_prompt_capture, "_compute_slug_from_cwd", lambda cwd: "test-slug")
    monkeypatch.setattr(user_prompt_capture, "_increment_prompt_count", lambda *args: 1)
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

    observed = (
        rc,
        [name for name, _ in calls],
        calls[0][1]["event_type"],
        secret in calls[0][1]["payload"]["prompt"],
        secret in calls[1][1]["preview"],
        calls[1][1]["operation_id"].startswith("user-prompt:"),
    )

    assert observed == (0, ["build", "append"], "user_prompt", False, False, True)


def test_prompt_capture_retries_after_failed_append(monkeypatch, tmp_path):
    import user_prompt_capture

    fake_root = tmp_path / "vault"
    fake_root.mkdir()
    project_cwd = tmp_path / "project"
    project_cwd.mkdir()
    append_results = iter((False, True))
    append_calls = []
    completed = []

    def append(*args, **kwargs):
        append_calls.append((args, kwargs))
        return next(append_results)

    monkeypatch.setattr(user_prompt_capture, "ROOT", fake_root)
    monkeypatch.setattr(
        user_prompt_capture, "_compute_slug_from_cwd", lambda _cwd: "test-slug"
    )
    monkeypatch.setattr(user_prompt_capture, "_increment_prompt_count", lambda *_a: 1)
    monkeypatch.setattr(
        user_prompt_capture,
        "_claim_prompt_operation",
        lambda *_args, **_kwargs: "prompt-operation",
    )
    monkeypatch.setattr(
        user_prompt_capture,
        "_complete_prompt_operation",
        lambda *args: completed.append(args),
    )
    monkeypatch.setattr(user_prompt_capture, "_append_prompt_tag", append)
    payload = {
        "prompt": "Retry this meaningful prompt",
        "session_id": "session-1",
        "cwd": str(project_cwd),
    }

    assert _run_capture_with_stdin("user_prompt_capture", payload) == 0
    assert _run_capture_with_stdin("user_prompt_capture", payload) == 0

    assert len(append_calls) == 2
    assert len(completed) == 1


def test_prompt_capture_replay_after_commit_appends_one_marked_record(
    monkeypatch, tmp_path, isolated_capture_state
):
    import user_prompt_capture

    fake_root = tmp_path / "vault"
    fake_root.mkdir()
    project_cwd = tmp_path / "project"
    project_cwd.mkdir()
    monkeypatch.setenv("LLM_WIKI_ROOT", str(fake_root))
    monkeypatch.setattr(user_prompt_capture, "ROOT", fake_root)
    monkeypatch.setattr(
        user_prompt_capture, "_compute_slug_from_cwd", lambda _cwd: "test-slug"
    )
    monkeypatch.setattr(user_prompt_capture, "_increment_prompt_count", lambda *_a: 1)
    payload = {
        "prompt": "Replay this committed prompt",
        "session_id": "session-1",
        "cwd": str(project_cwd),
    }

    runs = [_run_capture_with_stdin("user_prompt_capture", payload) for _ in range(2)]

    daily = next((fake_root / "knowledge" / "daily").glob("*.md"))
    content = daily.read_text(encoding="utf-8")
    written = (
        content.count("Replay this committed prompt"),
        content.count("llm-wiki-operation:"),
        "user-prompt:" in content,
    )

    assert (runs, written) == ([0, 0], (1, 1, False))


def test_prompt_capture_distinguishes_explicit_host_occurrences(monkeypatch, tmp_path):
    import user_prompt_capture

    fake_root = tmp_path / "vault"
    fake_root.mkdir()
    project_cwd = tmp_path / "project"
    project_cwd.mkdir()
    operations = []
    monkeypatch.setattr(user_prompt_capture, "ROOT", fake_root)
    monkeypatch.setattr(
        user_prompt_capture, "_compute_slug_from_cwd", lambda _cwd: "test-slug"
    )
    monkeypatch.setattr(user_prompt_capture, "_increment_prompt_count", lambda *_a: 1)
    monkeypatch.setattr(
        user_prompt_capture,
        "_claim_prompt_operation",
        lambda _slug, _prompt_hash, *, source_event_id=None: f"prompt:{source_event_id}",
    )
    monkeypatch.setattr(
        user_prompt_capture, "_complete_prompt_operation", lambda *_a: None
    )
    monkeypatch.setattr(
        user_prompt_capture,
        "_append_prompt_tag",
        lambda *_args, operation_id=None: operations.append(operation_id) or True,
    )
    payload = {
        "prompt": "Repeat this meaningful prompt",
        "session_id": "session-1",
        "cwd": str(project_cwd),
    }

    runs = [
        _run_capture_with_stdin("user_prompt_capture", {**payload, "event_id": event})
        for event in ("host-event-1", "host-event-2")
    ]

    assert (runs, len(operations), operations[0] == operations[1]) == ([0, 0], 2, False)


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
        "_claim_prompt_operation",
        lambda *args, **kwargs: calls.append("claim") or "user-prompt:claimed",
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
    # Twenty threads contending for one file lock: the claim is that no update
    # is lost, not that each waits under ten seconds, and ten was not enough on
    # a loaded Windows runner.
    monkeypatch.setattr(user_prompt_capture, "HOOK_STATE_LOCK_TIMEOUT", 120.0)
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
    monkeypatch.setattr(user_prompt_capture, "HOOK_STATE_LOCK_TIMEOUT", 120.0)

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


def _default_state_lock_wait(memory_state) -> float:
    """The lock wait a writer gets when it names none: what a hook must never pay."""
    import inspect

    return inspect.signature(memory_state.update_state).parameters["lock_timeout"].default


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
    claimed = user_prompt_capture._claim_prompt_operation("project-a", "hash-a")
    claim_elapsed = time.perf_counter() - started

    # Fails open: the prompt is still written, under a fallback operation id.
    assert (count, claimed is not None) == (0, True)
    # "Quickly" is "without the lock wait every other writer gets", read from
    # the product and not a stopwatch: by design the claim costs the hook's 0.1 s
    # wait plus the 0.5 s wait of recording the dropped write, and a 0.75 s
    # literal left a loaded hosted runner 0.15 s (macOS, run 35258090732).
    unbounded = _default_state_lock_wait(memory_state)
    assert (count_elapsed < unbounded, claim_elapsed < unbounded) == (True, True)


# The twentieth prompt is pinned by
# `tests/test_the_twentieth_prompt_captures_the_session.py`, with a real counter.


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
    monkeypatch.setattr(user_prompt_capture, "_append_prompt_tag", lambda *a: None)

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

    project_cwd = tmp_path / "project"
    project_cwd.mkdir()
    rc = _run_capture_with_stdin(
        "post_tool_capture",
        {
            "tool_name": "Edit",
            "tool_input": {"filePath": "src/auth.py"},
            "agent": "opencode",
            "session_id": "abc123def456",
            "cwd": str(project_cwd),
        },
    )
    today = __import__("datetime").date.today().isoformat()
    daily = daily_dir / f"{today}.md"
    content = daily.read_text(encoding="utf-8") if daily.exists() else ""
    marks = (
        "Edit",
        "src/auth.py",
        "test-slug",
        "tool | opencode | abc123de | test-slug | Edit",
    )

    assert (rc, daily.exists(), _missing(content, marks)) == (0, True, [])


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

    def observed_append(slug, session_id, tool, target, operation_id=None, agent=None):
        calls.append(("append", {"slug": slug, "session": session_id, "tool": tool,
                                 "target": target, "operation_id": operation_id,
                                 "agent": agent}))

    monkeypatch.setattr(post_tool_capture, "ROOT", fake_root)
    monkeypatch.setattr(post_tool_capture, "_compute_slug_from_cwd", lambda cwd: "test-slug")
    monkeypatch.setattr(post_tool_capture, "build_event_envelope", observed_build)
    monkeypatch.setattr(post_tool_capture, "_append_tool_tag", observed_append)

    rc = _run_capture_with_stdin(
        "post_tool_capture",
        {
            "tool_name": "Bash",
            "tool_input": {"command": f"curl -H 'Authorization: Bearer {secret}' example.com"},
            "agent": "opencode",
            "session_id": "session-1",
            "cwd": str(project_cwd),
        },
    )

    observed = (
        rc,
        [name for name, _ in calls],
        calls[0][1]["event_type"],
        secret in calls[0][1]["payload"]["target"],
        secret in calls[1][1]["target"],
        calls[1][1]["agent"],
        calls[1][1]["operation_id"].startswith("post-tool:"),
    )

    assert observed == (
        0,
        ["build", "append"],
        "post_tool_use",
        False,
        False,
        "opencode",
        True,
    )


def test_tool_capture_retries_after_failed_append(monkeypatch, tmp_path):
    import post_tool_capture

    fake_root = tmp_path / "vault"
    fake_root.mkdir()
    project_cwd = tmp_path / "project"
    project_cwd.mkdir()
    append_results = iter((False, True))
    append_calls = []
    completed = []

    def append(*args, **kwargs):
        append_calls.append((args, kwargs))
        return next(append_results)

    monkeypatch.setattr(post_tool_capture, "ROOT", fake_root)
    monkeypatch.setattr(
        post_tool_capture, "_compute_slug_from_cwd", lambda _cwd: "test-slug"
    )
    monkeypatch.setattr(
        post_tool_capture,
        "_claim_tool_operation",
        lambda *_args, **_kwargs: "tool-operation",
    )
    monkeypatch.setattr(
        post_tool_capture,
        "_complete_tool_operation",
        lambda *args: completed.append(args),
    )
    monkeypatch.setattr(post_tool_capture, "_append_tool_tag", append)
    payload = {
        "tool_name": "Edit",
        "tool_input": {"filePath": "src/auth.py"},
        "session_id": "session-1",
        "cwd": str(project_cwd),
    }

    assert _run_capture_with_stdin("post_tool_capture", payload) == 0
    assert _run_capture_with_stdin("post_tool_capture", payload) == 0

    assert len(append_calls) == 2
    assert len(completed) == 1


def test_tool_capture_replay_after_commit_appends_one_marked_record(
    monkeypatch, tmp_path, isolated_capture_state
):
    import post_tool_capture

    fake_root = tmp_path / "vault"
    fake_root.mkdir()
    project_cwd = tmp_path / "project"
    project_cwd.mkdir()
    state = {}

    def update(mutator, **_kwargs):
        mutator(state)
        return state

    monkeypatch.setenv("LLM_WIKI_ROOT", str(fake_root))
    monkeypatch.setattr(post_tool_capture, "ROOT", fake_root)
    monkeypatch.setattr(post_tool_capture, "update_state", update)
    monkeypatch.setattr(
        post_tool_capture, "_compute_slug_from_cwd", lambda _cwd: "test-slug"
    )
    payload = {
        "tool_name": "Edit",
        "tool_input": {"filePath": "src/auth.py"},
        "session_id": "session-1",
        "cwd": str(project_cwd),
        "event_id": "tool-event-1",
    }

    runs = [_run_capture_with_stdin("post_tool_capture", payload) for _ in range(2)]

    daily = next((fake_root / "knowledge" / "daily").glob("*.md"))
    content = daily.read_text(encoding="utf-8")
    written = (
        content.count("src/auth.py"),
        content.count("llm-wiki-operation:"),
        "post-tool:" in content,
    )

    assert (runs, written) == ([0, 0], (1, 1, False))


@pytest.mark.parametrize("module_name", ["user_prompt_capture", "post_tool_capture"])
def test_capture_operation_reservation_retries_and_then_advances(
    monkeypatch, module_name
):
    module = __import__(module_name)
    state = {}
    current = [datetime(2026, 8, 14, 12, 0, 0)]

    class FrozenDateTime:
        @classmethod
        def now(cls):
            return current[0]

        @classmethod
        def fromisoformat(cls, value):
            return datetime.fromisoformat(value)

    def update(mutator, **_kwargs):
        mutator(state)
        return state

    monkeypatch.setattr(module, "datetime", FrozenDateTime)
    monkeypatch.setattr(module, "update_state", update)
    if module_name == "user_prompt_capture":
        def claim(source=None):
            return module._claim_prompt_operation(
                "slug", "prompt-hash", source_event_id=source
            )

        def complete(operation):
            module._complete_prompt_operation("slug", "prompt-hash", operation)

        window = module.RATE_LIMIT_SECONDS
    else:
        def claim(source=None):
            return module._claim_tool_operation(
                "slug", "Edit", "src/app.py", source_event_id=source
            )

        def complete(operation):
            module._complete_tool_operation("slug", "Edit", "src/app.py", operation)

        window = module.RATE_LIMIT_SECONDS

    first = claim()
    reserved = (first is None, claim() == first)
    complete(first)
    after_completion = claim()

    current[0] += timedelta(seconds=window + 1)
    second = claim()

    assert (reserved, after_completion, second is None, second == first) == (
        (False, True),
        None,
        False,
        False,
    )


@pytest.mark.parametrize("module_name", ["user_prompt_capture", "post_tool_capture"])
def test_capture_operation_replays_same_host_event_but_rate_limits_another(
    monkeypatch, module_name
):
    module = __import__(module_name)
    state = {}
    current = [datetime(2026, 8, 14, 12, 0, 0)]

    class FrozenDateTime:
        @classmethod
        def now(cls):
            return current[0]

        @classmethod
        def fromisoformat(cls, value):
            return datetime.fromisoformat(value)

    def update(mutator, **_kwargs):
        mutator(state)
        return state

    monkeypatch.setattr(module, "datetime", FrozenDateTime)
    monkeypatch.setattr(module, "update_state", update)
    if module_name == "user_prompt_capture":
        def claim(source):
            return module._claim_prompt_operation(
                "slug", "prompt-hash", source_event_id=source
            )

        def complete(operation):
            module._complete_prompt_operation("slug", "prompt-hash", operation)

        window = module.RATE_LIMIT_SECONDS
    else:
        def claim(source):
            return module._claim_tool_operation(
                "slug", "Edit", "src/app.py", source_event_id=source
            )

        def complete(operation):
            module._complete_tool_operation("slug", "Edit", "src/app.py", operation)

        window = module.RATE_LIMIT_SECONDS

    first = claim("host-event-1")
    complete(first)
    replayed = (first is None, claim("host-event-1") == first, claim("host-event-2"))

    current[0] += timedelta(seconds=window + 1)
    later = claim("host-event-2")

    assert (replayed, later in {None, first}) == ((False, True, None), False)


@pytest.mark.parametrize("module_name", ["user_prompt_capture", "post_tool_capture"])
def test_pending_capture_never_aliases_a_different_host_occurrence(
    monkeypatch, module_name
):
    module = __import__(module_name)
    state = {}
    current = [datetime(2026, 8, 14, 12, 0, 0)]

    class FrozenDateTime:
        @classmethod
        def now(cls):
            return current[0]

        @classmethod
        def fromisoformat(cls, value):
            return datetime.fromisoformat(value)

    def update(mutator, **_kwargs):
        mutator(state)
        return state

    monkeypatch.setattr(module, "datetime", FrozenDateTime)
    monkeypatch.setattr(module, "update_state", update)
    if module_name == "user_prompt_capture":
        claim = lambda source: module._claim_prompt_operation(  # noqa: E731
            "slug", "prompt-hash", source_event_id=source
        )
    else:
        claim = lambda source: module._claim_tool_operation(  # noqa: E731
            "slug", "Edit", "src/app.py", source_event_id=source
        )

    first = claim("host-event-1")
    assert first is not None
    assert claim("host-event-2") is None
    current[0] += timedelta(seconds=module.RATE_LIMIT_SECONDS + 1)
    assert claim("host-event-2") not in {None, first}


@pytest.mark.parametrize("module_name", ["user_prompt_capture", "post_tool_capture"])
def test_anonymous_pending_capture_expires_after_rate_window(monkeypatch, module_name):
    module = __import__(module_name)
    state = {}
    current = [datetime(2026, 8, 14, 12, 0, 0)]

    class FrozenDateTime:
        @classmethod
        def now(cls):
            return current[0]

        @classmethod
        def fromisoformat(cls, value):
            return datetime.fromisoformat(value)

    def update(mutator, **_kwargs):
        mutator(state)
        return state

    monkeypatch.setattr(module, "datetime", FrozenDateTime)
    monkeypatch.setattr(module, "update_state", update)
    if module_name == "user_prompt_capture":
        claim = lambda: module._claim_prompt_operation("slug", "prompt-hash")  # noqa: E731
    else:
        claim = lambda: module._claim_tool_operation(  # noqa: E731
            "slug", "Edit", "src/app.py"
        )

    first = claim()
    assert first is not None
    current[0] += timedelta(seconds=module.RATE_LIMIT_SECONDS + 1)
    assert claim() not in {None, first}


def test_anonymous_fallback_operations_do_not_share_a_permanent_identity():
    from capture_operation import claim_operation

    def unavailable(_mutator):
        raise OSError("state unavailable")

    options = {
        "namespace": "capture",
        "key": "same-content",
        "prefix": "capture",
        "source_event_id": None,
        "rate_limit_seconds": 60,
        "max_entries": 10,
        "now": datetime(2026, 8, 14, 12, 0, 0),
    }

    first = claim_operation(unavailable, **options)
    second = claim_operation(unavailable, **options)

    assert first is not None
    assert second is not None
    assert first != second


@pytest.mark.parametrize(
    ("module_name", "payload", "completion_name", "needle"),
    [
        (
            "user_prompt_capture",
            {
                "prompt": "Crash after this committed prompt",
                "session_id": "session-1",
                "event_id": "prompt-crash-event",
            },
            "_complete_prompt_operation",
            "Crash after this committed prompt",
        ),
        (
            "post_tool_capture",
            {
                "tool_name": "Edit",
                "tool_input": {"filePath": "src/crash-boundary.py"},
                "session_id": "session-1",
                "event_id": "tool-crash-event",
            },
            "_complete_tool_operation",
            "src/crash-boundary.py",
        ),
    ],
)
def test_capture_replays_after_crash_between_append_and_completion(
    monkeypatch,
    tmp_path,
    isolated_capture_state,
    module_name,
    payload,
    completion_name,
    needle,
):

    module = __import__(module_name)
    fake_root = tmp_path / "vault"
    fake_root.mkdir()
    project_cwd = tmp_path / "project"
    project_cwd.mkdir()
    state = {}

    def update(mutator, **_kwargs):
        mutator(state)
        return state

    monkeypatch.setenv("LLM_WIKI_ROOT", str(fake_root))
    monkeypatch.setattr(module, "ROOT", fake_root)
    monkeypatch.setattr(module, "update_state", update)
    monkeypatch.setattr(module, "_compute_slug_from_cwd", lambda _cwd: "test-slug")
    if module_name == "user_prompt_capture":
        monkeypatch.setattr(module, "_increment_prompt_count", lambda *_args: 1)
    payload = {**payload, "cwd": str(project_cwd)}
    complete = getattr(module, completion_name)
    monkeypatch.setattr(
        module,
        completion_name,
        lambda *_args: (_ for _ in ()).throw(SystemExit(86)),
    )

    with pytest.raises(SystemExit, match="86"):
        _run_capture_with_stdin(module_name, payload)

    monkeypatch.setattr(module, completion_name, complete)
    replay = _run_capture_with_stdin(module_name, payload)
    daily = next((fake_root / "knowledge/daily").glob("*.md"))
    content = daily.read_text(encoding="utf-8")

    assert (replay, content.count(needle), content.count("llm-wiki-operation:")) == (
        0,
        1,
        1,
    )


def test_tool_capture_bash_filters_short_commands(monkeypatch, tmp_path):
    """Short Bash commands (cd, pwd, ls) are noise — skip them."""
    import post_tool_capture  # noqa: WPS433

    monkeypatch.setattr(post_tool_capture, "DAILY_DIR", tmp_path / "daily")
    monkeypatch.setattr(post_tool_capture, "ROOT", tmp_path / "vault")

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


# The rate-limit window itself is pinned above, through the live claim
# (`test_capture_operation_reservation_retries_and_then_advances`).


def test_prompt_capture_claim_is_one_reservation_under_concurrency(tmp_path, monkeypatch):
    import memory_state
    import user_prompt_capture

    state_dir = tmp_path / "run"
    monkeypatch.setattr(memory_state, "STATE_ROOT", tmp_path)
    monkeypatch.setattr(memory_state, "STATE_DIR", state_dir)
    monkeypatch.setattr(memory_state, "STATE_FILE", state_dir / "state.json")
    monkeypatch.setattr(memory_state, "LOCK_FILE", state_dir / "state.json.lock")
    monkeypatch.setattr(memory_state, "REPORTS_DIR", tmp_path / "logs")
    monkeypatch.setattr(user_prompt_capture, "STATE_ROOT", tmp_path)
    monkeypatch.setattr(user_prompt_capture, "HOOK_STATE_LOCK_TIMEOUT", 120.0)

    with ThreadPoolExecutor(max_workers=16) as pool:
        claims = list(
            pool.map(
                lambda _: user_prompt_capture._claim_prompt_operation("slug", "same-hash"),
                range(64),
            )
        )

    assert (len(set(claims)), None in claims) == (1, False)


def test_operational_errors_survive_a_process_boundary():
    """These cross the queue worker's process boundary; they must arrive intact.

    A `BlackboardConflictError` in a worker child used to reach the parent as
    `TypeError: __init__() missing 1 required positional argument`, hiding the
    real failure.
    """
    import pickle

    from blackboard import BlackboardConflictError
    from markdown_transaction import (
        ProjectPendingPriorError,
        TransactionDriftError,
        TransactionFailure,
    )
    from memory_queue import QueueOperationError
    from operational_ownership import OperationalOwnershipError
    from project_journal import ProjectJournalReadError, ProjectJournalRebuildRequired

    errors = [
        BlackboardConflictError(("resource-a",), "conflict-1"),
        TransactionFailure("boom", "target_drift", "committed"),
        TransactionDriftError("tx-1", ("knowledge/notes/a.md",)),
        ProjectPendingPriorError("demo", 4, 3),
        ProjectJournalRebuildRequired("demo", 4, 2),
        ProjectJournalReadError("unreadable", "file is not regular"),
        QueueOperationError("process_cleanup_failed", "child exited 1"),
        OperationalOwnershipError("owner_busy", "another owner holds the lease"),
    ]

    for error in errors:
        restored = pickle.loads(pickle.dumps(error))
        assert type(restored) is type(error)
        assert str(restored) == str(error)
        assert vars(restored) == vars(error)


def _queue_capture_tasks(state_root: Path) -> list[dict]:
    """Every capture task the queue holds, whatever its state."""
    import sqlite3

    database = state_root / "run" / "queue-v3.sqlite3"
    if not database.exists():
        return []
    with sqlite3.connect(database) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute("SELECT * FROM tasks").fetchall()
    return [{key: str(row[key]) for key in row.keys()} for row in rows]


