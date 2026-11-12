"""Second-format install and update transactions."""

from __future__ import annotations

import re
import secrets
from collections.abc import Mapping, Sequence
from pathlib import Path

from install_state import (
    InstallControlError,
    ManagedResource,
    active_install_health_for_schema,
    adopted_value,
    atomic_write,
    optional_install_record,
    preimage_references,
    read_origin,
    read_v2_snapshot,
    record_desired,
    replaced_whole,
    same_snapshot,
    transaction_resources,
    v2_manifest_from_transaction,
    v2_resource_snapshots,
    validate_v2_manifest_transaction,
    validate_v2_record_preimages,
)
from install_state import manifest_digest as _manifest_digest
from install_state import sha256 as _sha256
from llm_wiki import install_transactions, install_v2_mutation
from llm_wiki.formats.canonical import canonical_json_bytes
from llm_wiki.install_records import (
    Identity,
    ResourceDriftError,
    error_code,
    install_lock,
    prepare_install_root,
    record_identity,
    records_by_identity,
    resource_identity,
    resources_by_identity,
    taken_as_is,
    utc_now,
    write_record,
)
from llm_wiki.install_transactions import (
    ambiguous,
    failure_code,
    install_under_lock,
    mark_resource_state,
    record_failure,
    request_digest,
    validate_backend,
    validate_resources,
    validate_unique_names,
)
from llm_wiki.install_v2_mutation import (
    read_v2_resource,
    v2_prepare_records,
    v2_resource_record,
    v2_snapshot,
    write_resource_projection,
)
from reliable_memory import fsync_directory


class _UnreadableProjection:
    """The current projection could not be selected, so drift cannot be judged."""


# What a revert starts from when the resource was left in a state no single
# projection describes. It is not a value the resource can ever hold.
_UNREADABLE_PROJECTION = _UnreadableProjection()


def _revert_start_projection(
    install_root: Path,
    resource: ManagedResource,
    snapshots: Sequence[Mapping[str, object]],
    rollback: Mapping[str, object],
) -> bytes | None | _UnreadableProjection:
    """Where the revert starts; a resource left half applied is reported, not refused.

    Only a revert back to absence tolerates this. Refusing to read a mixed
    projection is what leaves an interrupted install with no way forward and no
    way back, because the rollback dies on the same read that failed the install.
    """
    try:
        return read_v2_resource(install_root, resource, snapshots)
    except InstallControlError as error:
        if rollback.get("state") != "absent":
            raise
        if error_code(error) != "install_scheduler_projection_conflict":
            raise
        return _UNREADABLE_PROJECTION


def _revert_already_done(
    current: bytes | None | _UnreadableProjection, rollback: Mapping[str, object]
) -> bool:
    """Whether the resource already sits where the revert wants it."""
    if isinstance(current, _UnreadableProjection):
        return False
    return same_snapshot(current, rollback)


def _require_revertible(
    current: bytes | None | _UnreadableProjection, desired: Mapping[str, object]
) -> None:
    """A revert only touches a resource that is still where the install left it."""
    if isinstance(current, _UnreadableProjection):
        return
    if not same_snapshot(current, desired):
        raise InstallControlError("install_rollback_drift")


def _v2_revert_resource(
    *,
    install_root: Path,
    transaction_path: Path,
    transaction: dict[str, object],
    record: dict[str, object],
    resource: ManagedResource,
) -> None:
    if record.get("state") in {"pending", "reverted"}:
        # Never written by this transaction, or already reverted by an earlier attempt:
        # there is nothing of ours to undo, and the file may hold edits made outside the
        # installer since (docs/research/2026-09-28-a-rollback-undoes-only-what-it-did.md).
        mark_resource_state(transaction_path, transaction, record, "reverted")
        return
    desired, _origin, rollback = v2_resource_snapshots(record)
    current = _revert_start_projection(
        install_root, resource, (desired, rollback), rollback
    )
    if _revert_already_done(current, rollback):
        mark_resource_state(transaction_path, transaction, record, "reverted")
        return
    _require_revertible(current, desired)
    mark_resource_state(transaction_path, transaction, record, "reverting")
    rollback_value = read_v2_snapshot(install_root, rollback)
    metadata = _resource_record_metadata(record)
    write_resource_projection(
        resource, read_v2_snapshot(install_root, desired), rollback_value, metadata
    )
    _require_rolled_back(install_root, resource, rollback)
    mark_resource_state(transaction_path, transaction, record, "reverted")


