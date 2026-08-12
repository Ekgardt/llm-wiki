"""Phase 0.5 regression tests: 3-tier FLUSH classification in flush_memory.

Locks in:
1. `_classify_response` correctly identifies FLUSH_MAJOR / FLUSH_MINOR /
   FLUSH_OK from the strict prompt protocol and rejects malformed output.
2. Deferred malformed responses remain queued instead of being appended.
3. `maybe_trigger_compile` only fires for tier="major" — minor/ok
   never spawns a compile process even if hour cutoff is met.
4. Prompt exclusions match the OpenCode capture path.
"""
from __future__ import annotations

import io
import json
import os
import re
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
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

    minor_only = (
        "**Gotchas / debugging**\n- Symptom -> cause -> fix.\n\n"
        "**Open questions**\n- What remains?"
    )
    command_only = "**Commands / snippets**\n- `uv run pytest -q`"
    assert flush_memory._classify_response(f"FLUSH_MINOR\n\n{minor_only}")[0] == "minor"
    assert flush_memory._classify_response(f"FLUSH_MINOR\n\n{command_only}")[0] == "minor"
    with pytest.raises(ValueError, match="flush classification"):
        flush_memory._classify_response(f"FLUSH_MAJOR\n\n{minor_only}")
    with pytest.raises(ValueError, match="flush classification"):
        flush_memory._classify_response(f"FLUSH_MAJOR\n\n{command_only}")


def test_classify_minor_with_body():
    import flush_memory  # noqa: WPS433

    raw = """FLUSH_MINOR

**Gotchas / debugging**
- Edit tool "Found N matches" → expand old_string with unique context, do not switch to replace_all.
"""
    tier, body = flush_memory._classify_response(raw)
    assert tier == "minor"
    assert "Gotchas" in body


def test_classify_ok_accepts_only_the_exact_token_after_outer_whitespace():
    import flush_memory  # noqa: WPS433

    for variant in ["FLUSH_OK", "FLUSH_OK\n", "  FLUSH_OK  "]:
        tier, body = flush_memory._classify_response(variant)
        assert tier == "ok", f"failed for variant {variant!r}"
        assert body == ""


def test_classify_non_ok_accepts_each_allowed_section_heading():
    import flush_memory  # noqa: WPS433

    for tier, heading in (
        ("FLUSH_MAJOR", "Decisions made"),
        ("FLUSH_MAJOR", "Lessons / patterns"),
        ("FLUSH_MINOR", "Commands / snippets"),
        ("FLUSH_MINOR", "Gotchas / debugging"),
        ("FLUSH_MINOR", "Open questions"),
    ):
        parsed_tier, body = flush_memory._classify_response(
            f"{tier}\n\n**{heading}**\n- Durable content."
        )

        assert parsed_tier == tier.removeprefix("FLUSH_").lower()
        assert body.startswith(f"**{heading}**")

    tier, body = flush_memory._classify_response(
        "FLUSH_MAJOR\n\n"
        "**Decisions made**\n- Keep the strict grammar.\n\n"
        "**Lessons / patterns**\n- Validate before persistence.\n\n"
        "**Commands / snippets**\n- `uv run pytest -q`\n\n"
        "**Gotchas / debugging**\n- Symptom -> cause -> fix.\n\n"
        "**Open questions**\n- What remains?"
    )
    assert tier == "major"
    assert body.count("\n**") == 4


def test_classify_response_rejects_unknown_or_non_exact_tokens():
    import flush_memory  # noqa: WPS433

    for raw in (
        None,
        "",
        "   \n\t  ",
        "FLUSH_OK.",
        "`FLUSH_OK`",
        "flush_major\n\n**Decisions made**\n- Wrong token case.",
        "FLUSH_UNKNOWN\n\n**Open questions**\n- Unknown token.",
        "Some preamble.\nFLUSH_OK",
        "Some preamble.\nFLUSH_MINOR\n\n**Open questions**\n- Later token.",
        "**Open questions**\n- Missing token.",
        "(summary failed: RuntimeError)",
        '{"operations": []}',
    ):
        with pytest.raises(ValueError, match="flush classification"):
            flush_memory._classify_response(raw)  # type: ignore[arg-type]


def test_classify_response_rejects_missing_or_unstructured_bodies():
    import flush_memory  # noqa: WPS433

    for raw in (
        "FLUSH_OK\n\n**Open questions**\n- Unexpected body.",
        "FLUSH_MAJOR",
        "FLUSH_MINOR\n\n",
        "FLUSH_MINOR\n\n**Open questions**",
        "FLUSH_MINOR\n\n**Open questions**\n-   ",
        "FLUSH_MINOR\n\nProse before.\n**Open questions**\n- Durable question.",
        "FLUSH_MINOR\n\n**Open questions**\n- Durable question.\nProse after.",
        "FLUSH_MINOR\n\n**Open questions**\nNon-bullet section content.",
        "FLUSH_MINOR\n\nUseful prose without a recognized heading.",
        "FLUSH_MINOR\n\n**Other section**\n- Not allowed.",
        "FLUSH_MINOR\n\n**Gotchas / debugging**\n- Valid.\n\n**Other section**\n- Not allowed.",
        "FLUSH_MINOR\n\n**Decisions made**\n- Major-only section.",
        "FLUSH_MINOR\n\n**Lessons / patterns**\n- Major-only section.",
        "FLUSH_MAJOR\n\n**Other section**\n- Not allowed.",
    ):
        with pytest.raises(ValueError, match="flush classification"):
            flush_memory._classify_response(raw)


