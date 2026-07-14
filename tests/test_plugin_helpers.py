"""Phase 5 tests: OpenCode plugin's Python file-IO helpers.

Locks in:
1. Each helper exits 0 on empty/malformed stdin (never breaks plugin).
2. Each helper writes the expected artifact (daily log line / state heartbeat).
3. Each helper is idempotent across multiple calls within the same day.
"""
from __future__ import annotations

import io
import json
import os
import stat
import subprocess
import sys
from argparse import Namespace
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

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


FIXED_TIME = datetime(2026, 7, 13, 12, 30, 45, tzinfo=timezone.utc)


def _semantic(envelope):
    return envelope.to_dict()


def test_equivalent_lifecycle_inputs_normalize_to_same_semantics():
    from integration_adapter import normalize_event

    timestamp = "2026-07-13T12:30:45Z"
    common = {
        "session": "session-1",
        "worktree": "C:/work/app",
        "reason": "completed",
        "transcript": "C:/tmp/session.jsonl",
    }
    events = [
        normalize_event(
            "claude",
            "session_end",
            {
                "session_id": common["session"],
                "cwd": common["worktree"],
                "reason": common["reason"],
                "transcript_path": common["transcript"],
                "timestamp": timestamp,
            },
            captured_at=FIXED_TIME,
        ),
        normalize_event(
            "opencode",
            "session_end",
            {
                "sessionInfo": {"id": common["session"]},
                "directory": common["worktree"],
                "reason": common["reason"],
                "transcriptPath": common["transcript"],
                "timestamp": timestamp,
            },
            captured_at=FIXED_TIME,
        ),
        normalize_event(
            "codex",
            "session_end",
            {
                "session_id": common["session"],
                "cwd": common["worktree"],
                "reason": common["reason"],
                "transcript": common["transcript"],
                "timestamp": timestamp,
            },
            captured_at=FIXED_TIME,
        ),
    ]

    assert [_semantic(event) for event in events] == [_semantic(events[0])] * 3
    assert [event.agent for event in events] == [None, None, None]
    assert events[0].payload == {
        "reason": "completed",
        "transcript_path": "C:/tmp/session.jsonl",
    }


def test_normalizer_leaves_missing_optional_source_fields_null():
    from integration_adapter import normalize_event

    envelope = normalize_event(
        "opencode",
        "session_end",
        {},
        occurred_at=FIXED_TIME,
        captured_at=FIXED_TIME,
    )

    data = envelope.to_dict()
    assert data["agent"] is None
    assert data["session"] is None
    assert data["project"] is None
    assert data["worktree"] is None
    assert data["severity"] is None
    assert data["parent_event_id"] is None
    assert data["source_event_id"] is None
    assert data["payload"] == {"reason": None, "transcript_path": None}


def test_normalizer_maps_explicit_host_event_id():
    from integration_adapter import normalize_event

    first = normalize_event("opencode", "session_start", {"event_id": "host-1"})
    second = normalize_event("opencode", "session_start", {"event_id": "host-2"})

    assert first.source_event_id == "host-1"
    assert first.event_id != second.event_id


def test_normalizer_redacts_before_hashing():
    from integration_adapter import normalize_event

    secret = "sk-abcdefghijklmnopqrstuvwxyz012345"
    first = normalize_event(
        "claude",
        "session_end",
        {"reason": f"token={secret}"},
        occurred_at=FIXED_TIME,
        captured_at=FIXED_TIME,
    )
    second = normalize_event(
        "claude",
        "session_end",
        {"reason": "token=[REDACTED]"},
        occurred_at=FIXED_TIME,
        captured_at=FIXED_TIME,
    )

    assert secret not in first.to_json()
    assert first.content_hash == second.content_hash


def test_normalizer_redacts_source_fields_before_envelope_storage():
    from integration_adapter import normalize_event

    secret = "sk-abcdefghijklmnopqrstuvwxyz012345"
    envelope = normalize_event(
        "claude",
        "session_end",
        {"session_id": secret, "cwd": f"C:/work/{secret}"},
        occurred_at=FIXED_TIME,
        captured_at=FIXED_TIME,
    )

    assert secret not in envelope.to_json()


