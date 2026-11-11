"""Applying one resource inside a second-format transaction."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path

from install_state import (
    MAX_TOTAL_PREIMAGE_BYTES,
    InstallControlError,
    ManagedResource,
    adopted_value,
    read_v2_snapshot,
    resource_snapshot,
    same_snapshot,
    v2_resource_snapshots,
)
from llm_wiki.install_records import ResourceDriftError, error_code, taken_as_is
from llm_wiki.install_transactions import (
    mark_resource_state,
    persist_resource_definition,
    require_recognized,
    resource_origin,
    write_preimage,
)


def v2_snapshot(install_root: Path, value: bytes | None) -> dict[str, object]:
    if value is None:
        return {"state": "absent"}
    return resource_snapshot(value, write_preimage(install_root, value))


def _found_value(resource: ManagedResource) -> bytes | None:
    """What the resource holds now; one taken as it is is read as it is on disk."""
    if taken_as_is(resource):
        return adopted_value(resource)
    return resource.read_owned()


def _first_origin(resource: ManagedResource, current: bytes | None) -> bytes | None:
    """What an uninstall puts back: an adopted or foreign file itself, else as before."""
    if resource.adopt_current:
        return current
    if current is not None and not resource.recognizes(current):
        return current
    return resource_origin(resource, current)


def v2_resource_record(install_root: Path, resource: ManagedResource) -> dict[str, object]:
    persist_resource_definition(install_root, resource)
    current = _found_value(resource)
    require_recognized(resource, current)
    origin = _first_origin(resource, current)
    baseline = v2_snapshot(install_root, origin)
    return {
        "desired": v2_snapshot(install_root, resource.desired),
        "id": resource.resource_id,
        "kind": resource.kind,
        "locator": resource.locator,
        "metadata": dict(resource.metadata),
        "origin": baseline,
        "rollback": dict(baseline),
        "state": "pending",
    }


def _v2_snapshot_size(snapshot: Mapping[str, object]) -> int:
    return int(snapshot.get("size", 0))


def _v2_record_size(record: Mapping[str, object]) -> int:
    snapshots = (record.get("desired"), record.get("origin"), record.get("rollback"))
    return sum(
        _v2_snapshot_size(snapshot) for snapshot in snapshots if isinstance(snapshot, Mapping)
    )


def v2_prepare_records(
    install_root: Path, resources: Sequence[ManagedResource]
) -> list[dict[str, object]]:
    records = [v2_resource_record(install_root, resource) for resource in resources]
    if sum(_v2_record_size(record) for record in records) > MAX_TOTAL_PREIMAGE_BYTES:
        raise InstallControlError("install_preimages_too_large")
    return records


def _snapshot_values(install_root: Path, snapshots: Sequence[Mapping[str, object]]) -> list[bytes]:
    values = [read_v2_snapshot(install_root, snapshot) for snapshot in snapshots]
    return [value for value in values if value is not None]


_PROJECTION_DRIFT_CODES = frozenset(
    {"install_scheduler_projection_conflict", "install_scheduler_projection_ambiguous"}
)


def _read_resource_projections(
    resource: ManagedResource, candidates: Sequence[bytes]
) -> bytes | None:
    if resource.read_projections is None:
        return resource.read_owned()
    try:
        return resource.read_projections(candidates)
    except InstallControlError as error:
        _raise_named_projection_drift(error, resource)
        raise


def _raise_named_projection_drift(error: InstallControlError, resource: ManagedResource) -> None:
    """A schedule that matches none of our renderings is named, with the way on."""
    if error_code(error) in _PROJECTION_DRIFT_CODES:
        raise ResourceDriftError(error_code(error), resource) from error


def write_resource_projection(
    resource: ManagedResource,
    expected: bytes | None,
    replacement: bytes | None,
    metadata: Mapping[str, object],
) -> None:
    if resource.write_projection is None:
        resource.write_owned(replacement)
        return
    resource.write_projection(expected, replacement, metadata)


def read_v2_resource(
    install_root: Path,
    resource: ManagedResource,
    snapshots: Sequence[Mapping[str, object]],
) -> bytes | None:
    return _read_resource_projections(resource, _snapshot_values(install_root, snapshots))


def _v2_mutate_resource(
    *,
    install_root: Path,
    transaction_path: Path,
    transaction: dict[str, object],
    record: dict[str, object],
    resource: ManagedResource,
) -> None:
    desired, _origin, _rollback = v2_resource_snapshots(record)
    desired_value = read_v2_snapshot(install_root, desired)
    current_value = read_v2_resource(install_root, resource, (desired, _rollback))
    mark_resource_state(transaction_path, transaction, record, "mutating")
    metadata = record.get("metadata")
    if not isinstance(metadata, Mapping):
        raise InstallControlError("install_transaction_invalid")
    write_resource_projection(resource, current_value, desired_value, metadata)
    if not same_snapshot(read_v2_resource(install_root, resource, (desired,)), desired):
        raise InstallControlError("install_resource_verification_failed")
    mark_resource_state(transaction_path, transaction, record, "verified")


def _v2_apply_resource(
    *,
    install_root: Path,
    transaction_path: Path,
    transaction: dict[str, object],
    record: dict[str, object],
    resource: ManagedResource,
) -> None:
    desired, _origin, rollback = v2_resource_snapshots(record)
    current = read_v2_resource(install_root, resource, (desired, rollback))
    if same_snapshot(current, desired):
        mark_resource_state(transaction_path, transaction, record, "verified")
        return
    if not same_snapshot(current, rollback):
        raise ResourceDriftError("install_resource_drift", resource)
    _v2_mutate_resource(
        install_root=install_root,
        transaction_path=transaction_path,
        transaction=transaction,
        record=record,
        resource=resource,
    )


def v2_mutate_resources(
    *,
    install_root: Path,
    transaction_path: Path,
    transaction: dict[str, object],
    records: Sequence[dict[str, object]],
    resources: Sequence[ManagedResource],
) -> None:
    for record, resource in zip(records, resources, strict=True):
        _v2_apply_resource(
            install_root=install_root,
            transaction_path=transaction_path,
            transaction=transaction,
            record=record,
            resource=resource,
        )
