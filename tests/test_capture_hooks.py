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
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

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


def _run_capture_process(module_name: str, payload: dict, env: dict[str, str]):
    return subprocess.run(
        [sys.executable, str(Path(__file__).resolve().parent.parent / "scripts" / f"{module_name}.py")],
        input=json.dumps(payload),
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )


class _BoundedOnlyInput(io.StringIO):
    def __init__(self, value: str):
        super().__init__(value)
        self.request_sizes: list[int] = []

    def read(self, size: int = -1) -> str:
        self.request_sizes.append(size)
        assert size > 0, "reader requested an unbounded allocation"
        return super().read(size)


class _BoundedOnlyBytes(io.BytesIO):
    def __init__(self, value: bytes):
        super().__init__(value)
        self.request_sizes: list[int] = []

    def read(self, size: int = -1) -> bytes:
        self.request_sizes.append(size)
        assert size > 0, "reader requested an unbounded allocation"
        return super().read(size)


class _BinaryBackedInput:
    def __init__(self, value: bytes):
        self.buffer = _BoundedOnlyBytes(value)

    def read(self, size: int = -1) -> str:
        raise AssertionError("binary-backed input must use stream.buffer")


def test_bounded_json_reader_requires_keyword_only_positive_byte_cap():
    import memory_state

    with pytest.raises(TypeError):
        memory_state.read_json_object_bounded(io.StringIO("{}"), 16)
    for invalid_cap in (0, -1, True, 1.5):
        with pytest.raises(ValueError):
            memory_state.read_json_object_bounded(
                io.StringIO("{}"),
                max_bytes=invalid_cap,
            )


def test_bounded_json_reader_prefers_binary_stream_and_rejects_overflow():
    import memory_state

    raw = b'{"operation":"append"}'
    accepted = _BinaryBackedInput(raw)
    oversized = _BinaryBackedInput(raw + b" ")
    downstream_mutations: list[str] = []

    assert memory_state.read_json_object_bounded(
        accepted,
        max_bytes=len(raw),
    ) == {"operation": "append"}
    result = memory_state.read_json_object_bounded(
        oversized,
        max_bytes=len(raw),
    )
    if result is not None:
        downstream_mutations.append(result["operation"])

    assert result is None
    assert downstream_mutations == []
    assert accepted.buffer.request_sizes == [len(raw) + 1]
    assert oversized.buffer.request_sizes == [len(raw) + 1]


def test_bounded_json_reader_text_fallback_counts_encoded_utf8_bytes():
    import memory_state

    text = '{"value":"é"}'
    encoded_size = len(text.encode("utf-8"))
    rejected = _BoundedOnlyInput(text)
    accepted = _BoundedOnlyInput(text)

    assert memory_state.read_json_object_bounded(
        rejected,
        max_bytes=encoded_size - 1,
    ) is None
    assert memory_state.read_json_object_bounded(
        accepted,
        max_bytes=encoded_size,
    ) == {"value": "é"}
    assert all(size > 0 for size in rejected.request_sizes + accepted.request_sizes)


@pytest.mark.parametrize("raw", (b"\xff", b"{", b"[]"))
def test_bounded_json_reader_rejects_invalid_input(raw: bytes):
    import memory_state

    assert memory_state.read_json_object_bounded(
        _BinaryBackedInput(raw),
        max_bytes=16,
    ) is None


@pytest.mark.parametrize(
    "raw",
    (
        r'{"\ud800":"value"}',
        r'{"\udfff":"value"}',
        r'{"value":"\ud800"}',
        r'{"value":"\udfff"}',
        r'{"nested":[{"\ud800":"value"}]}',
        r'{"nested":[{"\udfff":"value"}]}',
        r'{"nested":[{"value":"\ud800"}]}',
        r'{"nested":[{"value":"\udfff"}]}',
    ),
    ids=(
        "top-level-high-key",
        "top-level-low-key",
        "top-level-high-value",
        "top-level-low-value",
        "nested-high-key",
        "nested-low-key",
        "nested-high-value",
        "nested-low-value",
    ),
)
def test_bounded_json_reader_rejects_unpaired_surrogates_across_object_graph(
    raw: str,
):
    import memory_state

    encoded = raw.encode("ascii")

    assert memory_state.read_json_object_bounded(
        _BinaryBackedInput(encoded),
        max_bytes=len(encoded),
    ) is None


def test_bounded_json_reader_accepts_surrogate_pairs_as_unicode_scalars():
    import memory_state

    raw = r'{"\ud83d\ude00":{"nested":["\ud834\udd1e"]}}'
    encoded = raw.encode("ascii")

    assert memory_state.read_json_object_bounded(
        _BinaryBackedInput(encoded),
        max_bytes=len(encoded),
    ) == {
        chr(0x1F600): {"nested": [chr(0x1D11E)]},
    }


@pytest.mark.parametrize("error_type", (RecursionError, MemoryError))
def test_bounded_json_reader_catches_parser_resource_errors(monkeypatch, error_type):
    import memory_state

    def fail_parse(_text):
        raise error_type("injected parser resource failure")

    monkeypatch.setattr(memory_state.json, "loads", fail_parse)

    assert memory_state.read_json_object_bounded(
        _BinaryBackedInput(b"{}"),
        max_bytes=2,
    ) is None


def test_strict_json_depth_preflight_rejects_before_parser(monkeypatch):
    import memory_state

    nesting = 5_000
    raw = '{"value":' + "[" * nesting + "0" + "]" * nesting + "}"
    parser_calls: list[str] = []

    def forbidden_parse(*_args, **_kwargs):
        parser_calls.append("called")
        raise AssertionError("over-depth JSON reached json.loads")

    monkeypatch.setattr(memory_state.json, "loads", forbidden_parse)

    with pytest.raises(ValueError, match="depth limit"):
        memory_state.decode_json_object_strict(
            raw,
            max_bytes=len(raw),
            max_depth=32,
        )

    assert parser_calls == []


