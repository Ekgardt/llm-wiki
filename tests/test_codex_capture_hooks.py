"""Codex prompts and edits reach the same capture as Claude's and OpenCode's.

Codex supports `UserPromptSubmit` and `PostToolUse` for `apply_patch` and
`Bash`; until 2026-09-11 the template registered neither for capture
(docs/research/2026-09-11-codex-leaves-breadcrumbs-too.md). Codex adds plain
text printed by a `UserPromptSubmit` hook to the model's context, so the
capture path must print nothing.
"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

from tests.test_breadcrumb_storage import _bundle
from tests.test_breadcrumb_worker import ingress as ingress

ROOT = Path(__file__).resolve().parents[1]
PATCH = "*** Begin Patch\n*** Update File: src/app.py\n@@\n-old\n+new\n*** End Patch\n"


def _saved_payload(queue):
    manifests = list((queue.state_root / "run/capture-intents/ready").rglob("*.json"))
    assert len(manifests) == 1
    manifest = json.loads(manifests[0].read_bytes())
    return json.loads(_bundle(queue.state_root, manifest["intent_id"]).content)["payload"]


def _run(adapter_module, monkeypatch, event: str, payload: dict) -> None:
    stdin = io.TextIOWrapper(io.BytesIO(json.dumps(payload).encode("utf-8")), encoding="utf-8")
    monkeypatch.setattr(sys, "stdin", stdin)
    assert adapter_module.main(["--source", "codex", "--event", event]) == 0


def test_a_codex_prompt_runs_prompt_capture_and_prints_nothing(
    ingress, monkeypatch, capsys
):
    adapter_module, queue, _coordinator = ingress
    _run(adapter_module, monkeypatch, "user_prompt", {
        "hook_event_name": "UserPromptSubmit",
        "session_id": "019a-codex-session",
        "turn_id": "turn-1",
        "cwd": "/work/demo",
        "prompt": "Keep this request",
    })

    assert _saved_payload(queue)["prompt"] == "Keep this request"
    assert capsys.readouterr().out == ""



def test_a_codex_patch_is_an_edit_of_the_file_it_touches(ingress, monkeypatch, capsys):
    adapter_module, queue, _coordinator = ingress
    _run(adapter_module, monkeypatch, "post_tool_use", {
        "hook_event_name": "PostToolUse",
        "session_id": "019a-codex-session",
        "cwd": "/work/demo",
        "tool_name": "apply_patch",
        "tool_use_id": "call-1",
        "tool_input": {"command": PATCH},
        "tool_response": {"success": True},
    })

    payload = _saved_payload(queue)
    assert (payload["tool_name"], payload["target"]) == (
        "Edit", "src/app.py",
    )
    assert capsys.readouterr().out == ""


def test_the_codex_template_registers_capture_for_prompts_and_edits():
    from codex_hook_identity import is_our_codex_command

    hooks = json.loads((ROOT / "integrations/codex/hooks.json").read_text(encoding="utf-8"))["hooks"]
    prompt = hooks["UserPromptSubmit"][0]["hooks"][0]
    capture = next(group for group in hooks["PostToolUse"] if "apply_patch" in group["matcher"])

    assert (
        prompt["command"].endswith("--source codex --event user_prompt"),
        capture["hooks"][0]["command"].endswith("--source codex --event post_tool_use"),
        is_our_codex_command(prompt["command"]),
        is_our_codex_command(capture["hooks"][0]["commandWindows"]),
    ) == (True, True, True, True)
