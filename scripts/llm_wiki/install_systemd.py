"""The systemd user timer the installer owns."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from functools import partial
from pathlib import Path

from install_state import (
    InstallControlError,
    ManagedResource,
    read_managed_file,
    remove_managed_file,
    write_managed_file,
)
from llm_wiki.install_records import (
    CommandRunner,
    default_command_runner,
    require_command,
    try_command,
)
from llm_wiki.install_scheduler_projection import (
    SYSTEMD_DEFINITION_NAMES,
    current_definitions_bundle,
    definition_bundle,
    definition_candidate,
    load_persisted_definitions,
    read_scheduler_projections,
    recover_legacy_definitions,
    require_projection_before_write,
    require_scheduler_projection,
)
from llm_wiki.install_scheduler_render import installer_stable_uv_path, render_systemd_definitions


def _systemd_timer_state(runner: CommandRunner, systemctl: str, timer: str) -> str:
    enabled, _ = runner((systemctl, "--user", "is-enabled", timer), None)
    active, _ = runner((systemctl, "--user", "is-active", timer), None)
    if enabled == active == 0:
        return "active"
    if enabled != 0 and active != 0:
        return "absent"
    return "conflict"


def systemd_file_state(unit_directory: Path, definitions: Mapping[str, bytes]) -> str:
    values = [read_managed_file(unit_directory / name) for name in definitions]
    if set(values) == {None}:
        return "absent"
    expected = [definitions[name] for name in definitions]
    if values == expected:
        return "installed"
    return "conflict"


def combined_scheduler_state(file_state: str, service_states: Sequence[str]) -> str:
    signature = (file_state, frozenset(service_states))
    if signature == ("absent", frozenset({"absent"})):
        return "absent"
    if signature == ("installed", frozenset({"active"})):
        return "installed"
    return "conflict"


def _systemd_projection_state(
    unit_directory: Path,
    definitions: Mapping[str, bytes],
    runner: CommandRunner,
    systemctl: str,
) -> str:
    timers = ("llm-wiki-nightly.timer", "llm-wiki-weekly.timer")
    file_state = systemd_file_state(unit_directory, definitions)
    service_states = [_systemd_timer_state(runner, systemctl, timer) for timer in timers]
    return combined_scheduler_state(file_state, service_states)


def write_systemd_files(unit_directory: Path, definitions: Mapping[str, bytes]) -> None:
    for name, value in definitions.items():
        write_managed_file(unit_directory / name, value, 0o600)


def remove_systemd_files(unit_directory: Path, definitions: Mapping[str, bytes]) -> None:
    for name, expected in definitions.items():
        path = unit_directory / name
        current = read_managed_file(path)
        if current is None:
            continue
        if current != expected:
            raise InstallControlError("install_scheduler_file_drift")
        remove_managed_file(path)


def _install_systemd(
    unit_directory: Path,
    definitions: Mapping[str, bytes],
    runner: CommandRunner,
    systemctl: str,
) -> None:
    write_systemd_files(unit_directory, definitions)
    require_command(runner, (systemctl, "--user", "daemon-reload"))
    require_command(
        runner,
        (
            systemctl,
            "--user",
            "enable",
            "--now",
            "llm-wiki-nightly.timer",
            "llm-wiki-weekly.timer",
        ),
    )


def _uninstall_systemd(
    unit_directory: Path,
    definitions: Mapping[str, bytes],
    runner: CommandRunner,
    systemctl: str,
) -> None:
    try_command(
        runner,
        (
            systemctl,
            "--user",
            "disable",
            "--now",
            "llm-wiki-nightly.timer",
            "llm-wiki-weekly.timer",
        ),
    )
    try_command(
        runner,
        (
            systemctl,
            "--user",
            "clean",
            "--what=state",
            "llm-wiki-nightly.timer",
            "llm-wiki-weekly.timer",
        ),
    )
    remove_systemd_files(unit_directory, definitions)
    require_command(runner, (systemctl, "--user", "daemon-reload"))


def _write_systemd(
    value: bytes | None,
    *,
    unit_directory: Path,
    definitions: Mapping[str, bytes],
    runner: CommandRunner,
    systemctl: str,
) -> None:
    if value is None:
        _uninstall_systemd(unit_directory, definitions, runner, systemctl)
        return
    _install_systemd(unit_directory, definitions, runner, systemctl)


def _systemd_projection_writer(
    expected: bytes | None,
    replacement: bytes | None,
    *,
    resolver: Callable[[bytes], Mapping[str, bytes]],
    reader: Callable[[Sequence[bytes]], bytes | None],
    unit_directory: Path,
    runner: CommandRunner,
    systemctl: str,
) -> None:
    require_projection_before_write(reader, expected, replacement)
    old = None if expected is None else resolver(expected)
    new = None if replacement is None else resolver(replacement)
    if new is None:
        if old is None:
            return
        _uninstall_systemd(unit_directory, old, runner, systemctl)
    else:
        _install_systemd(unit_directory, new, runner, systemctl)
    require_scheduler_projection(reader, replacement)


def systemd_scheduler_resource(
    *,
    root: Path,
    state_root: Path,
    uv_path: Path,
    unit_directory: Path,
    runner: CommandRunner = default_command_runner,
    systemctl: str = "systemctl",
) -> ManagedResource:
    unit_directory = Path(unit_directory).resolve(strict=False)
    definitions = render_systemd_definitions(root, state_root, uv_path)
    desired = definition_bundle(definitions)
    persisted = {f"scheduler/linux/{name}": value for name, value in definitions.items()}
    legacy_directory = Path(state_root) / "run" / "install" / "scheduler" / "linux"
    legacy_loader = partial(
        load_persisted_definitions, legacy_directory, SYSTEMD_DEFINITION_NAMES
    )
    resolver = partial(
        definition_candidate,
        expected_names=SYSTEMD_DEFINITION_NAMES,
        legacy_loader=legacy_loader,
    )
    state_reader = partial(
        _systemd_projection_state,
        unit_directory,
        runner=runner,
        systemctl=systemctl,
    )
    absence_reader = partial(state_reader, {name: b"" for name in SYSTEMD_DEFINITION_NAMES})
    reader = partial(
        read_scheduler_projections,
        resolver=resolver,
        state_reader=state_reader,
        absence_reader=absence_reader,
    )
    return ManagedResource(
        resource_id="systemd-user-maintenance",
        kind="systemd_scheduler",
        locator=f"systemd-user://{unit_directory}",
        desired=desired,
        read_owned=lambda: reader((desired,)),
        write_owned=lambda value: _write_systemd(
            value,
            unit_directory=unit_directory,
            definitions=definitions,
            runner=runner,
            systemctl=systemctl,
        ),
        recognizes=lambda current: current == desired,
        read_projections=reader,
        write_projection=lambda expected, replacement, _metadata: _systemd_projection_writer(
            expected,
            replacement,
            resolver=resolver,
            reader=reader,
            unit_directory=unit_directory,
            runner=runner,
            systemctl=systemctl,
        ),
        recover_legacy_projection=lambda snapshot: recover_legacy_definitions(
            snapshot, SYSTEMD_DEFINITION_NAMES, legacy_loader
        ),
        metadata={
            "definition_set": "linux",
            "unit_directory": str(unit_directory),
            "uv_path": str(installer_stable_uv_path(uv_path)),
        },
        definitions=persisted,
        adopt_as_absent=True,
        read_current=partial(current_definitions_bundle, unit_directory, SYSTEMD_DEFINITION_NAMES),
        make_drop_in=partial(systemd_drop_in_resource, runner=runner, systemctl=systemctl),
    )


def systemd_drop_in_resource(
    path: Path,
    desired: bytes,
    *,
    runner: CommandRunner = default_command_runner,
    systemctl: str = "systemctl",
) -> ManagedResource:
    """A drop-in one update writes and hands over when it commits.

    It is a resource of that update only: a failure reverts it with the unit, and the
    `rollback` of that generation removes it before the unit is restored, so a line
    moved out of the unit never runs twice. It never enters the manifest, so no later
    update or uninstall touches it. Writing or removing it reloads systemd, which reads
    a drop-in only then (docs/research/2026-09-28-what-an-update-created-its-rollback-takes-back.md).
    """
    path = Path(path).resolve(strict=False)
    unit = path.parent.name.removesuffix(".d")
    return ManagedResource(
        resource_id=f"systemd-drop-in.{unit}",
        kind="systemd_drop_in",
        locator=str(path),
        desired=desired,
        read_owned=lambda: read_managed_file(path),
        write_owned=lambda value: _write_drop_in(path, value, runner, systemctl),
        recognizes=lambda current: current == desired,
        metadata={"file_mode": 0o600},
    )


def _write_drop_in(
    path: Path, value: bytes | None, runner: CommandRunner, systemctl: str
) -> None:
    write_managed_file(path, value, 0o600)
    _drop_empty_directory(path.parent)
    require_command(runner, (systemctl, "--user", "daemon-reload"))


def _drop_empty_directory(directory: Path) -> None:
    """The `<unit>.d` directory a removed drop-in leaves empty goes with it."""
    if directory.is_dir() and not any(directory.iterdir()):
        directory.rmdir()