def test_strict_json_lexical_preflight_bounds_tokens_and_number_length(
    monkeypatch,
):
    import memory_state

    monkeypatch.setattr(memory_state, "MAX_JSON_LEXICAL_TOKENS", 8, raising=False)
    with pytest.raises(ValueError, match="resource limit"):
        memory_state.decode_json_object_strict(
            '{"items":[0,1,2,3,4,5]}',
            max_bytes=64,
        )

    monkeypatch.setattr(memory_state, "MAX_JSON_NUMBER_CHARS", 8, raising=False)
    with pytest.raises(ValueError, match="number length limit"):
        memory_state.decode_json_object_strict(
            '{"value":123456789}',
            max_bytes=64,
        )


def test_strict_json_explicit_lexical_limit_preserves_default_and_exact_boundary(
    monkeypatch,
):
    import memory_state

    raw = '{"items":[0,1]}'
    lexical_tokens = sum(char in "{}[],:" for char in raw)
    monkeypatch.setattr(
        memory_state,
        "MAX_JSON_LEXICAL_TOKENS",
        lexical_tokens,
        raising=False,
    )

    assert memory_state.decode_json_object_strict(raw, max_bytes=len(raw)) == {
        "items": [0, 1]
    }
    assert memory_state.decode_json_object_strict(
        raw,
        max_bytes=len(raw),
        max_lexical_tokens=lexical_tokens,
    ) == {"items": [0, 1]}
    with pytest.raises(ValueError, match="lexical resource limit"):
        memory_state.decode_json_object_strict(
            raw,
            max_bytes=len(raw),
            max_lexical_tokens=lexical_tokens - 1,
        )


def test_strict_json_depth_preflight_ignores_escaped_structure_inside_strings():
    import memory_state

    raw = r'{"value":"[[[\"}]]]","nested":{"ok":true}}'

    assert memory_state.decode_json_object_strict(
        raw,
        max_bytes=len(raw),
        max_depth=2,
    ) == {
        "value": '[[["}]]]',
        "nested": {"ok": True},
    }


def test_strict_json_enforces_explicit_character_limit():
    import memory_state

    raw = '{"value":"é"}'

    with pytest.raises(ValueError, match="character limit"):
        memory_state.decode_json_object_strict(
            raw,
            max_bytes=len(raw.encode("utf-8")),
            max_chars=len(raw) - 1,
            max_depth=2,
            max_members=2,
        )


def test_strict_json_enforces_aggregate_member_limit():
    import memory_state

    raw = '{"first":1,"second":[2,3]}'

    with pytest.raises(ValueError, match="member limit"):
        memory_state.decode_json_object_strict(
            raw,
            max_bytes=len(raw),
            max_chars=len(raw),
            max_depth=2,
            max_members=2,
        )


def test_bounded_json_reader_catches_stream_oserror():
    import memory_state

    class FailingBuffer:
        def read(self, size: int = -1) -> bytes:
            assert size > 0, "reader requested an unbounded allocation"
            raise OSError("injected read failure")

    stream = SimpleNamespace(buffer=FailingBuffer())

    assert memory_state.read_json_object_bounded(stream, max_bytes=16) is None


def test_precompact_rejects_unpaired_surrogate_before_staging_or_spawn(
    monkeypatch,
    tmp_path,
):
    import precompact_capture

    staged: list[str] = []
    spawned: list[list[str]] = []
    monkeypatch.delenv("CLAUDE_INVOKED_BY", raising=False)
    monkeypatch.setattr(
        precompact_capture,
        "_stage_inline_transcript",
        lambda transcript: staged.append(transcript) or (tmp_path / "staged.txt"),
    )
    monkeypatch.setattr(
        precompact_capture,
        "spawn_detached",
        lambda args: spawned.append(args) or 1,
    )

    with patch.object(
        sys,
        "stdin",
        io.StringIO(r'{"session_id":"session-1","transcript":"\ud800"}'),
    ):
        assert precompact_capture.main() == 0

    assert staged == []
    assert spawned == []


def test_precompact_inline_transcript_uses_detached_ephemeral_file(monkeypatch):
    import precompact_capture

    calls = []

    def fake_spawn(args):
        transcript_path = Path(args[args.index("--transcript") + 1])
        calls.append((args, transcript_path.read_text(encoding="utf-8")))
        transcript_path.unlink()
        return 123

    monkeypatch.setattr(precompact_capture, "spawn_detached", fake_spawn)

    rc = _run_capture_with_stdin(
        "precompact_capture",
        {
            "session_id": "session-123",
            "transcript": "user: Keep this actual transcript private",
            "trigger": "opencode-compacting",
        },
    )

    assert rc == 0
    assert len(calls) == 1
    args, transported = calls[0]
    assert "--delete-transcript" in args
    assert "--transcript" in args
    assert "Keep this actual transcript private" not in " ".join(args)
    assert transported == "user: Keep this actual transcript private"


@pytest.mark.parametrize("module_name", ("precompact_capture", "session_end_capture"))
def test_flush_wrapper_uses_agent_root_for_nested_cwd(
    module_name: str,
    monkeypatch,
    tmp_path: Path,
):
    module = __import__(module_name)
    project = tmp_path / "project"
    nested = project / "src"
    nested.mkdir(parents=True)
    calls: list[list[str]] = []
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(project))
    monkeypatch.setattr(module, "spawn_detached", lambda args: calls.append(args) or 1)

    assert _run_capture_with_stdin(
        module_name,
        {
            "session_id": "session-1",
            "cwd": str(nested),
            "project_slug": "project",
        },
    ) == 0

    assert len(calls) == 1
    args = calls[0]
    assert args[args.index("--project-root") + 1] == str(project.resolve())


@pytest.mark.parametrize("nul_location", ("argv", "cwd"))
def test_spawn_detached_rejects_nul_and_closes_log_handles(
    nul_location: str,
    monkeypatch,
    tmp_path: Path,
):
    import memory_state

    args = [sys.executable, "-c", "pass"]
    cwd = None
    if nul_location == "argv":
        args.append("forged\0argument")
    else:
        cwd = Path(f"{tmp_path.resolve()}\0forged")

    opened = []
    real_open = open

    def tracking_open(*args, **kwargs):
        handle = real_open(*args, **kwargs)
        opened.append(handle)
        return handle

    monkeypatch.setattr(memory_state, "open", tracking_open, raising=False)
    result: int | ValueError | None = None
    try:
        result = memory_state.spawn_detached(
            args,
            stdout_path=tmp_path / "stdout.log",
            stderr_path=tmp_path / "stderr.log",
            cwd=cwd,
        )
    except ValueError as exc:
        result = exc
    finally:
        closed = [handle.closed for handle in opened]
        for handle in opened:
            handle.close()

    assert result is None
    assert closed == [True, True]


