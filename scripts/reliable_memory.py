"""Shared durability and validation primitives for reliable memory operations."""

from __future__ import annotations

import contextlib
import ctypes as ctypes
import os
import secrets
import sqlite3
import stat
import subprocess as subprocess
import time
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path

from bounded_io import IO_CHUNK_BYTES as IO_CHUNK_BYTES
from llm_wiki.formats.canonical import (
    canonical_json_bytes as canonical_json_bytes,
)
from llm_wiki.formats.canonical import (
    sha256_bytes as sha256_bytes,
)
from llm_wiki.formats.schema import (
    SchemaValidationError as SchemaValidationError,
)
from llm_wiki.formats.schema import (
    validate_schema as validate_schema,
)
from llm_wiki.formats.schema import (
    validate_schema_object as validate_schema_object,
)
from llm_wiki.reliability_defaults import DEFAULTS as DEFAULTS
from llm_wiki.reliability_defaults import ReliableMemoryDefaults as ReliableMemoryDefaults
from llm_wiki.restricted_paths import _is_posix_relative_text as _is_posix_relative_text
from llm_wiki.restricted_paths import _require_inside_a_root as _require_inside_a_root
from llm_wiki.restricted_paths import _require_normalized_relative as _require_normalized_relative
from llm_wiki.restricted_paths import _require_posix_relative_text as _require_posix_relative_text
from llm_wiki.restricted_paths import _under_root as _under_root
from llm_wiki.restricted_paths import restricted_relative_path as restricted_relative_path
from llm_wiki.runtime_contracts import (
    MetadataDurabilityUnavailable as MetadataDurabilityUnavailable,
)
from llm_wiki.runtime_contracts import RuntimeFileIdentity as RuntimeFileIdentity
from llm_wiki.runtime_contracts import UnsafeStateRoot as UnsafeStateRoot
from llm_wiki.runtime_environment import _CLOUD_DIRECTORY_NAMES as _CLOUD_DIRECTORY_NAMES
from llm_wiki.runtime_environment import _chmod_or_warn as _chmod_or_warn
from llm_wiki.runtime_environment import _decode_mount_path as _decode_mount_path
from llm_wiki.runtime_environment import _harden_runtime_owner_only as _harden_runtime_owner_only
from llm_wiki.runtime_environment import _has_reparse_attribute as _has_reparse_attribute
from llm_wiki.runtime_environment import _is_network_filesystem as _is_network_filesystem
from llm_wiki.runtime_environment import _known_network_path as _known_network_path
from llm_wiki.runtime_environment import (
    _owner_permissions_supported as _owner_permissions_supported,
)
from llm_wiki.runtime_environment import _parse_darwin_mounts as _parse_darwin_mounts
from llm_wiki.runtime_environment import _parse_posix_mounts as _parse_posix_mounts
from llm_wiki.runtime_environment import _path_is_under as _path_is_under
from llm_wiki.runtime_environment import _platform_system as _platform_system
from llm_wiki.runtime_environment import _posix_mount_table as _posix_mount_table
from llm_wiki.runtime_environment import _posix_network_path as _posix_network_path
from llm_wiki.runtime_environment import _query_darwin_mounts as _query_darwin_mounts
from llm_wiki.runtime_environment import _read_posix_mount_data as _read_posix_mount_data
from llm_wiki.runtime_environment import _require_applied_mode as _require_applied_mode
from llm_wiki.runtime_environment import (
    _require_local_non_reparse_root as _require_local_non_reparse_root,
)
from llm_wiki.runtime_environment import _require_owner_only_file as _require_owner_only_file
from llm_wiki.runtime_environment import _require_windows_owner_only as _require_windows_owner_only
from llm_wiki.runtime_environment import _set_owner_only as _set_owner_only
from llm_wiki.runtime_environment import _warn_if_cloud_synchronized as _warn_if_cloud_synchronized
from llm_wiki.runtime_environment import _windows_network_anchor as _windows_network_anchor
from llm_wiki.runtime_environment import _windows_reparse_point as _windows_reparse_point
from llm_wiki.runtime_fingerprints import _digest_runtime_descriptor as _digest_runtime_descriptor
from llm_wiki.runtime_fingerprints import _fingerprint_metadata as _fingerprint_metadata
from llm_wiki.runtime_fingerprints import (
    _require_fingerprint_metadata as _require_fingerprint_metadata,
)
from llm_wiki.runtime_fingerprints import _verified_runtime_digest as _verified_runtime_digest
from llm_wiki.runtime_fingerprints import fingerprint_regular_file as fingerprint_regular_file
from llm_wiki.runtime_fingerprints import fingerprint_runtime_file as fingerprint_runtime_file
from llm_wiki.runtime_publication import _adopted_or_duplicate as _adopted_or_duplicate
from llm_wiki.runtime_publication import _discard_staged as _discard_staged
from llm_wiki.runtime_publication import _fsync_parent_on_posix as _fsync_parent_on_posix
from llm_wiki.runtime_publication import _link_then_unlink as _link_then_unlink
from llm_wiki.runtime_publication import _move_into_place as _move_into_place
from llm_wiki.runtime_publication import _move_posix as _move_posix
from llm_wiki.runtime_publication import _move_windows as _move_windows
from llm_wiki.runtime_publication import _publication_digest as _publication_digest
from llm_wiki.runtime_publication import _publish_staged as _publish_staged
from llm_wiki.runtime_publication import _replace_in_place as _replace_in_place
from llm_wiki.runtime_publication import (
    _require_create_only_carries_no_evidence as _require_create_only_carries_no_evidence,
)
from llm_wiki.runtime_publication import _require_distinct_siblings as _require_distinct_siblings
from llm_wiki.runtime_publication import (
    _require_publication_arguments as _require_publication_arguments,
)
from llm_wiki.runtime_publication import (
    _require_replacement_carries_evidence as _require_replacement_carries_evidence,
)
from llm_wiki.runtime_publication import (
    _require_replacement_evidence as _require_replacement_evidence,
)
from llm_wiki.runtime_publication import _require_replacement_shape as _require_replacement_shape
from llm_wiki.runtime_publication import _require_runtime_arguments as _require_runtime_arguments
from llm_wiki.runtime_publication import _require_runtime_mode as _require_runtime_mode
from llm_wiki.runtime_publication import _require_runtime_payload as _require_runtime_payload
from llm_wiki.runtime_publication import _require_unchanged_identity as _require_unchanged_identity
from llm_wiki.runtime_publication import _resolved_parents as _resolved_parents
from llm_wiki.runtime_publication import (
    _resolved_runtime_destination as _resolved_runtime_destination,
)
from llm_wiki.runtime_publication import _resolved_sibling_pair as _resolved_sibling_pair
from llm_wiki.runtime_publication import _settled_publication as _settled_publication
from llm_wiki.runtime_publication import _unlink_staged as _unlink_staged
from llm_wiki.runtime_publication import (
    _verify_published_runtime_permissions as _verify_published_runtime_permissions,
)
from llm_wiki.runtime_publication import _write_all as _write_all
from llm_wiki.runtime_publication import _write_staged_bytes as _write_staged_bytes
from llm_wiki.runtime_publication import durable_publish_file as durable_publish_file
from llm_wiki.runtime_publication import publish_runtime_file as publish_runtime_file
from llm_wiki.runtime_records import _confirm_runtime_identity as _confirm_runtime_identity
from llm_wiki.runtime_records import _contained_runtime_metadata as _contained_runtime_metadata
from llm_wiki.runtime_records import _posix_runtime_identity_names as _posix_runtime_identity_names
from llm_wiki.runtime_records import _require_bounded_regular_file as _require_bounded_regular_file
from llm_wiki.runtime_records import _require_file_size_budget as _require_file_size_budget
from llm_wiki.runtime_records import (
    _require_nonnegative_file_budget as _require_nonnegative_file_budget,
)
from llm_wiki.runtime_records import (
    _require_runtime_parent_containment as _require_runtime_parent_containment,
)
from llm_wiki.runtime_records import _require_stable_read as _require_stable_read
from llm_wiki.runtime_records import (
    _runtime_identity_from_metadata as _runtime_identity_from_metadata,
)
from llm_wiki.runtime_records import _validated_runtime_metadata as _validated_runtime_metadata
from llm_wiki.runtime_records import (
    _windows_runtime_identity_names as _windows_runtime_identity_names,
)
from llm_wiki.runtime_records import (
    capture_operational_database_identity as capture_operational_database_identity,
)
from llm_wiki.runtime_records import capture_runtime_file_identity as capture_runtime_file_identity
from llm_wiki.runtime_records import read_runtime_bytes as read_runtime_bytes
from llm_wiki.runtime_records import validate_operational_db_file as validate_operational_db_file
from llm_wiki.runtime_records import validate_runtime_file as validate_runtime_file
from llm_wiki.runtime_sync import _close_windows_handle as _close_windows_handle
from llm_wiki.runtime_sync import _file_sync_mode as _file_sync_mode
from llm_wiki.runtime_sync import _fsync_directory_posix as _fsync_directory_posix
from llm_wiki.runtime_sync import _fsync_directory_windows as _fsync_directory_windows
from llm_wiki.runtime_sync import _metadata_durability_error as _metadata_durability_error
from llm_wiki.runtime_sync import _opened_directory_descriptor as _opened_directory_descriptor
from llm_wiki.runtime_sync import fsync_directory as fsync_directory
from llm_wiki.runtime_sync import fsync_file as fsync_file
from llm_wiki.runtime_sync import sync_runtime_directory as sync_runtime_directory
from llm_wiki.sqlite_contracts import MigrationStatement as MigrationStatement
from llm_wiki.sqlite_contracts import OperationalDatabaseContract as OperationalDatabaseContract
from llm_wiki.sqlite_contracts import (
    OperationalDatabaseContractError as OperationalDatabaseContractError,
)
from llm_wiki.sqlite_contracts import _require_migration_text as _require_migration_text
from llm_wiki.sqlite_contracts import _require_positive_int as _require_positive_int
from llm_wiki.sqlite_contracts import quote_sqlite_identifier as quote_sqlite_identifier
from llm_wiki.sqlite_deadlines import (
    _OPERATIONAL_PROGRESS_VM_STEPS as _OPERATIONAL_PROGRESS_VM_STEPS,
)
from llm_wiki.sqlite_deadlines import (
    configure_operational_deadline as configure_operational_deadline,
)
from llm_wiki.sqlite_deadlines import operational_busy_ms as operational_busy_ms
from llm_wiki.sqlite_deadlines import operational_deadline_active as operational_deadline_active
from llm_wiki.sqlite_deadlines import refresh_operational_deadline as refresh_operational_deadline
from llm_wiki.sqlite_migrations import (
    _apply_one_migration_statement as _apply_one_migration_statement,
)
from llm_wiki.sqlite_migrations import _migration_incomplete as _migration_incomplete
from llm_wiki.sqlite_migrations import _require_final_invariant as _require_final_invariant
from llm_wiki.sqlite_migrations import (
    _require_unique_statement_names as _require_unique_statement_names,
)
from llm_wiki.sqlite_migrations import run_resumable_migration as run_resumable_migration
from llm_wiki.sqlite_pragmas import _apply_operational_pragmas as _apply_operational_pragmas
from llm_wiki.sqlite_pragmas import (
    _apply_readonly_operational_pragmas as _apply_readonly_operational_pragmas,
)
from llm_wiki.sqlite_pragmas import _initialized_contract_identity as _initialized_contract_identity
from llm_wiki.sqlite_pragmas import _pragma_integer as _pragma_integer
from llm_wiki.sqlite_pragmas import _require_pragma as _require_pragma
from llm_wiki.sqlite_pragmas import (
    _validate_or_initialize_operational_contract as _validate_or_initialize_operational_contract,
)
from llm_wiki.sqlite_transactions import begin_immediate as begin_immediate