def test_opencode_tool_names_are_mapped_to_shared_capture_names():
    from integration_adapter import normalize_event

    envelope = normalize_event(
        "opencode",
        "post_tool_use",
        {"tool": "edit", "input": {"filePath": "src/auth.py"}},
        occurred_at=FIXED_TIME,
        captured_at=FIXED_TIME,
    )

    assert envelope.payload == {
        "tool_name": "Edit",
        "target": "src/auth.py",
        "changed": True,
        "dirty": True,
        "significant": True,
    }


def test_session_transcript_text_is_redacted_and_bounded():
    from integration_adapter import MAX_TRANSCRIPT_TEXT_CHARS, normalize_event

    secret = "sk-abcdefghijklmnopqrstuvwxyz012345"
    envelope = normalize_event(
        "opencode",
        "session_end",
        {"transcript_text": f"token={secret}"},
        occurred_at=FIXED_TIME,
        captured_at=FIXED_TIME,
    )

    assert envelope.payload["transcript_text"] == "token=[REDACTED]"
    with pytest.raises(ValueError, match="invalid integration event"):
        normalize_event(
            "opencode",
            "session_end",
            {"transcript_text": "x" * (MAX_TRANSCRIPT_TEXT_CHARS + 1)},
        )


def test_invalid_input_fails_closed_but_host_cli_exits_safely(capsys):
    import integration_adapter

    secret = "sk-abcdefghijklmnopqrstuvwxyz012345"
    with pytest.raises(ValueError, match="invalid integration event"):
        integration_adapter.normalize_event(
            "opencode",
            "unknown",
            {"payload": secret},
            occurred_at=FIXED_TIME,
            captured_at=FIXED_TIME,
        )

    with patch.object(sys, "stdin", io.StringIO(json.dumps({"payload": secret}))):
        assert integration_adapter.main(
            ["--source", "opencode", "--event", "unknown"]
        ) == 0
    assert secret not in capsys.readouterr().err


def test_delegate_receives_only_normalized_redacted_payload(monkeypatch, capsys):
    import integration_adapter

    secret = "sk-abcdefghijklmnopqrstuvwxyz012345"
    observed = {}

    def fake_run(*args, **kwargs):
        observed.update(kwargs)
        return SimpleNamespace(returncode=0, stdout="context", stderr=secret)

    monkeypatch.setattr(integration_adapter.subprocess, "run", fake_run)
    raw = {
        "session_id": secret,
        "cwd": f"C:/work/{secret}",
        "reason": f"token={secret}",
        "transcript_path": f"C:/tmp/{secret}.jsonl",
        "host_only": secret,
    }
    with patch.object(sys, "stdin", io.StringIO(json.dumps(raw))):
        rc = integration_adapter.main(
            [
                "--source",
                "claude",
                "--event",
                "session_end",
                "--delegate",
                "session_end_capture.py",
            ]
        )

    assert rc == 0
    delegated = json.loads(observed["input"])
    assert "host_only" not in delegated
    assert secret not in observed["input"]
    assert delegated["session_id"] == "[REDACTED_API_KEY]"
    assert delegated["reason"] == "token=[REDACTED]"
    assert capsys.readouterr().out == ""


