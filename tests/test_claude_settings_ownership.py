"""Claude user settings are owned by the install transaction, like every other config.

The installer merged them by content and outside the transaction, so an uninstall
left our hooks in place, pointing at a vault that was no longer there.
"""
from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))


def _hooks_module():
    return importlib.import_module("integration_hook_config")


TEMPLATE = {
    "$schema": "https://example.invalid/schema.json",
    "autoMemoryEnabled": True,
    "permissions": {"allow": ["Bash(ls *)"], "deny": ["Read(./.env)"]},
    "hooks": {
        "SessionStart": [
            {
                "matcher": "startup",
                "hooks": [{"type": "command", "command": "uv run session_start_context.py"}],
            }
        ]
    },
}


def _resource(tmp_path: Path):
    hooks = _hooks_module()
    return hooks.claude_settings_resource(
        tmp_path / ".claude" / "settings.json",
        TEMPLATE,
        tmp_path / "vault",
        tmp_path / "state",
    )


def _settings(tmp_path: Path) -> dict:
    return json.loads((tmp_path / ".claude" / "settings.json").read_text(encoding="utf-8"))


@pytest.fixture()
def user_settings(tmp_path: Path) -> Path:
    path = tmp_path / ".claude" / "settings.json"
    path.parent.mkdir()
    path.write_text(
        json.dumps(
            {
                "theme": "dark",
                "env": {"EDITOR": "vim"},
                "permissions": {"allow": ["Bash(git log *)"]},
                "hooks": {
                    "SessionStart": [
                        {"matcher": "startup", "hooks": [{"command": "user-own-hook"}]}
                    ]
                },
            }
        ),
        encoding="utf-8",
    )
    return path


def test_install_keeps_every_unrelated_setting(tmp_path: Path, user_settings: Path) -> None:
    resource = _resource(tmp_path)

    assert resource.read_owned() is None
    resource.write_owned(resource.desired)

    settings = _settings(tmp_path)
    assert settings["theme"] == "dark"
    assert settings["env"]["EDITOR"] == "vim"
    assert settings["hooks"]["SessionStart"][0]["hooks"] == [{"command": "user-own-hook"}]
    assert settings["env"]["LLM_WIKI_ROOT"] == str(tmp_path / "vault")
    assert settings["env"]["LLM_WIKI_STATE_ROOT"] == str(tmp_path / "state")
    assert resource.read_owned() == resource.desired


def test_installing_twice_writes_the_same_bytes(tmp_path: Path, user_settings: Path) -> None:
    resource = _resource(tmp_path)

    resource.write_owned(resource.desired)
    first = user_settings.read_bytes()
    resource.write_owned(resource.desired)

    assert user_settings.read_bytes() == first


def test_uninstall_takes_back_our_hooks_and_our_env(tmp_path: Path, user_settings: Path) -> None:
    resource = _resource(tmp_path)
    resource.write_owned(resource.desired)

    resource.write_owned(None)

    settings = _settings(tmp_path)
    assert resource.read_owned() is None
    assert settings["hooks"]["SessionStart"] == [
        {"matcher": "startup", "hooks": [{"command": "user-own-hook"}]}
    ]
    assert settings["env"] == {"EDITOR": "vim"}
    assert settings["theme"] == "dark"


def test_uninstall_leaves_the_permissions_it_granted(tmp_path: Path, user_settings: Path) -> None:
    """We cannot tell our copy of an entry from the user's, so we never remove one."""
    resource = _resource(tmp_path)
    resource.write_owned(resource.desired)

    resource.write_owned(None)

    permissions = _settings(tmp_path)["permissions"]
    assert permissions["allow"] == ["Bash(git log *)", "Bash(ls *)"]
    assert permissions["deny"] == ["Read(./.env)"]


