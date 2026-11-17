"""A compile receipt counts only when a committed transaction wrote its bytes."""
from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from bounded_io import read_stable_bytes
from llm_wiki.formats.canonical import sha256_bytes
from llm_wiki.formats.receipts import parse_compile_receipt_version as parse_compile_receipt_version

# One compile receipt; the largest on the live vault is 16.6 KB (2026-09-27). 1 MiB refuses a
# corrupted receipt before it is parsed.
MAX_RECEIPT_BYTES = 1024 * 1024


def require_transaction_authority(
    record: Mapping[str, object],
    coordinator: object,
    path: Path,
    vault: Path,
    raw_bytes: bytes,
    *, deadline: float | None = None,
) -> None:
    """A receipt is evidence only when a committed transaction wrote those bytes."""
    transaction = coordinator.committed_write_authority(str(record["operation_id"]), deadline=deadline)
    if transaction is None:
        raise ValueError("compile receipt has no committed transaction authority")
    operations = _transaction_operations(transaction)
    receipt_operation = operations.get(path.relative_to(vault).as_posix())
    if receipt_operation is None or receipt_operation.after_hash != sha256_bytes(
        raw_bytes
    ):
        raise ValueError("compile receipt bytes are not transaction-authoritative")
    require_operation_integrity(record, operations)


def _transaction_operations(transaction: object) -> dict[str, object]:
    return {item.path: item for item in transaction.operations}


def require_operation_integrity(
    record: Mapping[str, object], operations: Mapping[str, object]
) -> None:
    for operation in record["operations"]:
        authoritative = operations.get(operation["path"])
        if (
            authoritative is None
            or authoritative.kind != operation["kind"]
            or authoritative.after_hash != operation["after_sha256"]
        ):
            raise ValueError("compile receipt operation integrity failed")


def read_compile_receipt_version(logical_path, source_sha256, coordinator, *, path, vault, deadline=None):
    path = Path(path)
    vault = Path(vault)
    try:
        raw = read_stable_bytes(path, MAX_RECEIPT_BYTES, label="compile receipt")
    except FileNotFoundError:
        return None
    record = parse_compile_receipt_version(raw, logical_path=logical_path, source_sha256=source_sha256)
    version = record["schema_version"].rsplit("/", 1)[1]
    if path.name != f"{version}-{record['source_identity']}.md":
        raise ValueError("compile receipt filename disagrees")
    require_transaction_authority(record, coordinator, path, vault, raw, deadline=deadline)
    return record