@pytest.mark.parametrize(
    "failure_point",
    ("nul-stderr", "nul-stdout", "mkdir", "open", "popen"),
)
def test_spawn_detached_setup_failures_close_each_opened_handle_once(
    failure_point: str,
    monkeypatch,
    tmp_path: Path,
):
    import memory_state

    stdout_path = tmp_path / "stdout" / "spawn.log"
    stderr_path = tmp_path / "stderr" / "spawn.log"
    if failure_point == "nul-stderr":
        stderr_path = Path(f"{stderr_path}\0forged")
    elif failure_point == "nul-stdout":
        stdout_path = Path(f"{stdout_path}\0forged")

    opened = []
    real_open = open

    class TrackingHandle:
        def __init__(self, handle):
            self.handle = handle
            self.close_calls = 0

        def __getattr__(self, name):
            return getattr(self.handle, name)

        def close(self):
            self.close_calls += 1
            self.handle.close()

        def cleanup(self):
            if not self.handle.closed:
                self.handle.close()

    def tracking_open(path, *args, **kwargs):
        if failure_point == "open" and path == stderr_path:
            raise PermissionError("stderr open denied")
        tracked = TrackingHandle(real_open(path, *args, **kwargs))
        opened.append(tracked)
        return tracked

    monkeypatch.setattr(memory_state, "open", tracking_open, raising=False)
    if failure_point == "mkdir":
        real_mkdir = Path.mkdir

        def failing_mkdir(path, *args, **kwargs):
            if path == stderr_path.parent:
                raise PermissionError("stderr parent denied")
            return real_mkdir(path, *args, **kwargs)

        monkeypatch.setattr(Path, "mkdir", failing_mkdir)

    popen_calls = []

    def fake_popen(*args, **kwargs):
        popen_calls.append((args, kwargs))
        if failure_point == "popen":
            raise OSError("spawn denied")
        return SimpleNamespace(pid=12345)

    monkeypatch.setattr(memory_state.subprocess, "Popen", fake_popen)
    result: object = "did not return"
    error: OSError | ValueError | None = None
    try:
        result = memory_state.spawn_detached(
            [sys.executable, "-c", "pass"],
            stdout_path=stdout_path,
            stderr_path=stderr_path,
        )
    except (OSError, ValueError) as exc:
        error = exc
    finally:
        close_calls = [handle.close_calls for handle in opened]
        for handle in opened:
            handle.cleanup()

    expected_opened = 0 if failure_point == "nul-stdout" else 2 if failure_point == "popen" else 1
    assert error is None
    assert result is None
    assert len(opened) == expected_opened
    assert close_calls == [1] * expected_opened
    assert len(popen_calls) == (1 if failure_point == "popen" else 0)


@pytest.mark.parametrize("module_name", ("precompact_capture", "session_end_capture"))
@pytest.mark.parametrize(
    "nul_field",
    (
        "transcript_path",
        "session_id",
        "trigger",
        "project_slug",
        "project_root",
        "occurred_at",
    ),
)
def test_flush_wrapper_subprocess_rejects_nul_arguments_without_spawning(
    module_name: str,
    nul_field: str,
    tmp_path: Path,
):
    vault = tmp_path / "vault"
    fake_flush = vault / "scripts" / "flush_memory.py"
    fake_flush.parent.mkdir(parents=True)
    sentinel = tmp_path / "spawned.txt"
    fake_flush.write_text(
        "import os\n"
        "from pathlib import Path\n"
        "Path(os.environ['SPAWN_SENTINEL']).write_text('spawned', encoding='utf-8')\n",
        encoding="utf-8",
    )
    project = tmp_path / "project"
    project.mkdir()
    env = {
        **os.environ,
        "LLM_WIKI_ROOT": str(vault),
        "LLM_WIKI_STATE_ROOT": str(tmp_path / "runtime"),
        "SPAWN_SENTINEL": str(sentinel),
    }
    env.pop("CLAUDE_INVOKED_BY", None)

    payload = {
        "session_id": "session-1",
        "transcript_path": str(tmp_path / "transcript.jsonl"),
        "trigger": "hook-trigger",
        "project_slug": "project",
        "project_root": str(project.resolve()),
        "occurred_at": "2026-07-29T12:00:00+00:00",
    }
    payload[nul_field] = f"{payload[nul_field]}\0forged"

    result = _run_capture_process(module_name, payload, env)

    time.sleep(0.25)
    assert result.returncode == 0, result.stderr
    assert "Traceback" not in result.stderr
    assert not sentinel.exists()


def test_precompact_inline_transcript_spawn_failure_enqueues_before_delete(
    monkeypatch,
):
    import precompact_capture

    paths = []
    queued = []

    def fail_spawn(args):
        path = Path(args[args.index("--transcript") + 1])
        assert path.exists()
        paths.append(path)
        return None

    def enqueue(transcript, event, **metadata):
        assert paths[0].exists()
        queued.append((transcript, event, metadata))
        return True

    monkeypatch.setattr(precompact_capture, "spawn_detached", fail_spawn)
    monkeypatch.setattr(
        precompact_capture,
        "_enqueue_transcript_fallback",
        enqueue,
        raising=False,
    )
    transcript = "discarded-prefix-" + "x" * (
        precompact_capture.MAX_INLINE_TRANSCRIPT_CHARS + 100
    )

    rc = _run_capture_with_stdin(
        "precompact_capture",
        {
            "session_id": "session-1",
            "transcript": transcript,
            "trigger": "opencode-compacting",
            "project_slug": "alpha",
            "occurred_at": "2026-07-29T12:34:56.100000+00:00",
        },
    )

    assert rc == 0
    assert len(paths) == 1
    assert not paths[0].exists()
    assert len(queued) == 1
    queued_transcript, event, metadata = queued[0]
    assert queued_transcript == transcript[-precompact_capture.MAX_INLINE_TRANSCRIPT_CHARS :]
    assert event == "pre-compact"
    assert metadata["session_id"] == "session-1"
    assert metadata["trigger"] == "opencode-compacting"
    assert metadata["project_slug"] == "alpha"
    assert metadata["occurred_at"] == "2026-07-29T12:34:56.100000+00:00"