# The one bound on a capture decision file, for the writer and every reader. The
# indexer read it with 64 KiB while the writer allowed 1 MiB, so a verbose classifier
# answer (43 KB, stored twice: the wire answer and the plan) failed every retry and
# the day's summary was lost (audit 2026-09-27 B-3,
# docs/research/2026-09-27-one-file-one-bound.md). 1 MiB is the writer's bound, kept:
# about 170 times the 1 500-token answer the classifier asks for, so it refuses only an
# answer no summary needs, and CLI providers do not enforce the request's token cap.
MAX_CAPTURE_DECISION_BYTES = 1024 * 1024


def _second_writer_is_blocked(second: sqlite3.Connection) -> bool:
    """Whether a second immediate transaction is refused, which is the point."""
    try:
        second.execute("BEGIN IMMEDIATE")
    except sqlite3.OperationalError as exc:
        return "locked" in str(exc).lower() or "busy" in str(exc).lower()
    second.rollback()
    return False


def _close_probe_connections(connections: list[sqlite3.Connection]) -> None:
    for connection in connections:
        with contextlib.suppress(sqlite3.Error):
            connection.rollback()
        with contextlib.suppress(sqlite3.Error):
            connection.close()


def _remove_probe_files(probe: Path) -> None:
    for suffix in ("", "-journal", "-shm", "-wal"):
        with contextlib.suppress(OSError):
            Path(f"{probe}{suffix}").unlink()