def _resource_record_metadata(record: Mapping[str, object]) -> Mapping[str, object]:
    metadata = record.get("metadata")
    if not isinstance(metadata, Mapping):
        raise InstallControlError("install_transaction_invalid")
    return metadata


def _require_rolled_back(
    install_root: Path, resource: ManagedResource, rollback: Mapping[str, object]
) -> None:
    if not same_snapshot(read_v2_resource(install_root, resource, (rollback,)), rollback):
        raise InstallControlError("install_rollback_verification_failed")


def v2_rollback_mutations(
    *,
    install_root: Path,
    transaction_path: Path,
    transaction: dict[str, object],
    records: Sequence[dict[str, object]],
    resources: Sequence[ManagedResource],
) -> None:
    install_transactions.set_transaction_state(transaction_path, transaction, "reverting")
    pairs = zip(reversed(records), reversed(resources), strict=True)
    for record, resource in pairs:
        _v2_revert_resource(
            install_root=install_root,
            transaction_path=transaction_path,
            transaction=transaction,
            record=record,
            resource=resource,
        )
    install_transactions.set_transaction_state(transaction_path, transaction, "reverted")


def _replace_manifest_cas(path: Path, replacement: bytes, expected_sha256: str | None) -> None:
    current = _manifest_bytes(path)
    _require_manifest_cas(current, expected_sha256)
    atomic_write(path, replacement)


def _manifest_bytes(path: Path) -> bytes | None:
    if not path.exists():
        return None
    return path.read_bytes()


def _require_manifest_cas(current: bytes | None, expected_sha256: str | None) -> None:
    if expected_sha256 is None:
        _require_absent_manifest(current)
        return
    if current is None or _sha256(current) != expected_sha256:
        raise InstallControlError("install_manifest_changed")


def _require_absent_manifest(current: bytes | None) -> None:
    if current is not None:
        raise InstallControlError("install_manifest_conflict")


def _publish_v2_manifest(
    install_root: Path,
    transaction_path: Path,
    transaction: dict[str, object],
) -> dict[str, object]:
    manifest = v2_manifest_from_transaction(transaction)
    encoded = canonical_json_bytes(manifest)
    transaction["target_manifest_sha256"] = _sha256(encoded)
    install_transactions.set_transaction_state(transaction_path, transaction, "publishing")
    _replace_manifest_cas(
        install_root / "manifest.json",
        encoded,
        transaction.get("base_manifest_sha256"),
    )
    install_transactions.set_transaction_state(transaction_path, transaction, "committed")
    _prune_v2_preimages(install_root, (manifest, transaction))
    return manifest


def _owned_preimage_file(path: Path) -> bool:
    return (
        not path.is_symlink()
        and path.is_file()
        and re.fullmatch(r"[0-9a-f]{64}\.bin", path.name) is not None
    )


def _prune_v2_preimages(install_root: Path, records: Sequence[Mapping[str, object]]) -> None:
    reachable: set[str] = set()
    for record in records:
        reachable.update(preimage_references(record))
    preimages = install_root / "preimages"
    removed = False
    for path in preimages.iterdir():
        removed = _prune_preimage(path, reachable) or removed
    if removed:
        fsync_directory(preimages)


def _prune_preimage(path: Path, reachable: set[str]) -> bool:
    relative = f"preimages/{path.name}"
    if not _owned_preimage_file(path) or relative in reachable:
        return False
    path.unlink()
    return True


def v2_failed_operation(
    *,
    install_root: Path,
    transaction_path: Path,
    transaction: dict[str, object],
    records: Sequence[dict[str, object]],
    resources: Sequence[ManagedResource],
    error: Exception,
) -> None:
    record_failure(transaction_path, transaction, error)
    try:
        v2_rollback_mutations(
            install_root=install_root,
            transaction_path=transaction_path,
            transaction=transaction,
            records=records,
            resources=resources,
        )
    except Exception as rollback_error:
        # The forward failure is why the install stopped; the rollback's is why it is
        # quarantined. Keeping only the second hid the cause from the operator.
        transaction["error"] = {
            "code": failure_code(rollback_error),
            "cause": failure_code(error),
        }
        install_transactions.set_transaction_state(transaction_path, transaction, "quarantined")
        raise InstallControlError("install_rollback_quarantined") from error