def test_precompact_inline_transcript_spawn_and_enqueue_failure_retains_file(
    monkeypatch,
):
    import precompact_capture

    paths = []

    def fail_spawn(args):
        path = Path(args[args.index("--transcript") + 1])
        assert path.exists()
        paths.append(path)
        return None

    def fail_enqueue(*_args, **_kwargs):
        assert paths[0].exists()
        return False

    monkeypatch.setattr(precompact_capture, "spawn_detached", fail_spawn)
    monkeypatch.setattr(
        precompact_capture,
        "_enqueue_transcript_fallback",
        fail_enqueue,
        raising=False,
    )

    try:
        rc = _run_capture_with_stdin(
            "precompact_capture",
            {"transcript": "user: retain direct text", "trigger": "opencode-compacting"},
        )

        assert rc != 0
        assert len(paths) == 1
        assert paths[0].read_text(encoding="utf-8") == "user: retain direct text"
    finally:
        for path in paths:
            path.unlink(missing_ok=True)


def test_ephemeral_transcript_is_deleted_before_worker_summarizes():
    import flush_memory
    import precompact_capture

    transcript_path = precompact_capture._stage_inline_transcript(
        "user: bounded transcript"
    )

    text = flush_memory.read_transcript_tail(transcript_path, delete_after=True)

    assert text == "user: bounded transcript"
    assert not transcript_path.exists()


def test_failed_allowed_staged_transcript_read_retains_transport(
    monkeypatch,
):
    import flush_memory
    import precompact_capture

    transcript_path = precompact_capture._stage_inline_transcript(
        "user: retain this unreadable transport"
    )
    real_open = Path.open

    def denied_open(path, *args, **kwargs):
        if path == transcript_path:
            raise PermissionError("injected staged read denial")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", denied_open)
    try:
        assert flush_memory.read_transcript_tail(
            transcript_path,
            delete_after=True,
        ) == ""
        assert transcript_path.exists()
    finally:
        transcript_path.unlink(missing_ok=True)


def test_dedupe_early_return_still_claims_and_deletes_staged_transcript(monkeypatch):
    import flush_memory
    import precompact_capture

    transcript_path = precompact_capture._stage_inline_transcript(
        "user: retain this bounded content in memory"
    )
    monkeypatch.setattr(
        flush_memory,
        "parse_args",
        lambda: SimpleNamespace(
            event="pre-compact",
            session_id="duplicate-session",
            transcript=str(transcript_path),
            transcript_stdin=False,
            delete_transcript=True,
            trigger="opencode-compacting",
            project_slug="alpha",
            project_root="D:/alpha",
            occurred_at="2026-07-28T12:34:56+00:00",
        ),
    )
    monkeypatch.setattr(
        flush_memory,
        "_resolve_project_identity",
        lambda *_args: ("alpha", Path("D:/alpha")),
    )
    monkeypatch.setattr(flush_memory, "load_state", lambda: {})
    monkeypatch.setattr(flush_memory, "should_skip", lambda *args: True)
    monkeypatch.setattr(
        flush_memory,
        "_enqueue_transcript_fallback",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("prior dedupe is already durable")
        ),
    )
    monkeypatch.setattr(
        flush_memory,
        "summarize_with_llm",
        lambda *args: (_ for _ in ()).throw(AssertionError("dedupe must not summarize")),
    )

    assert flush_memory.main() == 0
    assert not transcript_path.exists()


def test_delete_transcript_rejects_files_outside_staging_namespace(tmp_path):
    import flush_memory

    fd, arbitrary_name = tempfile.mkstemp(prefix="other-app-", suffix=".txt")
    os.close(fd)
    fd, wrong_suffix_name = tempfile.mkstemp(
        prefix="llm-wiki-precompact-", suffix=".json"
    )
    os.close(fd)
    nested = tmp_path / "llm-wiki-precompact-nested.txt"
    nested.write_text("nested", encoding="utf-8")
    paths = [Path(arbitrary_name), Path(wrong_suffix_name), nested]
    try:
        for path in paths:
            assert flush_memory.read_transcript_tail(path, delete_after=True) == ""
            assert path.exists()
    finally:
        for path in paths:
            path.unlink(missing_ok=True)


def test_delete_transcript_rejects_staging_namespace_symlink(tmp_path):
    import flush_memory

    target = tmp_path / "private.txt"
    target.write_text("private", encoding="utf-8")
    fd, link_name = tempfile.mkstemp(
        prefix="llm-wiki-precompact-", suffix=".txt"
    )
    os.close(fd)
    link = Path(link_name)
    link.unlink()
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("file symlinks are not available on this platform")
    try:
        assert flush_memory.read_transcript_tail(link, delete_after=True) == ""
        assert link.is_symlink()
        assert target.read_text(encoding="utf-8") == "private"
    finally:
        link.unlink(missing_ok=True)


def test_delete_transcript_does_not_unlink_disallowed_path(tmp_path, monkeypatch):
    import flush_memory

    transcript_path = tmp_path / "not-an-allowed-transcript.txt"
    transcript_path.write_text("private", encoding="utf-8")
    monkeypatch.setattr(flush_memory, "_transcript_path_allowed", lambda path: False)

    text = flush_memory.read_transcript_tail(transcript_path, delete_after=True)

    assert text == ""
    assert transcript_path.exists()


def test_stdin_tail_reader_never_requests_unbounded_input():
    import flush_memory

    class BoundedOnlyStream(io.StringIO):
        def __init__(self, value):
            super().__init__(value)
            self.request_sizes = []

        def read(self, size=-1):
            assert size > 0, "reader requested an unbounded allocation"
            self.request_sizes.append(size)
            return super().read(size)

    stream = BoundedOnlyStream("a" * 100 + "TAIL")

    result = flush_memory.read_stream_tail(stream, max_chars=10, chunk_size=7)

    assert result == "aaaaaaTAIL"
    assert max(stream.request_sizes) == 7


