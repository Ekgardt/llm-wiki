"""Operational SQLite connections, lock probes and bounded-memory scans."""
from __future__ import annotations

import contextlib
import os
import secrets
import sqlite3
import stat
import time
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path

from llm_wiki.runtime_contracts import UnsafeStateRoot
from llm_wiki.runtime_environment import (
    require_local_non_reparse_root,
    set_owner_only,
    warn_if_cloud_synchronized,
    windows_reparse_point,
)
from llm_wiki.runtime_metadata import validate_operational_db_file
from llm_wiki.sqlite_contracts import OperationalDatabaseContract
from llm_wiki.sqlite_deadlines import (
    busy_database,
    configure_operational_deadline,
    operational_busy_ms,
    operational_deadline_active,
)
from llm_wiki.sqlite_pragmas import (
    apply_operational_pragmas,
    apply_readonly_operational_pragmas,
    validate_or_initialize_operational_contract,
)


def second_writer_is_blocked(second: sqlite3.Connection) -> bool:
    """Whether a second immediate transaction is refused, which is the point."""
    try:
        second.execute("BEGIN IMMEDIATE")
    except sqlite3.OperationalError as exc:
        return busy_database(exc)
    second.rollback()
    return False


def close_probe_connections(connections: list[sqlite3.Connection]) -> None:
    for connection in connections:
        with contextlib.suppress(sqlite3.Error):
            connection.rollback()
        with contextlib.suppress(sqlite3.Error):
            connection.close()


def remove_probe_files(probe: Path) -> None:
    for suffix in ("", "-journal", "-shm", "-wal"):
        with contextlib.suppress(OSError):
            Path(f"{probe}{suffix}").unlink()


def opened_probe(
    probe: Path, deadline: float, connections: list[sqlite3.Connection]
) -> sqlite3.Connection | None:
    """One probe connection, or None when the bounded probe has run out of time."""
    if time.monotonic() >= deadline:
        return None
    connection = sqlite3.connect(probe, timeout=0)
    connections.append(connection)
    return connection


def run_lock_probe(
    probe: Path, deadline: float, connections: list[sqlite3.Connection]
) -> bool | None:
    first = opened_probe(probe, deadline, connections)
    second = opened_probe(probe, deadline, connections)
    if first is None or second is None:
        return None
    first.execute("PRAGMA journal_mode=DELETE")
    first.execute("BEGIN IMMEDIATE")
    if time.monotonic() >= deadline:
        return None
    return second_writer_is_blocked(second)


def require_probe_component(path: Path) -> None:
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or windows_reparse_point(path):
        raise PermissionError("locking probe path must contain only real directories")


def require_probe_root_chain(root: Path) -> None:
    from llm_wiki.path_ancestry import acceptable_ancestor

    require_probe_component(root)
    for parent in root.parents:
        if not acceptable_ancestor(parent):
            raise PermissionError("locking probe ancestor is not trusted")


def probe_directory(root: Path) -> Path:
    root = root.absolute()
    require_probe_root_chain(root)
    directory = root / "run"
    require_probe_component(directory)
    if directory.lstat().st_dev != root.lstat().st_dev:
        raise PermissionError("locking probe must use the runtime root filesystem")
    return directory


def initialize_probe_directory(root: Path) -> None:
    require_probe_root_chain(root.absolute())
    (root / "run").mkdir(exist_ok=True)
    set_owner_only(probe_directory(root), 0o700)


def probe_directory_identity(directory: Path) -> tuple[int, int, int]:
    info = directory.lstat()
    return info.st_dev, info.st_ino, info.st_mode


def require_same_probe_directory(directory: Path, identity: tuple[int, int, int]) -> None:
    require_probe_root_chain(directory)
    if probe_directory_identity(directory) != identity:
        raise PermissionError("locking probe directory changed")


def contained_lock_probe(root, probe, identity, deadline, connections) -> bool | None:
    require_same_probe_directory(root, identity)
    result = run_lock_probe(probe, deadline, connections)
    require_same_probe_directory(root, identity)
    return result


def cleanup_lock_probe(probe, connections, root, identity) -> None:
    close_probe_connections(connections)
    if probe is None:
        return
    try:
        require_same_probe_directory(root, identity)
    except OSError:
        return  # The original directory is no longer owned at this path.
    remove_probe_files(probe)


def sqlite_lock_probe(root: Path, *, deadline: float = float("inf")) -> bool | None:
    """Probe existing run/ without changing the sealed vault root."""
    try:
        directory = probe_directory(root)
    except OSError:
        return None
    return sqlite_directory_lock_probe(directory, deadline=deadline)


