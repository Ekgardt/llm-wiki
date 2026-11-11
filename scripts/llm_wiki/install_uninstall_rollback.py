"""Uninstall and rollback of installed resources."""

from __future__ import annotations

import secrets
from collections.abc import Mapping, Sequence
from pathlib import Path

from install_state import (
    MANIFEST_SCHEMA,
    InstallControlError,
    ManagedResource,
    optional_install_record,
    read_install_record,
    read_state_bytes,
    transaction_resources,
    validate_v2_record_preimages,
)
from install_state import sha256 as _sha256
from llm_wiki import install_transactions, install_v2_mutation
from llm_wiki.formats.canonical import canonical_json_bytes
from llm_wiki.install_records import (
    Identity,
    install_lock,
    optional_record,
    read_record,
    record_identity,
    records_by_identity,
    utc_now,
    write_record,
)
from llm_wiki.install_transactions import (
    record_failure,
    require_installed_resources,
    require_resource_request,
    rollback_install,
    validate_resources,
)
from llm_wiki.install_v2_transactions import (
    checkpoint_resources,
    complete_v2_publication,
    continue_v2_operation,
    ordered_transaction_resources,
    published_v2_target,
    require_active_resources,
    require_v2_base_manifest,
    v2_failed_operation,
    v2_rollback_mutations,
    validate_active_manifest,
)
from reliable_memory import fsync_directory


def _uninstall_resource_record(record: Mapping[str, object]) -> dict[str, object]:
    result = dict(record)
    result["state"] = "pending"
    return result


def _new_uninstall_transaction(manifest: Mapping[str, object]) -> dict[str, object]:
    now = utc_now()
    resources = transaction_resources(manifest)
    return {
        "error": None,
        "id": secrets.token_hex(16),
        "operation": "uninstall",
        "release": manifest["release"],
        "request_sha256": manifest["request_sha256"],
        "resources": [_uninstall_resource_record(record) for record in resources],
        "scheduler_backend": manifest["scheduler_backend"],
        "schema": "install-transaction/v1",
        "started_at": now,
        "state": "prepared",
        "state_root": manifest["state_root"],
        "target_manifest_sha256": None,
        "updated_at": now,
        "vault_root": manifest["vault_root"],
    }


def _retire_manifest(path: Path, expected: Mapping[str, object]) -> None:
    current = read_record(path, MANIFEST_SCHEMA)
    if current != expected:
        raise InstallControlError("install_manifest_changed")
    path.unlink()
    fsync_directory(path.parent)


def _continue_uninstall(
    *,
    install_root: Path,
    transaction_path: Path,
    transaction: dict[str, object],
    manifest: dict[str, object],
    resources: Sequence[ManagedResource],
) -> dict[str, object]:
    records = transaction_resources(transaction)
    try:
        rollback_install(
            install_root=install_root,
            transaction_path=transaction_path,
            transaction=transaction,
            records=records,
            resources=resources,
        )
        install_transactions.set_transaction_state(transaction_path, transaction, "publishing")
        _retire_manifest(install_root / "manifest.json", manifest)
        install_transactions.set_transaction_state(transaction_path, transaction, "committed")
        return transaction
    except Exception as error:
        record_failure(transaction_path, transaction, error)
        install_transactions.set_transaction_state(transaction_path, transaction, "quarantined")
        raise


def _resume_or_start_uninstall(
    *,
    install_root: Path,
    transaction_path: Path,
    transaction: dict[str, object] | None,
    manifest: dict[str, object],
    resources: Sequence[ManagedResource],
) -> dict[str, object]:
    manifest_records = transaction_resources(manifest)
    require_resource_request(manifest_records, resources)
    require_installed_resources(manifest_records, resources)
    if transaction is None or transaction.get("operation") != "uninstall":
        transaction = _new_uninstall_transaction(manifest)
        write_record(transaction_path, transaction)
    return _continue_uninstall(
        install_root=install_root,
        transaction_path=transaction_path,
        transaction=transaction,
        manifest=manifest,
        resources=resources,
    )


