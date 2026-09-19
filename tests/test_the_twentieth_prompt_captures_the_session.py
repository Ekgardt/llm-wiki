"""The twentieth prompt captures the session so far through the adapter's own route.

See `docs/research/2026-09-17-the-twentieth-prompt-captures-the-session.md`.
"""
from __future__ import annotations

import io
import json
import sys
import uuid
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from tests.adopted_capture_vault import (  # noqa: E402
    adopted_capture_vault,
    capture_task_count,
    host_transcript,
    intent_summaries,
)


def _delegate_payload(raw: dict) -> dict:
    import integration_adapter

    envelope = integration_adapter.normalize_event("claude", "user_prompt", raw)
    return integration_adapter._canonical_capture_payload(envelope)


def test_a_prompt_reaches_its_delegate_with_the_transcript_the_host_named():
    payload = _delegate_payload(
        {
            "prompt": "what did we decide about the queue",
            "session_id": "session-1",
            "cwd": "/work/project",
            "transcript_path": "/host/projects/session-1.jsonl",
        }
    )

    assert payload["transcript_path"] == "/host/projects/session-1.jsonl"


def _submit_prompts(module, monkeypatch, tmp_path, hook: dict, count: int) -> list[list[str]]:
    """Run the prompt hook `count` times for one session; return what it started."""
    started: list[list[str]] = []
    vault = tmp_path / "vault"
    vault.mkdir(exist_ok=True)
    monkeypatch.setattr(module, "ROOT", vault)
    monkeypatch.setattr(module, "_append_prompt_tag", lambda *_a, **_k: False)
    monkeypatch.setattr(module, "spawn_detached", lambda args: started.append(args) or 1)
    for index in range(count):
        text = json.dumps(dict(hook, prompt=f"a meaningful prompt number {index}"))
        with patch.object(sys, "stdin", io.StringIO(text)):
            module.main()
    return started


def test_the_twentieth_prompt_starts_one_running_capture(monkeypatch, tmp_path):
    import user_prompt_capture

    project = tmp_path / "project"
    project.mkdir()
    hook = {
        "agent": "codex",
        "session_id": f"session-{uuid.uuid4()}",
        "cwd": str(project),
        "transcript_path": str(tmp_path / "session.jsonl"),
    }

    started = _submit_prompts(user_prompt_capture, monkeypatch, tmp_path, hook, 20)

    expected_payload = {
        "session_id": hook["session_id"],
        "cwd": str(project),
        "transcript_path": hook["transcript_path"],
        "trigger": "prompt-count-20",
    }
    assert [(args[1:5], json.loads(args[5])) for args in started] == [
        (
            [
                str(tmp_path / "vault" / "scripts" / "integration_adapter.py"),
                "--source",
                "codex",
                "--running-capture",
            ],
            expected_payload,
        )
    ]


def test_twenty_prompts_without_a_transcript_start_nothing(monkeypatch, tmp_path):
    import user_prompt_capture

    project = tmp_path / "project"
    project.mkdir()
    hook = {"session_id": f"session-{uuid.uuid4()}", "cwd": str(project)}

    assert _submit_prompts(user_prompt_capture, monkeypatch, tmp_path, hook, 20) == []


def test_a_running_capture_publishes_the_intent_and_wakes_the_worker(monkeypatch, tmp_path):
    import integration_adapter

    state_root, project = adopted_capture_vault(tmp_path, monkeypatch, integration_adapter)
    transcript = host_transcript(state_root, "running.jsonl", "we decided to keep the queue\n")
    woken: list[list[str]] = []
    monkeypatch.setattr(integration_adapter, "spawn_detached", lambda args: woken.append(args) or 1)
    payload = {
        "session_id": "session-running",
        "cwd": str(project),
        "transcript_path": str(transcript),
        "trigger": "prompt-count-20",
    }

    integration_adapter.main(["--source", "claude", "--running-capture", json.dumps(payload)])

    assert intent_summaries(state_root) == [
        ("pre_compact", "prompt-count-20", "session-running")
    ]
    assert (capture_task_count(state_root), woken[0][-1]) == (1, "--capture-worker")
