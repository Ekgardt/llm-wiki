"""Windows user environment and Task Scheduler resources."""

from __future__ import annotations

import json
import sys
from collections.abc import Callable, Mapping, Sequence
from functools import partial
from pathlib import Path
from types import ModuleType

from install_state import (
    MAX_PREIMAGE_BYTES,
    MAX_RECORD_BYTES,
    InstallControlError,
    ManagedResource,
    read_managed_file,
    same_snapshot,
    strict_json_object,
)
from llm_wiki.formats.canonical import canonical_json_bytes
from llm_wiki.install_records import CommandRunner, default_command_runner, require_command
from llm_wiki.install_scheduler_projection import (
    read_scheduler_projections,
    require_projection_before_write,
    require_scheduler_projection,
)
from llm_wiki.install_scheduler_render import (
    SCHEDULER_LIMIT_HOURS,
    installer_stable_uv_path,
    provider_items,
    scheduler_limit_hours,
)


def _windows_read_user_env(name: str) -> str | None:
    if sys.platform != "win32":
        raise InstallControlError("install_windows_environment_unavailable")
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            value, value_type = winreg.QueryValueEx(key, name)
    except FileNotFoundError:
        return None
    if value_type not in {winreg.REG_SZ, winreg.REG_EXPAND_SZ} or not isinstance(value, str):
        raise InstallControlError("install_windows_environment_invalid")
    return value


def _windows_write_user_env(name: str, value: str | None) -> None:
    if sys.platform != "win32":
        raise InstallControlError("install_windows_environment_unavailable")
    import winreg

    with winreg.CreateKeyEx(
        winreg.HKEY_CURRENT_USER, "Environment", access=winreg.KEY_SET_VALUE
    ) as key:
        if value is None:
            _windows_delete_user_env(winreg, key, name)
            return
        winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)


def _windows_delete_user_env(winreg: ModuleType, key: object, name: str) -> None:
    try:
        winreg.DeleteValue(key, name)
    except FileNotFoundError:
        return


def _environment_resource(
    name: str,
    value: str,
    read_value: Callable[[str], str | None],
    write_value: Callable[[str, str | None], None],
) -> ManagedResource:
    desired = value.encode("utf-8")
    return ManagedResource(
        resource_id=name.lower(),
        kind="user_environment",
        locator=f"windows-user-env://{name}",
        desired=desired,
        read_owned=lambda: _encoded_environment_value(read_value(name)),
        write_owned=lambda updated: write_value(name, _environment_update(updated)),
        recognizes=lambda current: current == desired,
        metadata={"environment_name": name},
        adopt_as_absent=True,
    )


def _environment_update(updated: bytes | None) -> str | None:
    if updated is None:
        return None
    return updated.decode("utf-8")


def _encoded_environment_value(value: str | None) -> bytes | None:
    if value is None:
        return None
    return value.encode("utf-8")


def windows_environment_resources(
    root: Path,
    state_root: Path,
    *,
    read_value: Callable[[str], str | None] = _windows_read_user_env,
    write_value: Callable[[str, str | None], None] = _windows_write_user_env,
) -> list[ManagedResource]:
    values = (
        ("LLM_WIKI_ROOT", str(Path(root).resolve())),
        ("LLM_WIKI_STATE_ROOT", str(Path(state_root).resolve())),
        *provider_items(root),
    )
    return [_environment_resource(name, value, read_value, write_value) for name, value in values]


# The specification names everything the contract promises about the tasks. Version 1
# did not name the time limits, so a machine registered with one-hour tasks compared
# equal to the corrected contract and was never rewritten. Version 1 stays readable:
# installed manifests record it, and uninstall and rollback have to take it back.
# See docs/research/2026-09-17-a-changed-task-setting-reaches-an-installed-machine.md.
WINDOWS_TASK_SPEC_VERSION = 2


WINDOWS_TASK_LIMIT_HOURS = SCHEDULER_LIMIT_HOURS


def render_windows_task_spec(root: Path, state_root: Path, uv_path: Path) -> bytes:
    value = {
        "root": str(Path(root).resolve()),
        "spec": WINDOWS_TASK_SPEC_VERSION,
        "state_root": str(Path(state_root).resolve()),
        "tasks": _expected_windows_tasks(root),
        "uv_path": str(installer_stable_uv_path(uv_path)),
    }
    return canonical_json_bytes(value)


def _legacy_windows_tasks() -> list[dict[str, object]]:
    return [
        {"at": "03:00", "kind": "nightly", "name": "LLMWiki-Nightly"},
        {
            "at": "04:00",
            "day": "Sunday",
            "kind": "weekly",
            "name": "LLMWiki-Weekly",
        },
    ]