def continue_v2_operation(
    *,
    install_root: Path,
    transaction_path: Path,
    transaction: dict[str, object],
    records: Sequence[dict[str, object]],
    resources: Sequence[ManagedResource],
) -> dict[str, object]:
    install_transactions.set_transaction_state(transaction_path, transaction, "mutating")
    try:
        install_v2_mutation.v2_mutate_resources(
            install_root=install_root,
            transaction_path=transaction_path,
            transaction=transaction,
            records=records,
            resources=resources,
        )
        return _publish_v2_manifest(install_root, transaction_path, transaction)
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


def _new_v2_install_transaction(
    *,
    state_root: Path,
    vault_root: Path,
    release: Mapping[str, object],
    backend: str,
    records: list[dict[str, object]],
    request_sha256: str,
) -> dict[str, object]:
    now = utc_now()
    transaction_id = secrets.token_hex(16)
    return {
        "base_manifest_sha256": None,
        "error": None,
        "generation": 1,
        "id": transaction_id,
        "installation_id": transaction_id,
        "operation": "install",
        "release": dict(release),
        "request_sha256": request_sha256,
        "resources": records,
        "rollback_point": None,
        "scheduler_backend": backend,
        "schema": "install-transaction/v2",
        "started_at": now,
        "state": "prepared",
        "state_root": str(Path(state_root).resolve()),
        "target_manifest_sha256": None,
        "updated_at": now,
        "vault_root": str(Path(vault_root).resolve()),
    }


def _start_v2_install(
    *,
    install_root: Path,
    state_root: Path,
    vault_root: Path,
    release: Mapping[str, object],
    scheduler_backend: str,
    resources: Sequence[ManagedResource],
    request_sha256: str,
    released: Sequence[ManagedResource] = (),
) -> dict[str, object]:
    records = [
        *v2_prepare_records(install_root, resources),
        *_released_records(install_root, released),
    ]
    transaction = _new_v2_install_transaction(
        state_root=state_root,
        vault_root=vault_root,
        release=release,
        backend=scheduler_backend,
        records=records,
        request_sha256=request_sha256,
    )
    validate_v2_record_preimages(install_root, transaction)
    transaction_path = install_root / "transaction.json"
    write_record(transaction_path, transaction)
    return continue_v2_operation(
        install_root=install_root,
        transaction_path=transaction_path,
        transaction=transaction,
        records=records,
        resources=[*resources, *released],
    )


def _released_records(
    install_root: Path, released: Sequence[ManagedResource]
) -> list[dict[str, object]]:
    return [_released_record(install_root, resource) for resource in released]


def _released_record(install_root: Path, resource: ManagedResource) -> dict[str, object]:
    """A file this transaction creates and hands over at commit; one already there is not ours."""
    if resource.read_owned() is not None:
        raise ambiguous(resource)
    metadata = {**resource.metadata, "_released_after_commit": True, "_retire_after_commit": True}
    return {
        "desired": v2_snapshot(install_root, resource.desired),
        "id": resource.resource_id,
        "kind": resource.kind,
        "locator": resource.locator,
        "metadata": metadata,
        "origin": {"state": "absent"},
        "rollback": {"state": "absent"},
        "state": "pending",
    }


def _normalized_snapshot(
    install_root: Path,
    snapshot: Mapping[str, object],
    actual: bytes | None,
) -> dict[str, object]:
    if not same_snapshot(actual, snapshot):
        raise InstallControlError("install_resource_drift")
    return v2_snapshot(install_root, actual)


def _checkpoint_desired_snapshot(
    install_root: Path,
    snapshot: Mapping[str, object],
    resource: ManagedResource,
) -> dict[str, object]:
    if snapshot.get("state") == "absent":
        return {"state": "absent"}
    if isinstance(snapshot.get("preimage"), str):
        value = read_origin(install_root, snapshot)
        return _normalized_snapshot(install_root, snapshot, value)
    return _checkpoint_owned_snapshot(install_root, snapshot, resource)


