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


def _codex_resource(hooks, destination: Path):
    return hooks.codex_hooks_resource(
        destination,
        hooks.codex_hooks_template(ROOT),
        config_existed=destination.exists(),
    )


def _historical_cursor_resource(hooks, install_control, destination: Path, command: str):
    """Rebuild the exact Cursor resource shape an install before 2026-08-26 wrote."""
    handlers = {"sessionStart": [{"command": command, "timeout": 10}]}
    desired = _canonical(hooks, {"hooks": handlers, "version": 1})
    existed = destination.exists()

    def write_owned(value: bytes | None) -> None:
        config = hooks._read_config(destination)[0]
        current = hooks._cursor_projection(config, handlers)
        hooks._write_cursor_projection(
            destination, current, value, {"config_existed": existed}
        )

    return install_control.ManagedResource(
        resource_id="cursor-user-hooks",
        kind="cursor_hooks_fragment",
        locator=str(destination),
        desired=desired,
        read_owned=lambda: hooks._cursor_projection(
            hooks._read_config(destination)[0], handlers
        ),
        write_owned=write_owned,
        recognizes=lambda current: current == desired,
        read_projections=lambda candidates: hooks._cursor_projection_any(
            hooks._read_config(destination)[0], candidates
        ),
        write_projection=lambda expected, replacement, metadata: (
            hooks._write_cursor_projection(destination, expected, replacement, metadata)
        ),
        metadata={"config_existed": existed},
        adopt_as_absent=False,
    )


def _historical_antigravity_resource(hooks, install_control, destination: Path, command: str):
    """Rebuild the exact Antigravity resource shape an install before 2026-08-26 wrote."""
    owned = {"PreInvocation": [{"command": command, "type": "command"}], "enabled": True}
    desired = _canonical(hooks, owned)
    existed = destination.exists()

    def write_owned(value: bytes | None) -> None:
        config = hooks._read_config(destination)[0]
        current = hooks._antigravity_projection_any(config, (desired,))
        hooks._write_antigravity_projection(
            destination, current, value, {"config_existed": existed}
        )

    return install_control.ManagedResource(
        resource_id="antigravity-user-hooks",
        kind="antigravity_hooks_fragment",
        locator=str(destination),
        desired=desired,
        read_owned=lambda: hooks._antigravity_projection_any(
            hooks._read_config(destination)[0], (desired,)
        ),
        write_owned=write_owned,
        recognizes=lambda current: current == desired,
        read_projections=lambda candidates: hooks._antigravity_projection_any(
            hooks._read_config(destination)[0], candidates
        ),
        write_projection=lambda expected, replacement, metadata: (
            hooks._write_antigravity_projection(destination, expected, replacement, metadata)
        ),
        metadata={"config_existed": existed},
        adopt_as_absent=False,
    )


def _canonical(hooks, value):
    return importlib.import_module("reliable_memory").canonical_json_bytes(value)


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
    resource = _codex_resource(hooks, destination)

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
        _codex_resource(hooks, oversized).read_owned()
    with pytest.raises(Exception, match="unsafe"):
        _codex_resource(hooks, directory).read_owned()


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
    resource = _codex_resource(hooks, destination)
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

    resource = _codex_resource(hooks, destination)

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

    resource = _codex_resource(hooks, destination)
    resource.write_owned(resource.desired)

    if os.name != "nt":
        assert stat.S_IMODE(destination.stat().st_mode) == 0o640
    assert owner_updates == [(original.st_uid, original.st_gid)]


def _release() -> dict[str, object]:
    return {
        "commit_oid": "a" * 40,
        "project_version": "4.0.0",
        "source_mode": "pinned_remote",
        "uv_lock_sha256": "b" * 64,
        "worktree_clean": True,
    }


def _install_and_retire(tmp_path: Path, historical, retired_id: str, relative: str):
    install_control = importlib.import_module("install_control")
    hooks = _hooks_module()
    home = tmp_path / "home"
    destination = home / relative
    destination.parent.mkdir(parents=True)
    original = {"team": {"Stop": [{"command": "user-stop"}]}, "version": 1, "hooks": {}}
    destination.write_text(json.dumps(original), encoding="utf-8")
    state_root = tmp_path / "state"
    state_root.mkdir()

    old = historical(hooks, install_control, destination, "llm-wiki capture")
    install_control.install_resources(
        state_root=state_root,
        vault_root=tmp_path / "vault",
        release=_release(),
        scheduler_backend="cron",
        resources=[old],
        control_version=2,
    )
    assert json.loads(destination.read_bytes()) != original

    retired = {
        resource.resource_id: resource
        for resource in (
            hooks.retired_cursor_hooks_resource(home),
            hooks.retired_antigravity_hooks_resource(home),
        )
    }[retired_id]
    install_control.uninstall_resources(state_root=state_root, resources=[retired])
    return destination, original


def test_uninstall_takes_back_a_cursor_fragment_written_before_cursor_was_retired(
    tmp_path: Path,
) -> None:
    destination, original = _install_and_retire(
        tmp_path,
        _historical_cursor_resource,
        "cursor-user-hooks",
        ".cursor/hooks.json",
    )

    assert json.loads(destination.read_bytes()) == original


def test_uninstall_takes_back_an_antigravity_fragment_written_before_it_was_retired(
    tmp_path: Path,
) -> None:
    destination, original = _install_and_retire(
        tmp_path,
        _historical_antigravity_resource,
        "antigravity-user-hooks",
        ".gemini/config/hooks.json",
    )

    assert json.loads(destination.read_bytes()) == original


def test_a_retired_fragment_cannot_be_installed_again(tmp_path: Path) -> None:
    hooks = _hooks_module()
    install_control = importlib.import_module("install_control")
    resource = hooks.retired_cursor_hooks_resource(tmp_path / "home")

    with pytest.raises(install_control.InstallControlError, match="install_resource_retired"):
        resource.write_owned(b'{"hooks":{},"version":1}')


def test_without_the_removal_path_an_old_fragment_could_never_be_taken_back(
    tmp_path: Path,
) -> None:
    """Why the removal path is kept: deleting it outright strands the user's file."""
    install_control = importlib.import_module("install_control")
    hooks = _hooks_module()
    home = tmp_path / "home"
    destination = home / ".cursor" / "hooks.json"
    destination.parent.mkdir(parents=True)
    destination.write_text(json.dumps({"version": 1, "hooks": {}}), encoding="utf-8")
    state_root = tmp_path / "state"
    state_root.mkdir()
    old = _historical_cursor_resource(hooks, install_control, destination, "llm-wiki capture")
    install_control.install_resources(
        state_root=state_root,
        vault_root=tmp_path / "vault",
        release=_release(),
        scheduler_backend="cron",
        resources=[old],
        control_version=2,
    )
    written = destination.read_bytes()

    with pytest.raises(
        install_control.InstallControlError, match="install_resource_request_mismatch"
    ):
        install_control.uninstall_resources(
            state_root=state_root,
            resources=[hooks.retired_antigravity_hooks_resource(home)],
        )

    assert destination.read_bytes() == written