def _v2_uninstall_record(active: Mapping[str, object]) -> dict[str, object]:
    return {
        "desired": active["origin"],
        "id": active["id"],
        "kind": active["kind"],
        "locator": active["locator"],
        "metadata": active["metadata"],
        "origin": active["origin"],
        "rollback": active["desired"],
        "state": "pending",
    }


def _new_v2_uninstall_transaction(
    manifest: Mapping[str, object],
) -> dict[str, object]:
    now = utc_now()
    records = [_v2_uninstall_record(record) for record in transaction_resources(manifest)]
    return {
        "base_manifest_sha256": _sha256(canonical_json_bytes(dict(manifest))),
        "error": None,
        "generation": int(manifest["generation"]) + 1,
        "id": secrets.token_hex(16),
        "installation_id": manifest["installation_id"],
        "operation": "uninstall",
        "release": manifest["release"],
        "request_sha256": manifest["request_sha256"],
        "resources": records,
        "rollback_point": None,
        "scheduler_backend": manifest["scheduler_backend"],
        "schema": "install-transaction/v2",
        "started_at": now,
        "state": "prepared",
        "state_root": manifest["state_root"],
        "target_manifest_sha256": None,
        "updated_at": now,
        "vault_root": manifest["vault_root"],
    }


def _retire_v2_manifest(path: Path, expected_sha256: str) -> None:
    current = read_state_bytes(path)
    if _sha256(current) != expected_sha256:
        raise InstallControlError("install_manifest_changed")
    path.unlink()
    fsync_directory(path.parent)


def _mutate_v2_uninstall(
    *,
    install_root: Path,
    transaction: dict[str, object],
    records: Sequence[dict[str, object]],
    resources: Sequence[ManagedResource],
) -> None:
    transaction_path = install_root / "transaction.json"
    install_transactions.set_transaction_state(transaction_path, transaction, "mutating")
    try:
        install_v2_mutation.v2_mutate_resources(
            install_root=install_root,
            transaction_path=transaction_path,
            transaction=transaction,
            records=records,
            resources=resources,
        )
    except Exception as error:
        v2_failed_operation(
            install_root=install_root,
            transaction_path=transaction_path,
            transaction=transaction,
            records=records,
            resources=resources,
            error=error,
        )
        raise


def _continue_v2_uninstall(
    install_root: Path,
    transaction: dict[str, object],
    resources: Sequence[ManagedResource],
) -> dict[str, object]:
    records = transaction_resources(transaction)
    ordered = ordered_transaction_resources(records, resources)
    _mutate_v2_uninstall(
        install_root=install_root,
        transaction=transaction,
        records=records,
        resources=ordered,
    )
    transaction_path = install_root / "transaction.json"
    install_transactions.set_transaction_state(transaction_path, transaction, "publishing")
    _retire_v2_manifest(install_root / "manifest.json", str(transaction["base_manifest_sha256"]))
    install_transactions.set_transaction_state(transaction_path, transaction, "committed")
    return transaction


def _start_v2_uninstall(
    install_root: Path,
    manifest: dict[str, object],
    transaction: dict[str, object] | None,
    resources: Sequence[ManagedResource],
) -> dict[str, object]:
    validate_active_manifest(install_root, manifest, transaction)
    require_active_resources(install_root, manifest, resources)
    checkpoint_resources(install_root, manifest, resources)
    uninstall = _new_v2_uninstall_transaction(manifest)
    validate_v2_record_preimages(install_root, uninstall)
    write_record(install_root / "transaction.json", uninstall)
    return _continue_v2_uninstall(install_root, uninstall, resources)


def _resume_inactive_v2_uninstall(
    transaction_path: Path, transaction: dict[str, object]
) -> dict[str, object]:
    if transaction.get("state") == "committed":
        return transaction
    if transaction.get("state") != "publishing":
        raise InstallControlError("install_manifest_absent")
    install_transactions.set_transaction_state(transaction_path, transaction, "committed")
    return transaction