def _opened_probe(
    probe: Path, deadline: float, connections: list[sqlite3.Connection]
) -> sqlite3.Connection | None:
    """One probe connection, or None when the bounded probe has run out of time."""
    if time.monotonic() >= deadline:
        return None
    connection = sqlite3.connect(probe, timeout=0)
    connections.append(connection)
    return connection


def _run_lock_probe(
    probe: Path, deadline: float, connections: list[sqlite3.Connection]
) -> bool | None:
    first = _opened_probe(probe, deadline, connections)
    second = _opened_probe(probe, deadline, connections)
    if first is None or second is None:
        return None
    first.execute("PRAGMA journal_mode=DELETE")
    first.execute("BEGIN IMMEDIATE")
    if time.monotonic() >= deadline:
        return None
    return _second_writer_is_blocked(second)


def _require_probe_component(path: Path) -> None:
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or _windows_reparse_point(path):
        raise PermissionError("locking probe path must contain only real directories")


def _require_probe_root_chain(root: Path) -> None:
    from bounded_io import _acceptable_ancestor

    _require_probe_component(root)
    for parent in root.parents:
        if not _acceptable_ancestor(parent):
            raise PermissionError("locking probe ancestor is not trusted")


def _probe_directory(root: Path) -> Path:
    root = root.absolute()
    _require_probe_root_chain(root)
    directory = root / "run"
    _require_probe_component(directory)
    if directory.lstat().st_dev != root.lstat().st_dev:
        raise PermissionError("locking probe must use the runtime root filesystem")
    return directory


