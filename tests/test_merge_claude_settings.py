"""Tests for merge_claude_settings (safe Claude user-settings merge)."""
from __future__ import annotations

import json
from pathlib import Path

import merge_claude_settings as mcs


def test_merge_keeps_user_hooks_and_replaces_ours(tmp_path):
    user = {
        "hooks": {
            "SessionStart": [
                {
                    "matcher": "startup",
                    "hooks": [
                        {"type": "command", "command": "echo user-hook", "timeout": 5},
                        {
                            "type": "command",
                            "command": "uv run python scripts/session_start_context.py",
                            "timeout": 15,
                        },
                    ],
                }
            ],
            "Notification": [
                {
                    "matcher": "",
                    "hooks": [{"type": "command", "command": "echo notify"}],
                }
            ],
        },
        "permissions": {"allow": ["Bash(echo *)"], "deny": []},
        "env": {"OTHER": "keep-me"},
    }
    template = {
        "autoMemoryEnabled": True,
        "permissions": {
            "allow": ["Bash(uv run --directory *)"],
            "deny": ["Read(./.env)"],
        },
        "hooks": {
            "SessionStart": [
                {
                    "matcher": "startup|resume|clear|compact",
                    "hooks": [
                        {
                            "type": "command",
                            "command": 'uv run --directory "$LLM_WIKI_ROOT" python scripts/session_start_context.py',
                            "timeout": 15,
                        }
                    ],
                }
            ]
        },
    }
    merged = mcs.merge_settings(user, template, "/vault", "/state")
    assert merged["env"]["OTHER"] == "keep-me"
    assert merged["env"]["LLM_WIKI_ROOT"] == "/vault"
    assert merged["env"]["LLM_WIKI_STATE_ROOT"] == "/state"
    assert "Bash(echo *)" in merged["permissions"]["allow"]
    assert "Bash(uv run --directory *)" in merged["permissions"]["allow"]
    assert "Read(./.env)" in merged["permissions"]["deny"]

    ss = merged["hooks"]["SessionStart"]
    cmds = []
    for block in ss:
        for h in block.get("hooks", []):
            cmds.append(h["command"])
    assert any("echo user-hook" in c for c in cmds)
    assert any("$LLM_WIKI_ROOT" in c and "session_start_context" in c for c in cmds)
    # Old relative ours removed
    assert not any(c == "uv run python scripts/session_start_context.py" for c in cmds)
    # Unrelated event preserved
    assert merged["hooks"]["Notification"]


def test_apply_merge_writes_backup(tmp_path):
    user_path = tmp_path / "settings.json"
    user_path.write_text(json.dumps({"env": {"X": "1"}}), encoding="utf-8")
    template = tmp_path / "template.json"
    template.write_text(
        json.dumps(
            {
                "hooks": {
                    "SessionEnd": [
                        {
                            "matcher": "",
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": "uv run python scripts/session_end_capture.py",
                                }
                            ],
                        }
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    mcs.apply_merge(user_path, template, "/v", "/s", dry_run=False)
    data = json.loads(user_path.read_text(encoding="utf-8"))
    assert data["env"]["LLM_WIKI_ROOT"] == "/v"
    backups = list(tmp_path.glob("settings.json.bak-llm-wiki-*"))
    assert len(backups) == 1
