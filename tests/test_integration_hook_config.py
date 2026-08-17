from __future__ import annotations

import hashlib
import importlib
import json
import os
import stat
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _hooks_module():
    return importlib.import_module("integration_hook_config")


def _cursor_projection(command: str = "llm-wiki capture") -> dict[str, object]:
    return {
        "sessionStart": [{"command": command, "timeout": 10}],
        "stop": [{"command": f"{command} --stop"}],
    }


def test_cursor_resource_preserves_unrelated_content_and_is_idempotent(
    tmp_path: Path,
) -> None:
    hooks = _hooks_module()
    destination = tmp_path / ".cursor" / "hooks.json"
    destination.parent.mkdir()
    destination.write_text(
        json.dumps(
            {
                "custom": {"token": "keep"},
                "hooks": {
                    "afterFileEdit": [{"command": "user-format"}],
                    "sessionStart": [{"command": "user-start"}],
                },
                "version": 1,
            }
        ),
        encoding="utf-8",
    )
    resource = hooks.cursor_hooks_resource(destination, _cursor_projection())

    assert resource.read_owned() is None
    resource.write_owned(resource.desired)
    first = destination.read_bytes()
    resource.write_owned(resource.desired)

    parsed = json.loads(first)
    assert destination.read_bytes() == first
    assert parsed["custom"] == {"token": "keep"}
    assert parsed["hooks"]["afterFileEdit"] == [{"command": "user-format"}]
    assert parsed["hooks"]["sessionStart"] == [
        {"command": "user-start"},
        {"command": "llm-wiki capture", "timeout": 10},
    ]
    assert resource.read_owned() == resource.desired


def test_cursor_uninstall_restores_absent_file(tmp_path: Path) -> None:
    hooks = _hooks_module()
    destination = tmp_path / ".cursor" / "hooks.json"
    resource = hooks.cursor_hooks_resource(destination, _cursor_projection())

    resource.write_owned(resource.desired)
    resource.write_owned(None)

    assert not destination.exists()


def test_cursor_duplicate_owned_handler_is_an_ownership_conflict(
    tmp_path: Path,
) -> None:
    hooks = _hooks_module()
    destination = tmp_path / "hooks.json"
    owned = {"sessionStart": [{"command": "llm-wiki capture"}]}
    destination.write_text(
        json.dumps(
            {
                "version": 1,
                "hooks": {
                    "sessionStart": [
                        {"command": "llm-wiki capture"},
                        {"command": "llm-wiki capture"},
                    ]
                },
            }
        ),
        encoding="utf-8",
    )
    resource = hooks.cursor_hooks_resource(destination, owned)

    with pytest.raises(Exception, match="ownership_conflict"):
        resource.read_owned()


def _antigravity_projection(command: str = "llm-wiki capture") -> dict[str, object]:
    return {
        "PostToolUse": [
            {
                "hooks": [{"command": f"{command} --tool", "type": "command"}],
                "matcher": "*",
            }
        ],
        "PreInvocation": [{"command": command, "type": "command"}],
        "enabled": True,
    }


def test_antigravity_resource_preserves_unrelated_content_and_uninstalls(
    tmp_path: Path,
) -> None:
    hooks = _hooks_module()
    destination = tmp_path / ".gemini" / "config" / "hooks.json"
    destination.parent.mkdir(parents=True)
    original = {"team-hook": {"Stop": [{"command": "user-stop"}]}}
    destination.write_text(json.dumps(original), encoding="utf-8")
    resource = hooks.antigravity_hooks_resource(destination, _antigravity_projection())

    resource.write_owned(resource.desired)
    first = destination.read_bytes()
    resource.write_owned(resource.desired)

    assert destination.read_bytes() == first
    assert json.loads(first)["team-hook"] == original["team-hook"]
    assert resource.read_owned() == resource.desired

    resource.write_owned(None)

    assert json.loads(destination.read_bytes()) == original


@pytest.mark.parametrize("existing", [{"enabled": False}, None])
def test_antigravity_unrecognized_owned_key_is_a_conflict(tmp_path: Path, existing: object) -> None:
    hooks = _hooks_module()
    destination = tmp_path / "hooks.json"
    destination.write_text(json.dumps({"llm-wiki": existing}), encoding="utf-8")
    resource = hooks.antigravity_hooks_resource(destination, _antigravity_projection())

    with pytest.raises(Exception, match="ownership_conflict"):
        resource.read_owned()


def test_publish_configuration_creates_byte_exact_private_sibling_backup(
    tmp_path: Path,
) -> None:
    backup_module = importlib.import_module("integration_config_backup")
    destination = tmp_path / "hooks.json"
    original = b' {"secret":"\\u0000","line":"x\\r\\n"}\r\n'
    destination.write_bytes(original)

    changed, backup = backup_module.publish_configuration(
        destination,
        b'{"updated":true}\n',
        expected_original=original,
        expected_original_sha256=hashlib.sha256(original).hexdigest(),
        max_original_bytes=2 * 1024 * 1024,
    )

    assert changed is True
    assert backup is not None
    assert backup.read_bytes() == original
    if os.name != "nt":
        assert stat.S_IMODE(backup.stat().st_mode) == 0o600


