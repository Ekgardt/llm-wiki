"""Reading the existing install record."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from install_state import (
    InstallControlError,
    read_install_record,
    transaction_resources,
)
from llm_wiki.install_records import BACKENDS


def record_metadata(record: Mapping[str, object]) -> dict[str, Mapping[str, object]]:
    result: dict[str, Mapping[str, object]] = {}
    for resource in transaction_resources(record):
        resource_id = resource.get("id")
        metadata = resource.get("metadata")
        if not isinstance(resource_id, str) or not isinstance(metadata, Mapping):
            raise InstallControlError("install_state_schema_invalid")
        # The first record of a name is the one `_recorded_profile` builds; a second one
        # (a profile an update moved) is rebuilt from its own record.
        result.setdefault(resource_id, metadata)
    return result


def require_record_roots(record: Mapping[str, object], root: Path, state_root: Path) -> None:
    if record.get("vault_root") != str(root):
        raise InstallControlError("install_vault_root_mismatch")
    if record.get("state_root") != str(state_root):
        raise InstallControlError("install_state_root_mismatch")


def record_backend(record: Mapping[str, object]) -> str:
    backend = record.get("scheduler_backend")
    if not isinstance(backend, str) or backend not in BACKENDS:
        raise InstallControlError("install_scheduler_backend_invalid")
    return backend


def existing_record(state_root: Path, command: str) -> dict[str, object]:
    install_root = state_root / "run" / "install"
    if command == "uninstall":
        return read_install_record(install_root / "manifest.json", "install-manifest/")
    return read_install_record(install_root / "transaction.json", "install-transaction/")


def record_resource_ids(record: Mapping[str, object]) -> set[str]:
    return {str(resource["id"]) for resource in transaction_resources(record)}


def recorded_profile(record: Mapping[str, object], fallback: Path | None) -> Path | None:
    """The profile the record wrote to; the login shell may have changed since."""
    for resource in transaction_resources(record):
        locator = resource.get("locator")
        if resource.get("id") == "unix-profile" and isinstance(locator, str):
            return Path(locator)
    return fallback
