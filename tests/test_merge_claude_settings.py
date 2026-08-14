"""Tests for merge_claude_settings (safe Claude user-settings merge)."""
from __future__ import annotations

import json
import os
import time
from datetime import datetime

import integration_config_backup as icb
import merge_claude_settings as mcs
import pytest


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
    original = b'{"env":{"X":"1"}}\r\n'
    user_path.write_bytes(original)
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
    assert backups[0].read_bytes() == original

    mcs.apply_merge(user_path, template, "/v", "/s", dry_run=False)

    backups = list(tmp_path.glob("settings.json.bak-llm-wiki-*"))
    assert len(backups) == 1
    assert backups[0].read_bytes() == original


def test_apply_merge_enforces_backup_retention_without_deleting_unrelated_files(tmp_path):
    user_path = tmp_path / "settings.json"
    original = b'{"env":{"X":"1"}}\n'
    user_path.write_bytes(original)
    template = tmp_path / "template.json"
    template.write_text('{"hooks":{}}\n', encoding="utf-8")
    now = time.time()
    old = None
    for index in range(11):
        backup = tmp_path / f"settings.json.bak-llm-wiki-20260801-000000-{index:06d}"
        with backup.open("wb") as handle:
            handle.truncate(11 * 1024 * 1024)
        modified = now - (91 * 24 * 60 * 60 if index == 0 else 11 - index)
        os.utime(backup, (modified, modified))
        if index == 0:
            old = backup
    unrelated = tmp_path / "settings.json.bak-user"
    unrelated.write_bytes(b"keep")

    mcs.apply_merge(user_path, template, "/v", "/s", dry_run=False)

    backups = list(tmp_path.glob("settings.json.bak-llm-wiki-*"))
    assert old is not None and not old.exists()
    assert len(backups) <= 10
    assert sum(path.stat().st_size for path in backups) <= 100 * 1024 * 1024
    assert any(path.read_bytes() == original for path in backups)
    assert unrelated.read_bytes() == b"keep"


def test_backup_timestamp_collision_rolls_into_the_next_second(tmp_path, monkeypatch):
    class FrozenDateTime:
        @classmethod
        def now(cls):
            return datetime(2026, 8, 14, 12, 0, 0, 999999)

    destination = tmp_path / "settings.json"
    destination.write_bytes(b"before")
    collision = tmp_path / "settings.json.bak-llm-wiki-20260814-120000-999999"
    collision.write_bytes(b"existing")
    monkeypatch.setattr(icb, "datetime", FrozenDateTime)

    changed, backup = icb.publish_configuration(destination, b"after")

    assert changed is True
    assert backup is not None
    assert backup.name == "settings.json.bak-llm-wiki-20260814-120001-000000"
    assert backup.read_bytes() == b"before"


@pytest.mark.parametrize("destination_exists", [True, False])
def test_publish_prunes_owned_backups_when_no_new_preimage_is_created(
    tmp_path, destination_exists
):
    destination = tmp_path / "settings.json"
    if destination_exists:
        destination.write_bytes(b"current")
    now = time.time()
    backups = []
    for index in range(11):
        backup = tmp_path / f"settings.json.bak-llm-wiki-20260801-000000-{index:06d}"
        backup.write_bytes(str(index).encode("ascii"))
        os.utime(backup, (now + index, now + index))
        backups.append(backup)

    changed, backup = icb.publish_configuration(destination, b"current")

    assert changed is not destination_exists
    assert backup is None
    assert not backups[0].exists()
    assert backups[-1].exists()
    assert len(list(tmp_path.glob("settings.json.bak-llm-wiki-*"))) == 10


def test_publish_keeps_newest_backup_when_it_alone_exceeds_size_limit(tmp_path):
    destination = tmp_path / "settings.json"
    destination.write_bytes(b"current")
    older = tmp_path / "settings.json.bak-llm-wiki-20260801-000000-000000"
    older.write_bytes(b"old")
    newest = tmp_path / "settings.json.bak-llm-wiki-20260802-000000-000000"
    with newest.open("wb") as handle:
        handle.truncate(101 * 1024 * 1024)
    now = time.time()
    os.utime(older, (now - 1, now - 1))
    os.utime(newest, (now, now))

    changed, backup = icb.publish_configuration(destination, b"current")

    assert changed is False
    assert backup is None
    assert not older.exists()
    assert newest.exists()


def test_apply_merge_rejects_malformed_user_settings_without_writing(tmp_path):
    user_path = tmp_path / "settings.json"
    original = b'{"hooks": ['
    user_path.write_bytes(original)
    template = tmp_path / "template.json"
    template.write_text(json.dumps({"hooks": {}}), encoding="utf-8")

    with pytest.raises(ValueError, match="user settings.*JSON"):
        mcs.apply_merge(user_path, template, "/v", "/s", dry_run=False)

    assert user_path.read_bytes() == original
    assert list(tmp_path.glob("settings.json.bak-llm-wiki-*")) == []
