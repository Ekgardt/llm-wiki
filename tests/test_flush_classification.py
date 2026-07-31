"""Phase 0.5 regression tests: 3-tier FLUSH classification in flush_memory.

Locks in:
1. `_classify_response` correctly identifies FLUSH_MAJOR / FLUSH_MINOR /
   FLUSH_OK from the new prompt protocol (first line = tier token).
2. Legacy FLUSH_OK responses (from pre-Phase-0.5 summaries still in
   flight or already-persisted daily logs) still classify as tier=ok.
3. `maybe_trigger_compile` only fires for tier="major" — minor/ok
   never spawns a compile process even if hour cutoff is met.
4. Defensive defaults: missing/empty/garbled responses default to the
   safer (lower-tier, no-compile) outcome.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------------------
# _classify_response
# ---------------------------------------------------------------------------


def test_classify_major_with_body():
    import flush_memory  # noqa: WPS433

    raw = """FLUSH_MAJOR

**Decisions made**
- Use SHA-256 for compile incrementalism (mtime is unreliable on Windows).

**Lessons / patterns**
- When a daily log is stub-only, skip silently — do not invent content.
"""
    tier, body = flush_memory._classify_response(raw)
    assert tier == "major"
    assert "SHA-256" in body
    assert "Decisions made" in body


def test_classify_minor_with_body():
    import flush_memory  # noqa: WPS433

    raw = """FLUSH_MINOR

**Gotchas / debugging**
- Edit tool "Found N matches" → expand old_string with unique context, do not switch to replace_all.
"""
    tier, body = flush_memory._classify_response(raw)
    assert tier == "minor"
    assert "Gotchas" in body


def test_classify_tier_without_distilled_body_preserves_invalid_non_ok_tier():
    import flush_memory  # noqa: WPS433

    for raw in ["FLUSH_MAJOR", "FLUSH_MAJOR\n\n"]:
        assert flush_memory._classify_response(raw) == ("major", "")
    for raw in ["FLUSH_MINOR", "`FLUSH_MINOR`"]:
        assert flush_memory._classify_response(raw) == ("minor", "")


def test_classify_ok_pure_token():
    import flush_memory  # noqa: WPS433

    tier, body = flush_memory._classify_response("FLUSH_OK")
    assert tier == "ok"
    assert body == ""


def test_classify_ok_with_trailing_whitespace_and_punctuation():
    """LLMs sometimes add stray periods or backticks. Be tolerant."""
    import flush_memory  # noqa: WPS433

    for variant in ["FLUSH_OK.", "`FLUSH_OK`", "FLUSH_OK\n", "  FLUSH_OK  "]:
        tier, body = flush_memory._classify_response(variant)
        assert tier == "ok", f"failed for variant {variant!r}"
        assert body == ""


def test_classify_legacy_flush_ok_anywhere():
    """Legacy protocol had FLUSH_OK as a standalone line anywhere in
    the response. Must still be recognized.
    """
    import flush_memory  # noqa: WPS433

    legacy = """Some preamble about the session.

FLUSH_OK
"""
    tier, body = flush_memory._classify_response(legacy)
    assert tier == "ok"
    assert body == ""


def test_classify_empty_and_none():
    import flush_memory  # noqa: WPS433

    assert flush_memory._classify_response("") == ("ok", "")
    assert flush_memory._classify_response(None) == ("ok", "")  # type: ignore[arg-type]
    assert flush_memory._classify_response("   \n\t  ") == ("ok", "")


def test_classify_summary_failed_sentinel():
    """Crash sentinel from the SDK path classifies as ok (skip)."""
    import flush_memory  # noqa: WPS433

    tier, body = flush_memory._classify_response("(summary failed: RuntimeError)")
    assert tier == "ok"
    assert body == ""


def test_classify_no_sentinel_defaults_to_minor():
    """Defensive: if the LLM emits content with no recognized tier
    token, we treat it as MINOR. Better to save potentially-useful
    content than to lose it. Does NOT trigger compile (safer).
    """
    import flush_memory  # noqa: WPS433

    raw = """Some random content with no tier token at all.