def test_noise_classes_flush_ok_and_jsonl_excludes_non_conversation_records(tmp_path):
    import flush_memory

    payload = flush_memory._build_flush_queue_payload(
        "A bounded transcript used to inspect the classifier request.",
        "session-end",
    )

    assert payload is not None
    prompt = " ".join(str(payload["prompt"]).split())
    assert (
        "FLUSH_MAJOR — requires a concrete DECISION with rationale or a reusable "
        "LESSON/pattern worth remembering across sessions."
    ) in prompt
    assert (
        "FLUSH_OK covers status/progress updates, audit/review verdicts or findings, "
        "file/path/code summaries, facts derivable from code/config, navigation, "
        "service/system prompts, shell telemetry, and other material that a future "
        "session can recover without memory."
    ) in prompt
    assert "single useful observation" not in prompt

    jsonl = tmp_path / "session.jsonl"
    jsonl.write_text(
        "\n".join(
            json.dumps(record)
            for record in (
                {
                    "type": "system",
                    "message": {
                        "role": "system",
                        "content": [{"type": "text", "text": "SERVICE_SYSTEM_EXCLUDED"}],
                    },
                },
                {"role": [], "content": "MALFORMED_ROLE_EXCLUDED"},
                {
                    "type": "user",
                    "message": {
                        "role": "user",
                        "content": [
                            {"type": [], "text": "MALFORMED_PART_EXCLUDED"},
                            {"type": "text", "text": "USER_TEXT_INCLUDED"},
                            {"type": "tool_result", "content": "SHELL_RESULT_EXCLUDED"},
                        ],
                    },
                },
                {
                    "type": "assistant",
                    "message": {
                        "role": "assistant",
                        "content": [
                            {"type": "text", "text": "ASSISTANT_TEXT_INCLUDED"},
                            {
                                "type": "tool_use",
                                "name": "Bash",
                                "input": {"command": "SHELL_COMMAND_EXCLUDED"},
                            },
                        ],
                    },
                },
                {"type": "tool", "role": "tool", "text": "TOOL_RECORD_EXCLUDED"},
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "assistant",
                        "content": [
                            {"type": "output_text", "text": "CODEX_TEXT_INCLUDED"}
                        ],
                    },
                },
            )
        ),
        encoding="utf-8",
    )
    filtered = flush_memory.read_transcript_tail(jsonl)
    assert filtered == (
        "user: USER_TEXT_INCLUDED\n\n"
        "assistant: ASSISTANT_TEXT_INCLUDED\n\n"
        "assistant: CODEX_TEXT_INCLUDED"
    )
    filtered_payload = flush_memory._build_flush_queue_payload(filtered, "session-end")
    assert filtered_payload is not None
    submitted = str(filtered_payload["prompt"])
    for excluded in (
        "SERVICE_SYSTEM_EXCLUDED",
        "SHELL_RESULT_EXCLUDED",
        "SHELL_COMMAND_EXCLUDED",
        "TOOL_RECORD_EXCLUDED",
        "MALFORMED_ROLE_EXCLUDED",
        "MALFORMED_PART_EXCLUDED",
    ):
        assert excluded not in submitted

    noise_only = tmp_path / "noise-only.jsonl"
    noise_only.write_text(
        "\n".join(
            (
                json.dumps({"role": "system", "text": "SYSTEM_ONLY_EXCLUDED"}),
                json.dumps(
                    {
                        "type": "user",
                        "message": {
                            "role": "user",
                            "content": [
                                {"type": "tool_result", "content": "TOOL_ONLY_EXCLUDED"}
                            ],
                        },
                    }
                ),
            )
        ),
        encoding="utf-8",
    )
    assert flush_memory.read_transcript_tail(noise_only) == ""
    assert flush_memory._build_flush_queue_payload("", "session-end") is None

    plain = tmp_path / "plain.log"
    plain.write_text("user: plain transcript compatibility", encoding="utf-8")
    assert flush_memory.read_transcript_tail(plain) == "user: plain transcript compatibility"

    truncated = tmp_path / "truncated.jsonl"
    truncated.write_text(
        '"role":"tool","output":"TRUNCATED_SHELL_EXCLUDED"',
        encoding="utf-8",
    )
    assert flush_memory.read_transcript_tail(truncated) == ""

    vault = tmp_path / "vault"
    state_root = tmp_path / "state"
    project = tmp_path / "project"
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
        "MEMORY_LLM_FAKE_RESPONSE": "FLUSH_OK",
    }
    noise_cases = {
        "status": "Status/progress: implemented the requested changes.",
        "audit": "Audit/review verdict: no findings remain.",
        "files": "File/path/code summary: changed src/a.py and tests/test_a.py.",
        "config": "Fact derivable from code/config: timeout is 30 seconds.",
        "navigation": "Navigation: inspected files and moved to the next module.",
        "service": "Service/system prompt: classify this maintenance transcript.",
        "shell": "Shell telemetry: command exited 0 after git status --short.",
    }
    for label, transcript_text in noise_cases.items():
        transcript = tmp_path / f"noise-{label}.txt"
        transcript.write_text(transcript_text, encoding="utf-8")
        result = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts" / "flush_memory.py"),
                "--event",
                "session-end",
                "--session-id",
                f"noise-{label}",
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
        assert result.returncode == 0, result.stderr
    assert not (vault / "knowledge" / "daily").exists()
    assert not list((state_root / "run" / "queue").glob("*.json"))