def _initialize_probe_directory(root: Path) -> None:
    _require_probe_root_chain(root.absolute())
    (root / "run").mkdir(exist_ok=True)
    _set_owner_only(_probe_directory(root), 0o700)


def _probe_directory_identity(directory: Path) -> tuple[int, int, int]:
    info = directory.lstat()
    return info.st_dev, info.st_ino, info.st_mode


def _require_same_probe_directory(directory: Path, identity: tuple[int, int, int]) -> None:
    _require_probe_root_chain(directory)
    if _probe_directory_identity(directory) != identity:
        raise PermissionError("locking probe directory changed")


def _contained_lock_probe(root, probe, identity, deadline, connections) -> bool | None:
    _require_same_probe_directory(root, identity)
    result = _run_lock_probe(probe, deadline, connections)
    _require_same_probe_directory(root, identity)
    return result


def _cleanup_lock_probe(probe, connections, root, identity) -> None:
    _close_probe_connections(connections)
    if probe is None:
        return
    try:
        _require_same_probe_directory(root, identity)
    except OSError:
        return  # The original directory is no longer owned at this path.
    _remove_probe_files(probe)


def _sqlite_lock_probe(root: Path, *, deadline: float = float("inf")) -> bool | None:
    """Probe existing run/ without changing the sealed vault root."""
    try:
        directory = _probe_directory(root)
    except OSError:
        return None
    return _sqlite_directory_lock_probe(directory, deadline=deadline)


def _sqlite_directory_lock_probe(directory: Path, *, deadline: float = float("inf")) -> bool | None:
    """Probe the database's actual directory without creating another runtime root."""
    probe = None
    identity = None
    connections: list[sqlite3.Connection] = []
    try:
        _require_probe_root_chain(directory.absolute())
        identity = _probe_directory_identity(directory)
        probe = directory / f".llm-wiki-lock-probe-{secrets.token_hex(16)}.sqlite3"
        return _contained_lock_probe(directory, probe, identity, deadline, connections)
    except (OSError, sqlite3.Error):
        return None
    finally:
        _cleanup_lock_probe(probe, connections, directory, identity)


def _prepare_local_database_directory(path: Path) -> Path:
    path = Path(path)
    _require_local_non_reparse_root(path)
    _warn_if_cloud_synchronized(path)
    path.mkdir(parents=True, exist_ok=True)
    _require_probe_root_chain(path.absolute())
    _set_owner_only(path, 0o700)
    return path


