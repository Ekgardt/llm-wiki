"""The first install transaction format and its recovery."""

from __future__ import annotations

import os
import secrets
from collections.abc import Mapping, Sequence
from pathlib import Path, PurePosixPath

from install_state import (
    MANIFEST_SCHEMA,
    MAX_PREIMAGE_BYTES,
    MAX_TOTAL_PREIMAGE_BYTES,
    TRANSACTION_SCHEMA,
    InstallControlError,
    ManagedResource,
    atomic_write,
    read_origin,
    resource_snapshot,
    resource_snapshots,
    same_snapshot,
    transaction_resources,
)
from install_state import sha256 as _sha256
from llm_wiki.formats.canonical import canonical_json_bytes
from llm_wiki.install_records import (
    BACKENDS,
    MAX_RESOURCES,
    ExplainedInstallError,
    ResourceDriftError,
    drift_next_step,
    error_code,
    optional_record,
    resource_identity,
    taken_as_is,
    utc_now,
    write_record,
)
from reliable_memory import fsync_directory


def validate_resources(resources: Sequence[ManagedResource]) -> None:
    _validate_resource_count(resources)
    _validate_resource_ids(resources)
    _validate_resource_sizes(resources)


def _validate_resource_count(resources: Sequence[ManagedResource]) -> None:
    if not resources or len(resources) > MAX_RESOURCES:
        raise InstallControlError("install_resource_count_invalid")


def _validate_resource_ids(resources: Sequence[ManagedResource]) -> None:
    """One resource per identity; a rollback or uninstall may name a moved one twice."""
    identities = [resource_identity(resource) for resource in resources]
    if len(set(identities)) != len(identities):
        raise InstallControlError("install_resource_id_duplicate")


def validate_unique_names(resources: Sequence[ManagedResource]) -> None:
    """A request names each resource once: the manifest records one place per name."""
    identifiers = [resource.resource_id for resource in resources]
    if len(set(identifiers)) != len(identifiers):
        raise InstallControlError("install_resource_id_duplicate")


def _validate_resource_sizes(resources: Sequence[ManagedResource]) -> None:
    if any(len(resource.desired) > MAX_PREIMAGE_BYTES for resource in resources):
        raise InstallControlError("install_resource_too_large")