def _owned_snapshot(
    install_root: Path, snapshot: Mapping[str, object], resource: ManagedResource
) -> dict[str, object]:
    """The resource as the checkpoint recorded it, or a drift that names the resource."""
    actual = resource.read_owned()
    if not same_snapshot(actual, snapshot):
        raise ResourceDriftError("install_resource_drift", resource)
    return v2_snapshot(install_root, actual)


def _checkpoint_owned_snapshot(
    install_root: Path,
    snapshot: Mapping[str, object],
    resource: ManagedResource,
) -> dict[str, object]:
    if resource.recover_legacy_projection is None:
        return _owned_snapshot(install_root, snapshot, resource)
    actual, persisted = resource.recover_legacy_projection(snapshot)
    if not same_snapshot(actual, snapshot):
        raise ResourceDriftError("install_resource_drift", resource)
    return v2_snapshot(install_root, persisted)


def _active_resource(
    resources: Mapping[Identity, ManagedResource], record: Mapping[str, object]
) -> ManagedResource:
    resource = resources.get(record_identity(record))
    if resource is None:
        raise InstallControlError("install_resource_request_mismatch")
    return resource


def _resource_checkpoint_desired(
    install_root: Path,
    snapshot: Mapping[str, object],
    resource: ManagedResource,
) -> dict[str, object]:
    if resource.adopt_current:
        return v2_snapshot(install_root, adopted_value(resource))
    return _checkpoint_desired_snapshot(install_root, snapshot, resource)


def _checkpoint_resource(
    install_root: Path,
    record: Mapping[str, object],
    resource: ManagedResource,
) -> dict[str, object]:
    desired_record = record_desired(record)
    desired = _resource_checkpoint_desired(install_root, desired_record, resource)
    origin_record = record.get("origin")
    if not isinstance(origin_record, Mapping):
        raise InstallControlError("install_state_schema_invalid")
    origin = read_origin(install_root, origin_record)
    if resource.adopt_current:
        origin = read_v2_snapshot(install_root, desired)
    return {
        "desired": desired,
        "id": record["id"],
        "kind": record["kind"],
        "locator": record["locator"],
        "metadata": record["metadata"],
        "origin": v2_snapshot(install_root, origin),
        "state": "verified",
    }


def require_active_resources(
    install_root: Path,
    manifest: Mapping[str, object],
    resources: Sequence[ManagedResource],
) -> None:
    by_id = resources_by_identity(resources)
    for record in transaction_resources(manifest):
        resource = _active_resource(by_id, record)
        desired = record_desired(record)
        current = read_v2_resource(install_root, resource, (desired,))
        if not same_snapshot(current, desired):
            raise ResourceDriftError("install_resource_drift", resource)


def checkpoint_resources(
    install_root: Path,
    manifest: Mapping[str, object],
    resources: Sequence[ManagedResource],
) -> list[dict[str, object]]:
    by_id = resources_by_identity(resources)
    return [
        _checkpoint_resource(install_root, record, _active_resource(by_id, record))
        for record in transaction_resources(manifest)
    ]


def _rollback_point(
    manifest: Mapping[str, object], records: list[dict[str, object]]
) -> dict[str, object]:
    return {
        "generation": manifest["generation"],
        "release": manifest["release"],
        "request_sha256": manifest["request_sha256"],
        "resources": records,
        "scheduler_backend": manifest["scheduler_backend"],
    }


def _new_v2_resource_record(
    install_root: Path,
    resource: ManagedResource,
    previous: Mapping[str, object] | None,
) -> dict[str, object]:
    if previous is None:
        return v2_resource_record(install_root, resource)
    return {
        "desired": v2_snapshot(install_root, resource.desired),
        "id": resource.resource_id,
        "kind": resource.kind,
        "locator": resource.locator,
        "metadata": previous["metadata"],
        "origin": previous["origin"],
        "rollback": _update_rollback(install_root, resource, previous),
        "state": "pending",
    }


def _update_rollback(
    install_root: Path, resource: ManagedResource, previous: Mapping[str, object]
) -> Mapping[str, object]:
    """What an update reverts to: what was installed, or what a taken-over file holds now."""
    if taken_as_is(resource):
        return v2_snapshot(install_root, adopted_value(resource))
    return previous["desired"]


def _updated_resource_records(
    install_root: Path,
    resources: Sequence[ManagedResource],
    checkpoint: Sequence[Mapping[str, object]],
) -> list[dict[str, object]]:
    previous = records_by_identity(checkpoint)
    return [
        _new_v2_resource_record(install_root, resource, previous.get(resource_identity(resource)))
        for resource in resources
    ]