def test_transcript_jsonl_tail_uses_complete_records_and_json_uses_whole_document(
    monkeypatch,
    tmp_path,
):
    import flush_memory

    monkeypatch.setattr(flush_memory, "_transcript_path_allowed", lambda _path: True)
    records = [
        {"role": "user", "content": "USER_COMPLETE"},
        {"role": "assistant", "content": "ASSISTANT_COMPLETE"},
    ]
    expected = "user: USER_COMPLETE\n\nassistant: ASSISTANT_COMPLETE"

    jsonl = tmp_path / "session.jsonl"
    jsonl.write_text(
        "\n".join(json.dumps(record) for record in records)
        + '\n{"role":"user","content":"INCOMPLETE',
        encoding="utf-8",
    )
    assert flush_memory.read_transcript_tail(jsonl, max_chars=len(expected)) == expected

    for name, document in (
        ("object", records[0]),
        ("array", records),
    ):
        path = tmp_path / f"{name}.json"
        path.write_text(json.dumps(document, indent=2), encoding="utf-8")
        expected_text = expected if name == "array" else "user: USER_COMPLETE"
        assert flush_memory.read_transcript_tail(path, max_chars=1_000) == expected_text

    oversized = tmp_path / "oversized.json"
    oversized.write_text(
        json.dumps({"role": "user", "content": "x" * 200}),
        encoding="utf-8",
    )
    result = flush_memory._read_transcript_tail_result(oversized, max_chars=64)
    assert result.successful is True
    assert result.text.startswith("user: ")
    assert result.text.endswith("x" * 58)
    assert len(result.text) == 64