def test_claude_context_delegate_preserves_stdout(monkeypatch, capsys, tmp_path):
    import integration_adapter

    monkeypatch.setattr(
        integration_adapter.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0,
            stdout='{"hookSpecificOutput": {"additionalContext": "safe"}}',
            stderr="",
        ),
    )
    with patch.object(sys, "stdin", io.StringIO("{}")):
        rc = integration_adapter.main(
            [
                "--source",
                "claude",
                "--event",
                "session_start",
                "--delegate",
                "session_start_context.py",
            ]
        )

    assert rc == 0
    assert json.loads(capsys.readouterr().out)["hookSpecificOutput"]["additionalContext"] == "safe"

    spawned = []
    monkeypatch.setattr(integration_adapter, "_project_context", lambda _event: ("app", Path("project")))
    monkeypatch.setattr(integration_adapter, "_record_activity", lambda *_args: True)
    monkeypatch.setattr(
        integration_adapter,
        "spawn_detached",
        lambda args: spawned.append(args) or 123,
    )
    monkeypatch.setattr(
        integration_adapter,
        "spawn_compile_if_idle",
        lambda: (_ for _ in ()).throw(AssertionError("compile ran in callback")),
    )
    monkeypatch.setattr(
        integration_adapter,
        "build_session_start_context",
        lambda: "# Project memory context\n\n## Health\n\nScheduler degraded.\n",
    )

    result = integration_adapter.ingest_event(
        integration_adapter.normalize_event("opencode", "session_start", {})
    )

    assert result["heartbeat_recorded"] is True
    assert result["maintenance_scheduled"] is True
    assert "## Health" in result["context"]
    assert spawned == [[
        sys.executable,
        str(integration_adapter.SCRIPTS_DIR / "integration_adapter.py"),
        "--maintenance",
    ]]

    pending_daily = tmp_path / "knowledge" / "daily" / "2026-07-13.md"
    order = []

    def drain(*args, **kwargs):
        assert args[0][-1] == "drain"
        assert kwargs["timeout"] == integration_adapter.MAINTENANCE_DRAIN_TIMEOUT_SECONDS
        pending_daily.parent.mkdir(parents=True)
        pending_daily.write_text("pending", encoding="utf-8")
        order.append("drain")
        return subprocess.CompletedProcess(args[0], 1, "secret stdout", "secret stderr")

    def compile_after_drain():
        assert pending_daily.exists()
        order.append("compile")
        return True, "spawned"

    monkeypatch.setattr(integration_adapter.subprocess, "run", drain)
    monkeypatch.setattr(integration_adapter, "spawn_compile_if_idle", compile_after_drain)
    assert integration_adapter.main(["--maintenance"]) == 0
    assert order == ["drain", "compile"]
    assert capsys.readouterr() == ("", "")

    secret = "sk-abcdefghijklmnopqrstuvwxyz012345"
    monkeypatch.setattr(
        integration_adapter.subprocess,
        "run",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            subprocess.TimeoutExpired(args[0], kwargs["timeout"], output=secret)
        ),
    )
    monkeypatch.setattr(
        integration_adapter,
        "spawn_compile_if_idle",
        lambda: (False, "secret=" + secret),
    )
    assert integration_adapter.main(["--maintenance"]) == 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""
    assert secret not in captured.out + captured.err


@pytest.mark.parametrize("delegate", ["user_prompt_capture.py", "session_start_context.py"])
def test_delegate_forwards_only_valid_hook_json(monkeypatch, capsys, delegate):
    import integration_adapter

    outputs = [
        '{"hookSpecificOutput":{}}',
        '{"hookSpecificOutput":{"additionalContext":"safe"}}',
    ]
    monkeypatch.setattr(
        integration_adapter.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0, stdout=outputs.pop(0), stderr=""
        ),
    )
    event = "user_prompt" if delegate == "user_prompt_capture.py" else "session_start"
    raw = {"prompt": "valid prompt"} if event == "user_prompt" else {}

    with patch.object(sys, "stdin", io.StringIO(json.dumps(raw))):
        assert integration_adapter.main(
            ["--source", "claude", "--event", event, "--delegate", delegate]
        ) == 0
    assert capsys.readouterr().out == ""

    with patch.object(sys, "stdin", io.StringIO(json.dumps(raw))):
        assert integration_adapter.main(
            ["--source", "claude", "--event", event, "--delegate", delegate]
        ) == 0
    assert json.loads(capsys.readouterr().out)["hookSpecificOutput"]["additionalContext"] == "safe"