@pytest.mark.parametrize("module_name", ("precompact_capture", "session_end_capture"))
def test_flush_wrappers_reject_oversized_hook_before_spawn(module_name, monkeypatch):
    module = __import__(module_name)
    spawned: list[list[str]] = []
    payload = json.dumps(
        {
            "session_id": "session-oversized",
            "transcript_path": "transcript.jsonl",
            "padding": "x" * 256,
        }
    )
    stream = _BoundedOnlyInput(payload)
    monkeypatch.delenv("CLAUDE_INVOKED_BY", raising=False)
    monkeypatch.setattr(module, "MAX_HOOK_STDIN_BYTES", 64, raising=False)
    monkeypatch.setattr(module, "spawn_detached", lambda args: spawned.append(args) or 1)

    with patch.object(sys, "stdin", stream):
        assert module.main() == 0

    assert stream.request_sizes and all(size > 0 for size in stream.request_sizes)
    assert spawned == []


def test_prompt_capture_rejects_oversized_hook_before_state_or_append(
    monkeypatch,
    tmp_path,
):
    import user_prompt_capture

    project = tmp_path / "project"
    project.mkdir()
    state_updates: list[dict] = []
    appended: list[tuple] = []
    payload = json.dumps(
        {
            "prompt": "Capture this meaningful prompt",
            "session_id": "session-oversized",
            "cwd": str(project),
            "padding": "x" * 256,
        }
    )
    stream = _BoundedOnlyInput(payload)
    monkeypatch.setattr(user_prompt_capture, "ROOT", tmp_path / "vault")
    monkeypatch.setattr(user_prompt_capture, "MAX_HOOK_STDIN_BYTES", 64, raising=False)
    monkeypatch.setattr(user_prompt_capture, "_compute_slug_from_cwd", lambda _cwd: "project")

    def track_update(mutator):
        state: dict = {}
        mutator(state)
        state_updates.append(state)
        return state

    monkeypatch.setattr(user_prompt_capture, "update_state", track_update)
    monkeypatch.setattr(
        user_prompt_capture,
        "_append_prompt_tag",
        lambda *args: appended.append(args),
    )

    with patch.object(sys, "stdin", stream):
        assert user_prompt_capture.main() == 0

    assert stream.request_sizes and all(size > 0 for size in stream.request_sizes)
    assert state_updates == []
    assert appended == []


def test_tool_capture_rejects_oversized_hook_before_state_or_append(
    monkeypatch,
    tmp_path,
):
    import post_tool_capture

    project = tmp_path / "project"
    project.mkdir()
    state_updates: list[dict] = []
    appended: list[tuple] = []
    payload = json.dumps(
        {
            "tool_name": "Edit",
            "tool_input": {"filePath": "src/app.py"},
            "session_id": "session-oversized",
            "cwd": str(project),
            "padding": "x" * 256,
        }
    )
    stream = _BoundedOnlyInput(payload)
    monkeypatch.setattr(post_tool_capture, "ROOT", tmp_path / "vault")
    monkeypatch.setattr(post_tool_capture, "MAX_HOOK_STDIN_BYTES", 64, raising=False)
    monkeypatch.setattr(post_tool_capture, "_compute_slug_from_cwd", lambda _cwd: "project")

    def track_update(mutator):
        state: dict = {}
        mutator(state)
        state_updates.append(state)
        return state

    monkeypatch.setattr(post_tool_capture, "update_state", track_update)
    monkeypatch.setattr(
        post_tool_capture,
        "_append_tool_tag",
        lambda *args: appended.append(args),
    )

    with patch.object(sys, "stdin", stream):
        assert post_tool_capture.main() == 0

    assert stream.request_sizes and all(size > 0 for size in stream.request_sizes)
    assert state_updates == []
    assert appended == []


def test_session_end_tag_rejects_oversized_hook_before_append(monkeypatch, tmp_path):
    import session_end_project_tag

    vault = tmp_path / "vault"
    project = tmp_path / "project"
    (vault / "knowledge").mkdir(parents=True)
    project.mkdir()
    appended: list[tuple[Path, str]] = []
    payload = json.dumps(
        {
            "session_id": "session-oversized",
            "cwd": str(project),
            "padding": "x" * 256,
        }
    )
    stream = _BoundedOnlyInput(payload)
    monkeypatch.setenv("LLM_WIKI_ROOT", str(vault))
    monkeypatch.setattr(session_end_project_tag, "MAX_HOOK_STDIN_BYTES", 64, raising=False)
    monkeypatch.setattr(session_end_project_tag, "_resolve_project_dir", lambda _payload: project)
    monkeypatch.setattr(session_end_project_tag, "_compute_slug", lambda *_args: "project")
    monkeypatch.setattr(
        session_end_project_tag,
        "_append_entry",
        lambda path, entry: appended.append((path, entry)),
    )

    with patch.object(sys, "stdin", stream):
        assert session_end_project_tag.main() == 0

    assert stream.request_sizes and all(size > 0 for size in stream.request_sizes)
    assert appended == []


@pytest.mark.parametrize(
    ("raw", "max_bytes"),
    (
        ("{not json", 64),
        (json.dumps({"padding": "x" * 128}), 16),
    ),
    ids=("malformed", "oversized"),
)
def test_session_end_tag_rejects_input_before_missing_vault_logging(
    monkeypatch,
    tmp_path,
    raw,
    max_bytes,
):
    import session_end_project_tag

    stream = _BoundedOnlyInput(raw)
    monkeypatch.setenv("LLM_WIKI_ROOT", str(tmp_path / "missing-vault"))
    monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(tmp_path / "runtime"))
    monkeypatch.setattr(
        session_end_project_tag,
        "MAX_HOOK_STDIN_BYTES",
        max_bytes,
    )

    with patch.object(sys, "stdin", stream):
        assert session_end_project_tag.main() == 0

    assert stream.request_sizes and all(size > 0 for size in stream.request_sizes)
    assert list(tmp_path.iterdir()) == []