def test_transcript_source_bound_allows_large_escaped_jsonl_and_whole_json(
    monkeypatch,
    tmp_path,
):
    import flush_memory

    monkeypatch.setattr(flush_memory, "_transcript_path_allowed", lambda _path: True)
    jsonl_content = "\u2603" * 50_000
    jsonl = tmp_path / "escaped.jsonl"
    jsonl.write_text(
        json.dumps(
            {"role": "user", "content": jsonl_content},
            ensure_ascii=True,
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    assert jsonl.stat().st_size > flush_memory.MAX_TRANSCRIPT_CHARS
    assert flush_memory.read_transcript_tail(jsonl) == f"user: {jsonl_content}"

    json_content = "\u0416" * 40_000
    whole = tmp_path / "escaped.json"
    whole.write_text(
        json.dumps(
            {"role": "assistant", "content": json_content},
            ensure_ascii=True,
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    assert whole.stat().st_size > flush_memory.MAX_TRANSCRIPT_CHARS
    assert flush_memory.read_transcript_tail(whole) == f"assistant: {json_content}"


@pytest.mark.parametrize("suffix", (".json", ".jsonl"))
def test_transcript_source_over_8_mib_fails_closed(monkeypatch, tmp_path, suffix):
    import flush_memory

    monkeypatch.setattr(flush_memory, "_transcript_path_allowed", lambda _path: True)
    source_limit = 8 * 1024 * 1024
    content = "x" * source_limit
    path = tmp_path / f"oversized{suffix}"
    path.write_text(
        json.dumps(
            {"role": "user", "content": content},
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    assert path.stat().st_size > source_limit

    assert flush_memory._read_transcript_tail_result(
        path,
        max_chars=source_limit + 1_000,
    ) == flush_memory.TranscriptReadResult("", False)


def test_jsonl_tail_reader_bounds_reverse_scan_bytes():
    import flush_memory

    class MeasuredStream(io.BytesIO):
        def __init__(self, value):
            super().__init__(value)
            self.bytes_read = 0

        def read(self, size=-1):
            assert 0 < size <= 8
            chunk = super().read(size)
            self.bytes_read += len(chunk)
            return chunk

    stream = MeasuredStream(b"x" * 1_000)

    assert flush_memory._read_jsonl_conversation_tail(
        stream,
        max_chars=16,
        chunk_size=8,
        max_source_bytes=64,
    ) == ("", False)
    assert stream.bytes_read <= 64


def test_jsonl_tail_distinguishes_complete_noise_from_incomplete_noise_scan():
    import flush_memory

    complete_noise = io.BytesIO(b'{"role":"tool"}\n')
    assert flush_memory._read_jsonl_conversation_tail(
        complete_noise,
        max_chars=16,
        chunk_size=8,
        max_source_bytes=64,
    ) == ("", True)

    incomplete_noise = io.BytesIO(
        b'{"role":"user","content":"outside scan"}\n'
        + b'{"role":"tool"}\n' * 10
    )
    assert flush_memory._read_jsonl_conversation_tail(
        incomplete_noise,
        max_chars=16,
        chunk_size=8,
        max_source_bytes=64,
    ) == ("", False)


def test_jsonl_tail_truncates_and_retains_oversized_conversation_message():
    import flush_memory

    stream = io.BytesIO(
        json.dumps(
            {"role": "user", "content": "0123456789" * 5},
            separators=(",", ":"),
        ).encode("utf-8")
    )

    assert flush_memory._read_jsonl_conversation_tail(
        stream,
        max_chars=24,
        chunk_size=128,
    ) == ("user: 234567890123456789", True)


def test_noise_only_jsonl_is_a_durable_deduped_noop(monkeypatch, tmp_path):
    import flush_memory

    transcript = tmp_path / "noise.jsonl"
    transcript.write_text(
        "\n".join(
            (
                json.dumps({"role": "system", "text": "service prompt"}),
                json.dumps({"role": "tool", "content": "shell telemetry"}),
            )
        ),
        encoding="utf-8",
    )
    args = SimpleNamespace(
        event="session-end",
        session_id="noise-session",
        transcript=str(transcript),
        transcript_stdin=False,
        trigger="hook",
        project_slug="alpha",
        project_root=str(tmp_path / "alpha"),
    )
    state: dict = {}
    monkeypatch.setattr(flush_memory, "_transcript_path_allowed", lambda _path: True)
    monkeypatch.setattr(
        flush_memory,
        "_resolve_project_identity",
        lambda *_args: ("alpha", Path(args.project_root)),
    )
    monkeypatch.setattr(flush_memory, "load_state", lambda: state)
    monkeypatch.setattr(
        flush_memory,
        "update_state",
        lambda mutator: (mutator(state), state)[1],
    )
    monkeypatch.setattr(
        flush_memory,
        "summarize_with_llm",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("noise-only transcripts must not reach the classifier")
        ),
    )
    monkeypatch.setattr(
        flush_memory,
        "_enqueue_transcript_fallback",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("noise-only transcripts must not be queued")
        ),
    )

    occurred_at = "2026-08-01T12:34:56.000000+00:00"
    first = flush_memory._process_flush(args, occurred_at, None)
    second = flush_memory._process_flush(args, occurred_at, None)

    assert first == flush_memory.FlushProcessStatus(
        0,
        True,
        "alpha",
        str(Path(args.project_root)),
        True,
    )
    assert second == first
    assert len(state["flush_dedupe"]) == 1
    assert state["flush_empty_count"] == 1
    assert state["flush_tier_counts"] == {"ok": 1}


def test_concurrent_malformed_flushes_enqueue_once_under_dedupe_lock(
    monkeypatch,
    tmp_path,
):
    import flush_memory
    import memory_queue

    args = SimpleNamespace(
        event="session-end",
        session_id="malformed-session",
        transcript=str(tmp_path / "session.txt"),
        transcript_stdin=False,
        trigger="hook",
        project_slug="alpha",
        project_root=str(tmp_path / "alpha"),
    )
    state: dict = {}
    state_lock = threading.Lock()
    initial_reads = threading.Barrier(2)
    queued: list[dict] = []
    Path(args.transcript).write_text(
        "A transcript with a malformed classifier response.",
        encoding="utf-8",
    )
    monkeypatch.setattr(flush_memory, "_transcript_path_allowed", lambda _path: True)
    monkeypatch.setattr(
        flush_memory,
        "_resolve_project_identity",
        lambda *_args: ("alpha", Path(args.project_root)),
    )

    def load_state():
        initial_reads.wait(timeout=3)
        return {}

    def update_state(mutator):
        with state_lock:
            mutator(state)
            return state

    monkeypatch.setattr(flush_memory, "load_state", load_state)
    monkeypatch.setattr(flush_memory, "update_state", update_state)
    monkeypatch.setattr(
        flush_memory,
        "summarize_with_llm",
        lambda *_args, **_kwargs: "FLUSH_UNKNOWN\n\nMalformed response.",
    )
    monkeypatch.setattr(
        memory_queue,
        "enqueue",
        lambda _task_type, payload: queued.append(payload) or f"task-{len(queued)}",
    )

    occurred_at = "2026-08-01T12:34:56.000000+00:00"
    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = list(
            pool.map(
                lambda _index: flush_memory._process_flush(args, occurred_at, None),
                range(2),
            )
        )

    assert all(status.code == 0 and status.durable for status in statuses)
    assert len(queued) == 1
    assert len(state["flush_dedupe"]) == 1


def test_queued_malformed_response_remains_pending_instead_of_appending(
    monkeypatch,
    tmp_path,
):
    import memory_queue

    for index, response in enumerate(
        (
            "arbitrary minor prose without a protocol token",
            "FLUSH_MINOR\n\n**Open questions**",
            "FLUSH_MINOR\n\nProse before.\n**Open questions**\n- Question.",
            "FLUSH_MINOR\n\n**Other section**\n- Unknown.",
            "FLUSH_MINOR\n\n**Decisions made**\n- Major-only.",
            "FLUSH_MAJOR\n\n**Gotchas / debugging**\n- Minor-only content.",
        )
    ):
        queue_dir = tmp_path / f"queue-{index}"
        daily_dir = tmp_path / f"daily-{index}"
        queue_dir.mkdir()
        monkeypatch.setattr(memory_queue, "_queue_dir", lambda current=queue_dir: current)
        monkeypatch.setattr(memory_queue, "_daily_dir", lambda current=daily_dir: current)
        memory_queue.enqueue("flush", {"prompt": "classify"})
        prepared = memory_queue.prepare_sdk_task()

        assert memory_queue.apply_sdk_result(
            {**prepared, "success": True, "response": response}
        ) == (True, "failure recorded")

        [pending] = memory_queue.list_pending()
        assert pending["attempts"] == 1
        assert "flush classification" in pending["last_error"]
        assert not daily_dir.exists()


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


def test_transcript_stdin_exact_ok_or_malformed_response_is_durable(tmp_path):
    for label, response, expected_queue_count in (
        ("exact", "FLUSH_OK", 0),
        ("punctuated", "FLUSH_OK.", 1),
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

        assert result.returncode == 0, result.stderr
        queue_files = list((state_root / "run" / "queue").glob("*.json"))
        assert len(queue_files) == expected_queue_count
        state_path = state_root / "run" / "state.json"
        state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
        if label == "exact":
            assert state["flush_empty_count"] == 1
        else:
            assert "flush_empty_count" not in state
            assert len(state["flush_dedupe"]) == 1
            queued = json.loads(queue_files[0].read_text(encoding="utf-8"))
            assert queued["type"] == "flush"
            assert "Routine status" in queued["payload"]["prompt"]


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

    tier, classified_body = flush_memory._classify_response(
        "FLUSH_MINOR\n\n**Gotchas / debugging**\n"
        "- `[10:03:00] prompt | nested-session | beta` remains bullet text."
    )
    _day, classified_block = flush_memory.render_flush_block(
        tier,
        classified_body,
        event="session-end",
        session_id="session-2",
        trigger="hook",
        project_slug="alpha",
        project_root=str(project_root),
        occurred_at="2026-07-29T10:00:00+00:00",
    )
    assert (
        "\n\\- `[10:03:00] prompt | nested-session | beta` remains bullet text.\n"
        in classified_block
    )


def test_render_flush_block_escapes_capture_marker_prefix_in_model_body(tmp_path):
    import flush_memory

    forged = f"<!-- llm-wiki-capture: {'b' * 64} -->"
    canonical = f"<!-- llm-wiki-capture: {'a' * 64} -->"
    _day, block = flush_memory.render_flush_block(
        "minor",
        f"**Gotchas / debugging**\n- Explain {forged} as untrusted text.",
        event="session-end",
        session_id="session-1",
        trigger="hook",
        project_slug="alpha",
        project_root=str((tmp_path / "alpha").resolve()),
        occurred_at="2026-08-01T10:00:00+00:00",
        idempotency_marker=canonical,
    )

    assert forged not in block
    assert f"&lt;!-- llm-wiki-capture: {'b' * 64} -->" in block
    assert block.splitlines().count(canonical) == 1


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
        project_identity_confirmed=True,
    ) == ""

    assert len(queued) == 1
    task_type, payload = queued[0]
    assert task_type == "flush"
    assert payload["provenance_version"] == 1
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


@pytest.mark.parametrize(
    ("transcript", "event", "session_id", "trigger", "slug", "root", "expected"),
    (
        (
            "  user: hello\r\n\r\nassistant: hi\r\n",
            "session-end",
            "session-1",
            "hook",
            "alpha",
            "D:/projects/alpha",
            "44a1108451025745662170243fec6372090b456747ba44324b141b3d7173abc6",
        ),
        (
            "user: caf\u00e9\n\nassistant: \u041f\u0440\u0438\u0432\u0435\u0442",
            "pre-compact",
            "s/2",
            "opencode-compacting",
            "beta",
            "/srv/\u03b2",
            "c2c47b6b1be3fadd684e78e9d7df332cc19921297e25f6ae970adb69f4c9905b",
        ),
    ),
)
def test_capture_id_has_stable_canonical_vectors(
    transcript,
    event,
    session_id,
    trigger,
    slug,
    root,
    expected,
):
    import flush_memory

    assert flush_memory.build_capture_id(
        transcript,
        event,
        session_id=session_id,
        trigger=trigger,
        project_slug=slug,
        project_root=root,
    ) == expected


def test_capture_id_cli_emits_only_python_canonical_id_for_unicode_controls():
    import flush_memory

    transcript = (
        "\u00a0\r\nuser: Unicode\u2003space\u0000control\u001finside\r"
        "\n\nassistant: keep parity\u2029\r\n\u2002"
    )
    provenance = {
        "event": "session-end",
        "session_id": "unicode-session",
        "trigger": "opencode-idle",
        "project_slug": "alpha",
        "project_root": str((ROOT / "unicode-project").resolve()),
    }
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "flush_memory.py"),
            "--capture-id",
            "--event",
            provenance["event"],
            "--session-id",
            provenance["session_id"],
            "--transcript-stdin",
            "--trigger",
            provenance["trigger"],
            "--project-slug",
            provenance["project_slug"],
            "--project-root",
            provenance["project_root"],
        ],
        cwd=ROOT,
        input=transcript.encode("utf-8"),
        capture_output=True,
        timeout=30,
        check=False,
    )

    expected = flush_memory.build_capture_id(transcript, **provenance)
    assert result.returncode == 0, result.stderr
    assert result.stderr == b""
    assert re.fullmatch(rb"[0-9a-f]{64}\r?\n", result.stdout)
    assert result.stdout.rstrip(b"\r\n") == expected.encode()


def test_capture_id_cli_enforces_eight_mib_stdin_cap():
    source_limit = 8 * 1024 * 1024
    command = [
        sys.executable,
        str(ROOT / "scripts" / "flush_memory.py"),
        "--capture-id",
        "--event",
        "session-end",
        "--session-id",
        "bounded-session",
        "--transcript-stdin",
        "--trigger",
        "opencode-idle",
        "--project-slug",
        "alpha",
        "--project-root",
        str((ROOT / "bounded-project").resolve()),
    ]

    accepted = subprocess.run(
        command,
        cwd=ROOT,
        input=b"x" * source_limit,
        capture_output=True,
        timeout=30,
        check=False,
    )
    rejected = subprocess.run(
        command,
        cwd=ROOT,
        input=b"x" * (source_limit + 1),
        capture_output=True,
        timeout=30,
        check=False,
    )

    assert accepted.returncode == 0
    assert re.fullmatch(rb"[0-9a-f]{64}\r?\n", accepted.stdout)
    assert accepted.stderr == b""
    assert rejected.returncode == 2
    assert rejected.stdout == b""
    assert rejected.stderr == b""


def test_new_flush_payload_capture_id_is_canonical_and_excludes_occurrence_time():
    import flush_memory

    common = {
        "transcript_excerpt": "user: stable capture\n\nassistant: stable response",
        "event": "session-end",
        "session_id": "session-1",
        "trigger": "hook",
        "project_slug": "alpha",
        "project_root": "D:/projects/alpha",
        "project_identity_confirmed": True,
    }

    first = flush_memory._build_flush_queue_payload(
        **common,
        occurred_at="2026-08-01T12:00:00+00:00",
    )
    second = flush_memory._build_flush_queue_payload(
        **common,
        occurred_at="2026-08-01T13:00:00+00:00",
    )

    assert first is not None and second is not None
    assert re.fullmatch(r"[0-9a-f]{64}", str(first["capture_id"]))
    assert first["capture_id"] == second["capture_id"]


def test_durable_queue_drops_version_when_provenance_is_sanitized(
    monkeypatch,
    tmp_path,
):
    import flush_memory
    import memory_queue

    monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(tmp_path))
    secret = "sk-abcdefghijklmnopqrstuvwxyz012345"
    cases = (
        (
            "whitespace-session",
            "project_root",
            "D:/projects/alpha  service",
        ),
        (
            "secret-session",
            "trigger",
            f"token={secret}",
        ),
    )

    for session_id, field, value in cases:
        provenance = {
            "session_id": session_id,
            "trigger": "opencode-idle",
            "project_slug": "alpha",
            "project_root": "D:/projects/alpha",
            field: value,
        }
        payload = flush_memory._build_flush_queue_payload(
            "A transcript with durable provenance.",
            "session-end",
            **provenance,
            occurred_at="2026-07-27T12:34:56+00:00",
            project_identity_confirmed=True,
        )
        assert payload is not None
        memory_queue.enqueue("flush", payload)

    queue_files = list((tmp_path / "run" / "queue").glob("*.json"))
    assert len(queue_files) == len(cases)
    persisted = {
        task["payload"]["session_id"]: task["payload"]
        for task in (
            json.loads(path.read_text(encoding="utf-8"))
            for path in queue_files
        )
    }

    whitespace_payload = persisted["whitespace-session"]
    assert whitespace_payload["project_root"] == "D:/projects/alpha service"
    assert "provenance_version" not in whitespace_payload

    secret_payload = persisted["secret-session"]
    assert secret not in secret_payload["trigger"]
    assert secret_payload["trigger"] != f"token={secret}"
    assert "provenance_version" not in secret_payload