def _active_v2_uninstall(
    install_root: Path,
    manifest: dict[str, object],
    transaction: dict[str, object] | None,
    resources: Sequence[ManagedResource],
) -> dict[str, object]:
    if transaction is not None and transaction.get("operation") == "uninstall":
        if transaction.get("state") == "reverting":
            return _resume_v2_uninstall_revert(install_root, transaction, resources)
        return _continue_v2_uninstall(install_root, transaction, resources)
    return _start_v2_uninstall(install_root, manifest, transaction, resources)


def _resume_v2_uninstall_revert(
    install_root: Path,
    transaction: dict[str, object],
    resources: Sequence[ManagedResource],
) -> dict[str, object]:
    records = transaction_resources(transaction)
    ordered = ordered_transaction_resources(records, resources)
    v2_rollback_mutations(
        install_root=install_root,
        transaction_path=install_root / "transaction.json",
        transaction=transaction,
        records=records,
        resources=ordered,
    )
    return transaction


def _uninstall_v2_under_lock(
    install_root: Path,
    manifest: dict[str, object] | None,
    transaction: dict[str, object] | None,
    resources: Sequence[ManagedResource],
) -> dict[str, object]:
    if manifest is not None:
        return _active_v2_uninstall(install_root, manifest, transaction, resources)
    if transaction is None or transaction.get("operation") != "uninstall":
        raise InstallControlError("install_manifest_absent")
    return _resume_inactive_v2_uninstall(install_root / "transaction.json", transaction)


def _uninstall_v1_under_lock(
    install_root: Path,
    manifest: dict[str, object] | None,
    transaction: dict[str, object] | None,
    resources: Sequence[ManagedResource],
) -> dict[str, object]:
    transaction_path = install_root / "transaction.json"
    if manifest is not None:
        return _resume_or_start_uninstall(
            install_root=install_root,
            transaction_path=transaction_path,
            transaction=transaction,
            manifest=manifest,
            resources=resources,
        )
    return _committed_uninstall_record(transaction)


def _committed_uninstall_record(transaction: dict[str, object] | None) -> dict[str, object]:
    """A finished uninstall answers with its own record; anything else has no manifest."""
    if transaction is not None and transaction.get("operation") == "uninstall":
        if transaction.get("state") == "committed":
            return transaction
    raise InstallControlError("install_manifest_absent")


def _uninstall_under_lock(
    install_root: Path, resources: Sequence[ManagedResource]
) -> dict[str, object]:
    manifest = optional_install_record(install_root / "manifest.json", "install-manifest/")
    transaction = optional_install_record(
        install_root / "transaction.json", "install-transaction/"
    )
    if _v2_manifest_record(manifest) or (
        transaction is not None and transaction.get("schema") == "install-transaction/v2"
    ):
        return _uninstall_v2_under_lock(install_root, manifest, transaction, resources)
    return _uninstall_v1_under_lock(install_root, manifest, transaction, resources)


def uninstall_resources(
    *,
    state_root: Path,
    resources: Sequence[ManagedResource],
    lock_timeout: float = 10.0,
) -> dict[str, object]:
    validate_resources(resources)
    install_root = Path(state_root) / "run" / "install"
    if not install_root.is_dir() or install_root.is_symlink():
        raise InstallControlError("install_state_absent")
    with install_lock(install_root / "install.lock", lock_timeout):
        return _uninstall_under_lock(install_root, resources)


def _retire_partial_manifest(install_root: Path, transaction: Mapping[str, object]) -> None:
    manifest_path = install_root / "manifest.json"
    manifest = optional_record(manifest_path, MANIFEST_SCHEMA)
    if manifest is None:
        return
    expected = transaction.get("target_manifest_sha256")
    actual = _sha256(canonical_json_bytes(manifest))
    if expected != actual:
        raise InstallControlError("install_manifest_transaction_mismatch")
    _retire_manifest(manifest_path, manifest)


def _rollback_checkpoint(manifest: Mapping[str, object]) -> Mapping[str, object]:
    checkpoint = manifest.get("rollback_point")
    if not isinstance(checkpoint, Mapping):
        raise InstallControlError("install_committed_rollback_unavailable")
    return checkpoint