def write_preimage(install_root: Path, value: bytes) -> str:
    """Binary: a preimage addressed by its own digest is stored byte for byte."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    digest = _sha256(value)
    relative = f"preimages/{digest}.bin"
    target = install_root / relative
    if target.exists():
        if target.read_bytes() != value:
            raise InstallControlError("install_preimage_conflict")
        return relative
    descriptor = os.open(target, flags, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        target.unlink(missing_ok=True)
        raise
    fsync_directory(target.parent)
    return relative


def _definition_target(install_root: Path, relative: str) -> Path:
    logical = PurePosixPath(relative)
    if logical.is_absolute() or ".." in logical.parts:
        raise InstallControlError("install_definition_path_invalid")
    if not logical.parts or logical.parts[0] != "scheduler":
        raise InstallControlError("install_definition_path_invalid")
    return install_root.joinpath(*logical.parts)


def persist_resource_definition(install_root: Path, resource: ManagedResource) -> None:
    for relative, value in resource.definitions.items():
        if len(value) > MAX_PREIMAGE_BYTES:
            raise InstallControlError("install_definition_too_large")
        target = _definition_target(install_root, relative)
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(target.parent, 0o700)
        if not target.exists() or target.read_bytes() != value:
            atomic_write(target, value)


def ambiguous(resource: ManagedResource) -> ExplainedInstallError:
    """A file the installer cannot tell as its own, named with the way on."""
    return ExplainedInstallError(
        "install_resource_ownership_ambiguous",
        f"{resource.resource_id} ({resource.locator}) holds content the installer cannot "
        f"tell as its own; {drift_next_step(resource)}",
    )


def require_recognized(resource: ManagedResource, current: bytes | None) -> None:
    """A file we cannot tell ours is refused, unless it is taken as it is."""
    if current is None or taken_as_is(resource):
        return
    if not resource.recognizes(current):
        raise ambiguous(resource)


def resource_origin(resource: ManagedResource, current: bytes | None) -> bytes | None:
    if current is not None and resource.adopt_as_absent:
        return None
    return current


def _resource_record(install_root: Path, resource: ManagedResource) -> dict[str, object]:
    persist_resource_definition(install_root, resource)
    current = resource.read_owned()
    if current is not None and not resource.recognizes(current):
        raise ambiguous(resource)
    origin = resource_origin(resource, current)
    preimage = write_preimage(install_root, origin) if origin is not None else None
    return {
        "id": resource.resource_id,
        "installed": resource_snapshot(resource.desired),
        "kind": resource.kind,
        "locator": resource.locator,
        "metadata": dict(resource.metadata),
        "origin": resource_snapshot(origin, preimage),
        "state": "pending",
    }


def request_digest(
    vault_root: Path,
    release: Mapping[str, object],
    backend: str,
    resources: Sequence[ManagedResource],
) -> str:
    request = {
        "release": dict(release),
        "resources": [_resource_request(resource) for resource in resources],
        "scheduler_backend": backend,
        "vault_root": str(Path(vault_root).resolve()),
    }
    return _sha256(canonical_json_bytes(request))


def _resource_request(resource: ManagedResource) -> dict[str, object]:
    return {
        "id": resource.resource_id,
        "installed": resource_snapshot(resource.desired),
        "kind": resource.kind,
        "locator": resource.locator,
        "metadata": dict(resource.metadata),
    }


def _new_transaction(
    *,
    state_root: Path,
    vault_root: Path,
    release: Mapping[str, object],
    backend: str,
    resource_records: list[dict[str, object]],
    request_sha256: str,
) -> dict[str, object]:
    now = utc_now()
    return {
        "error": None,
        "id": secrets.token_hex(16),
        "operation": "install",
        "release": dict(release),
        "request_sha256": request_sha256,
        "resources": resource_records,
        "scheduler_backend": backend,
        "schema": "install-transaction/v1",
        "started_at": now,
        "state": "prepared",
        "state_root": str(Path(state_root).resolve()),
        "target_manifest_sha256": None,
        "updated_at": now,
        "vault_root": str(Path(vault_root).resolve()),
    }


def set_transaction_state(
    transaction_path: Path, transaction: dict[str, object], state: str
) -> None:
    transaction["state"] = state
    transaction["updated_at"] = utc_now()
    write_record(transaction_path, transaction)


def mark_resource_state(
    transaction_path: Path,
    transaction: dict[str, object],
    record: dict[str, object],
    state: str,
) -> None:
    record["state"] = state
    transaction["updated_at"] = utc_now()
    write_record(transaction_path, transaction)


def _mutate_resource(
    transaction_path: Path,
    transaction: dict[str, object],
    record: dict[str, object],
    resource: ManagedResource,
) -> None:
    mark_resource_state(transaction_path, transaction, record, "mutating")
    resource.write_owned(resource.desired)
    if resource.read_owned() != resource.desired:
        raise InstallControlError("install_resource_verification_failed")
    mark_resource_state(transaction_path, transaction, record, "verified")


def _apply_resource(
    transaction_path: Path,
    transaction: dict[str, object],
    record: dict[str, object],
    resource: ManagedResource,
) -> None:
    current = resource.read_owned()
    installed, origin = resource_snapshots(record)
    if same_snapshot(current, installed):
        mark_resource_state(transaction_path, transaction, record, "verified")
        return
    if not same_snapshot(current, origin):
        raise ResourceDriftError("install_resource_drift", resource)
    _mutate_resource(transaction_path, transaction, record, resource)


def _manifest_from_transaction(transaction: Mapping[str, object]) -> dict[str, object]:
    resources = transaction["resources"]
    if not isinstance(resources, list):
        raise InstallControlError("install_transaction_invalid")
    return {
        "committed_at": transaction["started_at"],
        "generation": 1,
        "installation_id": transaction["id"],
        "release": transaction["release"],
        "request_sha256": transaction["request_sha256"],
        "resources": resources,
        "scheduler_backend": transaction["scheduler_backend"],
        "schema": "install-manifest/v1",
        "state_root": transaction["state_root"],
        "transaction_id": transaction["id"],
        "vault_root": transaction["vault_root"],
    }


def _publish_manifest(
    install_root: Path, transaction_path: Path, transaction: dict[str, object]
) -> dict[str, object]:
    manifest = _manifest_from_transaction(transaction)
    encoded = canonical_json_bytes(manifest)
    transaction["target_manifest_sha256"] = _sha256(encoded)
    set_transaction_state(transaction_path, transaction, "publishing")
    _publish_manifest_bytes(install_root / "manifest.json", encoded)
    set_transaction_state(transaction_path, transaction, "committed")
    return manifest


def _publish_manifest_bytes(path: Path, encoded: bytes) -> None:
    if path.exists() and path.read_bytes() != encoded:
        raise InstallControlError("install_manifest_conflict")
    if path.exists():
        return
    atomic_write(path, encoded)


def validate_backend(scheduler_backend: str) -> None:
    if scheduler_backend not in BACKENDS:
        raise InstallControlError("install_scheduler_backend_invalid")


def _origin_size(record: Mapping[str, object]) -> int:
    origin = record.get("origin")
    if not isinstance(origin, Mapping):
        raise InstallControlError("install_transaction_invalid")
    return int(origin.get("size", 0))


def _prepare_resource_records(
    install_root: Path, resources: Sequence[ManagedResource]
) -> list[dict[str, object]]:
    records = [_resource_record(install_root, resource) for resource in resources]
    if sum(_origin_size(record) for record in records) > MAX_TOTAL_PREIMAGE_BYTES:
        raise InstallControlError("install_preimages_too_large")
    return records


def _revert_resource(
    *,
    install_root: Path,
    transaction_path: Path,
    transaction: dict[str, object],
    record: dict[str, object],
    resource: ManagedResource,
) -> None:
    installed, origin = resource_snapshots(record)
    current = resource.read_owned()
    if same_snapshot(current, origin):
        mark_resource_state(transaction_path, transaction, record, "reverted")
        return
    if not same_snapshot(current, installed):
        raise InstallControlError("install_rollback_drift")
    _restore_origin(
        install_root=install_root,
        transaction_path=transaction_path,
        transaction=transaction,
        record=record,
        resource=resource,
        origin=origin,
    )


def _restore_origin(
    *,
    install_root: Path,
    transaction_path: Path,
    transaction: dict[str, object],
    record: dict[str, object],
    resource: ManagedResource,
    origin: Mapping[str, object],
) -> None:
    mark_resource_state(transaction_path, transaction, record, "reverting")
    resource.write_owned(read_origin(install_root, origin))
    if not same_snapshot(resource.read_owned(), origin):
        raise InstallControlError("install_rollback_verification_failed")
    mark_resource_state(transaction_path, transaction, record, "reverted")


def rollback_install(
    *,
    install_root: Path,
    transaction_path: Path,
    transaction: dict[str, object],
    records: Sequence[dict[str, object]],
    resources: Sequence[ManagedResource],
) -> None:
    set_transaction_state(transaction_path, transaction, "reverting")
    pairs = zip(reversed(records), reversed(resources), strict=True)
    for record, resource in pairs:
        _revert_resource(
            install_root=install_root,
            transaction_path=transaction_path,
            transaction=transaction,
            record=record,
            resource=resource,
        )
    set_transaction_state(transaction_path, transaction, "reverted")


def failure_code(error: Exception) -> str:
    if isinstance(error, InstallControlError):
        return error_code(error)
    return "install_resource_mutation_failed"


def record_failure(
    transaction_path: Path, transaction: dict[str, object], error: Exception
) -> None:
    transaction["error"] = {"code": failure_code(error)}
    transaction["updated_at"] = utc_now()
    write_record(transaction_path, transaction)


def _mutate_resources(
    transaction_path: Path,
    transaction: dict[str, object],
    records: Sequence[dict[str, object]],
    resources: Sequence[ManagedResource],
) -> None:
    for record, resource in zip(records, resources, strict=True):
        _apply_resource(transaction_path, transaction, record, resource)


def _rollback_after_error(
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
        rollback_install(
            install_root=install_root,
            transaction_path=transaction_path,
            transaction=transaction,
            records=records,
            resources=resources,
        )
    except Exception as rollback_error:
        transaction["error"] = {"code": failure_code(rollback_error)}
        set_transaction_state(transaction_path, transaction, "quarantined")
        raise InstallControlError("install_rollback_quarantined") from error


def _record_request(record: Mapping[str, object]) -> dict[str, object]:
    return {
        "id": record.get("id"),
        "installed": record.get("installed"),
        "kind": record.get("kind"),
        "locator": record.get("locator"),
        "metadata": record.get("metadata"),
    }


def require_resource_request(
    records: Sequence[Mapping[str, object]], resources: Sequence[ManagedResource]
) -> None:
    expected = [_resource_request(resource) for resource in resources]
    actual = [_record_request(record) for record in records]
    if actual != expected:
        raise InstallControlError("install_resource_request_mismatch")


def _require_request_digest(record: Mapping[str, object], request_sha256: str) -> None:
    if record.get("request_sha256") != request_sha256:
        raise InstallControlError("install_upgrade_not_supported")


def require_installed_resources(
    records: Sequence[Mapping[str, object]], resources: Sequence[ManagedResource]
) -> None:
    for record, resource in zip(records, resources, strict=True):
        installed, _origin = resource_snapshots(record)
        if not same_snapshot(resource.read_owned(), installed):
            raise ResourceDriftError("install_resource_drift", resource)


def _complete_published_transaction(
    transaction_path: Path,
    transaction: dict[str, object],
    manifest: Mapping[str, object],
) -> None:
    manifest_digest = _sha256(canonical_json_bytes(dict(manifest)))
    if transaction.get("target_manifest_sha256") != manifest_digest:
        raise InstallControlError("install_manifest_transaction_mismatch")
    if transaction.get("state") == "publishing":
        set_transaction_state(transaction_path, transaction, "committed")


def _accept_active_manifest(
    *,
    transaction_path: Path,
    transaction: dict[str, object],
    manifest: dict[str, object],
    request_sha256: str,
    resources: Sequence[ManagedResource],
) -> dict[str, object]:
    _require_request_digest(manifest, request_sha256)
    records = transaction_resources(manifest)
    require_resource_request(records, resources)
    require_installed_resources(records, resources)
    _complete_published_transaction(transaction_path, transaction, manifest)
    if transaction.get("state") != "committed":
        raise InstallControlError("install_transaction_manifest_conflict")
    return manifest


def _continue_install(
    *,
    install_root: Path,
    transaction_path: Path,
    transaction: dict[str, object],
    records: Sequence[dict[str, object]],
    resources: Sequence[ManagedResource],
) -> dict[str, object]:
    set_transaction_state(transaction_path, transaction, "mutating")
    try:
        _mutate_resources(transaction_path, transaction, records, resources)
        return _publish_manifest(install_root, transaction_path, transaction)
    except Exception as error:
        _rollback_after_error(
            install_root=install_root,
            transaction_path=transaction_path,
            transaction=transaction,
            records=records,
            resources=resources,
            error=error,
        )
        raise


def _start_install(
    *,
    install_root: Path,
    state_root: Path,
    vault_root: Path,
    release: Mapping[str, object],
    scheduler_backend: str,
    resources: Sequence[ManagedResource],
    request_sha256: str,
) -> dict[str, object]:
    records = _prepare_resource_records(install_root, resources)
    transaction = _new_transaction(
        state_root=state_root,
        vault_root=vault_root,
        release=release,
        backend=scheduler_backend,
        resource_records=records,
        request_sha256=request_sha256,
    )
    transaction_path = install_root / "transaction.json"
    write_record(transaction_path, transaction)
    return _continue_install(
        install_root=install_root,
        transaction_path=transaction_path,
        transaction=transaction,
        records=records,
        resources=resources,
    )


def _resume_install(
    *,
    install_root: Path,
    transaction_path: Path,
    transaction: dict[str, object],
    request_sha256: str,
    resources: Sequence[ManagedResource],
) -> dict[str, object]:
    _require_request_digest(transaction, request_sha256)
    records = transaction_resources(transaction)
    require_resource_request(records, resources)
    state = transaction.get("state")
    if state == "publishing":
        return _publish_manifest(install_root, transaction_path, transaction)
    if state not in {"prepared", "mutating"}:
        raise InstallControlError("install_transaction_blocks_new_work")
    return _continue_install(
        install_root=install_root,
        transaction_path=transaction_path,
        transaction=transaction,
        records=records,
        resources=resources,
    )


def _accept_recorded_manifest(
    *,
    transaction_path: Path,
    transaction: dict[str, object] | None,
    manifest: dict[str, object],
    request_sha256: str,
    resources: Sequence[ManagedResource],
) -> dict[str, object]:
    """An installed manifest is accepted only together with the transaction that wrote it."""
    if transaction is None:
        raise InstallControlError("install_manifest_without_transaction")
    return _accept_active_manifest(
        transaction_path=transaction_path,
        transaction=transaction,
        manifest=manifest,
        request_sha256=request_sha256,
        resources=resources,
    )


def install_under_lock(
    *,
    install_root: Path,
    state_root: Path,
    vault_root: Path,
    release: Mapping[str, object],
    scheduler_backend: str,
    resources: Sequence[ManagedResource],
) -> dict[str, object]:
    manifest_path = install_root / "manifest.json"
    transaction_path = install_root / "transaction.json"
    manifest = optional_record(manifest_path, MANIFEST_SCHEMA)
    transaction = optional_record(transaction_path, TRANSACTION_SCHEMA)
    request_sha256 = request_digest(vault_root, release, scheduler_backend, resources)
    if manifest is not None:
        return _accept_recorded_manifest(
            transaction_path=transaction_path,
            transaction=transaction,
            manifest=manifest,
            request_sha256=request_sha256,
            resources=resources,
        )
    if transaction is not None and transaction.get("state") not in {"reverted"}:
        return _resume_install(
            install_root=install_root,
            transaction_path=transaction_path,
            transaction=transaction,
            request_sha256=request_sha256,
            resources=resources,
        )
    return _start_install(
        install_root=install_root,
        state_root=state_root,
        vault_root=vault_root,
        release=release,
        scheduler_backend=scheduler_backend,
        resources=resources,
        request_sha256=request_sha256,
    )