def test_session_end_tag_requires_one_parser_confirmed_scoped_record(
    monkeypatch,
    tmp_path,
):
    import session_end_project_tag

    vault = tmp_path / "vault"
    project = (tmp_path / "project").resolve()
    other = (tmp_path / "other").resolve()
    (vault / "knowledge").mkdir(parents=True)
    project.mkdir()
    other.mkdir()
    appended: list[tuple[Path, str]] = []
    monkeypatch.setenv("LLM_WIKI_ROOT", str(vault))
    monkeypatch.setattr(
        session_end_project_tag,
        "_resolve_project_dir",
        lambda _payload: project,
    )
    monkeypatch.setattr(
        session_end_project_tag,
        "_compute_slug",
        lambda *_args: "project",
    )
    monkeypatch.setattr(
        session_end_project_tag,
        "_append_entry",
        lambda path, entry: appended.append((path, entry)),
    )

    def record(slug, root):
        return SimpleNamespace(
            kind="heading",
            slug=slug,
            project_root=str(root),
            meaningful=False,
        )

    rejected_results = (
        [],
        [record("other", project)],
        [record("PROJECT", other)],
        [record("project", project), record("project", project)],
    )
    for result in rejected_results:
        monkeypatch.setattr(
            session_end_project_tag,
            "parse_daily_records",
            lambda _entry, current=result: current,
            raising=False,
        )
        with patch.object(
            sys,
            "stdin",
            io.StringIO(json.dumps({"session_id": "session-1"})),
        ):
            assert session_end_project_tag.main() == 0

    assert appended == []

    monkeypatch.setattr(
        session_end_project_tag,
        "parse_daily_records",
        lambda _entry: [record("PROJECT", project)],
        raising=False,
    )
    with patch.object(
        sys,
        "stdin",
        io.StringIO(json.dumps({"session_id": "session-1"})),
    ):
        assert session_end_project_tag.main() == 0

    assert len(appended) == 1


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
    monkeypatch.setattr(
        user_prompt_capture, "update_state", lambda mutator: mutator({})
    )

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


def test_prompt_capture_uses_agent_root_for_nested_cwd(monkeypatch, tmp_path: Path):
    import user_prompt_capture

    vault = tmp_path / "vault"
    project = tmp_path / "project"
    nested = project / "src"
    vault.mkdir()
    nested.mkdir(parents=True)
    monkeypatch.setattr(user_prompt_capture, "ROOT", vault)
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(project))
    resolved: list[str] = []
    captured: list[tuple] = []
    monkeypatch.setattr(
        user_prompt_capture,
        "_compute_slug_from_cwd",
        lambda root: resolved.append(root) or "project",
    )
    monkeypatch.setattr(
        user_prompt_capture,
        "_capture_prompt_once",
        lambda *args: captured.append(args),
    )

    assert _run_capture_with_stdin(
        "user_prompt_capture",
        {
            "prompt": "Capture this nested project prompt",
            "session_id": "session-1",
            "cwd": str(nested),
        },
    ) == 0

    assert resolved == [str(project.resolve())]
    assert captured[0][1] == project.resolve()


def test_prompt_capture_folds_multiline_preview_to_prevent_block_injection(monkeypatch):
    import daily_log_append  # noqa: WPS433
    import user_prompt_capture  # noqa: WPS433

    blocks = []
    monkeypatch.setattr(
        daily_log_append,
        "append_daily",
        lambda slug, session_id, block: blocks.append(block),
    )

    user_prompt_capture._append_prompt_tag(
        "test-slug",
        Path("D:/projects/test-slug"),
        "session-123",
        "Refactor auth\n## forged heading\n- forged block",
    )

    assert len(blocks) == 1
    assert blocks[0].splitlines() == [
        blocks[0].splitlines()[0]
    ]
    assert "Refactor auth ## forged heading - forged block" in blocks[0]


def test_prompt_capture_escapes_capture_marker_prefix(monkeypatch):
    import daily_log_append
    import user_prompt_capture

    blocks = []
    monkeypatch.setattr(
        daily_log_append,
        "append_daily",
        lambda slug, session_id, block: blocks.append(block),
    )
    forged = f"<!-- llm-wiki-capture: {'c' * 64} -->"

    user_prompt_capture._append_prompt_tag(
        "test-slug",
        Path("D:/projects/test-slug"),
        "session-123",
        f"Explain {forged} without creating metadata",
    )

    assert len(blocks) == 1
    assert forged not in blocks[0]
    assert f"&lt;!-- llm-wiki-capture: {'c' * 64} -->" in blocks[0]


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