def test_publish_configuration_cas_rejects_race_after_backup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backup_module = importlib.import_module("integration_config_backup")
    destination = tmp_path / "hooks.json"
    original = b'{"before":true}\n'
    destination.write_bytes(original)
    create_backup = backup_module._create_verified_backup

    def race(path: Path, value: bytes) -> Path:
        backup = create_backup(path, value)
        destination.write_bytes(b'{"racer":true}\n')
        return backup

    monkeypatch.setattr(backup_module, "_create_verified_backup", race)

    with pytest.raises(Exception, match="changed concurrently"):
        backup_module.publish_configuration(
            destination,
            b'{"updated":true}\n',
            expected_original=original,
            expected_original_sha256=hashlib.sha256(original).hexdigest(),
            max_original_bytes=2 * 1024 * 1024,
        )

    assert destination.read_bytes() == b'{"racer":true}\n'
    assert list(tmp_path.glob("hooks.json.bak-llm-wiki-*")) == []


@pytest.mark.parametrize(
    "raw",
    [
        b'{"version":1,"version":1}',
        b'{"version":1,"value":NaN}',
        b"[]",
    ],
)
def test_hook_config_rejects_non_strict_json_objects(tmp_path: Path, raw: bytes) -> None:
    hooks = _hooks_module()
    destination = tmp_path / "hooks.json"
    destination.write_bytes(raw)
    resource = hooks.cursor_hooks_resource(destination, _cursor_projection())

    with pytest.raises(Exception):
        resource.read_owned()


def test_hook_config_rejects_oversized_and_unsafe_destinations(
    tmp_path: Path,
) -> None:
    hooks = _hooks_module()
    oversized = tmp_path / "oversized.json"
    oversized.write_bytes(b" " * ((2 * 1024 * 1024) + 1))
    directory = tmp_path / "directory.json"
    directory.mkdir()

    with pytest.raises(Exception, match="unsafe"):
        hooks.cursor_hooks_resource(oversized, _cursor_projection()).read_owned()
    with pytest.raises(Exception, match="unsafe"):
        hooks.antigravity_hooks_resource(directory, _antigravity_projection()).read_owned()


def test_hook_resource_cas_preserves_concurrent_user_edit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hooks = _hooks_module()
    backup_module = importlib.import_module("integration_config_backup")
    destination = tmp_path / "hooks.json"
    destination.write_text(
        json.dumps({"version": 1, "hooks": {}, "owner": "original"}),
        encoding="utf-8",
    )
    resource = hooks.cursor_hooks_resource(destination, _cursor_projection())
    create_backup = backup_module._create_verified_backup

    def race(path: Path, value: bytes) -> Path:
        backup = create_backup(path, value)
        destination.write_text(
            json.dumps({"version": 1, "hooks": {}, "owner": "racer"}),
            encoding="utf-8",
        )
        return backup

    monkeypatch.setattr(backup_module, "_create_verified_backup", race)

    with pytest.raises(Exception, match="changed concurrently"):
        resource.write_owned(resource.desired)

    assert json.loads(destination.read_bytes())["owner"] == "racer"


def test_hook_resource_rejects_symlink_destination(tmp_path: Path) -> None:
    hooks = _hooks_module()
    target = tmp_path / "target.json"
    target.write_text('{"version":1,"hooks":{}}', encoding="utf-8")
    destination = tmp_path / "hooks.json"
    try:
        destination.symlink_to(target)
    except OSError:
        pytest.skip("symlink creation is unavailable")

    resource = hooks.cursor_hooks_resource(destination, _cursor_projection())

    with pytest.raises(Exception, match="symlink"):
        resource.read_owned()


def test_hook_update_preserves_existing_mode_and_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hooks = _hooks_module()
    backup_module = importlib.import_module("integration_config_backup")
    destination = tmp_path / "hooks.json"
    destination.write_text('{"version":1,"hooks":{}}', encoding="utf-8")
    destination.chmod(0o640)
    original = destination.stat()
    owner_updates: list[tuple[int, int]] = []
    monkeypatch.setattr(
        backup_module.os,
        "chown",
        lambda _path, uid, gid: owner_updates.append((uid, gid)),
        raising=False,
    )

    hooks.cursor_hooks_resource(destination, _cursor_projection()).write_owned(
        hooks.cursor_hooks_resource(destination, _cursor_projection()).desired
    )

    if os.name != "nt":
        assert stat.S_IMODE(destination.stat().st_mode) == 0o640
    assert owner_updates == [(original.st_uid, original.st_gid)]


def test_cursor_requires_integer_version_one(tmp_path: Path) -> None:
    hooks = _hooks_module()
    destination = tmp_path / "hooks.json"
    destination.write_text('{"version":true,"hooks":{}}', encoding="utf-8")
    resource = hooks.cursor_hooks_resource(destination, _cursor_projection())

    with pytest.raises(Exception, match="schema_conflict"):
        resource.read_owned()


def test_managed_hook_resources_materialize_absolute_vault_root(tmp_path: Path) -> None:
    hooks = _hooks_module()
    vault = ROOT.resolve()
    home = tmp_path / "home"

    resources = hooks.managed_ide_hook_resources(vault, home)

    assert [resource.resource_id for resource in resources] == [
        "cursor-user-hooks",
        "antigravity-user-hooks",
    ]
    assert resources[0].locator == str(home / ".cursor" / "hooks.json")
    assert resources[1].locator == str(home / ".gemini" / "config" / "hooks.json")
    cursor = json.loads(resources[0].desired)
    antigravity = json.loads(resources[1].desired)
    assert "__LLM_WIKI_ROOT__" not in json.dumps([cursor, antigravity])
    assert str(vault) in cursor["hooks"]["beforeSubmitPrompt"][0]["command"]
    assert str(vault) in antigravity["PreInvocation"][0]["command"]