def _new_v2_update_transaction(
    *,
    manifest: Mapping[str, object],
    release: Mapping[str, object],
    backend: str,
    records: list[dict[str, object]],
    checkpoint: dict[str, object],
    request_sha256: str,
    base_manifest_sha256: str,
) -> dict[str, object]:
    now = utc_now()
    return {
        "base_manifest_sha256": base_manifest_sha256,
        "error": None,
        "generation": int(manifest["generation"]) + 1,
        "id": secrets.token_hex(16),
        "installation_id": manifest["installation_id"],
        "operation": "update",
        "release": dict(release),
        "request_sha256": request_sha256,
        "resources": records,
        "rollback_point": checkpoint,
        "scheduler_backend": backend,
        "schema": "install-transaction/v2",
        "started_at": now,
        "state": "prepared",
        "state_root": manifest["state_root"],
        "target_manifest_sha256": None,
        "updated_at": now,
        "vault_root": manifest["vault_root"],
    }


def validate_active_manifest(
    install_root: Path,
    manifest: Mapping[str, object],
    transaction: Mapping[str, object] | None,
) -> None:
    health = active_install_health_for_schema(install_root, manifest, transaction)
    if health.get("status") != "active":
        raise InstallControlError("install_transaction_blocks_new_work")


def _start_v2_update(
    *,
    install_root: Path,
    manifest: dict[str, object],
    release: Mapping[str, object],
    scheduler_backend: str,
    resources: Sequence[ManagedResource],
    request_sha256: str,
    retiring: Sequence[ManagedResource] = (),
    released: Sequence[ManagedResource] = (),
) -> dict[str, object]:
    checkpoint_records = checkpoint_resources(install_root, manifest, [*resources, *retiring])
    checkpoint = _rollback_point(manifest, checkpoint_records)
    # The new set first, then what this update hands over, then what it takes back.
    records = [
        *_updated_resource_records(install_root, resources, checkpoint_records),
        *_released_records(install_root, released),
        *_retiring_records(install_root, retiring, checkpoint_records),
    ]
    everything = [*resources, *released, *retiring]
    base_digest = _sha256(canonical_json_bytes(manifest))
    transaction = _new_v2_update_transaction(
        manifest=manifest,
        release=release,
        backend=scheduler_backend,
        records=records,
        checkpoint=checkpoint,
        request_sha256=request_sha256,
        base_manifest_sha256=base_digest,
    )
    validate_v2_record_preimages(install_root, transaction)
    transaction_path = install_root / "transaction.json"
    write_record(transaction_path, transaction)
    return continue_v2_operation(
        install_root=install_root,
        transaction_path=transaction_path,
        transaction=transaction,
        records=records,
        resources=everything,
    )


def _retiring_records(
    install_root: Path,
    retiring: Sequence[ManagedResource],
    checkpoint: Sequence[Mapping[str, object]],
) -> list[dict[str, object]]:
    """What the new set no longer owns, taken back after the new set is written.

    Each goes back to what it was before the installer, like an uninstall, and leaves
    the manifest when the update commits; a failure reverts it with the rest
    (docs/research/2026-09-28-an-update-replaces-what-it-owns.md).
    """
    previous = records_by_identity(checkpoint)
    return [
        _retiring_record(install_root, resource, previous[resource_identity(resource)])
        for resource in retiring
    ]


def _retiring_record(
    install_root: Path, resource: ManagedResource, previous: Mapping[str, object]
) -> dict[str, object]:
    metadata = dict(previous["metadata"])
    metadata["_retire_after_commit"] = True
    return {
        "desired": previous["origin"],
        "id": resource.resource_id,
        "kind": resource.kind,
        "locator": resource.locator,
        "metadata": metadata,
        "origin": previous["origin"],
        "rollback": _update_rollback(install_root, resource, previous),
        "state": "pending",
    }


