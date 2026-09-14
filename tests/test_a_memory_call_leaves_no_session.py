"""A memory call is not saved as a session, and a backfill does not keep old ones.

Every `claude -p` call left a private copy of its prompt under
`~/.claude/projects/-tmp-llm-wiki-provider-*` — 11 547 on this machine — and the
backfill would have kept them as session records. Research:
`docs/research/2026-09-14-a-memory-call-leaves-no-session.md`.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import backfill_sessions  # noqa: E402
import llm_client  # noqa: E402

TURN = '{"type":"user","message":{"role":"user","content":"why systemd?"}}\n'
ISOLATION = {"--no-session-persistence", "--tools", "--strict-mcp-config"}


def test_a_cli_that_has_the_flags_saves_no_session_and_loads_no_tools(monkeypatch):
    monkeypatch.setattr(llm_client, "_claude_cli_flags", lambda: frozenset(ISOLATION))

    command = llm_client._claude_command("/usr/bin/claude", None, "")

    assert (ISOLATION <= set(command), command[command.index("--tools") + 1]) == (True, "")


def test_an_older_cli_is_not_given_flags_it_does_not_know(monkeypatch):
    monkeypatch.setattr(llm_client, "_claude_cli_flags", lambda: frozenset())

    assert ISOLATION & set(llm_client._claude_command("/usr/bin/claude", None, "")) == set()


def _saved(root: Path, relative: str, text: str) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_the_backfill_skips_sessions_of_memory_calls_and_keeps_conversations(tmp_path):
    started_in_provider = json.dumps({"type": "session_meta", "payload": {"cwd": "/tmp/llm-wiki-provider-ab_1"}})
    _saved(tmp_path, "-tmp-llm-wiki-provider---2h7vub/one.jsonl", TURN)
    _saved(tmp_path, "2026/09/14/rollout-two.jsonl", started_in_provider + "\n" + TURN)
    kept = _saved(tmp_path, "-home-user-project/three.jsonl", json.dumps({"cwd": "/home/user/project"}) + "\n" + TURN)

    assert backfill_sessions._transcripts((tmp_path,)) == [kept]