def _checkpoint_by_identity(
    checkpoint: Mapping[str, object],
) -> dict[Identity, Mapping[str, object]]:
    records = checkpoint.get("resources")
    if not isinstance(records, list):
        raise InstallControlError("install_state_schema_invalid")
    if any(not isinstance(record, Mapping) for record in records):
        raise InstallControlError("install_state_schema_invalid")
    return records_by_identity(records)


def _rollback_target(
    active: Mapping[str, object], previous: Mapping[str, object] | None
) -> tuple[Mapping[str, object], Mapping[str, object], dict[str, object]]:
    if previous is not None:
        # The update recorded what it replaced: the previous release's value, or what an
        # adopted file held when it was taken over.
        return active["rollback"], previous["origin"], dict(previous["metadata"])
    metadata = dict(active["metadata"])
    metadata["_retire_after_commit"] = True
    return active["origin"], active["origin"], metadata


def _committed_rollback_record(
    active: Mapping[str, object], previous: Mapping[str, object] | None
) -> dict[str, object]:
    desired, origin, metadata = _rollback_target(active, previous)
    return {
        "desired": desired,
        "id": active["id"],
        "kind": active["kind"],
        "locator": active["locator"],
        "metadata": metadata,
        "origin": origin,
        "rollback": active["desired"],
        "state": "pending",
    }


def _committed_rollback_records(
    manifest: Mapping[str, object], checkpoint: Mapping[str, object]
) -> list[dict[str, object]]:
    previous = _checkpoint_by_identity(checkpoint)
    active = transaction_resources(manifest)
    return [
        *_restored_retired_records(previous, {record_identity(record) for record in active}),
        *(_committed_rollback_record(record, previous.get(record_identity(record))) for record in active),
    ]


def _released_removals(
    manifest: Mapping[str, object], transaction: Mapping[str, object] | None
) -> list[dict[str, object]]:
    """What the committed update handed over, taken back by its own rollback only.

    Only the transaction that published this manifest names them; after the next update
    they are the operator's.
    """
    if transaction is None or transaction.get("id") != manifest.get("transaction_id"):
        return []
    records = transaction_resources(transaction)
    return [_released_removal(record) for record in records if _was_released(record)]


def _was_released(record: Mapping[str, object]) -> bool:
    metadata = record.get("metadata")
    return isinstance(metadata, Mapping) and metadata.get("_released_after_commit") is True


def _released_removal(record: Mapping[str, object]) -> dict[str, object]:
    return {
        "desired": record["origin"],
        "id": record["id"],
        "kind": record["kind"],
        "locator": record["locator"],
        "metadata": dict(record["metadata"]),
        "origin": record["origin"],
        "rollback": record["desired"],
        "state": "pending",
    }


def _restored_retired_records(
    previous: Mapping[Identity, Mapping[str, object]], active: set[Identity]
) -> list[dict[str, object]]:
    """What the update took back because the new set no longer owned it, put back first."""
    return [
        _restored_retired_record(record)
        for identity, record in previous.items()
        if identity not in active
    ]


def _restored_retired_record(previous: Mapping[str, object]) -> dict[str, object]:
    return {
        "desired": previous["desired"],
        "id": previous["id"],
        "kind": previous["kind"],
        "locator": previous["locator"],
        "metadata": dict(previous["metadata"]),
        "origin": previous["origin"],
        "rollback": previous["origin"],
        "state": "pending",
    }


def _new_v2_rollback_transaction(
    manifest: Mapping[str, object],
    checkpoint: Mapping[str, object],
    records: list[dict[str, object]],
) -> dict[str, object]:
    now = utc_now()
    return {
        "base_manifest_sha256": _sha256(canonical_json_bytes(dict(manifest))),
        "error": None,
        "generation": int(manifest["generation"]) + 1,
        "id": secrets.token_hex(16),
        "installation_id": manifest["installation_id"],
        "operation": "rollback",
        "release": checkpoint["release"],
        "request_sha256": checkpoint["request_sha256"],
        "resources": records,
        "rollback_point": None,
        "scheduler_backend": checkpoint["scheduler_backend"],
        "schema": "install-transaction/v2",
        "started_at": now,
        "state": "prepared",
        "state_root": manifest["state_root"],
        "target_manifest_sha256": None,
        "updated_at": now,
        "vault_root": manifest["vault_root"],
    }