def _expected_windows_tasks(root: Path | None = None) -> list[dict[str, object]]:
    limits = WINDOWS_TASK_LIMIT_HOURS if root is None else scheduler_limit_hours(root)
    return [
        {**task, "limit_hours": limits[str(task["kind"])]}
        for task in _legacy_windows_tasks()
    ]


def _windows_task_limit(task: object, legacy: dict[str, object]) -> int:
    if not isinstance(task, dict):
        raise InstallControlError("install_windows_task_spec_invalid")
    hours = task.get("limit_hours")
    valid = type(hours) is int and hours > 0
    if not valid or task != {**legacy, "limit_hours": hours}:
        raise InstallControlError("install_windows_task_spec_invalid")
    return hours


def _validated_windows_tasks(tasks: object) -> list[dict[str, object]]:
    legacy = _legacy_windows_tasks()
    if not isinstance(tasks, list) or len(tasks) != len(legacy):
        raise InstallControlError("install_windows_task_spec_invalid")
    return [
        {**template, "limit_hours": _windows_task_limit(task, template)}
        for task, template in zip(tasks, legacy)
    ]


def _windows_spec_shape(version: int, tasks: object) -> tuple[set[str], object]:
    if version == 1:
        return {"root", "state_root", "tasks", "uv_path"}, _legacy_windows_tasks()
    return {"root", "spec", "state_root", "tasks", "uv_path"}, _validated_windows_tasks(tasks)


def _windows_spec_version(value: Mapping[str, object]) -> int:
    """Which script contract a decoded specification was registered under."""
    version = value.get("spec", 1)
    if type(version) is not int or version not in {1, 2}:
        raise InstallControlError("install_windows_task_spec_invalid")
    keys, tasks = _windows_spec_shape(version, value.get("tasks"))
    if set(value) != keys or value.get("tasks") != tasks:
        raise InstallControlError("install_windows_task_spec_invalid")
    return int(version)


def _require_windows_spec_path(value: object, expected: Path | None = None) -> Path:
    if not isinstance(value, str) or not Path(value).is_absolute():
        raise InstallControlError("install_windows_task_spec_invalid")
    resolved = Path(value).resolve()
    if expected is not None and resolved != Path(expected).resolve():
        raise InstallControlError("install_windows_task_spec_invalid")
    return resolved


def decode_windows_task_spec(candidate: bytes, root: Path, state_root: Path) -> dict[str, object]:
    if len(candidate) > MAX_PREIMAGE_BYTES:
        raise InstallControlError("install_windows_task_spec_invalid")
    value = strict_json_object(candidate)
    _windows_spec_version(value)
    _require_windows_spec_path(value.get("root"), root)
    _require_windows_spec_path(value.get("state_root"), state_root)
    _require_windows_spec_path(value.get("uv_path"))
    return value


def _windows_task_command(
    *,
    powershell: str,
    script_path: Path,
    root: Path,
    state_root: Path,
    uv_path: Path,
    mode: str | None,
    spec_version: int = WINDOWS_TASK_SPEC_VERSION,
) -> tuple[str, ...]:
    command = (
        powershell,
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(script_path),
        "-VaultRoot",
        str(Path(root).resolve()),
        "-StateRoot",
        str(Path(state_root).resolve()),
        "-UvPath",
        str(installer_stable_uv_path(uv_path)),
        "-SpecVersion",
        str(spec_version),
    )
    if mode is None:
        return command
    return (*command, mode)


def windows_task_command_from_spec(
    spec: Mapping[str, object],
    *,
    powershell: str,
    script_path: Path,
    mode: str | None,
) -> tuple[str, ...]:
    command = _windows_task_command(
        powershell=powershell,
        script_path=script_path,
        root=Path(str(spec["root"])),
        state_root=Path(str(spec["state_root"])),
        uv_path=Path(str(spec["uv_path"])),
        mode=mode,
        spec_version=_windows_spec_version(spec),
    )
    if spec.get("spec", 1) == 1:
        return command
    limits = {str(task["kind"]): task["limit_hours"] for task in spec["tasks"]}
    return (*command, "-NightlyLimitHours", str(limits["nightly"]),
            "-WeeklyLimitHours", str(limits["weekly"]))


