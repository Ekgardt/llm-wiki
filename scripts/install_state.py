"""Validated install records and the managed-resource primitives every install step shares."""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import stat
import tempfile
from collections.abc import Callable, Mapping, Sequence
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path

from llm_wiki.formats.canonical import canonical_json_bytes
from llm_wiki.formats.schema import validate_schema
from reliable_memory import fsync_directory

# One install-control record (`run/install/*.json`); a session record allows 8 MiB.
MAX_RECORD_BYTES = 1024 * 1024
MAX_PREIMAGE_BYTES = 2 * 1024 * 1024
MAX_TOTAL_PREIMAGE_BYTES = 16 * 1024 * 1024


_SCHEMA_ROOT = Path(__file__).resolve().parent / "schemas"
MANIFEST_SCHEMA = _SCHEMA_ROOT / "install-manifest-v1.json"
TRANSACTION_SCHEMA = _SCHEMA_ROOT / "install-transaction-v1.json"
_MANIFEST_V2_SCHEMA = _SCHEMA_ROOT / "install-manifest-v2.json"
_TRANSACTION_V2_SCHEMA = _SCHEMA_ROOT / "install-transaction-v2.json"
_INSTALL_SCHEMAS = {
    "install-manifest/v1": MANIFEST_SCHEMA,
    "install-manifest/v2": _MANIFEST_V2_SCHEMA,
    "install-transaction/v1": TRANSACTION_SCHEMA,
    "install-transaction/v2": _TRANSACTION_V2_SCHEMA,
}


class InstallControlError(RuntimeError):
    """Stable fail-closed install control-plane error."""


@dataclass(frozen=True, slots=True)
class ManagedResource:
    resource_id: str
    kind: str
    locator: str
    desired: bytes
    read_owned: Callable[[], bytes | None]
    write_owned: Callable[[bytes | None], None]
    recognizes: Callable[[bytes], bool]
    read_projections: Callable[[Sequence[bytes]], bytes | None] | None = None
    write_projection: Callable[[bytes | None, bytes | None, Mapping[str, object]], None] | None = (
        None
    )
    recover_legacy_projection: Callable[[Mapping[str, object]], tuple[bytes, bytes]] | None = None
    supports_v2_recovery: bool = True
    metadata: Mapping[str, object] = field(default_factory=dict)
    definitions: Mapping[str, bytes] = field(default_factory=dict, repr=False, compare=False)
    adopt_as_absent: bool = False
    # The operator's explicit `--adopt`: this run takes over the resource as it is now,
    # keeping its current content as the rollback point instead of refusing the drift
    # (docs/research/2026-09-28-a-rollback-undoes-only-what-it-did.md).
    adopt_current: bool = field(default=False, compare=False)
    # The resource as it is on disk even when no rendering of ours matches it: what a
    # replaced scheduler records as its rollback point. None where it cannot be read so.
    read_current: Callable[[], bytes | None] | None = field(default=None, compare=False)
    # A systemd scheduler's way to make one of its drop-ins a resource with the same
    # command runner, so writing or removing it reloads the same manager.
    make_drop_in: Callable[[Path, bytes], ManagedResource] | None = field(
        default=None, compare=False
    )


# Files the installer owns whole: nothing but its own rendering belongs in them, so an
# update replaces them instead of refusing a change made outside it. The displaced
# version stays the rollback point, a copy is kept, and the report names it; a local
# setting belongs in a systemd drop-in, which the installer never touches
# (docs/research/2026-09-28-an-update-replaces-what-it-owns.md). A shared file keeps
# the fragment rules, and a cron table or a Windows task cannot be read back as it is.
WHOLLY_OWNED_KINDS = frozenset({"systemd_scheduler", "launchd_scheduler", "opencode_plugin"})


def replaced_whole(resource: ManagedResource) -> bool:
    return resource.kind in WHOLLY_OWNED_KINDS


def sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def atomic_write(path: Path, value: bytes) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _restore_external_owner(path: Path, owner: tuple[int, int] | None) -> None:
    if owner is None or not hasattr(os, "chown"):
        return
    os.chown(path, *owner)


