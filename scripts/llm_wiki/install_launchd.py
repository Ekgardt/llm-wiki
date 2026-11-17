"""The launchd agent the installer owns."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from functools import partial
from pathlib import Path

from install_state import (
    ManagedResource,
)
from llm_wiki.install_records import CommandRunner, default_command_runner, require_command
from llm_wiki.install_scheduler_projection import (
    LAUNCHD_DEFINITION_NAMES,
    current_definitions_bundle,
    definition_bundle,
    definition_candidate,
    load_persisted_definitions,
    read_scheduler_projections,
    recover_legacy_definitions,
    require_projection_before_write,
    require_scheduler_projection,
)
from llm_wiki.install_scheduler_render import installer_stable_uv_path, render_launchd_definitions
from llm_wiki.install_systemd import (
    combined_scheduler_state,
    remove_systemd_files,
    systemd_file_state,
    write_systemd_files,
)


def _launchd_label(name: str) -> str:
    return name.removesuffix(".plist")


# `launchctl print` exits 113 ("Could not find service") for a job launchd does not
# have; any other failure says nothing about the job (audit 2026-09-26, regress 11,
# docs/research/2026-09-26-launchd-absent-only-when-launchd-says-so.md).
LAUNCHD_SERVICE_NOT_FOUND = 113


_LAUNCHD_PRINT_STATES = {0: "active", LAUNCHD_SERVICE_NOT_FOUND: "absent"}


def _launchd_job_state(runner: CommandRunner, launchctl: str, domain: str, label: str) -> str:
    """`active`, `absent`, or `unknown` when launchctl failed for another reason."""
    exit_code, _output = runner((launchctl, "print", f"{domain}/{label}"), None)
    return _LAUNCHD_PRINT_STATES.get(exit_code, "unknown")


def _launchd_projection_state(
    launch_agents_directory: Path,
    definitions: Mapping[str, bytes],
    runner: CommandRunner,
    launchctl: str,
    domain: str,
) -> str:
    file_state = systemd_file_state(launch_agents_directory, definitions)
    states = [
        _launchd_job_state(runner, launchctl, domain, _launchd_label(name)) for name in definitions
    ]
    return combined_scheduler_state(file_state, states)


def _install_launchd(
    launch_agents_directory: Path,
    definitions: Mapping[str, bytes],
    runner: CommandRunner,
    launchctl: str,
    domain: str,
) -> None:
    write_systemd_files(launch_agents_directory, definitions)
    for name in definitions:
        require_command(
            runner,
            (launchctl, "bootstrap", domain, str(launch_agents_directory / name)),
        )


def uninstall_launchd(
    launch_agents_directory: Path,
    definitions: Mapping[str, bytes],
    runner: CommandRunner,
    launchctl: str,
    domain: str,
) -> None:
    for name in reversed(definitions):
        bootout_if_loaded(runner, launchctl, domain, _launchd_label(name))
    remove_systemd_files(launch_agents_directory, definitions)


def bootout_if_loaded(runner: CommandRunner, launchctl: str, domain: str, label: str) -> None:
    """A job launchd no longer has is already out; `bootout` of it fails and blocked the uninstall.

    Only a job launchd says it does not have is skipped: a `print` that failed
    otherwise still gets its `bootout`, whose failure then stops the uninstall
    instead of leaving the job loaded. Audit C-34,
    docs/research/2026-09-25-the-installers-agree.md.
    """
    if _launchd_job_state(runner, launchctl, domain, label) == "absent":
        return
    require_command(runner, (launchctl, "bootout", f"{domain}/{label}"))


def _write_launchd(
    value: bytes | None,
    *,
    launch_agents_directory: Path,
    definitions: Mapping[str, bytes],
    runner: CommandRunner,
    launchctl: str,
    domain: str,
) -> None:
    if value is None:
        uninstall_launchd(launch_agents_directory, definitions, runner, launchctl, domain)
        return
    _install_launchd(launch_agents_directory, definitions, runner, launchctl, domain)


def _launchd_projection_writer(
    expected: bytes | None,
    replacement: bytes | None,
    *,
    resolver: Callable[[bytes], Mapping[str, bytes]],
    reader: Callable[[Sequence[bytes]], bytes | None],
    launch_agents_directory: Path,
    runner: CommandRunner,
    launchctl: str,
    domain: str,
) -> None:
    require_projection_before_write(reader, expected, replacement)
    if expected is not None:
        old = resolver(expected)
        uninstall_launchd(launch_agents_directory, old, runner, launchctl, domain)
    if replacement is not None:
        new = resolver(replacement)
        _install_launchd(launch_agents_directory, new, runner, launchctl, domain)
    require_scheduler_projection(reader, replacement)


def launchd_scheduler_resource(
    *,
    root: Path,
    state_root: Path,
    uv_path: Path,
    launch_agents_directory: Path,
    uid: int,
    runner: CommandRunner = default_command_runner,
    launchctl: str = "launchctl",
) -> ManagedResource:
    launch_agents_directory = Path(launch_agents_directory).resolve(strict=False)
    definitions = render_launchd_definitions(root, state_root, uv_path)
    desired = definition_bundle(definitions)
    domain = f"gui/{uid}"
    persisted = {f"scheduler/macos/{name}": value for name, value in definitions.items()}
    legacy_directory = Path(state_root) / "run" / "install" / "scheduler" / "macos"
    legacy_loader = partial(
        load_persisted_definitions, legacy_directory, LAUNCHD_DEFINITION_NAMES
    )
    resolver = partial(
        definition_candidate,
        expected_names=LAUNCHD_DEFINITION_NAMES,
        legacy_loader=legacy_loader,
    )
    state_reader = partial(
        _launchd_projection_state,
        launch_agents_directory,
        runner=runner,
        launchctl=launchctl,
        domain=domain,
    )
    absence_reader = partial(state_reader, {name: b"" for name in LAUNCHD_DEFINITION_NAMES})
    reader = partial(
        read_scheduler_projections,
        resolver=resolver,
        state_reader=state_reader,
        absence_reader=absence_reader,
    )
    return ManagedResource(
        resource_id="launchd-user-maintenance",
        kind="launchd_scheduler",
        locator=f"launchd-user://{domain}",
        desired=desired,
        read_owned=lambda: reader((desired,)),
        write_owned=lambda value: _write_launchd(
            value,
            launch_agents_directory=launch_agents_directory,
            definitions=definitions,
            runner=runner,
            launchctl=launchctl,
            domain=domain,
        ),
        recognizes=lambda current: current == desired,
        read_projections=reader,
        write_projection=lambda expected, replacement, _metadata: _launchd_projection_writer(
            expected,
            replacement,
            resolver=resolver,
            reader=reader,
            launch_agents_directory=launch_agents_directory,
            runner=runner,
            launchctl=launchctl,
            domain=domain,
        ),
        recover_legacy_projection=lambda snapshot: recover_legacy_definitions(
            snapshot, LAUNCHD_DEFINITION_NAMES, legacy_loader
        ),
        metadata={
            "definition_set": "macos",
            "launchd_domain": domain,
            "uv_path": str(installer_stable_uv_path(uv_path)),
        },
        definitions=persisted,
        adopt_as_absent=True,
        read_current=partial(
            current_definitions_bundle, launch_agents_directory, LAUNCHD_DEFINITION_NAMES
        ),
    )
