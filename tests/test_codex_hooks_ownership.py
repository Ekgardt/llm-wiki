"""Codex hooks are owned by the install transaction, like Claude's settings.

They were merged by `codex_memory merge-hooks` outside the transaction, so an
uninstall left them running against a vault that was gone.
"""
from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

OUR_COMMAND = 'uv run --directory "$LLM_WIKI_ROOT" python scripts/codex_memory.py hook'

TEMPLATE = {
    "hooks": {
        "SessionStart": [
            {
                "matcher": "startup",
                "hooks": [{"type": "command", "command": OUR_COMMAND, "timeout": 15}],
            }
        ]
    }
}


def _hooks_module():
    return importlib.import_module("integration_hook_config")


def _resource(tmp_path: Path):
    return _hooks_module().codex_hooks_resource(tmp_path / ".codex" / "hooks.json", TEMPLATE)


def _document(tmp_path: Path) -> dict:
    return json.loads((tmp_path / ".codex" / "hooks.json").read_text(encoding="utf-8"))


@pytest.fixture()
def codex_hooks(tmp_path: Path) -> Path:
    path = tmp_path / ".codex" / "hooks.json"
    path.parent.mkdir()
    path.write_text(
        json.dumps(
            {
                "version": 2,
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


def test_install_keeps_the_users_own_hooks(tmp_path: Path, codex_hooks: Path) -> None:
    resource = _resource(tmp_path)

    assert resource.read_owned() is None
    resource.write_owned(resource.desired)

    document = _document(tmp_path)
    assert document["version"] == 2
    assert document["hooks"]["SessionStart"][0]["hooks"] == [{"command": "user-own-hook"}]
    assert document["hooks"]["SessionStart"][-1]["hooks"][0]["command"] == OUR_COMMAND
    assert resource.read_owned() == resource.desired


def test_uninstall_takes_back_only_our_groups(tmp_path: Path, codex_hooks: Path) -> None:
    resource = _resource(tmp_path)
    resource.write_owned(resource.desired)

    resource.write_owned(None)

    assert resource.read_owned() is None
    assert _document(tmp_path)["hooks"]["SessionStart"] == [
        {"matcher": "startup", "hooks": [{"command": "user-own-hook"}]}
    ]


def test_a_hooks_file_we_created_is_removed_again(tmp_path: Path) -> None:
    (tmp_path / ".codex").mkdir()
    resource = _resource(tmp_path)

    resource.write_owned(resource.desired)
    assert (tmp_path / ".codex" / "hooks.json").is_file()
    resource.write_owned(None)

    assert not (tmp_path / ".codex" / "hooks.json").exists()


def test_installing_twice_writes_the_same_bytes(tmp_path: Path, codex_hooks: Path) -> None:
    resource = _resource(tmp_path)

    resource.write_owned(resource.desired)
    first = codex_hooks.read_bytes()
    resource.write_owned(resource.desired)

    assert codex_hooks.read_bytes() == first


def test_a_group_mixing_our_handler_with_the_users_is_refused(
    tmp_path: Path, codex_hooks: Path
) -> None:
    from install_control import InstallControlError

    codex_hooks.write_text(
        json.dumps(
            {
                "hooks": {
                    "SessionStart": [
                        {"hooks": [{"command": "user-own-hook"}, {"command": OUR_COMMAND}]}
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    resource = _resource(tmp_path)

    with pytest.raises(InstallControlError, match="integration_codex_ownership_conflict"):
        resource.read_owned()


def test_the_shipped_template_is_accepted() -> None:
    template = _hooks_module().codex_hooks_template(ROOT)

    assert "SessionStart" in template["hooks"]


def test_the_inline_state_probe_writes_nothing(tmp_path: Path) -> None:
    """The installer asks before owning the file; asking must not change anything."""
    import codex_memory

    config = tmp_path / "config.toml"
    config.write_text("[features]\nhooks = false\n", encoding="utf-8")
    before = config.read_bytes()

    state = codex_memory._inline_hook_state(config, TEMPLATE)

    assert state == "disabled"
    assert config.read_bytes() == before
