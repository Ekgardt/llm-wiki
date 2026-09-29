"""An update takes over the hooks an older release wrote, and refuses another vault's.

On the live vault the update that first gave Claude's settings to the install
transaction stopped with a bare `install_resource_ownership_ambiguous`: the file
held our own hook blocks from the release before (a PreCompact command that still
named `--delegate precompact_capture.py`), and ownership was recognised only by
byte equality with the new release's blocks. Every machine updating across that
change stopped the same way; Claude-27 had to be taken over with `--adopt`.
See docs/research/2026-09-28-an-update-replaces-what-it-owns.md.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from install_control import InstallControlError, file_resource, install_resources
from integration_hook_config import claude_settings_resource

_OLD_ADAPTER = "uv run python scripts/integration_adapter.py --event pre_compact --delegate x"
_NEW_ADAPTER = "uv run python scripts/integration_adapter.py --event pre_compact"
_USERS_OWN = {"matcher": "startup", "hooks": [{"type": "command", "command": "user-own-hook"}]}
TEMPLATE = {
    "hooks": {
        "PreCompact": [{"matcher": "auto", "hooks": [{"type": "command", "command": _NEW_ADAPTER}]}]
    }
}


def _release(version: str) -> dict[str, object]:
    return {
        "commit_oid": "a" * 40,
        "project_version": version,
        "source_mode": "pinned_remote",
        "uv_lock_sha256": "b" * 64,
        "worktree_clean": True,
    }


def _install(tmp_path: Path, version: str, resources: list) -> dict:
    return install_resources(
        state_root=tmp_path / "state",
        vault_root=tmp_path / "vault",
        release=_release(version),
        scheduler_backend="systemd_user",
        resources=resources,
        control_version=2,
    )


def _profile(tmp_path: Path):
    return file_resource(
        resource_id="profile", kind="test_value", path=tmp_path / "profile", desired=b"ours\n"
    )


def _settings_path(tmp_path: Path) -> Path:
    return tmp_path / ".claude" / "settings.json"


def _older_release_settings(tmp_path: Path, vault: Path) -> None:
    """What the separate merge script of an older release left in the file."""
    old = {"matcher": "auto", "hooks": [{"type": "command", "command": _OLD_ADAPTER}]}
    settings = {
        "theme": "dark",
        "env": {"LLM_WIKI_ROOT": str(vault), "LLM_WIKI_STATE_ROOT": str(tmp_path / "state")},
        "hooks": {"PreCompact": [old], "SessionStart": [_USERS_OWN]},
    }
    _settings_path(tmp_path).parent.mkdir()
    _settings_path(tmp_path).write_text(json.dumps(settings), encoding="utf-8")


def _claude(tmp_path: Path):
    return claude_settings_resource(
        _settings_path(tmp_path), TEMPLATE, tmp_path / "vault", tmp_path / "state"
    )


@pytest.fixture
def installed(tmp_path: Path) -> Path:
    for name in ("state", "vault"):
        (tmp_path / name).mkdir()
    _install(tmp_path, "4.0.0", [_profile(tmp_path)])
    return tmp_path


def test_an_update_replaces_the_hooks_an_older_release_wrote(installed: Path) -> None:
    _older_release_settings(installed, installed / "vault")

    _install(installed, "4.1.0", [_profile(installed), _claude(installed)])

    settings = json.loads(_settings_path(installed).read_text(encoding="utf-8"))
    commands = [block["hooks"][0]["command"] for block in settings["hooks"]["PreCompact"]]
    assert (commands, settings["hooks"]["SessionStart"], settings["theme"]) == (
        [_NEW_ADAPTER],
        [_USERS_OWN],
        "dark",
    )


def test_hooks_serving_another_vault_are_refused_by_name(installed: Path) -> None:
    _older_release_settings(installed, installed / "another-vault")

    with pytest.raises(InstallControlError) as refused:
        _install(installed, "4.1.0", [_profile(installed), _claude(installed)])

    said = str(refused.value)
    assert (said.split(":")[0], "claude-user-settings" in said, str(_settings_path(installed)) in said) == (
        "install_resource_ownership_ambiguous",
        True,
        True,
    )