def sqlite_directory_lock_probe(directory: Path, *, deadline: float = float("inf")) -> bool | None:
    """Probe the database's actual directory without creating another runtime root."""
    probe = None
    identity = None
    connections: list[sqlite3.Connection] = []
    try:
        require_probe_root_chain(directory.absolute())
        identity = probe_directory_identity(directory)
        probe = directory / f".llm-wiki-lock-probe-{secrets.token_hex(16)}.sqlite3"
        return contained_lock_probe(directory, probe, identity, deadline, connections)
    except (OSError, sqlite3.Error):
        return None
    finally:
        cleanup_lock_probe(probe, connections, directory, identity)


def prepare_local_database_directory(path: Path) -> Path:
    path = Path(path)
    require_local_non_reparse_root(path)
    warn_if_cloud_synchronized(path)
    path.mkdir(parents=True, exist_ok=True)
    require_probe_root_chain(path.absolute())
    set_owner_only(path, 0o700)
    return path


def validate_state_root(path: Path) -> None:
    """Fail closed when a runtime root lacks known-safe local lock semantics."""
    path = prepare_local_database_directory(path)
    initialize_probe_directory(path)
    if sqlite_lock_probe(path) is not True:
        raise UnsafeStateRoot(f"state root failed the SQLite two-connection locking probe: {path}")


def validate_database_directory(path: Path) -> None:
    """Validate a database parent; only a state-root initializer creates run/."""
    path = prepare_local_database_directory(path)
    if sqlite_directory_lock_probe(path) is not True:
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
    require_operational_open_arguments(busy_ms, contract, initialize_contract)
    busy_ms = operational_busy_ms(busy_ms, deadline)
    path = Path(path)
    validate_database_directory(path.parent)
    expected = operational_db_identity(path)
    connection = sqlite3.connect(
        path,
        timeout=busy_ms / 1_000,
        isolation_level=None,
    )
    try:
        configure_operational_deadline(connection, deadline)
        configure_operational_connection(
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


def require_operational_open_arguments(
    busy_ms: int,
    contract: OperationalDatabaseContract | None,
    initialize_contract: bool,
) -> None:
    if busy_ms < 0:
        raise ValueError("busy_ms must be non-negative")
    if initialize_contract and contract is None:
        raise ValueError("initialize_contract requires an operational database contract")


def operational_db_identity(path: Path) -> os.stat_result:
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


def configure_operational_connection(
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
    apply_operational_pragmas(connection, busy_ms)
    if contract is not None:
        validate_or_initialize_operational_contract(
            connection,
            contract,
            initialize=initialize_contract,
        )
    set_owner_only(path, 0o600)


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
JOURNAL_REPLAY_BUSY_MS = 250


def hot_journal(path: Path) -> Path:
    return Path(f"{Path(path)}-journal")


def readonly_write_refusal(error: BaseException) -> bool:
    return "readonly database" in str(error)


def replayed_hot_journal(path: Path, *, deadline: float | None = None) -> bool:
    """Let SQLite replay a stranded journal; True when it is gone afterwards."""
    busy_ms = operational_busy_ms(JOURNAL_REPLAY_BUSY_MS, deadline)
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
    return not hot_journal(path).exists()


def require_replayed_journal(path: Path, error: sqlite3.OperationalError, *, deadline: float | None = None) -> None:
    """Re-raise the original refusal unless a stranded journal explains it."""
    operational_deadline_active(deadline)
    if not readonly_write_refusal(error) or not hot_journal(path).exists():
        raise error
    if not replayed_hot_journal(path, deadline=deadline):
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
    `require_replayed_journal` for why, and for what is deliberately not done.
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
        return opened_readonly_operational_db(path, state_root, **arguments)
    except sqlite3.OperationalError as error:
        require_replayed_journal(Path(path), error, deadline=deadline)
    return opened_readonly_operational_db(path, state_root, **arguments)


def opened_readonly_operational_db(
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
        apply_readonly_operational_pragmas(database, operational_busy_ms(busy_ms, deadline))
        if contract is not None:
            validate_or_initialize_operational_contract(
                database, contract, initialize=False
            )
        operational_deadline_active(deadline)
        return database
    except Exception:
        database.close()
        operational_deadline_active(deadline)
        raise

# Preserve the supported facade pickle paths; these are aliases, not wrappers.
validate_state_root.__module__ = "reliable_memory"
validate_database_directory.__module__ = "reliable_memory"
open_operational_db.__module__ = "reliable_memory"
streamed_rows.__module__ = "reliable_memory"
open_readonly_operational_db.__module__ = "reliable_memory"