def _active_v2_request(
    *,
    install_root: Path,
    manifest: dict[str, object],
    transaction: dict[str, object] | None,
    release: Mapping[str, object],
    scheduler_backend: str,
    resources: Sequence[ManagedResource],
    request_sha256: str,
    retiring: Sequence[ManagedResource] = (),
    released: Sequence[ManagedResource] = (),
) -> dict[str, object]:
    validate_active_manifest(install_root, manifest, transaction)
    if not released and _same_v2_request(manifest, request_sha256, resources):
        require_active_resources(install_root, manifest, resources)
        checkpoint_resources(install_root, manifest, resources)
        return manifest
    return _start_v2_update(
        install_root=install_root,
        manifest=manifest,
        release=release,
        scheduler_backend=scheduler_backend,
        resources=resources,
        request_sha256=request_sha256,
        retiring=retiring,
        released=released,
    )


def _same_v2_request(
    manifest: Mapping[str, object], request_sha256: str, resources: Sequence[ManagedResource]
) -> bool:
    """Whether the rerun asks for what is installed and finds it in place.

    A wholly owned file changed since is replaced by a new generation, the same as an
    update, instead of being refused as drift.
    """
    if manifest.get("schema") != "install-manifest/v2":
        return False
    if manifest.get("request_sha256") != request_sha256:
        return False
    return not _whole_files_changed(manifest, resources)


def _whole_files_changed(
    manifest: Mapping[str, object], resources: Sequence[ManagedResource]
) -> bool:
    by_id = resources_by_identity(resources)
    pairs = ((record, _active_resource(by_id, record)) for record in transaction_resources(manifest))
    return any(_whole_file_changed(record, resource) for record, resource in pairs)


def _whole_file_changed(record: Mapping[str, object], resource: ManagedResource) -> bool:
    if not replaced_whole(resource):
        return False
    return not same_snapshot(adopted_value(resource), record_desired(record))


def v2_nonterminal(transaction: Mapping[str, object] | None) -> bool:
    if transaction is None or transaction.get("schema") != "install-transaction/v2":
        return False
    return transaction.get("state") in {"prepared", "mutating", "publishing"}


def ordered_transaction_resources(
    records: Sequence[Mapping[str, object]],
    resources: Sequence[ManagedResource],
) -> list[ManagedResource]:
    by_id = resources_by_identity(resources)
    return [_active_resource(by_id, record) for record in records]


def published_v2_target(
    manifest: Mapping[str, object] | None, transaction: Mapping[str, object]
) -> bool:
    target = transaction.get("target_manifest_sha256")
    if not isinstance(target, str):
        return False
    return _manifest_digest(manifest) == target


def require_v2_base_manifest(
    manifest: Mapping[str, object] | None, transaction: Mapping[str, object]
) -> None:
    if _manifest_digest(manifest) != transaction.get("base_manifest_sha256"):
        raise InstallControlError("install_manifest_transaction_mismatch")


def complete_v2_publication(
    transaction_path: Path,
    transaction: dict[str, object],
    manifest: dict[str, object] | None,
) -> dict[str, object]:
    if manifest is None:
        raise InstallControlError("install_manifest_transaction_mismatch")
    validate_v2_manifest_transaction(transaction_path.parent, manifest, transaction)
    install_transactions.set_transaction_state(transaction_path, transaction, "committed")
    return manifest


def _resume_v2_operation(
    *,
    install_root: Path,
    manifest: dict[str, object] | None,
    transaction: dict[str, object] | None,
    request_sha256: str,
    resources: Sequence[ManagedResource],
) -> dict[str, object] | None:
    if not v2_nonterminal(transaction):
        return None
    _require_matching_request(transaction, request_sha256)
    records = transaction_resources(transaction)
    ordered = ordered_transaction_resources(records, resources)
    transaction_path = install_root / "transaction.json"
    if published_v2_target(manifest, transaction):
        return complete_v2_publication(transaction_path, transaction, manifest)
    require_v2_base_manifest(manifest, transaction)
    return continue_v2_operation(
        install_root=install_root,
        transaction_path=transaction_path,
        transaction=transaction,
        records=records,
        resources=ordered,
    )


def _require_matching_request(
    transaction: Mapping[str, object] | None, request_sha256: str
) -> None:
    if transaction is None or transaction.get("request_sha256") != request_sha256:
        raise InstallControlError("install_transaction_blocks_new_work")