def test_delegate_timeout_is_bounded_and_secret_free(monkeypatch, capsys):
    import integration_adapter

    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(args="secret", timeout=kwargs["timeout"])

    monkeypatch.setattr(integration_adapter.subprocess, "run", timeout)
    with patch.object(sys, "stdin", io.StringIO("{}")):
        assert integration_adapter.main(
            [
                "--source",
                "claude",
                "--event",
                "session_start",
                "--delegate",
                "session_start_context.py",
            ]
        ) == 0

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "integration_adapter: capture skipped\n"


def test_failed_delegate_does_not_forward_valid_hook_json(monkeypatch, capsys):
    import integration_adapter

    monkeypatch.setattr(
        integration_adapter.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=1,
            stdout='{"hookSpecificOutput":{"additionalContext":"unsafe"}}',
            stderr="",
        ),
    )
    with patch.object(sys, "stdin", io.StringIO("{}")):
        assert integration_adapter.main(
            [
                "--source",
                "claude",
                "--event",
                "session_start",
                "--delegate",
                "session_start_context.py",
            ]
        ) == 0

    assert capsys.readouterr().out == ""


def test_codex_run_script_timeout_returns_fixed_failure(monkeypatch, tmp_path):
    import codex_memory

    def timeout(*args, **kwargs):
        assert kwargs["timeout"] > 0
        raise subprocess.TimeoutExpired(cmd="secret", timeout=kwargs["timeout"])

    monkeypatch.setattr(codex_memory.subprocess, "run", timeout)

    result = codex_memory._run_script("session_end_project_tag.py", tmp_path, "{}")

    assert result.returncode == 124
    assert result.stdout == ""
    assert result.stderr == ""


def test_codex_lookup_tier_uses_bounded_runner_and_fixed_timeout_error(
    monkeypatch, capsys
):
    import codex_memory

    calls = []
    monkeypatch.setattr(
        codex_memory,
        "_run_script",
        lambda name, project_dir, stdin_text="": calls.append(
            (name, project_dir, stdin_text)
        )
        or subprocess.CompletedProcess([], 124, "secret stdout", "secret stderr"),
    )

    assert codex_memory.command_lookup_tier(Namespace()) == 124
    assert calls[0][0] == "lookup_mode.py"
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "codex_memory: lookup timed out\n"


def test_codex_daily_log_uses_shared_ingest_policy(monkeypatch, tmp_path):
    import codex_memory

    observed = {}

    def fake_ingest(envelope, **kwargs):
        observed["envelope"] = envelope
        observed.update(kwargs)
        return {
            "slug": "project",
            "heartbeat_recorded": True,
            "daily_log_written": False,
            "flush_spawned": False,
            "transcript_path": None,
        }

    monkeypatch.setattr(codex_memory, "ingest_event", fake_ingest, raising=False)

    rc = codex_memory.command_daily_log(
        Namespace(
            cwd=str(tmp_path),
            reason="turn-end",
            session_id="session-1",
            transcript="",
            trigger="codex",
            force_stub=False,
            json=True,
        )
    )

    assert rc == 0
    assert observed["envelope"].session == "session-1"
    assert observed["force_stub"] is False
    assert observed["trigger"] == "codex"
    source = (SCRIPTS_DIR / "codex_memory.py").read_text(encoding="utf-8")
    assert "def _record_heartbeat" not in source
    assert "def _spawn_flush_memory" not in source


