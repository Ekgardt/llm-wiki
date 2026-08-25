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

from argparse import Namespace
from pathlib import Path

import pytest

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


def test_flush_cleans_ephemeral_transcript_on_early_return(monkeypatch, tmp_path):
    import flush_memory

    state_root = tmp_path / "state"
    transient = state_root / "cache" / "transient-transcripts" / "transient.txt"
    transient.parent.mkdir(parents=True)
    transient.write_text("redacted transcript", encoding="utf-8")
    monkeypatch.setattr(flush_memory, "STATE_ROOT", state_root)
    monkeypatch.setattr(
        flush_memory,
        "parse_args",
        lambda: Namespace(
            event="session-end",
            session_id="session-1",
            transcript=str(transient),
            trigger="opencode",
            ephemeral_transcript=True,
        ),
    )
    monkeypatch.setattr(flush_memory, "load_state", lambda: {})
    monkeypatch.setattr(flush_memory, "should_skip", lambda *args: True)

    assert flush_memory.main() == 0
    assert not transient.exists()


def test_provider_exception_queues_before_ephemeral_cleanup(monkeypatch, tmp_path):
    import flush_memory
    import llm_client
    import memory_queue

    state_root = tmp_path / "state"
    transient = state_root / "cache" / "transient-transcripts" / "transient.txt"
    transient.parent.mkdir(parents=True)
    transient.write_text("durable decision", encoding="utf-8")
    queued = []
    monkeypatch.setattr(flush_memory, "STATE_ROOT", state_root)
    monkeypatch.setattr(
        flush_memory,
        "parse_args",
        lambda: Namespace(
            event="session-end",
            session_id="session-1",
            transcript=str(transient),
            trigger="opencode",
            source_event_id="event-1",
            checkpoint_reason="session_end",
            ephemeral_transcript=True,
        ),
    )
    monkeypatch.setattr(flush_memory, "load_state", lambda: {})
    monkeypatch.setattr(
        flush_memory,
        "update_state",
        lambda callback: (_ for _ in ()).throw(AssertionError("must not record dedupe")),
    )
    monkeypatch.setattr(
        llm_client,
        "call_llm",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("provider failed")),
    )
    monkeypatch.setattr(
        memory_queue,
        "enqueue",
        lambda kind, payload: queued.append((kind, payload)) or "queued-1",
    )

    assert flush_memory.main() == 0
    assert [kind for kind, _payload in queued] == ["flush"]
    assert "durable decision" in queued[0][1]["prompt"]
    assert not transient.exists()


def test_provider_and_queue_failure_preserves_ephemeral_transcript(monkeypatch, tmp_path):
    import flush_memory
    import llm_client
    import memory_queue

    state_root = tmp_path / "state"
    transient = state_root / "cache" / "transient-transcripts" / "transient.txt"
    transient.parent.mkdir(parents=True)
    transient.write_text("durable decision", encoding="utf-8")
    monkeypatch.setattr(flush_memory, "STATE_ROOT", state_root)
    monkeypatch.setattr(
        flush_memory,
        "parse_args",
        lambda: Namespace(
            event="session-end",
            session_id="session-1",
            transcript=str(transient),
            trigger="opencode",
            source_event_id="event-1",
            checkpoint_reason="session_end",
            ephemeral_transcript=True,
        ),
    )
    monkeypatch.setattr(flush_memory, "load_state", lambda: {})
    monkeypatch.setattr(
        llm_client,
        "call_llm",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("provider failed")),
    )
    monkeypatch.setattr(
        memory_queue,
        "enqueue",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("queue failed")),
    )

    with pytest.raises(RuntimeError, match="not durably persisted"):
        flush_memory.main()
    assert transient.exists()


def test_flush_allows_transcript_under_external_state_cache(monkeypatch, tmp_path):
    import tempfile

    import flush_memory

    state_root = tmp_path / "external state"
    transcript = state_root / "cache" / "transient-transcripts" / "event.txt"
    transcript.parent.mkdir(parents=True)
    transcript.write_text("safe", encoding="utf-8")
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "different-home")
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path / "different-temp"))
    monkeypatch.setattr(flush_memory, "ROOT", tmp_path / "different-vault")
    monkeypatch.setattr(flush_memory, "STATE_ROOT", state_root, raising=False)

    assert flush_memory._transcript_path_allowed(transcript)


def test_flush_rejects_system_temp_and_broad_vault_cache(monkeypatch, tmp_path):
    import tempfile

    import flush_memory

    system_temp = tmp_path / "system-temp"
    broad_cache = tmp_path / "vault" / "cache"
    system_temp.mkdir()
    broad_cache.mkdir(parents=True)
    temp_transcript = system_temp / "session.jsonl"
    cache_transcript = broad_cache / "session.txt"
    temp_transcript.write_text("safe", encoding="utf-8")
    cache_transcript.write_text("safe", encoding="utf-8")
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(system_temp))
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    monkeypatch.setattr(flush_memory, "ROOT", tmp_path / "vault")
    monkeypatch.setattr(flush_memory, "STATE_ROOT", tmp_path / "state")

    assert not flush_memory._transcript_path_allowed(temp_transcript)
    assert not flush_memory._transcript_path_allowed(cache_transcript)