def test_a_settings_file_we_created_is_removed_again(tmp_path: Path) -> None:
    (tmp_path / ".claude").mkdir()
    resource = _resource(tmp_path)

    resource.write_owned(resource.desired)
    assert (tmp_path / ".claude" / "settings.json").is_file()
    resource.write_owned(None)

    assert not (tmp_path / ".claude" / "settings.json").exists()


def test_a_block_mixing_our_hook_with_the_users_is_refused(
    tmp_path: Path, user_settings: Path
) -> None:
    """Stripping it rewrites their block; keeping it double-fires ours."""
    from install_control import InstallControlError

    user_settings.write_text(
        json.dumps(
            {
                "hooks": {
                    "SessionStart": [
                        {
                            "hooks": [
                                {"command": "user-own-hook"},
                                {"command": "uv run session_start_context.py"},
                            ]
                        }
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    resource = _resource(tmp_path)

    with pytest.raises(InstallControlError, match="integration_claude_ownership_conflict"):
        resource.read_owned()


def test_a_hand_edited_hook_block_is_refused_rather_than_overwritten(
    tmp_path: Path, user_settings: Path
) -> None:
    resource = _resource(tmp_path)
    resource.write_owned(resource.desired)
    settings = _settings(tmp_path)
    settings["hooks"]["SessionStart"][-1]["matcher"] = "resume"
    (tmp_path / ".claude" / "settings.json").write_text(json.dumps(settings), encoding="utf-8")

    drifted = resource.read_owned()

    assert drifted is not None and drifted != resource.desired


def test_the_shipped_template_is_accepted(tmp_path: Path) -> None:
    hooks = _hooks_module()

    template = hooks.claude_settings_template(ROOT)

    assert "SessionStart" in template["hooks"]


def test_uninstall_rebuilds_every_resource_the_manifest_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The rebuild used to name only the managed IDE fragments.

    Anything else the install owned — the OpenCode plugin, and now the Claude
    settings — was simply absent from the uninstall, so it stayed on disk.
    """
    import argparse

    import install_control

    monkeypatch.setattr(install_control, "_base_install_resources", lambda **_kwargs: [])
    record = {
        "vault_root": str(tmp_path / "vault"),
        "state_root": str(tmp_path / "state"),
        "scheduler_backend": "cron",
        "resources": [
            {"id": "opencode-plugin", "metadata": {}},
            {"id": "claude-user-settings", "metadata": {"config_existed": False}},
        ],
    }
    monkeypatch.setattr(install_control, "_existing_record", lambda *_args: record)
    root = tmp_path / "vault"
    (root / "scripts").mkdir(parents=True)
    (root / "scripts" / "llm-wiki-memory-opencode.js").write_text(
        "const _EMBEDDED_ROOT = null; // llm-wiki:embedded-root\n", encoding="utf-8"
    )
    (root / "integrations" / "claude-code").mkdir(parents=True)
    (root / "integrations" / "claude-code" / "settings.json").write_text(
        json.dumps(TEMPLATE), encoding="utf-8"
    )
    args = argparse.Namespace(
        root=root,
        state_root=tmp_path / "state",
        uv_path=tmp_path / "uv",
        home=tmp_path / "home",
        profile=None,
        powershell_path=None,
    )

    resources = install_control._resources_from_existing_args(args, "uninstall")

    identifiers = [resource.resource_id for resource in resources]
    assert identifiers == ["opencode-plugin", "claude-user-settings"]
    claude = resources[-1]
    assert claude.metadata["config_existed"] is False


def test_the_opencode_destination_resolves_on_this_platform(tmp_path: Path) -> None:
    """`opencode_global_dir` names platforms posix/windows, not `sys.platform`.

    Passing `sys.platform` straight through made `--opencode-plugin` raise on
    every platform there is, which no test noticed because none called it.
    """
    import install_control

    destination = install_control._opencode_plugin_destination(tmp_path)

    assert destination.name == "llm-wiki-memory.js"
    assert destination.parent.name == "plugins"