@pytest.mark.parametrize("event_type", ["session_start", "session_end", "pre_compact"])
def test_shared_ingest_persists_heartbeat_with_derived_slug(
    tmp_path, monkeypatch, event_type
):
    import integration_adapter

    vault = tmp_path / "vault"
    state_root = tmp_path / "state root"
    project = tmp_path / "Project With Spaces"
    (vault / "knowledge" / "projects").mkdir(parents=True)
    project.mkdir()
    monkeypatch.setattr(integration_adapter, "ROOT", vault)
    monkeypatch.setattr(integration_adapter, "STATE_ROOT", state_root)
    monkeypatch.setenv("LLM_WIKI_ROOT", str(vault))
    monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(state_root))

    result = integration_adapter.ingest_event(
        integration_adapter.normalize_event(
            "opencode",
            event_type,
            {"directory": str(project), "sessionId": "session-1"},
        )
    )

    assert result["heartbeat_recorded"] is True
    state = json.loads((state_root / "run" / "state.json").read_text(encoding="utf-8"))
    heartbeat = state["codex_heartbeats"]["project-with-spaces"]
    assert heartbeat["project_root"] == str(project.resolve())
    assert heartbeat["session_id"] == "session-1"


def test_shared_ingest_materializes_redacted_transient_and_delegates(tmp_path, monkeypatch):
    import integration_adapter

    vault = tmp_path / "vault"
    state_root = tmp_path / "state"
    project = tmp_path / "project"
    (vault / "knowledge" / "projects").mkdir(parents=True)
    project.mkdir()
    monkeypatch.setattr(integration_adapter, "ROOT", vault)
    monkeypatch.setattr(integration_adapter, "STATE_ROOT", state_root)
    calls = []
    monkeypatch.setattr(
        integration_adapter,
        "_run_delegate",
        lambda name, payload, **kwargs: calls.append((name, payload))
        or SimpleNamespace(
            returncode=0,
            stdout=(
                '{"flush_started": true}'
                if name == "session_end_capture.py"
                else ""
            ),
            stderr="",
        ),
    )
    secret = "sk-abcdefghijklmnopqrstuvwxyz012345"
    envelope = integration_adapter.normalize_event(
        "opencode",
        "session_end",
        {
            "directory": str(project),
            "sessionId": "session-1",
            "transcript_text": f"decision token={secret}",
        },
    )

    result = integration_adapter.ingest_event(envelope, trigger="opencode-idle")

    transient = Path(calls[0][1]["transcript_path"])
    assert transient.parent == state_root / "cache" / "transient-transcripts"
    assert secret not in transient.read_text(encoding="utf-8")
    if os.name != "nt":
        assert stat.S_IMODE(transient.stat().st_mode) & 0o077 == 0
    assert calls[0][0] == "session_end_project_tag.py"
    assert calls[1][0] == "session_end_capture.py"
    assert calls[1][1]["ephemeral_transcript"] is True
    assert result["flush_spawned"] is True


@pytest.mark.parametrize("event_type", ["session_end", "pre_compact"])
def test_transient_cleanup_on_delegate_exception(tmp_path, monkeypatch, event_type):
    import integration_adapter

    state_root = tmp_path / "state"
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setattr(integration_adapter, "STATE_ROOT", state_root)
    monkeypatch.setattr(
        integration_adapter,
        "_run_delegate",
        lambda name, payload, **kwargs: (_ for _ in ()).throw(
            subprocess.TimeoutExpired(cmd=name, timeout=1)
        ),
    )
    envelope = integration_adapter.normalize_event(
        "opencode",
        event_type,
        {
            "directory": str(project),
            "sessionId": "session-1",
            "transcript_text": "bounded transcript",
        },
    )

    with pytest.raises(subprocess.TimeoutExpired):
        integration_adapter.ingest_event(envelope)

    transient_dir = state_root / "cache" / "transient-transcripts"
    assert not list(transient_dir.glob("*.txt"))


def _try_symlink(link: Path, target: Path, *, target_is_directory: bool) -> bool:
    try:
        link.symlink_to(target, target_is_directory=target_is_directory)
        return True
    except OSError:
        if os.name == "nt" and target_is_directory:
            result = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(link), str(target)],
                capture_output=True,
                check=False,
            )
            if result.returncode == 0:
                return True
        return False