def validate_state_root(path: Path) -> None:
    """Fail closed when a runtime root lacks known-safe local lock semantics."""
    path = _prepare_local_database_directory(path)
    _initialize_probe_directory(path)
    if _sqlite_lock_probe(path) is not True:
        raise UnsafeStateRoot(f"state root failed the SQLite two-connection locking probe: {path}")


def validate_database_directory(path: Path) -> None:
    """Validate a database parent; only a state-root initializer creates run/."""
    path = _prepare_local_database_directory(path)
    if _sqlite_directory_lock_probe(path) is not True:
        raise UnsafeStateRoot(f"database directory failed the SQLite two-connection locking probe: {path}")


def open_operational_db(
    path: Path,
    *,
    busy_ms: int,
    contract: OperationalDatabaseContract | None = None,
    initialize_contract: bool = False,
    deadline: float | None = None,
) -> sqlite3.Connection:
    """Open an owner-restricted rollback-journal operational database."""
    _require_operational_open_arguments(busy_ms, contract, initialize_contract)
    busy_ms = operational_busy_ms(busy_ms, deadline)
    path = Path(path)
    validate_database_directory(path.parent)
    expected = _operational_db_identity(path)
    connection = sqlite3.connect(
        path,
        timeout=busy_ms / 1_000,
        isolation_level=None,
    )
    try:
        configure_operational_deadline(connection, deadline)
        _configure_operational_connection(
            connection,
            path,
            expected,
            busy_ms=operational_busy_ms(busy_ms, deadline),
            contract=contract,
            initialize_contract=initialize_contract,
        )
        operational_deadline_active(deadline)
        return connection
    except Exception:
        connection.close()
        operational_deadline_active(deadline)
        raise


def _require_operational_open_arguments(
    busy_ms: int,
    contract: OperationalDatabaseContract | None,
    initialize_contract: bool,
) -> None:
    if busy_ms < 0:
        raise ValueError("busy_ms must be non-negative")
    if initialize_contract and contract is None:
        raise ValueError("initialize_contract requires an operational database contract")


def _operational_db_identity(path: Path) -> os.stat_result:
    """Create the database file, or validate an existing one without a probe."""
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(path, flags, 0o600)
    except FileExistsError:
        return validate_operational_db_file(path, path.parent, max_bytes=1 << 50)
    # O_EXCL proves this inode was just created here, so no connection in this
    # process can hold locks on it and closing the descriptor strips nothing.
    os.close(descriptor)
    return path.stat(follow_symlinks=False)


def _configure_operational_connection(
    connection: sqlite3.Connection,
    path: Path,
    expected: os.stat_result,
    *,
    busy_ms: int,
    contract: OperationalDatabaseContract | None,
    initialize_contract: bool,
) -> None:
    current = path.stat(follow_symlinks=False)
    if not os.path.samestat(expected, current):
        raise PermissionError("operational database identity changed while opening")
    _apply_operational_pragmas(connection, busy_ms)
    if contract is not None:
        _validate_or_initialize_operational_contract(
            connection,
            contract,
            initialize=initialize_contract,
        )
    _set_owner_only(path, 0o600)


# A crashed writer leaves a rollback journal beside the database, and SQLite
# can only replay one by opening read-write. Every read-only open afterwards
# fails with "attempt to write a readonly database" — and the callers here are
# validators, so the whole memory write path fences itself off behind a message
# about an invalid adoption record, which names neither the file nor the cause.
#
# Seen twice on the live vault on 2026-08-29, at 03:58 and again at 15:08. Both
# times no process held the journal; both times one read-write open cleared it
# and `PRAGMA integrity_check` answered `ok`. Nothing in the product did that,
# so the vault stayed fenced — the second time a day of project checkpoints
# piled up undrained — until a person went looking.
#
# The recovery is attempted only when the journal is actually there, only once,
# and only if the write lock is free within a quarter second. A live writer
# legitimately holds a hot journal, and there the original refusal is right.
_JOURNAL_REPLAY_BUSY_MS = 250


def _hot_journal(path: Path) -> Path:
    return Path(f"{Path(path)}-journal")


def _readonly_write_refusal(error: BaseException) -> bool:
    return "readonly database" in str(error)