def _windows_task_state_value(output: bytes) -> str:
    if len(output) > MAX_RECORD_BYTES:
        raise InstallControlError("install_scheduler_output_oversized")
    try:
        value = json.loads(output)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InstallControlError("install_windows_task_state_invalid") from exc
    if not isinstance(value, dict) or value.get("state") not in {
        "absent",
        "conflict",
        "equivalent",
    }:
        raise InstallControlError("install_windows_task_state_invalid")
    return str(value["state"])


def _write_windows_tasks(
    value: bytes | None, runner: CommandRunner, command: tuple[str, ...]
) -> None:
    if value is None:
        require_command(runner, (*command, "-Uninstall"))
        return
    require_command(runner, command)


def _windows_projection_state(
    spec: Mapping[str, object],
    *,
    runner: CommandRunner,
    powershell: str,
    script_path: Path,
) -> str:
    command = windows_task_command_from_spec(
        spec, powershell=powershell, script_path=script_path, mode=None
    )
    exit_code, output = runner((*command, "-StateJson"), None)
    if exit_code != 0:
        raise InstallControlError("install_windows_task_state_failed")
    states = {"absent": "absent", "conflict": "conflict", "equivalent": "installed"}
    return states[_windows_task_state_value(output)]


def _windows_projection_writer(
    expected: bytes | None,
    replacement: bytes | None,
    *,
    resolver: Callable[[bytes], Mapping[str, object]],
    reader: Callable[[Sequence[bytes]], bytes | None],
    runner: CommandRunner,
    powershell: str,
    script_path: Path,
) -> None:
    require_projection_before_write(reader, expected, replacement)
    if expected is not None:
        old = resolver(expected)
        command = windows_task_command_from_spec(
            old, powershell=powershell, script_path=script_path, mode="-Uninstall"
        )
        require_command(runner, command)
    if replacement is not None:
        new = resolver(replacement)
        command = windows_task_command_from_spec(
            new, powershell=powershell, script_path=script_path, mode=None
        )
        require_command(runner, command)
    require_scheduler_projection(reader, replacement)


def _load_persisted_windows_spec(path: Path, root: Path, state_root: Path) -> bytes:
    value = read_managed_file(path)
    if value is None:
        raise InstallControlError("install_legacy_definition_missing")
    decode_windows_task_spec(value, root, state_root)
    return value


def _recover_windows_spec(
    snapshot: Mapping[str, object], loader: Callable[[], bytes]
) -> tuple[bytes, bytes]:
    candidate = loader()
    if not same_snapshot(candidate, snapshot):
        raise InstallControlError("install_legacy_definition_mismatch")
    return candidate, candidate


def windows_task_scheduler_resource(
    *,
    root: Path,
    state_root: Path,
    uv_path: Path,
    script_path: Path,
    powershell: str,
    runner: CommandRunner = default_command_runner,
) -> ManagedResource:
    script_path = Path(script_path).resolve(strict=False)
    desired = render_windows_task_spec(root, state_root, uv_path)
    command = windows_task_command_from_spec(
        decode_windows_task_spec(desired, root, state_root),
        powershell=powershell,
        script_path=script_path,
        mode=None,
    )
    persisted_path = Path(state_root) / "run" / "install" / "scheduler" / "windows" / "tasks.json"
    resolver = partial(decode_windows_task_spec, root=root, state_root=state_root)
    state_reader = partial(
        _windows_projection_state,
        runner=runner,
        powershell=powershell,
        script_path=script_path,
    )
    absence_reader = partial(state_reader, resolver(desired))
    reader = partial(
        read_scheduler_projections,
        resolver=resolver,
        state_reader=state_reader,
        absence_reader=absence_reader,
    )
    legacy_loader = partial(_load_persisted_windows_spec, persisted_path, root, state_root)
    return ManagedResource(
        resource_id="windows-task-scheduler-maintenance",
        kind="windows_task_scheduler",
        locator="windows-task-scheduler://LLMWiki",
        desired=desired,
        read_owned=lambda: reader((desired,)),
        write_owned=lambda value: _write_windows_tasks(value, runner, command),
        recognizes=lambda current: current == desired,
        read_projections=reader,
        write_projection=lambda expected, replacement, _metadata: _windows_projection_writer(
            expected,
            replacement,
            resolver=resolver,
            reader=reader,
            runner=runner,
            powershell=powershell,
            script_path=script_path,
        ),
        recover_legacy_projection=lambda snapshot: _recover_windows_spec(snapshot, legacy_loader),
        metadata={
            "definition_set": "windows",
            "uv_path": str(installer_stable_uv_path(uv_path)),
        },
        definitions={"scheduler/windows/tasks.json": desired},
        adopt_as_absent=True,
    )