def test_precompact_transcript_text_materializes_and_confirms_start(tmp_path, monkeypatch):
    import integration_adapter

    state_root = tmp_path / "state"
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setattr(integration_adapter, "STATE_ROOT", state_root)
    calls = []
    monkeypatch.setattr(
        integration_adapter,
        "_run_delegate",
        lambda name, payload, **kwargs: calls.append((name, payload))
        or SimpleNamespace(
            returncode=0, stdout='{"flush_started": true}', stderr=""
        ),
    )
    envelope = integration_adapter.normalize_event(
        "opencode",
        "pre_compact",
        {
            "directory": str(project),
            "transcript_text": "compact tail",
        },
    )

    result = integration_adapter.ingest_event(envelope)

    assert calls[0][0] == "precompact_capture.py"
    assert calls[0][1]["ephemeral_transcript"] is True
    assert result["flush_spawned"] is True
    assert Path(calls[0][1]["transcript_path"]).exists()


def test_windows_transient_permissions_use_bounded_icacls(monkeypatch, tmp_path):
    import integration_adapter

    state_root = tmp_path / "state"
    external_dir = tmp_path / "external-dir"
    state_root.mkdir()
    external_dir.mkdir()
    if not _try_symlink(state_root / "cache", external_dir, target_is_directory=True):
        pytest.skip("directory symlink/reparse creation unavailable")
    monkeypatch.setattr(integration_adapter, "STATE_ROOT", state_root)
    envelope = integration_adapter.normalize_event("opencode", "session_end", {})
    with pytest.raises(PermissionError, match="transient"):
        integration_adapter._write_transient_transcript(envelope, "private")
    assert not list(external_dir.rglob("*"))

    (state_root / "cache").unlink()
    transient_dir = state_root / "cache" / "transient-transcripts"
    transient_dir.mkdir(parents=True)
    external_file = tmp_path / "external.txt"
    external_file.write_text("untouched", encoding="utf-8")
    collision = transient_dir / f"{envelope.event_id}-collision.txt"
    file_link_available = _try_symlink(
        collision, external_file, target_is_directory=False
    )
    if not file_link_available:
        collision.write_text("untouched", encoding="utf-8")
    suffixes = iter(("collision", "unique"))
    monkeypatch.setattr(
        integration_adapter.secrets, "token_hex", lambda _size: next(suffixes)
    )
    restrict_file_permissions = integration_adapter._restrict_file_permissions
    monkeypatch.setattr(integration_adapter, "_restrict_file_permissions", lambda _path: None)
    created = integration_adapter._write_transient_transcript(envelope, "private")
    assert created.name == f"{envelope.event_id}-unique.txt"
    assert external_file.read_text(encoding="utf-8") == "untouched"
    assert collision.read_text(encoding="utf-8") == "untouched"

    if os.name != "nt":
        transient_dir.chmod(0o733)
        monkeypatch.setattr(integration_adapter.secrets, "token_hex", lambda _size: "unsafe")
        with pytest.raises(PermissionError, match="private"):
            integration_adapter._write_transient_transcript(envelope, "private")
        transient_dir.chmod(0o700)

    swap_state = tmp_path / "swap-state"
    swap_parent = swap_state / "cache" / "transient-transcripts"
    swap_parent.mkdir(parents=True)
    moved_parent = tmp_path / "moved-transient"
    external_parent = tmp_path / "external-transient"
    external_parent.mkdir()
    real_fsync = integration_adapter.os.fsync
    swapped = False

    def fsync_and_swap(descriptor):
        nonlocal swapped
        real_fsync(descriptor)
        if swapped:
            return
        swapped = True
        swap_parent.rename(moved_parent)
        if not _try_symlink(swap_parent, external_parent, target_is_directory=True):
            swap_parent.mkdir()

    monkeypatch.setattr(integration_adapter, "STATE_ROOT", swap_state)
    monkeypatch.setattr(integration_adapter.os, "fsync", fsync_and_swap)
    monkeypatch.setattr(integration_adapter.secrets, "token_hex", lambda _size: "swap")
    with pytest.raises(PermissionError, match="transient"):
        integration_adapter._write_transient_transcript(envelope, "private")
    assert not list(moved_parent.glob("*.txt"))
    assert not list(external_parent.glob("*.txt"))

    path = tmp_path / "transient.txt"
    path.write_text("safe", encoding="utf-8")
    calls = []
    monkeypatch.setattr(
        integration_adapter, "_restrict_file_permissions", restrict_file_permissions
    )
    monkeypatch.setattr(integration_adapter.os, "name", "nt")
    monkeypatch.setenv("USERNAME", "Test User")
    monkeypatch.setattr(
        integration_adapter.subprocess,
        "run",
        lambda args, **kwargs: calls.append((args, kwargs))
        or SimpleNamespace(returncode=0),
    )

    integration_adapter._restrict_file_permissions(path)

    assert calls[0][0] == [
        "icacls",
        str(path),
        "/inheritance:r",
        "/grant:r",
        "Test User:(R,W)",
    ]
    assert calls[0][1]["timeout"] > 0
    if not file_link_available:
        pytest.skip("file symlink creation unavailable; regular collision verified")
    assert collision.is_symlink()