def test_unconfirmed_or_incomplete_flush_payload_stays_unversioned():
    import flush_memory

    complete = {
        "transcript_excerpt": "A transcript with durable provenance.",
        "event": "session-end",
        "session_id": "session-1",
        "trigger": "opencode-idle",
        "project_slug": "alpha",
        "project_root": "D:/projects/alpha",
        "occurred_at": "2026-07-27T12:34:56Z",
    }

    unconfirmed = flush_memory._build_flush_queue_payload(**complete)
    assert unconfirmed is not None
    assert "provenance_version" not in unconfirmed

    for field, incomplete_value in (
        ("trigger", "   "),
        ("session_id", "UNKNOWN"),
        ("project_slug", "unknown"),
        ("project_root", "Unknown"),
    ):
        incomplete = flush_memory._build_flush_queue_payload(
            **{**complete, field: incomplete_value},
            project_identity_confirmed=True,
        )
        assert incomplete is not None
        assert "provenance_version" not in incomplete


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


def test_malformed_staged_flush_queues_once_records_dedupe_and_deletes_transport(
    monkeypatch,
    tmp_path,
):
    import flush_memory
    import memory_queue
    import precompact_capture

    transcript = precompact_capture._stage_inline_transcript("Malformed staged response input.")
    args = _staged_flush_args(transcript, tmp_path / "alpha")
    queued: list[dict] = []
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
        lambda *_args, **_kwargs: "FLUSH_UNKNOWN\n\nUnknown response.",
    )
    monkeypatch.setattr(flush_memory, "update_state", lambda mutator: mutator(state))

    def enqueue(_task_type, payload):
        assert transcript.exists()
        queued.append(payload)
        return "task-1"

    monkeypatch.setattr(memory_queue, "enqueue", enqueue)

    assert flush_memory.main() == 0
    assert len(queued) == 1
    assert queued[0]["event"] == "pre-compact"
    assert "Malformed staged response input." in queued[0]["prompt"]
    assert len(state["flush_dedupe"]) == 1
    assert not transcript.exists()