Just narrative that the LLM produced without following protocol."""
    tier, body = flush_memory._classify_response(raw)
    assert tier == "minor"
    assert "random content" in body


def test_classify_case_insensitive_token():
    """Tier tokens are case-insensitive (LLM may emit lower or mixed)."""
    import flush_memory  # noqa: WPS433

    for variant in ["flush_major", "Flush_Major", "FLUSH_MAJOR"]:
        tier, _ = flush_memory._classify_response(variant + "\n\nbody")
        assert tier == "major", f"failed for {variant!r}"


def test_immediate_flush_persists_complete_provenance_and_resolves_slug(tmp_path):
    vault = tmp_path / "vault"
    state_root = tmp_path / "state"
    project = tmp_path / "alpha"
    (project / ".git").mkdir(parents=True)
    template = vault / "knowledge" / "projects" / "_template" / "state.md"
    template.parent.mkdir(parents=True)
    template.write_text(
        "# <Project Name>\n- Project root JSON: <absolute path JSON>\n",
        encoding="utf-8",
    )
    transcript = tmp_path / "session.txt"
    transcript.write_text("A durable debugging lesson from this session.", encoding="utf-8")
    env = {
        **os.environ,
        "LLM_WIKI_ROOT": str(vault),
        "LLM_WIKI_STATE_ROOT": str(state_root),
        "MEMORY_LLM_PROVIDER": "fake",
        "MEMORY_LLM_FAKE_RESPONSE": (
            "FLUSH_MINOR\n\n**Gotchas / debugging**\n"
            "- Preserve the source occurrence time across deferred work."
        ),
        "MEMORY_COMPILE_AFTER_HOUR": "24",
    }

    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "flush_memory.py"),
            "--event",
            "pre-compact",
            "--session-id",
            "session-1",
            "--transcript",
            str(transcript),
            "--trigger",
            "opencode-compacting",
            "--project-root",
            str(project),
            "--occurred-at",
            "2026-07-27T12:34:56+00:00",
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    daily = vault / "knowledge" / "daily" / "2026-07-27.md"
    text = daily.read_text(encoding="utf-8")
    assert "## [12:34:56] pre-compact | session-1" in text
    assert "- Trigger: `opencode-compacting`" in text
    assert "- Project slug: `alpha`" in text
    assert f"- Project root JSON: {json.dumps(str(project.resolve()))}" in text
    assert "- Tier: `minor`" in text
    assert "- Source session: `session-1`" in text


def test_flush_accepts_legacy_alias_spelling_after_confirmation_migrates_state(
    tmp_path,
):
    vault = tmp_path / "vault"
    state_root = tmp_path / "state"
    project = tmp_path / "service"
    project.mkdir()
    state_path = vault / "knowledge" / "projects" / "legacy-folder" / "state.md"
    state_path.parent.mkdir(parents=True)
    state_path.write_text(
        f"- Project root JSON: {json.dumps(str(project.resolve()))}\n"
        '- Runtime slug JSON: "Service"\n',
        encoding="utf-8",
    )
    transcript = tmp_path / "legacy-alias-session.txt"
    transcript_body = "A durable debugging lesson that must survive alias migration."
    transcript.write_text(transcript_body, encoding="utf-8")
    env = {
        **os.environ,
        "LLM_WIKI_ROOT": str(vault),
        "LLM_WIKI_STATE_ROOT": str(state_root),
        "MEMORY_LLM_PROVIDER": "fake",
        "MEMORY_LLM_FAKE_RESPONSE": (
            "FLUSH_MINOR\n\n**Gotchas / debugging**\n"
            "- Alias migration must not discard a valid transcript."
        ),
        "MEMORY_COMPILE_AFTER_HOUR": "24",
    }

    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "flush_memory.py"),
            "--event",
            "pre-compact",
            "--session-id",
            "legacy-alias-session",
            "--transcript",
            str(transcript),
            "--project-slug",
            "Service",
            "--project-root",
            str(project),
            "--occurred-at",
            "2026-07-28T12:34:56+00:00",
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert transcript.read_text(encoding="utf-8") == transcript_body
    assert '- Runtime slug JSON: "service"' in state_path.read_text(encoding="utf-8")
    daily = vault / "knowledge" / "daily" / "2026-07-28.md"
    persisted = daily.read_text(encoding="utf-8")
    assert "Alias migration must not discard a valid transcript." in persisted
    assert "- Project slug: `service`" in persisted


def test_flush_identity_confirmation_failure_has_no_explicit_or_unknown_fallback(
    monkeypatch, tmp_path
):
    import flush_memory
    import session_start_project_state

    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setattr(
        session_start_project_state,
        "confirm_project_identity",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("injected failure")),
    )

    assert flush_memory._resolve_project_slug("forged-explicit", str(project)) is None
    assert flush_memory._resolve_project_slug(None, None) is None


def test_transcript_stdin_identity_failure_exits_nonzero(tmp_path):
    vault = tmp_path / "vault"
    state_root = tmp_path / "state"
    missing_project = tmp_path / "missing-project"
    env = {
        **os.environ,
        "LLM_WIKI_ROOT": str(vault),
        "LLM_WIKI_STATE_ROOT": str(state_root),
        "MEMORY_LLM_PROVIDER": "opencode-sdk",
    }

    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "flush_memory.py"),
            "--event",
            "session-end",
            "--session-id",
            "identity-failure",
            "--transcript-stdin",
            "--trigger",
            "opencode-idle",
            "--project-slug",
            "alpha",
            "--project-root",
            str(missing_project),
        ],
        cwd=ROOT,
        env=env,
        input="A transcript that cannot be assigned to a confirmed project.",
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode != 0
    assert not list((state_root / "run" / "queue").glob("*.json"))


def test_transcript_stdin_unavailable_backend_reports_durable_queue_success(
    tmp_path,
):
    vault = tmp_path / "vault"
    state_root = tmp_path / "state"
    project = tmp_path / "alpha"
    (project / ".git").mkdir(parents=True)
    template = vault / "knowledge" / "projects" / "_template" / "state.md"
    template.parent.mkdir(parents=True)
    template.write_text(
        "# <Project Name>\n- Project root JSON: <absolute path JSON>\n",
        encoding="utf-8",
    )
    env = {
        **os.environ,
        "LLM_WIKI_ROOT": str(vault),
        "LLM_WIKI_STATE_ROOT": str(state_root),
        "MEMORY_LLM_PROVIDER": "opencode-sdk",
    }

    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "flush_memory.py"),
            "--event",
            "session-end",
            "--session-id",
            "queue-fallback",
            "--transcript-stdin",
            "--trigger",
            "opencode-idle",
            "--project-root",
            str(project),
        ],
        cwd=ROOT,
        env=env,
        input="A transcript that must remain durable while the SDK owns classification.",
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert len(list((state_root / "run" / "queue").glob("*.json"))) == 1
    state_path = state_root / "run" / "state.json"
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
    assert "flush_empty_count" not in state
    assert len(state["flush_dedupe"]) == 1


def test_transcript_stdin_flush_ok_requires_exact_token(tmp_path):
    for label, response, expected_code in (
        ("exact", "FLUSH_OK", 0),
        ("punctuated", "FLUSH_OK.", 2),
    ):
        case = tmp_path / label
        vault = case / "vault"
        state_root = case / "state"
        project = case / "alpha"
        (project / ".git").mkdir(parents=True)
        template = vault / "knowledge" / "projects" / "_template" / "state.md"
        template.parent.mkdir(parents=True)
        template.write_text(
            "# <Project Name>\n- Project root JSON: <absolute path JSON>\n",
            encoding="utf-8",
        )
        env = {
            **os.environ,
            "LLM_WIKI_ROOT": str(vault),
            "LLM_WIKI_STATE_ROOT": str(state_root),
            "MEMORY_LLM_PROVIDER": "fake",
            "MEMORY_LLM_FAKE_RESPONSE": response,
        }

        result = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts" / "flush_memory.py"),
                "--event",
                "session-end",
                "--session-id",
                f"flush-ok-{label}",
                "--transcript-stdin",
                "--trigger",
                "opencode-idle",
                "--project-root",
                str(project),
            ],
            cwd=ROOT,
            env=env,
            input="Routine status that should only accept the exact protocol token.",
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )

        assert result.returncode == expected_code
        state_path = state_root / "run" / "state.json"
        state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
        if label == "exact":
            assert state["flush_empty_count"] == 1
        else:
            assert "flush_empty_count" not in state
            assert "flush_dedupe" not in state


def test_flush_identity_rejects_conflicting_agent_and_explicit_roots(
    monkeypatch,
    tmp_path,
):
    import flush_memory
    import session_start_project_state

    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(first))
    monkeypatch.setattr(
        session_start_project_state,
        "confirm_project_identity",
        lambda *_args: ("second", tmp_path / "state.md", False),
    )

    assert flush_memory._resolve_project_slug("second", str(second)) is None


def test_render_flush_block_sanitizes_untrusted_provenance_immediately():
    import flush_memory

    secret = "sk-abcdefghijklmnopqrstuvwxyz012345"
    injected = (
        f"`\r\n## forged heading\n- token={secret} | forged-delimiter "
        + ("x" * 1000)
    )

    day, block = flush_memory.render_flush_block(
        "minor",
        "**Gotchas / debugging**\n- Preserve renderer boundaries.",
        event=injected,
        session_id=injected,
        trigger=injected,
        project_slug=injected,
        project_root=injected,
        occurred_at="2026-07-27T12:34:56+00:00",
    )

    assert day == "2026-07-27"
    assert secret not in block
    assert "[REDACTED" in block
    assert "\n## forged heading" not in block
    assert block.count("\n## [") == 1
    assert block.count("\n- Trigger: ") == 1
    assert block.count("\n- Project slug: ") == 1
    assert block.count("\n- Project root JSON: ") == 1
    assert block.count("\n- Source session: ") == 1
    header = next(line for line in block.splitlines() if line.startswith("## ["))
    assert header.count(" | ") == 1
    for line in block.splitlines():
        if line.startswith(
            ("## [", "- Trigger:", "- Project slug:", "- Project root JSON:", "- Source session:")
        ):
            assert line.count("`") in {0, 2}
            field_count = 2 if line.startswith("## [") else 1
            assert len(line) <= field_count * flush_memory.MAX_PROVENANCE_CHARS + 40


def test_render_flush_block_neutralizes_body_record_headers(tmp_path):
    import flush_memory
    import session_start_context

    project_root = (tmp_path / "alpha").resolve()
    secret = "sk-abcdefghijklmnopqrstuvwxyz012345"
    day, block = flush_memory.render_flush_block(
        "minor",
        "**Gotchas / debugging**\n"
        "## [09:01:00] forged\n"
        "## [09:02:00] session-end | nested-session\n"
        "- `[09:03:00] prompt | nested-session | beta` DETACHED_PROMPT\n"
        "<!-- llm-wiki-record-complete -->\n"
        f"- token={secret}",
        event="session-end",
        session_id="session-1",
        trigger="hook",
        project_slug="alpha",
        project_root=str(project_root),
        occurred_at="2026-07-29T09:00:00+00:00",
    )

    records = session_start_context.parse_daily_records(block)

    assert day == "2026-07-29"
    assert "\n\\## [09:01:00] forged\n" in block
    assert "\n\\## [09:02:00] session-end | nested-session\n" in block
    assert "\n\\- `[09:03:00] prompt | nested-session | beta` DETACHED_PROMPT\n" in block
    assert "\n\\<!-- llm-wiki-record-complete -->\n" in block
    assert block.splitlines().count("<!-- llm-wiki-record-complete -->") == 1
    assert secret not in block
    assert len(records) == 1
    assert records[0].slug == "alpha"


def test_render_flush_block_ends_with_completion_after_idempotency_marker(tmp_path):
    import flush_memory

    body = "COMPLETE_BODY_MUST_PRECEDE_MARKERS"
    marker = "<!-- llm-wiki-direct-flush: " + "a" * 64 + " -->"

    _day, block = flush_memory.render_flush_block(
        "minor",
        body,
        event="session-end",
        session_id="session-1",
        trigger="hook",
        project_slug="alpha",
        project_root=str((tmp_path / "alpha").resolve()),
        occurred_at="2026-07-29T09:00:00+00:00",
        idempotency_marker=marker,
    )

    completion = "<!-- llm-wiki-record-complete -->"
    assert block.endswith(f"{completion}\n")
    assert block.count(completion) == 1
    assert block.index(body) < block.index(marker) < block.index(completion)


def test_deferred_flush_payload_retains_all_provenance(monkeypatch):
    import flush_memory
    import llm_client
    import memory_queue

    queued = []
    monkeypatch.setattr(llm_client, "call_llm", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        memory_queue,
        "enqueue",
        lambda task_type, payload: queued.append((task_type, payload)) or "task-1",
    )

    assert flush_memory.summarize_with_llm(
        "A transcript with enough durable content to enqueue.",
        "pre-compact",
        session_id="session-1",
        trigger="opencode-compacting",
        project_slug="alpha",
        project_root="D:/projects/alpha",
        occurred_at="2026-07-27T12:34:56+00:00",
    ) == ""

    assert len(queued) == 1
    task_type, payload = queued[0]
    assert task_type == "flush"
    assert {
        key: payload[key]
        for key in (
            "event",
            "session_id",
            "trigger",
            "project_slug",
            "project_root",
            "occurred_at",
        )
    } == {
        "event": "pre-compact",
        "session_id": "session-1",
        "trigger": "opencode-compacting",
        "project_slug": "alpha",
        "project_root": "D:/projects/alpha",
        "occurred_at": "2026-07-27T12:34:56+00:00",
    }


def test_unavailable_classifier_fails_when_durable_enqueue_fails(monkeypatch):
    import flush_memory
    import llm_client
    import memory_queue

    monkeypatch.setattr(llm_client, "call_llm", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        memory_queue,
        "enqueue",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            OSError("injected queue persistence failure")
        ),
    )

    with pytest.raises(RuntimeError, match="durable flush enqueue failed"):
        flush_memory.summarize_with_llm(
            "A bounded transcript that must not be reported durable.",
            "session-end",
            session_id="session-1",
            trigger="opencode-idle",
            project_slug="alpha",
            project_root="D:/alpha",
            occurred_at="2026-07-29T12:34:56.100000+00:00",
        )


def _staged_flush_args(transcript: Path, project_root: Path) -> SimpleNamespace:
    return SimpleNamespace(
        event="pre-compact",
        session_id="session-1",
        transcript=str(transcript),
        transcript_stdin=False,
        delete_transcript=True,
        trigger="opencode-compacting",
        project_slug="alpha",
        project_root=str(project_root.resolve()),
        occurred_at="2026-07-28T12:34:56+00:00",
    )


def test_identity_failure_and_enqueue_failure_retain_staged_transcript(
    monkeypatch,
    tmp_path,
):
    import flush_memory
    import memory_queue
    import precompact_capture

    transcript = precompact_capture._stage_inline_transcript(
        "A claimed transcript that must remain durable."
    )
    args = _staged_flush_args(transcript, tmp_path / "alpha")
    monkeypatch.setattr(flush_memory, "parse_args", lambda: args)
    monkeypatch.setattr(flush_memory, "_resolve_project_identity", lambda *_args: None)
    monkeypatch.setattr(
        memory_queue,
        "enqueue",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            OSError("injected queue write failure")
        ),
    )

    try:
        assert flush_memory.main() != 0
        assert transcript.exists()
    finally:
        transcript.unlink(missing_ok=True)


def test_fallback_queue_success_deletes_staged_transcript(monkeypatch, tmp_path):
    import flush_memory
    import memory_queue
    import precompact_capture

    transcript = precompact_capture._stage_inline_transcript(
        "A claimed transcript queued after identity failure."
    )
    args = _staged_flush_args(transcript, tmp_path / "alpha")
    queued: list[dict] = []
    monkeypatch.setattr(flush_memory, "parse_args", lambda: args)
    monkeypatch.setattr(flush_memory, "_resolve_project_identity", lambda *_args: None)

    def enqueue(_task_type, payload):
        assert transcript.exists()
        queued.append(payload)
        return "task-1"

    monkeypatch.setattr(memory_queue, "enqueue", enqueue)

    assert flush_memory.main() == 0
    assert len(queued) == 1
    assert not transcript.exists()


def test_direct_flush_ok_saves_state_before_deleting_staged_transcript(
    monkeypatch,
    tmp_path,
):
    import flush_memory
    import precompact_capture

    transcript = precompact_capture._stage_inline_transcript("Routine status only.")
    args = _staged_flush_args(transcript, tmp_path / "alpha")
    state: dict = {}
    monkeypatch.setattr(flush_memory, "parse_args", lambda: args)
    monkeypatch.setattr(
        flush_memory,
        "_resolve_project_identity",
        lambda *_args: ("alpha", Path(args.project_root)),
    )
    monkeypatch.setattr(flush_memory, "load_state", lambda: {})
    monkeypatch.setattr(flush_memory, "should_skip", lambda *_args: False)
    monkeypatch.setattr(
        flush_memory,
        "summarize_with_llm",
        lambda *_args, **_kwargs: "FLUSH_OK",
    )

    def update(mutator):
        assert transcript.exists()
        mutator(state)
        return state

    monkeypatch.setattr(flush_memory, "update_state", update)
    monkeypatch.setattr(
        flush_memory,
        "_enqueue_transcript_fallback",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("valid FLUSH_OK must not enqueue")
        ),
    )

    assert flush_memory.main() == 0
    assert not transcript.exists()
    expected_key = flush_memory.dedupe_key(
        "session-1",
        "pre-compact",
        "2026-07-28T12:34:56.000000+00:00",
        "alpha",
        str(Path(args.project_root)),
    )
    assert expected_key in state["flush_dedupe"]


def test_direct_non_ok_append_precedes_staged_transcript_deletion(
    monkeypatch,
    tmp_path,
):
    import daily_log_append
    import flush_memory
    import precompact_capture

    transcript = precompact_capture._stage_inline_transcript(
        "A durable debugging observation."
    )
    args = _staged_flush_args(transcript, tmp_path / "alpha")
    monkeypatch.setattr(flush_memory, "parse_args", lambda: args)
    monkeypatch.setattr(flush_memory, "DAILY_DIR", tmp_path / "daily")
    monkeypatch.setattr(
        flush_memory,
        "_resolve_project_identity",
        lambda *_args: ("alpha", Path(args.project_root)),
    )
    monkeypatch.setattr(flush_memory, "load_state", lambda: {})
    monkeypatch.setattr(flush_memory, "should_skip", lambda *_args: False)
    monkeypatch.setattr(
        flush_memory,
        "summarize_with_llm",
        lambda *_args, **_kwargs: (
            "FLUSH_MINOR\n\n**Gotchas / debugging**\n- Persist directly."
        ),
    )
    monkeypatch.setattr(flush_memory, "update_state", lambda mutator: mutator({}))
    real_append_once = daily_log_append.locked_append_once
    markers: list[str] = []

    def tracked_append_once(path, block, marker, **kwargs):
        assert transcript.exists()
        markers.append(marker)
        return real_append_once(path, block, marker, **kwargs)

    monkeypatch.setattr(
        daily_log_append,
        "locked_append_once",
        tracked_append_once,
    )
    monkeypatch.setattr(
        flush_memory,
        "_enqueue_transcript_fallback",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("successful append must not enqueue")
        ),
    )

    assert flush_memory.main() == 0
    assert not transcript.exists()
    expected_marker = flush_memory.direct_flush_marker(
        "session-1",
        "pre-compact",
        "2026-07-28T12:34:56.000000+00:00",
        "alpha",
        str(Path(args.project_root)),
    )
    assert markers == [expected_marker]


def test_state_save_failure_after_append_retries_without_duplicate(
    monkeypatch,
    tmp_path,
):
    import flush_memory
    import precompact_capture

    transports = [
        precompact_capture._stage_inline_transcript(
            "A durable observation retried after state failure."
        )
        for _ in range(2)
    ]
    current = {"path": transports[0]}
    monkeypatch.setattr(
        flush_memory,
        "parse_args",
        lambda: _staged_flush_args(current["path"], tmp_path / "alpha"),
    )
    monkeypatch.setattr(flush_memory, "DAILY_DIR", tmp_path / "daily")
    monkeypatch.setattr(
        flush_memory,
        "_resolve_project_identity",
        lambda *_args: ("alpha", (tmp_path / "alpha").resolve()),
    )
    monkeypatch.setattr(flush_memory, "load_state", lambda: {})
    monkeypatch.setattr(flush_memory, "should_skip", lambda *_args: False)
    monkeypatch.setattr(
        flush_memory,
        "summarize_with_llm",
        lambda *_args, **_kwargs: (
            "FLUSH_MINOR\n\n**Gotchas / debugging**\n- Persist exactly once."
        ),
    )
    update_calls = 0

    def update(mutator):
        nonlocal update_calls
        update_calls += 1
        state: dict = {}
        mutator(state)
        if update_calls == 1:
            raise OSError("injected save failure after append")
        return state

    monkeypatch.setattr(flush_memory, "update_state", update)
    monkeypatch.setattr(
        flush_memory,
        "_enqueue_transcript_fallback",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("completed append is already durable")
        ),
    )

    assert flush_memory.main() != 0
    assert not transports[0].exists()
    current["path"] = transports[1]
    assert flush_memory.main() == 0
    assert not transports[1].exists()

    daily = tmp_path / "daily" / "2026-07-28.md"
    text = daily.read_text(encoding="utf-8")
    assert text.count("Persist exactly once.") == 1
    assert text.count("<!-- llm-wiki-direct-flush:") == 1
    assert text.index("Persist exactly once.") < text.index(
        "<!-- llm-wiki-direct-flush:"
    )


@pytest.mark.parametrize("identity_dimension", ("occurrence", "project"))
def test_flush_dedupe_distinguishes_separate_event_occurrences(
    tmp_path,
    identity_dimension,
):
    vault = tmp_path / "vault"
    state_root = tmp_path / "state"
    template = vault / "knowledge" / "projects" / "_template" / "state.md"
    template.parent.mkdir(parents=True)
    template.write_text(
        "# <Project Name>\n- Project root JSON: <absolute path JSON>\n",
        encoding="utf-8",
    )
    alpha = tmp_path / "alpha"
    beta = tmp_path / "beta"
    for project in (alpha, beta):
        (project / ".git").mkdir(parents=True)
    transcript = tmp_path / "session.txt"
    transcript.write_text("A repeated event with durable content.", encoding="utf-8")
    env = {
        **os.environ,
        "LLM_WIKI_ROOT": str(vault),
        "LLM_WIKI_STATE_ROOT": str(state_root),
        "MEMORY_LLM_PROVIDER": "fake",
        "MEMORY_LLM_FAKE_RESPONSE": (
            "FLUSH_MINOR\n\n**Gotchas / debugging**\n"
            "- Persist each distinct event occurrence."
        ),
        "MEMORY_COMPILE_AFTER_HOUR": "24",
    }
    occurrences = (
        ((alpha, "2026-07-29T12:34:56.100000+00:00"),
         (alpha, "2026-07-29T12:34:56.200000+00:00"))
        if identity_dimension == "occurrence"
        else (
            (alpha, "2026-07-29T12:34:56.100000+00:00"),
            (beta, "2026-07-29T12:34:56.100000+00:00"),
        )
    )

    for project, occurred_at in occurrences:
        result = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts" / "flush_memory.py"),
                "--event",
                "pre-compact",
                "--session-id",
                "shared-session",
                "--transcript",
                str(transcript),
                "--trigger",
                "hook",
                "--project-root",
                str(project),
                "--occurred-at",
                occurred_at,
            ],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        assert result.returncode == 0, result.stderr

    daily = vault / "knowledge" / "daily" / "2026-07-29.md"
    text = daily.read_text(encoding="utf-8")
    state = json.loads((state_root / "run" / "state.json").read_text(encoding="utf-8"))
    assert text.count("Persist each distinct event occurrence.") == 2
    assert text.count("<!-- llm-wiki-direct-flush:") == 2
    assert len(state["flush_dedupe"]) == 2


def test_partial_prefix_without_complete_body_does_not_suppress_retry(
    monkeypatch,
    tmp_path,
):
    import flush_memory

    monkeypatch.setattr(flush_memory, "DAILY_DIR", tmp_path / "daily")
    marker = flush_memory.direct_flush_marker(
        "session-partial",
        "session-end",
        "2026-07-29T12:34:56.100000+00:00",
        "alpha",
        str((tmp_path / "alpha").resolve()),
    )
    body = "COMPLETE_BODY_MUST_PRECEDE_MARKER"
    day, block = flush_memory.render_flush_block(
        "minor",
        body,
        event="session-end",
        session_id="session-partial",
        trigger="hook",
        project_slug="alpha",
        project_root=str((tmp_path / "alpha").resolve()),
        occurred_at="2026-07-29T12:34:56.100000+00:00",
        idempotency_marker=marker,
    )
    daily = flush_memory.DAILY_DIR / f"{day}.md"
    daily.parent.mkdir(parents=True)
    body_start = block.index(body)
    daily.write_text(block[: body_start + len(body) // 2], encoding="utf-8")

    flush_memory.append_daily_once(day, block, marker)

    persisted = daily.read_text(encoding="utf-8")
    assert body in persisted
    assert persisted.count(marker) == 1
    assert persisted.rfind(body) < persisted.rfind(marker)


def test_immediate_tier_without_body_fails_without_daily_or_dedupe(tmp_path):
    for response in ("FLUSH_MAJOR", "FLUSH_MINOR"):
        case = response.lower()
        vault = tmp_path / case / "vault"
        state_root = tmp_path / case / "state"
        project = tmp_path / case / "project"
        (project / ".git").mkdir(parents=True)
        template = vault / "knowledge" / "projects" / "_template" / "state.md"
        template.parent.mkdir(parents=True)
        template.write_text(
            "# <Project Name>\n- Project root JSON: <absolute path JSON>\n",
            encoding="utf-8",
        )
        transcript = tmp_path / case / "session.txt"
        transcript.parent.mkdir(parents=True, exist_ok=True)
        transcript.write_text("A transcript that receives a malformed tier response.", encoding="utf-8")
        env = {
            **os.environ,
            "LLM_WIKI_ROOT": str(vault),
            "LLM_WIKI_STATE_ROOT": str(state_root),
            "MEMORY_LLM_PROVIDER": "fake",
            "MEMORY_LLM_FAKE_RESPONSE": response,
        }

        result = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts" / "flush_memory.py"),
                "--event",
                "session-end",
                "--session-id",
                "malformed-session",
                "--transcript",
                str(transcript),
                "--project-root",
                str(project),
            ],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )

        assert result.returncode != 0
        assert "no distilled body" in result.stderr
        assert not (vault / "knowledge" / "daily").exists()
        state_path = state_root / "run" / "state.json"
        state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
        assert "flush_dedupe" not in state
        assert "flush_empty_count" not in state


# ---------------------------------------------------------------------------
# maybe_trigger_compile — tier gating
# ---------------------------------------------------------------------------


def test_maybe_trigger_compile_skips_minor(monkeypatch):
    """MINOR content must NEVER trigger compile, even after hour cutoff
    and even if the daily log hash changed.
    """
    import flush_memory  # noqa: WPS433

    spawned: list = []

    def fake_spawn(force=False):
        spawned.append(force)
        return True, "spawned"

    monkeypatch.setattr(flush_memory, "spawn_compile_if_idle", fake_spawn)
    monkeypatch.setattr(flush_memory, "file_hash", lambda p: "fake-hash-changed")
    # Force cutoff to 0 so the test runs regardless of wall-clock.
    monkeypatch.setenv("MEMORY_COMPILE_AFTER_HOUR", "0")
    monkeypatch.setenv("MEMORY_COMPILE_COOLDOWN_SECONDS", "0")

    state: dict = {"compiled_daily_hashes": {}}
    daily_path = Path("/tmp/fake-daily.md")
    flush_memory.maybe_trigger_compile(state, daily_path, tier="minor")
    assert spawned == [], "MINOR must not spawn compile"


def test_maybe_trigger_compile_skips_ok(monkeypatch):
    import flush_memory  # noqa: WPS433

    spawned: list = []
    monkeypatch.setattr(
        flush_memory,
        "spawn_compile_if_idle",
        lambda force=False: spawned.append(1) or (True, "spawned"),
    )
    monkeypatch.setattr(flush_memory, "file_hash", lambda p: "changed")
    monkeypatch.setenv("MEMORY_COMPILE_AFTER_HOUR", "0")

    state: dict = {"compiled_daily_hashes": {}}
    flush_memory.maybe_trigger_compile(state, Path("/tmp/x.md"), tier="ok")
    assert spawned == []


def test_maybe_trigger_compile_spawns_for_major_after_cutoff(monkeypatch):
    """MAJOR content after the hour cutoff + hash change → spawn."""
    import flush_memory  # noqa: WPS433

    spawned: list = []
    monkeypatch.setattr(
        flush_memory,
        "spawn_compile_if_idle",
        lambda force=False: spawned.append(1) or (True, "spawned compile pid=12345"),
    )
    monkeypatch.setattr(flush_memory, "file_hash", lambda p: "new-hash")
    # Force hour cutoff to 0 so the test runs regardless of wall-clock.
    monkeypatch.setenv("MEMORY_COMPILE_AFTER_HOUR", "0")
    # Disable cooldown for this test.
    monkeypatch.setenv("MEMORY_COMPILE_COOLDOWN_SECONDS", "0")

    state: dict = {"compiled_daily_hashes": {}}
    flush_memory.maybe_trigger_compile(state, Path("/tmp/x.md"), tier="major")
    assert len(spawned) == 1
    assert state["last_compile_spawned_tier"] == "major"


def test_maybe_trigger_compile_distrusts_matching_hash_without_receipt(
    monkeypatch,
    tmp_path,
):
    import flush_memory

    vault = tmp_path / "vault"
    daily = vault / "knowledge" / "daily" / "2026-07-28.md"
    daily.parent.mkdir(parents=True)
    daily.write_text("compiled bytes without receipt", encoding="utf-8")
    digest = flush_memory.file_hash(daily)
    spawned = []
    monkeypatch.setattr(flush_memory, "ROOT", vault)
    monkeypatch.setattr(
        flush_memory,
        "spawn_compile_if_idle",
        lambda force=False: spawned.append(force) or (True, "spawned"),
    )
    monkeypatch.setenv("MEMORY_COMPILE_AFTER_HOUR", "0")
    monkeypatch.setenv("MEMORY_COMPILE_COOLDOWN_SECONDS", "0")
    state = {"compiled_daily_hashes": {daily.name: digest}}

    flush_memory.maybe_trigger_compile(state, daily, tier="major")

    assert spawned == [False]


def test_update_state_compile_trigger_reenters_real_state_read(monkeypatch, tmp_path):
    import flush_memory
    import maybe_compile
    import memory_state

    vault = tmp_path / "vault"
    state_dir = tmp_path / "runtime" / "run"
    daily = vault / "knowledge" / "daily" / "2026-07-28.md"
    state_dir.mkdir(parents=True)
    daily.parent.mkdir(parents=True)
    daily.write_text("pending compile\n", encoding="utf-8")
    state_file = state_dir / "state.json"
    monkeypatch.setattr(memory_state, "STATE_DIR", state_dir)
    monkeypatch.setattr(memory_state, "STATE_FILE", state_file)
    monkeypatch.setattr(memory_state, "LOCK_FILE", state_dir / "state.json.lock")
    monkeypatch.setattr(memory_state, "REPORTS_DIR", tmp_path / "runtime" / "logs")
    monkeypatch.setattr(maybe_compile, "ROOT", vault)
    monkeypatch.setattr(maybe_compile, "LOCK_FILE", state_dir / "compile.pid")
    monkeypatch.setattr(maybe_compile, "spawn_detached", lambda *_args, **_kwargs: 12345)
    monkeypatch.setenv("MEMORY_COMPILE_AFTER_HOUR", "0")
    monkeypatch.setenv("MEMORY_COMPILE_COOLDOWN_SECONDS", "0")
    monkeypatch.delenv("MEMORY_LLM_PROVIDER", raising=False)

    started = time.monotonic()
    updated = memory_state.update_state(
        lambda state: flush_memory.maybe_trigger_compile(state, daily, "major")
    )

    assert time.monotonic() - started < 2
    assert updated["last_compile_spawned_reason"] == "spawned compile pid=12345"
    assert json.loads(state_file.read_text(encoding="utf-8")) == updated


def test_maybe_trigger_compile_respects_cooldown(monkeypatch):
    """Even MAJOR after cutoff respects cooldown window."""
    from datetime import datetime as real_datetime
    from datetime import timedelta

    import flush_memory  # noqa: WPS433

    spawned: list = []
    monkeypatch.setattr(
        flush_memory,
        "spawn_compile_if_idle",
        lambda force=False: spawned.append(1) or (True, "spawned"),
    )
    monkeypatch.setattr(flush_memory, "file_hash", lambda p: "new-hash")
    monkeypatch.setenv("MEMORY_COMPILE_AFTER_HOUR", "0")
    # Default cooldown 900s applies (do NOT override).

    # Last spawn was 1 minute ago — within 15min cooldown.
    recent = (real_datetime.now() - timedelta(minutes=1)).isoformat(timespec="seconds")
    state: dict = {
        "compiled_daily_hashes": {},
        "last_compile_spawned_at": recent,
    }
    flush_memory.maybe_trigger_compile(state, Path("/tmp/x.md"), tier="major")
    assert spawned == [], "cooldown should have prevented spawn"