def test_claude_hooks_route_through_shared_adapter_and_preserve_contract():
    settings = json.loads(
        (SCRIPTS_DIR.parent / "integrations" / "claude-code" / "settings.json").read_text(
            encoding="utf-8"
        )
    )

    expected = {
        "SessionStart": ([15, 10], "session_start"),
        "PreCompact": ([15], "pre_compact"),
        "SessionEnd": ([15, 10], "session_end"),
        "UserPromptSubmit": ([5], "user_prompt"),
        "PostToolUse": ([5], "post_tool_use"),
    }
    for hook_name, (timeouts, event_name) in expected.items():
        hooks = settings["hooks"][hook_name][0]["hooks"]
        assert [hook["timeout"] for hook in hooks] == timeouts
        assert all("scripts/integration_adapter.py" in hook["command"] for hook in hooks)
        assert all(f"--event {event_name}" in hook["command"] for hook in hooks)


@pytest.mark.parametrize(
    ("transcript", "force_stub", "expected"),
    [("", False, "heartbeat"), ("", True, "stub"), ("session.jsonl", False, "flush")],
)
def test_codex_passes_sanitized_policy_to_shared_ingest(
    monkeypatch, tmp_path, capsys, transcript, force_stub, expected
):
    import codex_memory

    transcript_path = str(tmp_path / transcript) if transcript else ""
    if transcript:
        Path(transcript_path).write_text("{}\n", encoding="utf-8")
    observed = {}

    def fake_ingest(envelope, **kwargs):
        observed["envelope"] = envelope
        observed.update(kwargs)
        return {
            "slug": "app",
            "heartbeat_recorded": expected == "heartbeat",
            "daily_log_written": expected in {"stub", "flush"},
            "flush_spawned": expected == "flush",
            "transcript_path": envelope.payload["transcript_path"],
            "returncode": 0,
        }

    monkeypatch.setattr(codex_memory, "ingest_event", fake_ingest)
    secret = "sk-abcdefghijklmnopqrstuvwxyz012345"

    rc = codex_memory.command_daily_log(
        Namespace(
            cwd=str(tmp_path),
            reason=f"token={secret}",
            session_id=secret,
            transcript=transcript_path,
            trigger=f"secret={secret}",
            force_stub=force_stub,
            json=True,
        )
    )

    assert rc == 0
    assert observed["envelope"].session == "[REDACTED_API_KEY]"
    assert observed["envelope"].payload["reason"] == "token=[REDACTED]"
    assert observed["force_stub"] is force_stub
    assert observed["trigger"] == "secret=[REDACTED]"
    assert secret not in capsys.readouterr().out