def test_flush_rejects_symlink_and_non_file_in_transient_directory(
    monkeypatch, tmp_path
):
    import flush_memory

    state_root = tmp_path / "state"
    transient_dir = state_root / "cache" / "transient-transcripts"
    transient_dir.mkdir(parents=True)
    target = transient_dir / "target.txt"
    link = transient_dir / "link.txt"
    directory = transient_dir / "directory.txt"
    target.write_text("safe", encoding="utf-8")
    directory.mkdir()
    monkeypatch.setattr(flush_memory, "STATE_ROOT", state_root)

    assert not flush_memory._transcript_path_allowed(directory)
    assert flush_memory._transcript_path_allowed(target)
    try:
        link.symlink_to(target)
    except OSError:
        return

    assert not flush_memory._transcript_path_allowed(link)


def test_flush_rejects_agent_config_files_outside_exact_session_subtrees(
    monkeypatch, tmp_path
):
    import flush_memory

    home = tmp_path / "home"
    rejected = [
        home / ".claude" / "settings.json",
        home / ".codex" / "auth.json",
        home / ".codex" / "config.json",
        home / ".config" / "opencode" / "opencode.json",
    ]
    for path in rejected:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("private", encoding="utf-8")
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setattr(flush_memory, "STATE_ROOT", tmp_path / "state")

    assert all(not flush_memory._transcript_path_allowed(path) for path in rejected)


def test_flush_accepts_only_exact_session_and_state_transient_subtrees(
    monkeypatch, tmp_path
):
    import flush_memory

    home = tmp_path / "home"
    state_root = tmp_path / "state"
    vault_root = tmp_path / "vault"
    accepted = [
        home / ".claude" / "projects" / "project" / "session.jsonl",
        home / ".codex" / "sessions" / "2026" / "session.jsonl",
        state_root / "cache" / "transient-transcripts" / "event.txt",
    ]
    rejected_vault_transient = (
        vault_root / "cache" / "transient-transcripts" / "event.txt"
    )
    for path in [*accepted, rejected_vault_transient]:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("safe", encoding="utf-8")
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setattr(flush_memory, "ROOT", vault_root)
    monkeypatch.setattr(flush_memory, "STATE_ROOT", state_root)

    assert all(flush_memory._transcript_path_allowed(path) for path in accepted)
    assert not flush_memory._transcript_path_allowed(rejected_vault_transient)


def test_flush_ephemeral_cleanup_rejects_paths_outside_transient_cache(
    monkeypatch, tmp_path
):
    import flush_memory

    protected = tmp_path / "protected.txt"
    protected.write_text("keep", encoding="utf-8")
    monkeypatch.setattr(flush_memory, "STATE_ROOT", tmp_path / "state")
    monkeypatch.setattr(
        flush_memory,
        "parse_args",
        lambda: Namespace(
            event="session-end",
            session_id="session-1",
            transcript=str(protected),
            trigger="opencode",
            ephemeral_transcript=True,
        ),
    )
    monkeypatch.setattr(flush_memory, "load_state", lambda: {})
    monkeypatch.setattr(flush_memory, "should_skip", lambda *args: True)

    assert flush_memory.main() == 0
    assert protected.exists()


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


def test_the_classifier_sees_both_ends_of_a_long_transcript(tmp_path, monkeypatch):
    """A tail-only window shows the least decisive part of a long session.

    Measured on forty real sessions (`NEW-62`): the full budget promoted one,
    a third of the budget promoted four. Work states its problem at the start
    and decides in the middle; the tail is tool output. This asserts the shape
    of the sample, not a tier — the tier is what the stand measures.
    """
    import flush_memory

    transcript = tmp_path / "session.jsonl"
    opening = "OPENING-DECISION " * 200
    closing = "CLOSING-NOISE " * 200
    middle = "filler " * 20_000
    transcript.write_text(opening + middle + closing, encoding="utf-8")
    monkeypatch.setattr(flush_memory, "_transcript_path_allowed", lambda path: True)

    excerpt = flush_memory.read_transcript_excerpt(transcript, max_chars=4_000)

    assert len(excerpt) <= 4_000 + len(flush_memory.TRANSCRIPT_GAP_NOTE)
    assert "OPENING-DECISION" in excerpt
    assert "CLOSING-NOISE" in excerpt
    assert flush_memory.TRANSCRIPT_GAP_NOTE in excerpt


def test_a_transcript_within_budget_is_passed_through_whole(tmp_path, monkeypatch):
    """No gap marker where nothing was dropped: the note must mean something."""
    import flush_memory

    transcript = tmp_path / "short.jsonl"
    transcript.write_text("one\ntwo\nthree\n", encoding="utf-8")
    monkeypatch.setattr(flush_memory, "_transcript_path_allowed", lambda path: True)

    excerpt = flush_memory.read_transcript_excerpt(transcript, max_chars=4_000)

    assert excerpt == "one\ntwo\nthree\n"
    assert flush_memory.TRANSCRIPT_GAP_NOTE not in excerpt