def atomic_external_write(
    path: Path,
    value: bytes,
    mode: int,
    owner: tuple[int, int] | None = None,
) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        _restore_external_owner(temporary, owner)
        os.replace(temporary, path)
        fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_constant(value: str) -> object:
    raise ValueError(f"invalid JSON constant: {value}")


def read_state_bytes(path: Path) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise InstallControlError("install_state_unsafe")
    if path.stat().st_size > MAX_RECORD_BYTES:
        raise InstallControlError("install_state_oversized")
    return _provider_proof_bytes(path, MAX_RECORD_BYTES)


def strict_json_object(raw: bytes) -> dict[str, object]:
    try:
        value = json.loads(
            raw,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise InstallControlError("install_state_invalid_json") from exc
    if not isinstance(value, dict) or canonical_json_bytes(value) != raw:
        raise InstallControlError("install_state_noncanonical")
    return value


def _install_schema(value: Mapping[str, object], record_type: str) -> Path:
    schema_name = value.get("schema")
    if not isinstance(schema_name, str) or not schema_name.startswith(record_type):
        raise InstallControlError("install_state_schema_invalid")
    try:
        return _INSTALL_SCHEMAS[schema_name]
    except KeyError as exc:
        raise InstallControlError("install_state_schema_invalid") from exc


def read_install_record(path: Path, record_type: str) -> dict[str, object]:
    return validated_install_record(read_state_bytes(path), record_type)


def optional_install_record(path: Path, record_type: str) -> dict[str, object] | None:
    if not path.exists() and not path.is_symlink():
        return None
    return read_install_record(path, record_type)


def read_managed_file(path: Path) -> bytes | None:
    if path.is_symlink():
        raise InstallControlError("install_resource_symlink")
    if not path.exists():
        return None
    return _read_regular_managed_file(path)


def _read_regular_managed_file(path: Path) -> bytes:
    if not path.is_file() or path.stat().st_size > MAX_PREIMAGE_BYTES:
        raise InstallControlError("install_resource_file_invalid")
    return path.read_bytes()


def remove_managed_file(path: Path) -> None:
    if path.is_symlink() or not path.is_file():
        raise InstallControlError("install_resource_file_invalid")
    path.unlink()
    fsync_directory(path.parent)


def write_managed_file(path: Path, value: bytes | None, mode: int) -> None:
    if value is None:
        remove_managed_file(path)
        return
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.parent.is_symlink():
        raise InstallControlError("install_resource_parent_symlink")
    atomic_external_write(path, value, mode)


def file_resource(
    *,
    resource_id: str,
    kind: str,
    path: Path,
    desired: bytes,
    definition_path: str | None = None,
    mode: int = 0o600,
    adopt_as_absent: bool = True,
) -> ManagedResource:
    path = Path(path).resolve(strict=False)
    metadata: dict[str, object] = {"file_mode": mode}
    definitions: dict[str, bytes] = {}
    if definition_path is not None:
        metadata["definition_path"] = definition_path
        definitions[definition_path] = desired
    return ManagedResource(
        resource_id=resource_id,
        kind=kind,
        locator=str(path),
        desired=desired,
        read_owned=lambda: read_managed_file(path),
        write_owned=lambda value: write_managed_file(path, value, mode),
        recognizes=lambda current: current == desired,
        metadata=metadata,
        definitions=definitions,
        adopt_as_absent=adopt_as_absent,
    )


def decode_definition_value(value: object) -> bytes:
    if not isinstance(value, str):
        raise InstallControlError("install_definition_bundle_invalid")
    try:
        decoded = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise InstallControlError("install_definition_bundle_invalid") from exc
    if len(decoded) > MAX_PREIMAGE_BYTES:
        raise InstallControlError("install_definition_bundle_oversized")
    return decoded


def resource_snapshot(value: bytes | None, preimage: str | None = None) -> dict[str, object]:
    if value is None:
        return {"state": "absent"}
    result: dict[str, object] = {
        "sha256": sha256(value),
        "size": len(value),
        "state": "present",
    }
    if preimage is not None:
        result["preimage"] = preimage
    return result


def same_snapshot(value: bytes | None, snapshot: Mapping[str, object]) -> bool:
    if snapshot.get("state") == "absent":
        return value is None
    return value is not None and resource_snapshot(value) == {
        key: item for key, item in snapshot.items() if key != "preimage"
    }


def resource_snapshots(
    record: Mapping[str, object],
) -> tuple[Mapping[str, object], Mapping[str, object]]:
    installed = record["installed"]
    origin = record["origin"]
    if not isinstance(installed, Mapping) or not isinstance(origin, Mapping):
        raise InstallControlError("install_transaction_invalid")
    return installed, origin


def _preimage_path(install_root: Path, origin: Mapping[str, object]) -> Path:
    relative = origin.get("preimage")
    if not isinstance(relative, str):
        raise InstallControlError("install_preimage_reference_invalid")
    if not relative.startswith("preimages/"):
        raise InstallControlError("install_preimage_reference_invalid")
    return install_root / relative


def _read_preimage(path: Path) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise InstallControlError("install_preimage_invalid")
    if path.stat().st_size > MAX_PREIMAGE_BYTES:
        raise InstallControlError("install_preimage_invalid")
    return _provider_proof_bytes(path, MAX_PREIMAGE_BYTES)


def read_origin(install_root: Path, origin: Mapping[str, object]) -> bytes | None:
    if origin.get("state") == "absent":
        return None
    value = _read_preimage(_preimage_path(install_root, origin))
    if not same_snapshot(value, origin):
        raise InstallControlError("install_preimage_invalid")
    return value


def transaction_resources(transaction: Mapping[str, object]) -> list[dict[str, object]]:
    resources = transaction.get("resources")
    if not isinstance(resources, list):
        raise InstallControlError("install_transaction_invalid")
    if any(not isinstance(resource, dict) for resource in resources):
        raise InstallControlError("install_transaction_invalid")
    return resources


def v2_resource_snapshots(
    record: Mapping[str, object],
) -> tuple[Mapping[str, object], Mapping[str, object], Mapping[str, object]]:
    values = (record.get("desired"), record.get("origin"), record.get("rollback"))
    if any(not isinstance(value, Mapping) for value in values):
        raise InstallControlError("install_transaction_invalid")
    desired, origin, rollback = values
    return desired, origin, rollback


def read_v2_snapshot(install_root: Path, snapshot: Mapping[str, object]) -> bytes | None:
    return read_origin(install_root, snapshot)


def _v2_manifest_resources(
    records: Sequence[Mapping[str, object]],
) -> list[dict[str, object]]:
    resources = []
    for record in records:
        resource = dict(record)
        metadata = dict(resource.get("metadata", {}))
        if metadata.pop("_retire_after_commit", False):
            continue
        resource["metadata"] = metadata
        resource["state"] = "verified"
        resources.append(resource)
    return resources


def v2_manifest_from_transaction(
    transaction: Mapping[str, object],
) -> dict[str, object]:
    return {
        "committed_at": transaction["started_at"],
        "generation": transaction["generation"],
        "installation_id": transaction["installation_id"],
        "release": transaction["release"],
        "request_sha256": transaction["request_sha256"],
        "resources": _v2_manifest_resources(transaction_resources(transaction)),
        "rollback_point": transaction["rollback_point"],
        "scheduler_backend": transaction["scheduler_backend"],
        "schema": "install-manifest/v2",
        "state_root": transaction["state_root"],
        "transaction_id": transaction["id"],
        "vault_root": transaction["vault_root"],
    }


def _direct_preimage(value: Mapping[str, object]) -> set[str]:
    direct = value.get("preimage")
    if isinstance(direct, str):
        return {direct}
    return set()


def _mapping_preimages(value: Mapping[str, object]) -> set[str]:
    references = _direct_preimage(value)
    for item in value.values():
        references.update(preimage_references(item))
    return references


def _sequence_preimages(value: Sequence[object]) -> set[str]:
    references: set[str] = set()
    for item in value:
        references.update(preimage_references(item))
    return references


def preimage_references(value: object) -> set[str]:
    if isinstance(value, Mapping):
        return _mapping_preimages(value)
    if isinstance(value, list):
        return _sequence_preimages(value)
    return set()


def record_desired(record: Mapping[str, object]) -> Mapping[str, object]:
    desired = record.get("desired", record.get("installed"))
    if not isinstance(desired, Mapping):
        raise InstallControlError("install_state_schema_invalid")
    return desired


def manifest_digest(manifest: Mapping[str, object] | None) -> str | None:
    if manifest is None:
        return None
    return sha256(canonical_json_bytes(dict(manifest)))


def _install_health(
    status: str,
    health: str,
    codes: Sequence[str],
    deletion_codes: Sequence[str],
) -> dict[str, object]:
    return {
        "codes": sorted(set(codes)),
        "deletion_codes": sorted(set(deletion_codes)),
        "health": health,
        "status": status,
    }


def _validate_preimage_references(
    install_root: Path, records: Sequence[Mapping[str, object]]
) -> None:
    total = 0
    for record in records:
        _installed, origin = resource_snapshots(record)
        if origin.get("state") == "absent":
            continue
        value = read_origin(install_root, origin)
        total += len(value or b"")
    if total > MAX_TOTAL_PREIMAGE_BYTES:
        raise InstallControlError("install_preimages_too_large")


def _v2_record_preimages(install_root: Path, record: Mapping[str, object]) -> int:
    desired, origin, rollback = v2_resource_snapshots(record)
    values = (
        read_v2_snapshot(install_root, desired),
        read_v2_snapshot(install_root, origin),
        read_v2_snapshot(install_root, rollback),
    )
    return sum(len(value or b"") for value in values)


def _v2_preimage_size(install_root: Path, records: Sequence[Mapping[str, object]]) -> int:
    return sum(_v2_record_preimages(install_root, record) for record in records)


def _v2_checkpoint_records(checkpoint: object) -> list[Mapping[str, object]]:
    if not isinstance(checkpoint, Mapping):
        raise InstallControlError("install_state_schema_invalid")
    records = checkpoint.get("resources")
    if not isinstance(records, list) or any(not isinstance(record, Mapping) for record in records):
        raise InstallControlError("install_state_schema_invalid")
    return records


def _v2_checkpoint_resource_size(install_root: Path, record: Mapping[str, object]) -> int:
    desired = record_desired(record)
    origin = record.get("origin")
    if not isinstance(origin, Mapping):
        raise InstallControlError("install_state_schema_invalid")
    values = (
        read_v2_snapshot(install_root, desired),
        read_v2_snapshot(install_root, origin),
    )
    return sum(len(value or b"") for value in values)


def _v2_checkpoint_preimages(install_root: Path, checkpoint: object) -> int:
    if checkpoint is None:
        return 0
    records = _v2_checkpoint_records(checkpoint)
    return sum(_v2_checkpoint_resource_size(install_root, record) for record in records)


def validate_v2_record_preimages(install_root: Path, record: Mapping[str, object]) -> None:
    records = transaction_resources(record)
    total = _v2_preimage_size(install_root, records)
    total += _v2_checkpoint_preimages(install_root, record.get("rollback_point"))
    if total > MAX_TOTAL_PREIMAGE_BYTES:
        raise InstallControlError("install_preimages_too_large")


def validate_v2_manifest_transaction(
    install_root: Path,
    manifest: Mapping[str, object],
    transaction: Mapping[str, object],
) -> None:
    _require_transaction_binding(manifest, transaction)
    if v2_manifest_from_transaction(transaction) != manifest:
        raise InstallControlError("install_manifest_transaction_mismatch")
    validate_v2_record_preimages(install_root, manifest)
    validate_v2_record_preimages(install_root, transaction)


def _require_transaction_binding(
    manifest: Mapping[str, object], transaction: Mapping[str, object]
) -> None:
    """The manifest names its transaction, its request, and is the transaction's target."""
    if (
        manifest.get("transaction_id") != transaction.get("id")
        or manifest.get("request_sha256") != transaction.get("request_sha256")
    ):
        raise InstallControlError("install_manifest_transaction_mismatch")
    expected = transaction.get("target_manifest_sha256")
    if expected != sha256(canonical_json_bytes(dict(manifest))):
        raise InstallControlError("install_manifest_transaction_mismatch")


def _validate_manifest_transaction(
    install_root: Path,
    manifest: Mapping[str, object],
    transaction: Mapping[str, object],
) -> None:
    _require_transaction_binding(manifest, transaction)
    _validate_preimage_references(install_root, transaction_resources(manifest))


def _required_install_transaction(
    transaction: Mapping[str, object] | None,
) -> Mapping[str, object]:
    if transaction is None:
        raise InstallControlError("install_manifest_without_transaction")
    if transaction.get("operation") != "install":
        raise InstallControlError("install_manifest_without_transaction")
    return transaction


def _active_health_for_state(state: object) -> dict[str, object]:
    values = {
        "committed": _install_health("active", "ok", [], ["install_manifest_retained"]),
        "quarantined": _install_health(
            "quarantined",
            "error",
            ["install_transaction_quarantined"],
            ["install_manifest_retained", "install_transaction_quarantined"],
        ),
        "publishing": _install_health(
            "nonterminal",
            "degraded",
            ["install_transaction_nonterminal"],
            ["install_manifest_retained", "install_transaction_nonterminal"],
        ),
    }
    try:
        return values[str(state)]
    except KeyError as exc:
        raise InstallControlError("install_manifest_transaction_mismatch") from exc


def _active_install_health(
    install_root: Path,
    manifest: Mapping[str, object],
    transaction: Mapping[str, object] | None,
) -> dict[str, object]:
    required = _required_install_transaction(transaction)
    _validate_manifest_transaction(install_root, manifest, required)
    return _active_health_for_state(required.get("state"))


def _active_v2_install_health(
    install_root: Path,
    manifest: Mapping[str, object],
    transaction: Mapping[str, object] | None,
) -> dict[str, object]:
    if not _v2_transaction_operation(transaction):
        raise InstallControlError("install_manifest_without_transaction")
    if manifest.get("transaction_id") == transaction.get("id"):
        validate_v2_manifest_transaction(install_root, manifest, transaction)
        return _active_health_for_state(transaction.get("state"))
    return _active_v2_base_health(install_root, manifest, transaction)


def _v2_transaction_operation(transaction: Mapping[str, object] | None) -> bool:
    if transaction is None or transaction.get("schema") != "install-transaction/v2":
        return False
    return transaction.get("operation") in {
        "install",
        "update",
        "rollback",
        "uninstall",
    }


_BASE_HEALTH_STATES = {
    "quarantined": (
        "quarantined",
        "error",
        ("install_transaction_quarantined",),
        ("install_manifest_retained", "install_transaction_quarantined"),
    ),
    "reverted": ("active", "ok", (), ("install_manifest_retained",)),
    **{
        state: (
            "nonterminal",
            "degraded",
            ("install_transaction_nonterminal",),
            ("install_manifest_retained", "install_transaction_nonterminal"),
        )
        for state in ("prepared", "mutating", "publishing", "reverting")
    },
}


def _active_v2_base_health_state(state: object) -> dict[str, object]:
    health = _BASE_HEALTH_STATES.get(state)
    if health is None:
        raise InstallControlError("install_manifest_transaction_mismatch")
    return _install_health(*health)


def _active_v2_base_health(
    install_root: Path,
    manifest: Mapping[str, object],
    transaction: Mapping[str, object],
) -> dict[str, object]:
    if transaction.get("base_manifest_sha256") != manifest_digest(manifest):
        raise InstallControlError("install_manifest_transaction_mismatch")
    _validate_v2_base_linkage(manifest, transaction)
    validate_v2_record_preimages(install_root, transaction)
    return _active_v2_base_health_state(transaction.get("state"))


def _validate_v2_base_linkage(
    manifest: Mapping[str, object], transaction: Mapping[str, object]
) -> None:
    if transaction.get("generation") != int(manifest["generation"]) + 1:
        raise InstallControlError("install_manifest_transaction_mismatch")
    linked = ("installation_id", "state_root", "vault_root")
    if any(transaction.get(key) != manifest.get(key) for key in linked):
        raise InstallControlError("install_manifest_transaction_mismatch")


def active_install_health_for_schema(
    install_root: Path,
    manifest: Mapping[str, object],
    transaction: Mapping[str, object] | None,
) -> dict[str, object]:
    if (
        transaction is not None
        and transaction.get("schema") == "install-transaction/v2"
        and manifest.get("transaction_id") != transaction.get("id")
    ):
        return _active_v2_base_health(install_root, manifest, transaction)
    if manifest.get("schema") == "install-manifest/v2":
        return _active_v2_install_health(install_root, manifest, transaction)
    return _active_install_health(install_root, manifest, transaction)


PROVIDER_READ_PROOF = ContextVar("install_provider_read_proof", default=None)


def _provider_proof_bytes(path, limit):
    proof = PROVIDER_READ_PROOF.get()
    if proof is None:
        return path.read_bytes()
    return proof.read(path, limit)


def validated_install_record(raw, record_type):
    value = strict_json_object(raw)
    try:
        validate_schema(value, _install_schema(value, record_type))
    except ValueError as exc:
        raise InstallControlError("install_state_schema_invalid") from exc
    return value


def _inactive_committed_health(transaction: Mapping[str, object]) -> dict[str, object]:
    if transaction.get("operation") != "uninstall":
        raise InstallControlError("install_transaction_orphaned")
    return _install_health("uninstalled", "ok", [], [])


def _inactive_health_for_state(
    transaction: Mapping[str, object],
) -> dict[str, object]:
    nonterminal = _install_health(
        "nonterminal",
        "degraded",
        ["install_transaction_nonterminal"],
        ["install_transaction_nonterminal"],
    )
    handlers: dict[str, Callable[[], dict[str, object]]] = {
        "committed": lambda: _inactive_committed_health(transaction),
        "mutating": lambda: nonterminal,
        "prepared": lambda: nonterminal,
        "publishing": lambda: nonterminal,
        "quarantined": lambda: _install_health(
            "quarantined",
            "error",
            ["install_transaction_quarantined"],
            ["install_transaction_quarantined"],
        ),
        "reverted": lambda: _install_health("rolled_back", "ok", [], []),
        "reverting": lambda: nonterminal,
    }
    try:
        return handlers[str(transaction.get("state"))]()
    except KeyError as exc:
        raise InstallControlError("install_transaction_orphaned") from exc


def _inactive_install_health(
    install_root: Path, transaction: Mapping[str, object]
) -> dict[str, object]:
    _validate_preimage_references(install_root, transaction_resources(transaction))
    return _inactive_health_for_state(transaction)


def _inactive_install_health_for_schema(
    install_root: Path, transaction: Mapping[str, object]
) -> dict[str, object]:
    if transaction.get("schema") == "install-transaction/v2":
        validate_v2_record_preimages(install_root, transaction)
        return _inactive_health_for_state(transaction)
    return _inactive_install_health(install_root, transaction)


def _validate_install_records(install_root: Path) -> dict[str, object]:
    manifest = optional_install_record(install_root / "manifest.json", "install-manifest/")
    transaction = optional_install_record(
        install_root / "transaction.json", "install-transaction/"
    )
    if manifest is not None:
        return active_install_health_for_schema(install_root, manifest, transaction)
    if transaction is not None:
        return _inactive_install_health_for_schema(install_root, transaction)
    return _empty_install_root_health(install_root)


def _empty_install_root_health(install_root: Path) -> dict[str, object]:
    entries = {entry.name for entry in install_root.iterdir()}
    if entries <= {"install.lock", "preimages", "scheduler"}:
        return _install_health("absent", "ok", [], [])
    raise InstallControlError("install_artifact_state_unknown")


def _install_root_kind(install_root: Path) -> str:
    try:
        metadata = install_root.lstat()
    except FileNotFoundError:
        return "missing"
    except OSError:
        return "unsafe"
    if stat.S_ISLNK(metadata.st_mode):
        return "unsafe"
    if stat.S_ISDIR(metadata.st_mode):
        return "directory"
    return "unsafe"


def validate_install_state(state_root: Path) -> dict[str, object]:
    install_root = Path(state_root) / "run" / "install"
    kind = _install_root_kind(install_root)
    if kind == "missing":
        return _install_health("absent", "ok", [], [])
    if kind != "directory":
        return _install_health(
            "corrupt", "error", ["install_state_corrupt"], ["install_state_corrupt"]
        )
    try:
        return _validate_install_records(install_root)
    except (InstallControlError, OSError, UnicodeError, ValueError):
        return _install_health(
            "corrupt", "error", ["install_state_corrupt"], ["install_state_corrupt"]
        )


def adopted_value(resource: ManagedResource) -> bytes | None:
    """What an adopted resource holds now: as it is on disk, even if no rendering matches."""
    if resource.read_current is not None:
        return resource.read_current()
    return resource.read_owned()