def test_state_save_failure_after_enqueue_reuses_capture_on_retry(
    monkeypatch,
    tmp_path,
):
    import flush_memory
    import memory_queue

    queue_dir = tmp_path / "queue"
    queue_dir.mkdir()
    monkeypatch.setattr(memory_queue, "_queue_dir", lambda: queue_dir)
    transcript = tmp_path / "session.txt"
    transcript.write_text(
        "A malformed response must retain one durable capture.",
        encoding="utf-8",
    )
    args = SimpleNamespace(
        event="session-end",
        session_id="retry-session",
        transcript=str(transcript),
        transcript_stdin=False,
        trigger="hook",
        project_slug="alpha",
        project_root=str((tmp_path / "alpha").resolve()),
    )
    monkeypatch.setattr(flush_memory, "_transcript_path_allowed", lambda _path: True)
    monkeypatch.setattr(
        flush_memory,
        "_resolve_project_identity",
        lambda *_args: ("alpha", Path(args.project_root)),
    )
    monkeypatch.setattr(flush_memory, "load_state", lambda: {})
    monkeypatch.setattr(
        flush_memory,
        "summarize_with_llm",
        lambda *_args, **_kwargs: "FLUSH_UNKNOWN\n\nMalformed response.",
    )
    saves = 0

    def update(mutator):
        nonlocal saves
        state: dict = {}
        mutator(state)
        saves += 1
        if saves == 1:
            raise OSError("injected state save failure after enqueue")
        return state

    monkeypatch.setattr(flush_memory, "update_state", update)

    first = flush_memory._process_flush(
        args,
        "2026-08-01T12:00:00+00:00",
        None,
    )
    second = flush_memory._process_flush(
        args,
        "2026-08-01T13:00:00+00:00",
        None,
    )

    assert first.code == 2 and first.durable is True
    assert second.code == 0 and second.durable is True
    [pending] = memory_queue.list_pending()
    assert re.fullmatch(r"[0-9a-f]{64}", pending["payload"]["capture_id"])


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
    expected_capture_id = flush_memory.build_capture_id(
        "A durable debugging observation.",
        "pre-compact",
        session_id="session-1",
        trigger="opencode-compacting",
        project_slug="alpha",
        project_root=str(Path(args.project_root)),
    )
    assert markers == [flush_memory.capture_marker(expected_capture_id)]