def test_codex_normalization_failure_is_host_safe_and_secret_free(monkeypatch, tmp_path, capsys):
    import codex_memory

    secret = "sk-abcdefghijklmnopqrstuvwxyz012345"
    monkeypatch.setattr(
        codex_memory,
        "normalize_event",
        lambda *args, **kwargs: (_ for _ in ()).throw(ValueError(secret)),
    )

    rc = codex_memory.command_daily_log(
        Namespace(cwd=str(tmp_path), reason=secret, json=False)
    )

    captured = capsys.readouterr()
    assert rc == 0
    assert captured.out == ""
    assert captured.err == "codex_memory: capture skipped\n"
    assert secret not in captured.err


def test_codex_missing_optional_fields_degrade_to_heartbeat(monkeypatch, tmp_path):
    import codex_memory

    calls = []
    monkeypatch.setattr(
        codex_memory,
        "ingest_event",
        lambda envelope, **kwargs: calls.append((envelope, kwargs))
        or {
            "slug": "app",
            "heartbeat_recorded": True,
            "daily_log_written": False,
            "flush_spawned": False,
            "transcript_path": None,
            "returncode": 0,
        },
    )

    rc = codex_memory.command_daily_log(Namespace(cwd=str(tmp_path)))

    assert rc == 0
    assert len(calls) == 1
    assert calls[0][0].session is None


def test_codex_malformed_trigger_falls_back_to_sanitized_reason(monkeypatch, tmp_path):
    import codex_memory

    transcript = tmp_path / "session.jsonl"
    transcript.write_text("{}\n", encoding="utf-8")
    flush_calls = []
    monkeypatch.setattr(
        codex_memory,
        "ingest_event",
        lambda envelope, **kwargs: flush_calls.append(kwargs)
        or {
            "slug": "app",
            "heartbeat_recorded": False,
            "daily_log_written": True,
            "flush_spawned": True,
            "transcript_path": str(transcript),
            "returncode": 0,
        },
    )

    rc = codex_memory.command_daily_log(
        Namespace(
            cwd=str(tmp_path),
            reason="token=sk-abcdefghijklmnopqrstuvwxyz012345",
            session_id="session",
            transcript=str(transcript),
            trigger=123,
            force_stub=False,
            json=True,
        )
    )

    assert rc == 0
    assert flush_calls[0]["trigger"] == "token=[REDACTED]"


def test_codex_delegate_failure_does_not_echo_output(monkeypatch, tmp_path, capsys):
    import codex_memory

    secret = "sk-abcdefghijklmnopqrstuvwxyz012345"
    monkeypatch.setattr(
        codex_memory,
        "ingest_event",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError(secret)),
    )

    rc = codex_memory.command_daily_log(
        Namespace(
            cwd=str(tmp_path),
            reason="end",
            session_id="session",
            transcript="",
            trigger="codex",
            force_stub=True,
            json=False,
        )
    )

    captured = capsys.readouterr()
    assert rc == 0
    assert captured.out == ""
    assert captured.err == "codex_memory: capture failed\n"
    assert secret not in captured.err


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


def test_heartbeat_record_preserves_missing_optional_source_fields(tmp_path, monkeypatch):
    fake_state_dir = tmp_path / "run"
    fake_state_dir.mkdir(parents=True)
    fake_state_file = fake_state_dir / "state.json"
    import memory_state

    monkeypatch.setattr(memory_state, "STATE_ROOT", tmp_path)
    monkeypatch.setattr(memory_state, "STATE_DIR", fake_state_dir)
    monkeypatch.setattr(memory_state, "STATE_FILE", fake_state_file)
    monkeypatch.setattr(memory_state, "LOCK_FILE", fake_state_dir / "state.json.lock")

    rc = _run_with_stdin(
        "heartbeat_record",
        json.dumps({"slug": "test-project", "reason": "session_end"}),
    )

    assert rc == 0
    heartbeat = json.loads(fake_state_file.read_text(encoding="utf-8"))[
        "codex_heartbeats"
    ]["test-project"]
    assert heartbeat["session_id"] is None
    assert heartbeat["project_root"] is None


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