def test_tool_capture_logs_file_mutation(monkeypatch, tmp_path):
    """A direct file mutation produces a breadcrumb."""
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
    monkeypatch.setattr(
        post_tool_capture, "update_state", lambda mutator: mutator({})
    )

    project_cwd = tmp_path / "project"
    project_cwd.mkdir()
    rc = _run_capture_with_stdin(
        "post_tool_capture",
        {
            "tool_name": "Edit",
            "tool_input": {"filePath": "src/auth.py\n## forged heading"},
            "session_id": "abc123def456",
            "cwd": str(project_cwd),
        },
    )
    assert rc == 0
    rc = _run_capture_with_stdin(
        "post_tool_capture",
        {
            "tool_name": "apply_patch",
            "tool_input": {
                "patchText": (
                    "*** Begin Patch\n"
                    "*** Update File: src/patched.py\n"
                    "*** End Patch"
                )
            },
            "session_id": "abc123def456",
            "cwd": str(project_cwd),
        },
    )
    assert rc == 0
    rc = _run_capture_with_stdin(
        "post_tool_capture",
        {
            "tool_name": "NotebookEdit",
            "tool_input": {"notebook_path": "notebooks/analysis.ipynb"},
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
    assert "src/auth.py ## forged heading" in content
    assert "\n## forged heading" not in content
    assert "ApplyPatch" in content
    assert "src/patched.py" in content
    assert "NotebookEdit" in content
    assert "notebooks/analysis.ipynb" in content
    assert "test-slug" in content


def test_tool_capture_rejects_all_shell_breadcrumbs(
    monkeypatch, tmp_path
):
    """Shell command text is never a mutation breadcrumb."""
    import post_tool_capture  # noqa: WPS433

    monkeypatch.setattr(post_tool_capture, "ROOT", tmp_path / "vault")
    captured = []
    monkeypatch.setattr(
        post_tool_capture,
        "_capture_tool_once",
        lambda slug, session_id, tool, target, project_identity="": captured.append(target),
    )

    commands = [
        "pwd",
        "git status",
        "git status --short",
        "git diff",
        "git diff --cached --stat",
        "Get-ChildItem -Force",
        "ls -la src",
        "dir /b",
        "uv run pytest -q",
        "git diff > review.patch",
        "ls | Select-String py",
    ]
    for tool_name in ("Bash", "Shell"):
        for command in commands:
            rc = _run_capture_with_stdin(
                "post_tool_capture",
                {
                    "tool_name": tool_name,
                    "tool_input": {"command": command},
                    "session_id": "s1",
                    "cwd": str(tmp_path / "project"),
                },
            )
            assert rc == 0

    assert captured == []


def test_tool_capture_skips_empty_file_targets(monkeypatch, tmp_path):
    """File-changing tools without a path must not create blank breadcrumbs."""
    import post_tool_capture  # noqa: WPS433

    appended = []
    monkeypatch.setattr(post_tool_capture, "ROOT", tmp_path / "vault")
    monkeypatch.setattr(post_tool_capture, "_append_tool_tag", lambda *a: appended.append(a))

    rc = _run_capture_with_stdin(
        "post_tool_capture",
        {
            "tool_name": "Edit",
            "tool_input": {"filePath": "   "},
            "session_id": "s1",
            "cwd": str(tmp_path / "project"),
        },
    )

    assert rc == 0
    assert appended == []


def test_tool_capture_skips_vault_internal_sessions_and_targets(monkeypatch, tmp_path):
    """Tool calls whose cwd or resolved mutation target is in ROOT are skipped."""
    import post_tool_capture  # noqa: WPS433

    fake_root = tmp_path / "vault"
    fake_root.mkdir()
    project = tmp_path / "project"
    project.mkdir()
    captured = []
    monkeypatch.setattr(post_tool_capture, "ROOT", fake_root)
    monkeypatch.setattr(
        post_tool_capture,
        "_capture_tool_once",
        lambda *args, **kwargs: captured.append((args, kwargs)),
    )

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
    assert captured == []

    rc = _run_capture_with_stdin(
        "post_tool_capture",
        {
            "tool_name": "Edit",
            "tool_input": {
                "filePath": str(fake_root / "knowledge" / "daily" / "capture.md"),
                "workdir": str(project),
            },
            "session_id": "s1",
            "cwd": str(project),
        },
    )
    assert rc == 0
    assert captured == []

    def fail_resolution(*_args):
        raise OSError("resolution failed")

    monkeypatch.setattr(post_tool_capture, "_resolved_target", fail_resolution)
    rc = _run_capture_with_stdin(
        "post_tool_capture",
        {
            "tool_name": "Edit",
            "tool_input": {"filePath": "src/unresolved.py"},
            "session_id": "s1",
            "cwd": str(project),
            "project_root": str(project),
        },
    )
    assert rc == 0
    assert captured == []


def test_tool_capture_rejects_cwd_outside_explicit_project_root(
    monkeypatch,
    tmp_path: Path,
):
    import post_tool_capture

    vault = tmp_path / "vault"
    project = tmp_path / "project"
    unrelated = tmp_path / "unrelated"
    for directory in (vault, project, unrelated):
        directory.mkdir()
    monkeypatch.setattr(post_tool_capture, "ROOT", vault)
    monkeypatch.setattr(
        post_tool_capture,
        "_compute_slug_from_cwd",
        lambda _root: "project",
    )
    captured: list[tuple] = []
    monkeypatch.setattr(
        post_tool_capture,
        "_capture_tool_once",
        lambda *args, **kwargs: captured.append((args, kwargs)),
    )

    assert _run_capture_with_stdin(
        "post_tool_capture",
        {
            "tool_name": "Edit",
            "tool_input": {"filePath": "src/cross-project.py"},
            "session_id": "session-1",
            "cwd": str(unrelated),
            "project_root": str(project),
        },
    ) == 0

    assert captured == []


def test_tool_capture_state_key_does_not_persist_raw_target(tmp_path):
    vault = tmp_path / "vault"
    state_root = tmp_path / "state"
    project = tmp_path / "project"
    vault.mkdir()
    state_root.mkdir()
    (project / ".git").mkdir(parents=True)
    template = vault / "knowledge" / "projects" / "_template" / "state.md"
    template.parent.mkdir(parents=True)
    template.write_text(
        "# <Project Name>\n- Project root JSON: <absolute path JSON>\n",
        encoding="utf-8",
    )
    env = os.environ.copy()
    env.update({"LLM_WIKI_ROOT": str(vault), "LLM_WIKI_STATE_ROOT": str(state_root)})
    secret_target = "src/super-secret-value.py"
    run_dir = state_root / "run"
    run_dir.mkdir()
    state_file = run_dir / "state.json"
    state_file.write_text(
        json.dumps(
            {
                "tool_capture_dedupe": {
                    "project::Edit::src/legacy-secret.py": "2026-01-01T00:00:00",
                    "v1:" + "a" * 64: "2026-01-01T00:00:00",
                },
                "unrelated": {"keep": "unchanged"},
            }
        ),
        encoding="utf-8",
    )

    result = _run_capture_process(
        "post_tool_capture",
        {
            "tool_name": "Edit",
            "tool_input": {"filePath": secret_target},
            "session_id": "s1",
            "cwd": str(project),
        },
        env,
    )

    assert result.returncode == 0, result.stderr
    state_text = state_file.read_text(encoding="utf-8")
    assert secret_target not in state_text
    assert "super-secret-value" not in state_text
    assert "legacy-secret" not in state_text
    state = json.loads(state_text)
    keys = state["tool_capture_dedupe"]
    assert all(len(key) <= 67 for key in keys)
    assert "v1:" + "a" * 64 in keys
    assert state["unrelated"] == {"keep": "unchanged"}


def test_tool_capture_append_failure_does_not_suppress_retry(tmp_path):
    vault = tmp_path / "vault"
    state_root = tmp_path / "state"
    project = tmp_path / "project"
    (vault / "knowledge").mkdir(parents=True)
    state_root.mkdir()
    (project / ".git").mkdir(parents=True)
    template = vault / "knowledge" / "projects" / "_template" / "state.md"
    template.parent.mkdir(parents=True)
    template.write_text(
        "# <Project Name>\n- Project root JSON: <absolute path JSON>\n",
        encoding="utf-8",
    )
    blocked_daily = vault / "knowledge" / "daily"
    blocked_daily.write_text("not a directory", encoding="utf-8")
    env = os.environ.copy()
    env.update({"LLM_WIKI_ROOT": str(vault), "LLM_WIKI_STATE_ROOT": str(state_root)})
    payload = {
        "tool_name": "Edit",
        "tool_input": {"filePath": "src/retry.py"},
        "session_id": "s1",
        "cwd": str(project),
    }

    first = _run_capture_process("post_tool_capture", payload, env)
    assert first.returncode == 0
    state_file = state_root / "run" / "state.json"
    first_state = json.loads(state_file.read_text(encoding="utf-8")) if state_file.exists() else {}
    assert first_state.get("tool_capture_dedupe", {}) == {}

    blocked_daily.unlink()
    second = _run_capture_process("post_tool_capture", payload, env)

    assert second.returncode == 0, second.stderr
    daily_files = list(blocked_daily.glob("*.md"))
    assert len(daily_files) == 1
    assert daily_files[0].read_text(encoding="utf-8").count("src/retry.py") == 1


def test_tool_capture_concurrent_hooks_append_once(tmp_path):
    vault = tmp_path / "vault"
    state_root = tmp_path / "state"
    project = tmp_path / "project"
    vault.mkdir()
    state_root.mkdir()
    (project / ".git").mkdir(parents=True)
    template = vault / "knowledge" / "projects" / "_template" / "state.md"
    template.parent.mkdir(parents=True)
    template.write_text(
        "# <Project Name>\n- Project root JSON: <absolute path JSON>\n",
        encoding="utf-8",
    )
    env = os.environ.copy()
    env.update({"LLM_WIKI_ROOT": str(vault), "LLM_WIKI_STATE_ROOT": str(state_root)})
    payload = {
        "tool_name": "Edit",
        "tool_input": {"filePath": "src/concurrent.py"},
        "session_id": "s1",
        "cwd": str(project),
    }
    workers = 12
    barrier = threading.Barrier(workers)

    def run_hook():
        barrier.wait()
        return _run_capture_process("post_tool_capture", payload, env)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(lambda _: run_hook(), range(workers)))

    assert all(result.returncode == 0 for result in results)
    daily_files = list((vault / "knowledge" / "daily").glob("*.md"))
    assert len(daily_files) == 1
    content = daily_files[0].read_text(encoding="utf-8")
    assert content.count("src/concurrent.py") == 1


def test_tool_capture_atomic_transaction_claims_once(monkeypatch):
    import post_tool_capture  # noqa: WPS433

    state = {}
    state_lock = threading.Lock()
    appended = []

    def locked_update(mutator):
        with state_lock:
            mutator(state)
            return state

    def slow_append(*args):
        time.sleep(0.01)
        appended.append(args)

    monkeypatch.setattr(post_tool_capture, "update_state", locked_update)
    monkeypatch.setattr(post_tool_capture, "_append_tool_tag", slow_append)

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(
            pool.map(
                    lambda _: post_tool_capture._capture_tool_once(
                        "project",
                        "session-1",
                        "Edit",
                        "src/atomic.py",
                        project_root=Path("D:/projects/project"),
                    ),
                range(8),
            )
        )

    assert len(appended) == 1


# ---------------------------------------------------------------------------
# Atomic prompt dedupe
# ---------------------------------------------------------------------------


def test_prompt_capture_atomic_transaction_dedupes_repeated_prompt(monkeypatch):
    import user_prompt_capture  # noqa: WPS433

    state = {}
    appended = []
    monkeypatch.setattr(
        user_prompt_capture,
        "update_state",
        lambda mutator: (mutator(state), state)[1],
    )
    monkeypatch.setattr(
        user_prompt_capture,
        "_append_prompt_tag",
        lambda *args: appended.append(args),
    )

    root = Path("D:/projects/slug")
    user_prompt_capture._capture_prompt_once("slug", root, "s1", "same prompt", "abc")
    user_prompt_capture._capture_prompt_once("slug", root, "s1", "same prompt", "abc")

    assert len(appended) == 1


@pytest.mark.parametrize("module_name", ("user_prompt_capture", "post_tool_capture"))
def test_capture_identity_failure_has_no_folder_name_fallback(
    module_name,
    monkeypatch,
    tmp_path,
):
    import session_start_project_state

    capture_module = __import__(module_name)
    project = tmp_path / "Alpha`[Beta]#(Gamma)_Delta!"
    project.mkdir()

    def fail_confirmation(_project, _projects):
        raise RuntimeError("simulated confirmation failure")

    monkeypatch.setattr(
        session_start_project_state,
        "confirm_project_identity",
        fail_confirmation,
        raising=False,
    )

    slug = capture_module._compute_slug_from_cwd(str(project))

    assert slug is None


def test_two_unclaimed_same_basename_prompts_are_not_captured(monkeypatch, tmp_path):
    import user_prompt_capture

    vault = tmp_path / "vault"
    (vault / "knowledge" / "projects").mkdir(parents=True)
    first = tmp_path / "workspace-a" / "service"
    second = tmp_path / "workspace-b" / "service"
    first.mkdir(parents=True)
    second.mkdir(parents=True)
    monkeypatch.setattr(user_prompt_capture, "ROOT", vault)
    captured: list[tuple] = []
    monkeypatch.setattr(
        user_prompt_capture,
        "_capture_prompt_once",
        lambda *args: captured.append(args),
    )

    for project in (first, second):
        monkeypatch.setattr(
            user_prompt_capture,
            "_read_hook_input",
            lambda project=project: {
                "prompt": "This prompt must not receive an unconfirmed project alias.",
                "session_id": f"session-{project.parent.name}",
                "cwd": str(project),
            },
        )
        assert user_prompt_capture.main() == 0

    assert captured == []