def _continue_v2_rollback(
    install_root: Path,
    transaction: dict[str, object],
    resources: Sequence[ManagedResource],
) -> dict[str, object]:
    records = transaction_resources(transaction)
    ordered = ordered_transaction_resources(records, resources)
    continue_v2_operation(
        install_root=install_root,
        transaction_path=install_root / "transaction.json",
        transaction=transaction,
        records=records,
        resources=ordered,
    )
    return transaction


def _resume_v2_rollback(
    install_root: Path,
    manifest: dict[str, object],
    transaction: dict[str, object],
    resources: Sequence[ManagedResource],
) -> dict[str, object]:
    if transaction.get("state") == "reverting":
        records = transaction_resources(transaction)
        ordered = ordered_transaction_resources(records, resources)
        v2_rollback_mutations(
            install_root=install_root,
            transaction_path=install_root / "transaction.json",
            transaction=transaction,
            records=records,
            resources=ordered,
        )
        return transaction
    if published_v2_target(manifest, transaction):
        complete_v2_publication(install_root / "transaction.json", transaction, manifest)
        return transaction
    require_v2_base_manifest(manifest, transaction)
    return _continue_v2_rollback(install_root, transaction, resources)


def _start_v2_committed_rollback(
    install_root: Path,
    manifest: dict[str, object],
    transaction: dict[str, object] | None,
    resources: Sequence[ManagedResource],
) -> dict[str, object]:
    validate_active_manifest(install_root, manifest, transaction)
    checkpoint = _rollback_checkpoint(manifest)
    require_active_resources(install_root, manifest, resources)
    checkpoint_resources(install_root, manifest, resources)
    records = [
        *_released_removals(manifest, transaction),
        *_committed_rollback_records(manifest, checkpoint),
    ]
    rollback = _new_v2_rollback_transaction(manifest, checkpoint, records)
    validate_v2_record_preimages(install_root, rollback)
    write_record(install_root / "transaction.json", rollback)
    return _continue_v2_rollback(install_root, rollback, resources)


def _interrupted_v2_operation(transaction: Mapping[str, object] | None) -> bool:
    """An install or update left between states; `rollback` reverts what it did.

    A quarantined one is included: its revert stopped, and retrying the same bounded
    revert is the operator's way out. It writes only files that still hold what this
    transaction wrote, so it cannot overwrite an edit made since
    (docs/research/2026-09-28-a-rollback-undoes-only-what-it-did.md).
    """
    if transaction is None or transaction.get("operation") not in {"install", "update"}:
        return False
    return transaction.get("state") in {
        "prepared",
        "mutating",
        "publishing",
        "reverting",
        "quarantined",
    }


def _required_active_manifest(
    manifest: dict[str, object] | None,
) -> dict[str, object]:
    if manifest is None:
        raise InstallControlError("install_manifest_absent")
    return manifest


def _revert_interrupted_v2_operation(
    install_root: Path,
    manifest: dict[str, object] | None,
    transaction: dict[str, object],
    resources: Sequence[ManagedResource],
) -> dict[str, object]:
    if published_v2_target(manifest, transaction):
        active = complete_v2_publication(install_root / "transaction.json", transaction, manifest)
        return _start_v2_committed_rollback(install_root, active, transaction, resources)
    require_v2_base_manifest(manifest, transaction)
    records = transaction_resources(transaction)
    ordered = ordered_transaction_resources(records, resources)
    transaction_path = install_root / "transaction.json"
    try:
        v2_rollback_mutations(
            install_root=install_root,
            transaction_path=transaction_path,
            transaction=transaction,
            records=records,
            resources=ordered,
        )
        return transaction
    except Exception as error:
        record_failure(transaction_path, transaction, error)
        install_transactions.set_transaction_state(transaction_path, transaction, "quarantined")
        raise