def test_stdin_append_failure_enqueues_same_bounded_capture(monkeypatch, tmp_path):
    import flush_memory
    import memory_queue

    transcript = "A stdin debugging transcript that must survive append failure."
    project_root = (tmp_path / "alpha").resolve()
    project_root.mkdir()
    queue_dir = tmp_path / "queue"
    queue_dir.mkdir()
    state: dict = {}
    args = SimpleNamespace(
        event="session-end",
        session_id="stdin-session",
        transcript="",
        transcript_stdin=True,
        delete_transcript=False,
        trigger="opencode-idle",
        project_slug="alpha",
        project_root=str(project_root),
    )
    occurred_at = "2026-08-01T12:34:56.000000+00:00"
    response = (
        "FLUSH_MINOR\n\n**Gotchas / debugging**\n"
        "- Preserve classified stdin after append failure."
    )
    monkeypatch.setattr(sys, "stdin", io.StringIO(transcript))
    monkeypatch.setattr(memory_queue, "_queue_dir", lambda: queue_dir)
    monkeypatch.setattr(
        flush_memory,
        "_resolve_project_identity",
        lambda *_args, **_kwargs: ("alpha", project_root),
    )
    monkeypatch.setattr(flush_memory, "load_state", lambda: state)
    monkeypatch.setattr(
        flush_memory,
        "update_state",
        lambda mutator: (mutator(state), state)[1],
    )
    monkeypatch.setattr(
        flush_memory,
        "summarize_with_llm",
        lambda *_args, **_kwargs: response,
    )
    monkeypatch.setattr(
        flush_memory,
        "append_daily_once",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            OSError("injected stdin append failure")
        ),
    )

    status = flush_memory._process_flush(args, occurred_at, None)

    assert status == flush_memory.FlushProcessStatus(
        0,
        True,
        "alpha",
        str(project_root),
        True,
    )
    [task] = memory_queue.list_pending()
    payload = task["payload"]
    assert transcript in payload["prompt"]
    assert payload["session_id"] == "stdin-session"
    assert payload["trigger"] == "opencode-idle"
    assert payload["project_slug"] == "alpha"
    assert payload["project_root"] == str(project_root)
    assert payload["occurred_at"] == occurred_at
    assert payload["capture_id"] == flush_memory.build_capture_id(
        transcript,
        "session-end",
        session_id="stdin-session",
        trigger="opencode-idle",
        project_slug="alpha",
        project_root=str(project_root),
    )
    assert len(state["flush_dedupe"]) == 1