def _replayed_hot_journal(path: Path, *, deadline: float | None = None) -> bool:
    """Let SQLite replay a stranded journal; True when it is gone afterwards."""
    busy_ms = operational_busy_ms(_JOURNAL_REPLAY_BUSY_MS, deadline)
    try:
        database = sqlite3.connect(
            str(Path(path)), timeout=busy_ms / 1_000
        )
    except sqlite3.Error:
        operational_deadline_active(deadline)
        return False
    try:
        configure_operational_deadline(database, deadline)
        database.execute("PRAGMA journal_mode")
        operational_deadline_active(deadline)
    except sqlite3.Error:
        operational_deadline_active(deadline)
        return False
    finally:
        database.close()
    return not _hot_journal(path).exists()


def _require_replayed_journal(path: Path, error: sqlite3.OperationalError, *, deadline: float | None = None) -> None:
    """Re-raise the original refusal unless a stranded journal explains it."""
    operational_deadline_active(deadline)
    if not _readonly_write_refusal(error) or not _hot_journal(path).exists():
        raise error
    if not _replayed_hot_journal(path, deadline=deadline):
        raise error


# Rows fetched per step of a streamed scan of an operational table, so memory holds one
# batch rather than a table: doctor's materialised scan peaked at 67 MB of Python
# objects on the 29 275-row installed vault. 1 000 rows keep a batch well under 1 MB at
# the measured 291-byte mean preconditions and bound the time between stop checks;
# review if a batch's rows grow past that mean by orders of magnitude.
OPERATIONAL_SCAN_BATCH_ROWS = 1_000


def streamed_rows(
    database: sqlite3.Connection,
    query: str,
    parameters: Sequence[object] = (),
    *,
    stop: Callable[[], None],
) -> Iterator[sqlite3.Row]:
    """Every row of the query, one batch in memory at a time.

    `stop` runs before each batch is handed out and raises to end the scan (a
    deadline). No row count is capped: a table that grows with use is read whole
    rather than refused past a number, which is how doctor and the installed-vault
    check both misjudged a busy vault. See
    `docs/research/2026-09-27-doctor-reads-every-transaction.md`.
    """
    cursor = database.execute(query, tuple(parameters))
    while batch := cursor.fetchmany(OPERATIONAL_SCAN_BATCH_ROWS):
        stop()
        yield from batch


def open_readonly_operational_db(
    path: Path,
    state_root: Path,
    *,
    max_bytes: int | None,
    owner_only: bool = False,
    busy_ms: int = 0,
    contract: OperationalDatabaseContract | None = None,
    deadline: float | None = None,
) -> sqlite3.Connection:
    """Open a validated runtime SQLite database read-only and fail on path races.

    One stranded rollback journal is replayed and the open retried once; see
    `_require_replayed_journal` for why, and for what is deliberately not done.
    """
    if busy_ms < 0:
        raise ValueError("busy_ms must be non-negative")
    operational_deadline_active(deadline)
    arguments = {
        "deadline": deadline,
        "max_bytes": max_bytes,
        "owner_only": owner_only,
        "busy_ms": busy_ms,
        "contract": contract,
    }
    try:
        return _opened_readonly_operational_db(path, state_root, **arguments)
    except sqlite3.OperationalError as error:
        _require_replayed_journal(Path(path), error, deadline=deadline)
    return _opened_readonly_operational_db(path, state_root, **arguments)


def _opened_readonly_operational_db(
    path: Path,
    state_root: Path,
    *,
    max_bytes: int | None,
    owner_only: bool,
    busy_ms: int,
    contract: OperationalDatabaseContract | None,
    deadline: float | None = None,
) -> sqlite3.Connection:
    """Validate the file and open it read-only, once, with no recovery."""
    busy_ms = operational_busy_ms(busy_ms, deadline)
    expected = validate_operational_db_file(
        path, state_root, max_bytes=max_bytes, owner_only=owner_only
    )
    database = sqlite3.connect(
        f"{Path(path).resolve(strict=True).as_uri()}?mode=ro",
        uri=True,
        timeout=busy_ms / 1_000,
        isolation_level=None,
    )
    try:
        configure_operational_deadline(database, deadline)
        current = Path(path).stat(follow_symlinks=False)
        if not os.path.samestat(expected, current):
            raise PermissionError("runtime database identity changed while opening")
        _apply_readonly_operational_pragmas(database, operational_busy_ms(busy_ms, deadline))
        if contract is not None:
            _validate_or_initialize_operational_contract(
                database, contract, initialize=False
            )
        operational_deadline_active(deadline)
        return database
    except Exception:
        database.close()
        operational_deadline_active(deadline)
        raise