def _rollback_v2_under_lock(
    install_root: Path,
    manifest: dict[str, object] | None,
    transaction: dict[str, object] | None,
    resources: Sequence[ManagedResource],
) -> dict[str, object]:
    if _interrupted_v2_operation(transaction):
        return _revert_interrupted_v2_operation(install_root, manifest, transaction, resources)
    resumed = _resume_or_accept_v2_rollback(install_root, manifest, transaction, resources)
    if resumed is not None:
        return resumed
    return _start_v2_committed_rollback(
        install_root, _required_active_manifest(manifest), transaction, resources
    )


def _resume_or_accept_v2_rollback(
    install_root: Path,
    manifest: dict[str, object] | None,
    transaction: dict[str, object] | None,
    resources: Sequence[ManagedResource],
) -> dict[str, object] | None:
    if transaction is None or transaction.get("operation") != "rollback":
        return None
    active = _required_active_manifest(manifest)
    return _rollback_resolution(install_root, active, transaction, resources)


def _rollback_resolution(
    install_root: Path,
    active: dict[str, object],
    transaction: dict[str, object],
    resources: Sequence[ManagedResource],
) -> dict[str, object] | None:
    if transaction.get("state") == "committed":
        return _accepted_v2_rollback(active, transaction)
    if transaction.get("state") not in {
        "prepared",
        "mutating",
        "publishing",
        "reverting",
    }:
        return None
    return _resume_v2_rollback(install_root, active, transaction, resources)


def _accepted_v2_rollback(
    manifest: Mapping[str, object], transaction: dict[str, object]
) -> dict[str, object] | None:
    if manifest.get("transaction_id") != transaction.get("id"):
        return None
    return transaction


def _require_v1_rollback_transaction(transaction: Mapping[str, object]) -> None:
    if transaction.get("schema") != "install-transaction/v1":
        raise InstallControlError("install_rollback_operation_invalid")
    if transaction.get("operation") != "install":
        raise InstallControlError("install_rollback_operation_invalid")


def _continue_v1_rollback(
    install_root: Path,
    transaction: dict[str, object],
    records: Sequence[dict[str, object]],
    resources: Sequence[ManagedResource],
) -> dict[str, object]:
    transaction_path = install_root / "transaction.json"
    try:
        rollback_install(
            install_root=install_root,
            transaction_path=transaction_path,
            transaction=transaction,
            records=records,
            resources=resources,
        )
        _retire_partial_manifest(install_root, transaction)
        return transaction
    except Exception as error:
        record_failure(transaction_path, transaction, error)
        install_transactions.set_transaction_state(transaction_path, transaction, "quarantined")
        raise


def _rollback_v1_under_lock(
    install_root: Path,
    transaction: dict[str, object],
    resources: Sequence[ManagedResource],
) -> dict[str, object]:
    _require_v1_rollback_transaction(transaction)
    if transaction.get("state") == "reverted":
        return transaction
    if transaction.get("state") not in {"prepared", "mutating", "publishing", "reverting"}:
        raise InstallControlError("install_transaction_blocks_rollback")
    records = transaction_resources(transaction)
    require_resource_request(records, resources)
    return _continue_v1_rollback(install_root, transaction, records, resources)


def _v2_manifest_record(manifest: Mapping[str, object] | None) -> bool:
    return manifest is not None and manifest.get("schema") == "install-manifest/v2"


def _rollback_under_lock(
    install_root: Path, resources: Sequence[ManagedResource]
) -> dict[str, object]:
    manifest = optional_install_record(install_root / "manifest.json", "install-manifest/")
    transaction = read_install_record(install_root / "transaction.json", "install-transaction/")
    if _v2_manifest_record(manifest) or transaction.get("schema") == "install-transaction/v2":
        return _rollback_v2_under_lock(install_root, manifest, transaction, resources)
    return _rollback_v1_under_lock(install_root, transaction, resources)


def rollback_resources(
    *,
    state_root: Path,
    resources: Sequence[ManagedResource],
    lock_timeout: float = 10.0,
) -> dict[str, object]:
    validate_resources(resources)
    install_root = Path(state_root) / "run" / "install"
    if not install_root.is_dir() or install_root.is_symlink():
        raise InstallControlError("install_state_absent")
    with install_lock(install_root / "install.lock", lock_timeout):
        return _rollback_under_lock(install_root, resources)