def test_staged_append_success_then_raise_queues_marker_noop(monkeypatch, tmp_path):
    import daily_log_append
    import flush_memory
    import memory_queue
    import precompact_capture

    transcript_text = "A staged capture whose append acknowledgement is ambiguous."
    transcript = precompact_capture._stage_inline_transcript(transcript_text)
    project_root = (tmp_path / "alpha").resolve()
    project_root.mkdir()
    args = _staged_flush_args(transcript, project_root)
    daily_dir = tmp_path / "daily"
    queue_dir = tmp_path / "queue"
    queue_dir.mkdir()
    state: dict = {}
    response = (
        "FLUSH_MINOR\n\n**Gotchas / debugging**\n"
        "- Queue an ambiguous append and apply it exactly once."
    )
    monkeypatch.setattr(flush_memory, "parse_args", lambda: args)
    monkeypatch.setattr(flush_memory, "DAILY_DIR", daily_dir)
    monkeypatch.setattr(memory_queue, "_daily_dir", lambda: daily_dir)
    monkeypatch.setattr(memory_queue, "_queue_dir", lambda: queue_dir)
    monkeypatch.setattr(daily_log_append, "STATE_ROOT", tmp_path / "runtime")
    monkeypatch.setattr(
        flush_memory,
        "_resolve_project_identity",
        lambda *_args, **_kwargs: ("alpha", project_root),
    )
    monkeypatch.setattr(flush_memory, "load_state", lambda: state)
    monkeypatch.setattr(
        flush_memory,
        "update_state",
        lambda mutator: (mutator(state), state)[1],
    )
    monkeypatch.setattr(
        flush_memory,
        "summarize_with_llm",
        lambda *_args, **_kwargs: response,
    )

    def append_then_raise(day, block, marker):
        assert transcript.exists()
        daily_log_append.locked_append_once(
            daily_dir / f"{day}.md",
            block,
            marker,
            state_root=tmp_path / "runtime",
        )
        raise OSError("injected acknowledgement failure after append")

    monkeypatch.setattr(flush_memory, "append_daily_once", append_then_raise)

    try:
        assert flush_memory.main() == 0
        assert not transcript.exists()
        [task] = memory_queue.list_pending()
        expected_capture_id = flush_memory.build_capture_id(
            transcript_text,
            "pre-compact",
            session_id="session-1",
            trigger="opencode-compacting",
            project_slug="alpha",
            project_root=str(project_root),
        )
        marker = flush_memory.capture_marker(expected_capture_id)
        assert task["payload"]["capture_id"] == expected_capture_id
        assert len(state["flush_dedupe"]) == 1

        memory_queue.apply_classified_flush_response(task, response)

        [daily] = list(daily_dir.glob("*.md"))
        content = daily.read_text(encoding="utf-8")
        assert content.count("Queue an ambiguous append and apply it exactly once.") == 1
        assert content.count(marker) == 1
    finally:
        transcript.unlink(missing_ok=True)


def test_file_append_and_enqueue_failure_is_non_durable(monkeypatch, tmp_path):
    import flush_memory
    import memory_queue

    transcript = tmp_path / "session.txt"
    transcript.write_text(
        "An ordinary transcript whose append and deferred enqueue both fail.",
        encoding="utf-8",
    )
    project_root = (tmp_path / "alpha").resolve()
    project_root.mkdir()
    state: dict = {}
    enqueue_attempts = 0
    args = SimpleNamespace(
        event="session-end",
        session_id="file-session",
        transcript=str(transcript),
        transcript_stdin=False,
        delete_transcript=False,
        trigger="hook",
        project_slug="alpha",
        project_root=str(project_root),
    )
    monkeypatch.setattr(flush_memory, "_transcript_path_allowed", lambda _path: True)
    monkeypatch.setattr(
        flush_memory,
        "_resolve_project_identity",
        lambda *_args, **_kwargs: ("alpha", project_root),
    )
    monkeypatch.setattr(flush_memory, "load_state", lambda: state)
    monkeypatch.setattr(
        flush_memory,
        "update_state",
        lambda mutator: (mutator(state), state)[1],
    )
    monkeypatch.setattr(
        flush_memory,
        "summarize_with_llm",
        lambda *_args, **_kwargs: (
            "FLUSH_MINOR\n\n**Gotchas / debugging**\n"
            "- Do not report durability when both persistence paths fail."
        ),
    )
    monkeypatch.setattr(
        flush_memory,
        "append_daily_once",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            OSError("injected ordinary append failure")
        ),
    )

    def fail_enqueue(*_args, **_kwargs):
        nonlocal enqueue_attempts
        enqueue_attempts += 1
        raise OSError("injected queue persistence failure")

    monkeypatch.setattr(memory_queue, "enqueue", fail_enqueue)

    status = flush_memory._process_flush(
        args,
        "2026-08-01T13:45:00.000000+00:00",
        None,
    )

    assert status.code != 0
    assert status.durable is False
    assert enqueue_attempts == 1
    assert "flush_dedupe" not in state
    assert transcript.exists()


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
    assert text.count("<!-- llm-wiki-capture:") == 1
    assert text.index("Persist exactly once.") < text.index(
        "<!-- llm-wiki-capture:"
    )


@pytest.mark.parametrize("identity_dimension", ("occurrence", "project"))
def test_capture_identity_ignores_occurrence_time_and_distinguishes_project(
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
            "- Persist each distinct capture."
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
    expected_captures = 1 if identity_dimension == "occurrence" else 2
    assert text.count("Persist each distinct capture.") == expected_captures
    assert text.count("<!-- llm-wiki-capture:") == expected_captures
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


def test_malformed_ordinary_session_events_enqueue_once_without_daily_loss(tmp_path):
    for event, response in (
        ("session-end", "FLUSH_UNKNOWN\n\nUnknown response."),
        ("pre-compact", "FLUSH_MINOR"),
    ):
        case = event
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
                event,
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

        assert result.returncode == 0, result.stderr
        assert not (vault / "knowledge" / "daily").exists()
        queue_files = list((state_root / "run" / "queue").glob("*.json"))
        assert len(queue_files) == 1
        queued = json.loads(queue_files[0].read_text(encoding="utf-8"))
        assert queued["type"] == "flush"
        assert queued["payload"]["event"] == event
        assert "malformed tier response" in queued["payload"]["prompt"]
        state_path = state_root / "run" / "state.json"
        state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
        assert len(state["flush_dedupe"]) == 1
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