def _install_v2_under_lock(
    *,
    install_root: Path,
    state_root: Path,
    vault_root: Path,
    release: Mapping[str, object],
    scheduler_backend: str,
    resources: Sequence[ManagedResource],
    retiring: Sequence[ManagedResource] = (),
    released: Sequence[ManagedResource] = (),
) -> dict[str, object]:
    manifest = optional_install_record(install_root / "manifest.json", "install-manifest/")
    transaction = optional_install_record(
        install_root / "transaction.json", "install-transaction/"
    )
    request_sha256 = request_digest(vault_root, release, scheduler_backend, resources)
    resumed = _resume_v2_operation(
        install_root=install_root,
        manifest=manifest,
        transaction=transaction,
        request_sha256=request_sha256,
        resources=[*resources, *retiring, *released],
    )
    if resumed is not None:
        return resumed
    if manifest is not None:
        return _active_v2_request(
            install_root=install_root,
            manifest=manifest,
            transaction=transaction,
            release=release,
            scheduler_backend=scheduler_backend,
            resources=resources,
            request_sha256=request_sha256,
            retiring=retiring,
            released=released,
        )
    _require_settled_transaction(transaction)
    return _start_v2_install(
        install_root=install_root,
        state_root=state_root,
        vault_root=vault_root,
        release=release,
        scheduler_backend=scheduler_backend,
        resources=resources,
        request_sha256=request_sha256,
        released=released,
    )


def transaction_settled(transaction: Mapping[str, object] | None) -> bool:
    if transaction is None:
        return True
    return transaction.get("state") in {"committed", "reverted"}


def _require_settled_transaction(transaction: Mapping[str, object] | None) -> None:
    if not transaction_settled(transaction):
        raise InstallControlError("install_transaction_blocks_new_work")


def _install_v2(
    *,
    state_root: Path,
    vault_root: Path,
    release: Mapping[str, object],
    scheduler_backend: str,
    resources: Sequence[ManagedResource],
    lock_timeout: float,
    retiring: Sequence[ManagedResource] = (),
    released: Sequence[ManagedResource] = (),
) -> dict[str, object]:
    install_root = prepare_install_root(state_root)
    with install_lock(install_root / "install.lock", lock_timeout):
        return _install_v2_under_lock(
            install_root=install_root,
            state_root=state_root,
            vault_root=vault_root,
            release=release,
            scheduler_backend=scheduler_backend,
            resources=resources,
            retiring=retiring,
            released=released,
        )


def _validate_v2_recovery_support(resources: Sequence[ManagedResource]) -> None:
    if any(not resource.supports_v2_recovery for resource in resources):
        raise InstallControlError("install_v2_resource_recovery_unsupported")


def _validate_request(
    resources: Sequence[ManagedResource],
    scheduler_backend: str,
    retiring: Sequence[ManagedResource],
    control_version: int,
) -> None:
    validate_resources(resources)
    validate_unique_names(resources)
    validate_backend(scheduler_backend)
    if retiring and control_version != 2:
        raise InstallControlError("install_control_version_invalid")


def install_resources(
    *,
    state_root: Path,
    vault_root: Path,
    release: Mapping[str, object],
    scheduler_backend: str,
    resources: Sequence[ManagedResource],
    lock_timeout: float = 10.0,
    control_version: int = 1,
    retiring: Sequence[ManagedResource] = (),
    released: Sequence[ManagedResource] = (),
) -> dict[str, object]:
    """Install or update the resources; `retiring` are recorded ones the request drops.

    An update writes the requested set first and takes the retiring ones back after it,
    in one transaction, so a failure reverts both. `released` are files the transaction
    creates and hands over when it commits (a systemd drop-in): its rollback removes
    them, nothing later touches them. Only control version 2 records either.
    """
    _validate_request(resources, scheduler_backend, [*retiring, *released], control_version)
    if control_version == 2:
        _validate_v2_recovery_support(resources)
        return _install_v2(
            state_root=state_root,
            vault_root=vault_root,
            release=release,
            scheduler_backend=scheduler_backend,
            resources=resources,
            lock_timeout=lock_timeout,
            retiring=retiring,
            released=released,
        )
    if control_version != 1:
        raise InstallControlError("install_control_version_invalid")
    install_root = prepare_install_root(state_root)
    with install_lock(install_root / "install.lock", lock_timeout):
        return install_under_lock(
            install_root=install_root,
            state_root=state_root,
            vault_root=vault_root,
            release=release,
            scheduler_backend=scheduler_backend,
            resources=resources,
        )
