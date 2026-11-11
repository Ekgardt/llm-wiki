"""Rebuilding resources from an existing install record."""

from __future__ import annotations

import argparse
from collections.abc import Callable, Mapping, Sequence
from functools import partial
from pathlib import Path

from install_state import (
    InstallControlError,
    ManagedResource,
    read_origin,
    transaction_resources,
)
from llm_wiki import install_record_reader, install_resource_builder
from llm_wiki.install_profile import profile_resource
from llm_wiki.install_record_reader import (
    record_backend,
    record_metadata,
    record_resource_ids,
    recorded_profile,
    require_record_roots,
)
from llm_wiki.install_records import record_identity, resource_identity
from llm_wiki.install_scheduler_render import installer_stable_uv_path


def resources_from_existing_args(args: argparse.Namespace, command: str) -> list[ManagedResource]:
    record = install_record_reader.existing_record(args.state_root.resolve(), command)
    return resources_from_record(args, record)


def resources_from_record(
    args: argparse.Namespace, record: Mapping[str, object]
) -> list[ManagedResource]:
    root = args.root.resolve()
    state_root = args.state_root.resolve()
    require_record_roots(record, root, state_root)
    identifiers = record_resource_ids(record)
    built = install_resource_builder.build_install_resources(
        backend=record_backend(record),
        root=root,
        state_root=state_root,
        uv_path=installer_stable_uv_path(args.uv_path),
        home=args.home.resolve(),
        profile=recorded_profile(record, args.profile),
        powershell_path=args.powershell_path,
        retired_cursor_hooks="cursor-user-hooks" in identifiers,
        retired_antigravity_hooks="antigravity-user-hooks" in identifiers,
        opencode_plugin="opencode-plugin" in identifiers,
        claude_settings="claude-user-settings" in identifiers,
        codex_hooks="codex-user-hooks" in identifiers,
        ownership_metadata=record_metadata(record),
    )
    return [*built, *_recorded_extras(args, record, built)]


def _recorded_extras(
    args: argparse.Namespace, record: Mapping[str, object], built: Sequence[ManagedResource]
) -> list[ManagedResource]:
    """Recorded resources the current request builders do not make: the old place of a
    moved profile or scheduler, and a drop-in an update handed over."""
    have = {resource_identity(resource) for resource in built}
    missing = [item for item in transaction_resources(record) if record_identity(item) not in have]
    return [_rebuilt(args, item, built) for item in _rebuildable(missing)]


def _rebuildable(records: Sequence[Mapping[str, object]]) -> list[Mapping[str, object]]:
    """A kind no rebuilder knows stays missing, and the transaction refuses it by identity."""
    return [record for record in records if str(record.get("kind")) in _REBUILDERS]


def _rebuilt(
    args: argparse.Namespace, record: Mapping[str, object], built: Sequence[ManagedResource]
) -> ManagedResource:
    return _REBUILDERS[str(record["kind"])](args, record, built)


def _public_metadata(record: Mapping[str, object]) -> dict[str, object]:
    metadata = record.get("metadata")
    if not isinstance(metadata, Mapping):
        raise InstallControlError("install_state_schema_invalid")
    return {key: value for key, value in metadata.items() if not str(key).startswith("_")}


def _rebuilt_profile(
    args: argparse.Namespace, record: Mapping[str, object], _built: Sequence[ManagedResource]
) -> ManagedResource:
    return profile_resource(
        Path(str(record["locator"])),
        args.root.resolve(),
        args.state_root.resolve(),
        _public_metadata(record),
    )


def _rebuilt_scheduler(backend: str) -> Callable[..., ManagedResource]:
    def rebuild(
        args: argparse.Namespace, _record: Mapping[str, object], _built: Sequence[ManagedResource]
    ) -> ManagedResource:
        return install_resource_builder.posix_scheduler_resource(
            backend=backend,
            root=args.root.resolve(),
            state_root=args.state_root.resolve(),
            uv_path=installer_stable_uv_path(args.uv_path),
            ownership_metadata={},
        )

    return rebuild


def _rebuilt_drop_in(
    args: argparse.Namespace, record: Mapping[str, object], built: Sequence[ManagedResource]
) -> ManagedResource:
    """The drop-in, made by the systemd scheduler so it reloads the same manager."""
    rebuilt_systemd = partial(_rebuilt_scheduler("systemd_user"), args, record, built)
    schedulers = [resource for resource in built if resource.make_drop_in is not None]
    scheduler = next(iter(schedulers), None) or rebuilt_systemd()
    value = _recorded_value(args.state_root.resolve() / "run" / "install", record)
    return scheduler.make_drop_in(Path(str(record["locator"])), value)


def _recorded_value(install_root: Path, record: Mapping[str, object]) -> bytes:
    """The content a record writes or restores, whichever of the two it holds."""
    values = [_snapshot_value(install_root, record.get(name)) for name in ("desired", "rollback")]
    present = [value for value in values if value is not None]
    if not present:
        raise InstallControlError("install_transaction_invalid")
    return present[0]


def _snapshot_value(install_root: Path, snapshot: object) -> bytes | None:
    if not isinstance(snapshot, Mapping):
        raise InstallControlError("install_transaction_invalid")
    return read_origin(install_root, snapshot)


_REBUILDERS: dict[str, Callable[..., ManagedResource]] = {
    "profile_fragment": _rebuilt_profile,
    "systemd_scheduler": _rebuilt_scheduler("systemd_user"),
    "launchd_scheduler": _rebuilt_scheduler("launchd"),
    "systemd_drop_in": _rebuilt_drop_in,
}
