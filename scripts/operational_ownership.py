"""Canonical fenced ownership for Reliability V3 operational actors."""

from __future__ import annotations

import contextlib
import ctypes
import hashlib
import json
import os
import re
import secrets
import sqlite3
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal, cast

import markdown_transaction
import process_liveness
from process_liveness import _is_plain_int, _platform_system
from reliable_memory import (
    DEFAULTS,
    OperationalDatabaseContract,
    RuntimeFileIdentity,
    begin_immediate,
    canonical_json_bytes,
    capture_runtime_file_identity,
    open_operational_db,
    read_runtime_bytes,
    restricted_relative_path,
    sha256_bytes,
)

OwnerRole = Literal[
    "capture",
    "project",
    "markdown-writer",
    "queue-worker",
    "compile",
    "doctor",
    "nightly",
    "weekly",
    "lsp",
    "queue-operator",
    "repair",
    "runtime-deletion-check",
]
ProcessState = Literal["alive", "dead", "unknown"]

_ROLES = frozenset(
    {
        "capture",
        "project",
        "markdown-writer",
        "queue-worker",
        "compile",
        "doctor",
        "nightly",
        "weekly",
        "lsp",
        "queue-operator",
        "repair",
        "runtime-deletion-check",
    }
)
_LONG_LEASE_ROLES = frozenset(
    {"queue-worker", "compile", "nightly", "weekly", "queue-operator", "repair"}
)
_MARKER_ROLES = frozenset({"compile", "nightly", "weekly"})
_COORDINATOR_CONTRACT = OperationalDatabaseContract(application_id=0x4C575433)
_COORDINATOR_CANDIDATE = "markdown-transactions-v3.candidate.sqlite3"
_MAX_MARKER_BYTES = 4096


class OperationalOwnershipError(RuntimeError):
    """Stable failure from canonical operational ownership."""

    def __init__(self, code: str, message: str | None = None) -> None:
        self.code = code
        super().__init__(message or code)

    def __reduce__(self):
        return (self.__class__, (self.code, str(self)))


@dataclass(frozen=True)
class ProcessIdentity:
    pid: int
    start_identity: str


@dataclass(frozen=True)
class OwnerLease:
    state_root: Path
    role: OwnerRole
    scope: str
    actor_id: str
    token: str
    epoch: int
    process: ProcessIdentity
    acquired_at: datetime
    heartbeat_at: datetime
    expires_at: datetime
    ttl_seconds: int
    heartbeat_seconds: int


@dataclass(frozen=True)
class MarkerIdentity:
    relative_path: str
    sha256: str
    file_identity: RuntimeFileIdentity
    pid: int


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _unsupported_platform() -> OperationalOwnershipError:
    return OperationalOwnershipError(
        "unsupported_platform", "operational process identity is unsupported"
    )


def process_start_identity(pid: int) -> str | None:
    """The OS process-start identity, or ``None`` for a missing process.

    The probes live in `process_liveness` so that a lock file naming its owner
    can be read without importing the coordinator; this module keeps the name
    and the refusal its callers expect.
    Research: docs/research/2026-09-17-a-lock-names-the-process-not-only-its-number.md
    """
    try:
        return process_liveness.process_start_identity(pid)
    except process_liveness.ProcessIdentityUnavailable as exc:
        raise _unsupported_platform() from exc


def current_process_identity() -> ProcessIdentity:
    pid = os.getpid()
    start_identity = process_start_identity(pid)
    if start_identity is None:
        raise OperationalOwnershipError(
            "current_process_identity_unavailable",
            "current process identity is unavailable",
        )
    return ProcessIdentity(pid=pid, start_identity=start_identity)


def process_identity_state(identity: ProcessIdentity) -> ProcessState:
    _validate_process(identity)
    try:
        observed = process_start_identity(identity.pid)
    except OperationalOwnershipError:
        raise
    except (OSError, PermissionError):
        return "unknown"
    if observed is None or observed != identity.start_identity:
        return "dead"
    return "alive"


@dataclass(frozen=True)
class _WindowsTokenApi:
    get_current_process: object
    open_process_token: object
    get_token_information: object
    convert_sid: object
    local_free: object
    close_handle: object


def _windows_token_api(advapi32: object, kernel32: object) -> _WindowsTokenApi:
    """Bind the token entry points this identity uses, with exact signatures."""
    from ctypes import wintypes

    get_current_process = kernel32.GetCurrentProcess
    get_current_process.argtypes = ()
    get_current_process.restype = wintypes.HANDLE
    open_process_token = advapi32.OpenProcessToken
    open_process_token.argtypes = (
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.HANDLE),
    )
    open_process_token.restype = wintypes.BOOL
    get_token_information = advapi32.GetTokenInformation
    get_token_information.argtypes = (
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    )
    get_token_information.restype = wintypes.BOOL
    convert_sid = advapi32.ConvertSidToStringSidW
    convert_sid.argtypes = (ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR))
    convert_sid.restype = wintypes.BOOL
    local_free = kernel32.LocalFree
    local_free.argtypes = (ctypes.c_void_p,)
    local_free.restype = ctypes.c_void_p
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = (wintypes.HANDLE,)
    close_handle.restype = wintypes.BOOL
    return _WindowsTokenApi(
        get_current_process=get_current_process,
        open_process_token=open_process_token,
        get_token_information=get_token_information,
        convert_sid=convert_sid,
        local_free=local_free,
        close_handle=close_handle,
    )


def _windows_open_token(api: _WindowsTokenApi) -> object:
    from ctypes import wintypes

    token = wintypes.HANDLE()
    if not api.open_process_token(
        api.get_current_process(), 0x0008, ctypes.byref(token)
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    return token


def _windows_token_information(api: _WindowsTokenApi, token: object) -> object:
    from ctypes import wintypes

    required = wintypes.DWORD()
    api.get_token_information(token, 1, None, 0, ctypes.byref(required))
    if ctypes.get_last_error() != 122 or required.value <= 0:
        raise ctypes.WinError(ctypes.get_last_error())
    buffer = ctypes.create_string_buffer(required.value)
    if not api.get_token_information(
        token, 1, buffer, required.value, ctypes.byref(required)
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    return buffer


def _windows_token_sid_text(api: _WindowsTokenApi, token: object) -> str | None:
    from ctypes import wintypes

    buffer = _windows_token_information(api, token)
    sid = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p))[0]
    sid_text = wintypes.LPWSTR()
    if not api.convert_sid(sid, ctypes.byref(sid_text)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return sid_text.value
    finally:
        api.local_free(sid_text)


def _validated_windows_actor(value: object) -> str:
    if value is None or re.fullmatch(r"S-[0-9]+(?:-[0-9]+)+", value) is None:
        raise OSError("Windows actor SID was malformed")
    actor = f"windows-sid:{value}"
    if len(actor.encode("ascii")) > 256:
        raise OSError("Windows actor SID exceeded the ownership bound")
    return actor


def _windows_actor_identity() -> str:
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    api = _windows_token_api(advapi32, kernel32)
    token = _windows_open_token(api)
    try:
        value = _windows_token_sid_text(api, token)
    finally:
        api.close_handle(token)
    return _validated_windows_actor(value)


def current_actor_identity() -> str:
    system = _platform_system()
    if system == "Windows":
        return _windows_actor_identity()
    if system in {"Linux", "Darwin"} and hasattr(os, "getuid"):
        return f"posix-uid:{os.getuid()}"
    raise _unsupported_platform()


def ownership_actor_identity(role: str, scope: str) -> str:
    """The actor is the agent taking the lease, not the human running it.

    `maintenance_owners.actor_id` is UNIQUE, so whatever this returns decides
    how many leases may exist at once. Returning the plain user identity made
    that one lease per machine account: measured 2026-08-28, holding
    `nightly/global` refused `queue-worker`, `capture`, `markdown-writer`,
    `project`, `compile`, `doctor` and `repair` alike, so the nightly pass
    locked out the very steps it spawns. The table already declares the real
    exclusion rule as `PRIMARY KEY(role, scope)`; naming the agent leaves that
    rule the only one that binds and changes no schema.

    Every mainstream lock manager scopes ownership this way — an etcd lease or
    a ZooKeeper session holds many keys at once, and a PostgreSQL session
    re-acquiring its own advisory lock always succeeds. See
    `docs/research/2026-08-28-who-is-an-actor-in-a-lock.md`.
    """
    process = current_process_identity()
    agent = f"{process.pid}\x00{process.start_identity}\x00{role}\x00{scope}"
    digest = hashlib.blake2s(agent.encode("utf-8"), digest_size=16).hexdigest()
    return f"{current_actor_identity()}#agent:{digest}"


def _validate_process(identity: ProcessIdentity) -> None:
    if not isinstance(identity, ProcessIdentity):
        raise TypeError("process must be a ProcessIdentity")
    if isinstance(identity.pid, bool) or not isinstance(identity.pid, int) or identity.pid <= 0:
        raise ValueError("process pid must be a positive integer")
    _bounded_text(identity.start_identity, "process start identity", 512)


def _utf8_size(value: str, label: str) -> int:
    try:
        return len(value.encode("utf-8", errors="strict"))
    except UnicodeError as exc:
        raise ValueError(f"{label} must be valid UTF-8") from exc


def _bounded_text(value: object, label: str, maximum: int) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must be a non-empty string")
    if _utf8_size(value, label) > maximum or "\0" in value:
        raise ValueError(f"{label} exceeds its bound")
    return value


def _as_utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("ownership clock must return a timezone-aware datetime")
    return value.astimezone(timezone.utc)


def _timestamp(value: datetime) -> str:
    """Always six fractional digits, so the text order SQL compares is time order.

    `isoformat()` drops the fraction on a whole second, and `Z` sorts after `.`:
    `…:00Z` compared greater than `…:00.500000Z`. The reader takes both shapes.
    Research: docs/research/2026-09-17-a-lock-names-the-process-not-only-its-number.md
    """
    return _as_utc(value).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _parse_timestamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise OperationalOwnershipError("owner_record_invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise OperationalOwnershipError("owner_record_invalid") from exc
    return _as_utc(parsed)


def _timing(role: OwnerRole) -> tuple[int, int]:
    return (120, 40) if role in _LONG_LEASE_ROLES else (30, 10)


def _validate_role(role: object) -> OwnerRole:
    if not isinstance(role, str) or role not in _ROLES:
        raise ValueError("role is not one of the closed ownership roles")
    return cast(OwnerRole, role)


def _valid_marker_size(value: object) -> bool:
    return _is_plain_int(value) and 0 <= value <= _MAX_MARKER_BYTES


def _valid_marker_mtime(value: object) -> bool:
    return _is_plain_int(value) and value >= 0


def _require_marker_metadata(identity: RuntimeFileIdentity) -> None:
    if not _valid_marker_size(identity.size) or not _valid_marker_mtime(
        identity.mtime_ns
    ):
        raise ValueError("marker file identity has invalid metadata")


def _file_identity_value(identity: RuntimeFileIdentity) -> dict[str, object]:
    if not isinstance(identity, RuntimeFileIdentity):
        raise TypeError("marker file_identity must be a RuntimeFileIdentity")
    _bounded_text(identity.platform, "marker identity platform", 64)
    _bounded_text(identity.volume, "marker identity volume", 512)
    _bounded_text(identity.file_id, "marker identity file ID", 512)
    _require_marker_metadata(identity)
    return {
        "file_id": identity.file_id,
        "mtime_ns": identity.mtime_ns,
        "platform": identity.platform,
        "size": identity.size,
        "volume": identity.volume,
    }


def _marker_json(marker: MarkerIdentity) -> bytes:
    return canonical_json_bytes(
        {"file_identity": _file_identity_value(marker.file_identity), "pid": marker.pid}
    )


def _valid_marker_columns(path: object, digest: object, raw: object) -> bool:
    return isinstance(path, str) and isinstance(digest, str) and isinstance(raw, bytes)


def _runtime_file_identity(file_value: object) -> RuntimeFileIdentity:
    return RuntimeFileIdentity(
        platform=file_value["platform"],
        volume=file_value["volume"],
        file_id=file_value["file_id"],
        size=file_value["size"],
        mtime_ns=file_value["mtime_ns"],
    )


def _marker_from_columns(path: str, digest: str, raw: bytes) -> MarkerIdentity:
    try:
        value = json.loads(raw)
        marker = MarkerIdentity(
            relative_path=path,
            sha256=digest,
            file_identity=_runtime_file_identity(value["file_identity"]),
            pid=value["pid"],
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise OperationalOwnershipError("owner_record_invalid") from exc
    if canonical_json_bytes(value) != raw:
        raise OperationalOwnershipError("owner_record_invalid")
    return marker


def _marker_from_row(row: sqlite3.Row) -> MarkerIdentity | None:
    values = (row["marker_path"], row["marker_sha256"], row["marker_identity_json"])
    if values == (None, None, None):
        return None
    path, digest, raw = values
    if not _valid_marker_columns(path, digest, raw):
        raise OperationalOwnershipError("owner_record_invalid")
    return _marker_from_columns(path, digest, raw)


def _require_marker_given(role: OwnerRole, marker: object) -> None:
    if marker is None:
        raise ValueError(f"{role} ownership requires marker identity")


def _require_marker_presence(role: OwnerRole, marker: object) -> None:
    """A marker role must carry one; every other role must not."""
    if role in _MARKER_ROLES:
        _require_marker_given(role, marker)
        return
    if marker is not None:
        raise ValueError(f"{role} ownership does not accept marker identity")


def _require_marker_identity_fields(
    marker: MarkerIdentity, process: ProcessIdentity
) -> None:
    if re.fullmatch(r"[0-9a-f]{64}", marker.sha256) is None:
        raise ValueError("marker sha256 must be lowercase 64-hex")
    if marker.pid != process.pid:
        raise ValueError("marker pid does not match the owner process")


def _validated_marker_path(marker: object, process: ProcessIdentity) -> str:
    if not isinstance(marker, MarkerIdentity):
        raise TypeError("marker must be a MarkerIdentity")
    relative = restricted_relative_path(marker.relative_path, ("run",)).as_posix()
    if len(relative.encode("utf-8")) > 4096:
        raise ValueError("marker path exceeds its bound")
    _require_marker_identity_fields(marker, process)
    return relative


def _validated_marker_json(marker: MarkerIdentity) -> bytes:
    marker_json = _marker_json(marker)
    if len(marker_json) > _MAX_MARKER_BYTES:
        raise ValueError("marker identity exceeds its bound")
    return marker_json


def _require_marker_match(
    before: object, after: object, content: bytes, marker: MarkerIdentity
) -> None:
    if before != marker.file_identity or after != marker.file_identity:
        raise OperationalOwnershipError("marker_identity_invalid")
    if sha256_bytes(content) != marker.sha256:
        raise OperationalOwnershipError("marker_identity_invalid")


@dataclass(frozen=True)
class _AcquireRequest:
    """One validated acquisition, so the steps below share one vocabulary."""

    role: OwnerRole
    scope: str
    actor_id: str
    token: str
    process: ProcessIdentity
    marker_path: str | None
    marker_sha256: str | None
    marker_json: bytes | None


def _row_process(row: sqlite3.Row) -> ProcessIdentity:
    return ProcessIdentity(
        pid=row["process_id"], start_identity=row["process_start_identity"]
    )


def _require_lease_epoch(epoch: object) -> None:
    if not _is_plain_int(epoch) or epoch < 1:
        raise ValueError("lease epoch must be positive")


def _require_lease_timing(lease: OwnerLease, role: OwnerRole) -> None:
    if (lease.ttl_seconds, lease.heartbeat_seconds) != _timing(role):
        raise ValueError("lease timing does not match its role")


def _next_owner_epoch(database: sqlite3.Connection, request: _AcquireRequest) -> int:
    epoch_row = database.execute(
        """INSERT INTO maintenance_owner_epochs(role,scope,last_epoch)
           VALUES (?,?,1)
           ON CONFLICT(role,scope) DO UPDATE
           SET last_epoch=maintenance_owner_epochs.last_epoch+1
           RETURNING last_epoch""",
        (request.role, request.scope),
    ).fetchone()
    if epoch_row is None:
        raise OperationalOwnershipError("owner_epoch_unavailable")
    return int(epoch_row[0])


def _insert_owner_row(
    database: sqlite3.Connection,
    request: _AcquireRequest,
    epoch: int,
    now: datetime,
    expires: datetime,
) -> None:
    try:
        result = database.execute(
            """INSERT INTO maintenance_owners(
                   role,scope,actor_id,owner_token,process_id,
                   process_start_identity,fencing_epoch,acquired_at,
                   heartbeat_at,expires_at,marker_path,marker_sha256,
                   marker_identity_json
               ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                request.role,
                request.scope,
                request.actor_id,
                request.token,
                request.process.pid,
                request.process.start_identity,
                epoch,
                _timestamp(now),
                _timestamp(now),
                _timestamp(expires),
                request.marker_path,
                request.marker_sha256,
                request.marker_json,
            ),
        )
    except sqlite3.IntegrityError as exc:
        raise OperationalOwnershipError("owner_identity_conflict") from exc
    if result.rowcount != 1:
        raise OperationalOwnershipError("owner_fence_lost")


# Every table whose rows exist only while a canonical owner does, named with
# the columns carrying that owner's fencing tuple. All three hold a real
# FOREIGN KEY to `maintenance_owners`, so a reclaim that leaves one behind
# cannot delete the owner at all, and each is also counted by the
# coordinator's cross-table shape invariant. Column order is `_owner_key`.
_OWNER_PROJECTIONS = (
    (
        "intent_fences",
        (
            "canonical_role",
            "canonical_scope",
            "canonical_actor_id",
            "canonical_owner_token",
            "canonical_fencing_epoch",
        ),
    ),
    (
        "project_leases",
        (
            "canonical_role",
            "canonical_scope",
            "actor_id",
            "lease_token",
            "fencing_epoch",
        ),
    ),
    (
        "writer_owners",
        (
            "canonical_role",
            "canonical_scope",
            "actor_id",
            "owner_token",
            "fencing_epoch",
        ),
    ),
)
_OWNER_PROJECTION_DELETES = tuple(
    "DELETE FROM {} WHERE {}".format(
        table, " AND ".join(f"{column}=?" for column in columns)
    )
    for table, columns in _OWNER_PROJECTIONS
)
# A binding projection must have the fence it was projected under, so it goes
# with that fence and only with that fence - exactly the pairing that
# `release_intent_fence` performs on the ordinary path.
_OWNER_BINDING_DELETE = """DELETE FROM capture_binding_projections
   WHERE EXISTS (
       SELECT 1 FROM intent_fences AS fence
        WHERE fence.intent_id=capture_binding_projections.intent_id
          AND fence.token=capture_binding_projections.intent_fence_token
          AND fence.fencing_epoch=capture_binding_projections.intent_fence_epoch
          AND fence.canonical_role=? AND fence.canonical_scope=?
          AND fence.canonical_actor_id=? AND fence.canonical_owner_token=?
          AND fence.canonical_fencing_epoch=?
   )"""


def _owner_key(row: sqlite3.Row) -> tuple[object, ...]:
    return (
        row["role"],
        row["scope"],
        row["actor_id"],
        row["owner_token"],
        row["fencing_epoch"],
    )


def _delete_owner_projections(database: sqlite3.Connection, row: sqlite3.Row) -> None:
    """Remove the rows that exist only because this dead owner existed.

    Called only from the reclaim path, so the caller has already proved the
    lease expired and the process is gone. Every statement is scoped by the
    owner's whole fencing tuple, so it can reach nothing but rows naming this
    owner; a live owner's projection does not match and is not touched.
    """
    parameters = _owner_key(row)
    database.execute(_OWNER_BINDING_DELETE, parameters)
    for statement in _OWNER_PROJECTION_DELETES:
        database.execute(statement, parameters)


class OwnershipRegistry:
    def __init__(
        self,
        state_root: Path,
        *,
        clock: Callable[[], datetime] = utc_now,
        process_probe: Callable[[ProcessIdentity], ProcessState] = process_identity_state,
    ) -> None:
        self.state_root = Path(state_root)
        self.database_path = self.state_root / "run" / _COORDINATOR_CANDIDATE
        self._clock = clock
        self._process_probe = process_probe
        markdown_transaction.validate_coordinator_v3_database(
            self.database_path, state_root=self.state_root
        )

    @classmethod
    def _from_adopted_database(
        cls,
        state_root: Path,
        database_path: Path,
        *,
        clock: Callable[[], datetime] = utc_now,
        process_probe: Callable[[ProcessIdentity], ProcessState] = process_identity_state,
    ) -> OwnershipRegistry:
        """Open a coordinator already validated by the adoption boundary."""
        instance = cls.__new__(cls)
        instance.state_root = Path(state_root)
        instance.database_path = Path(database_path)
        instance._clock = clock
        instance._process_probe = process_probe
        with contextlib.closing(instance._connect()):
            pass
        return instance

    def _connect(self) -> sqlite3.Connection:
        return open_operational_db(
            self.database_path,
            busy_ms=DEFAULTS.markdown_busy_ms,
            contract=_COORDINATOR_CONTRACT,
        )

    def _validate_marker(
        self,
        role: OwnerRole,
        marker: MarkerIdentity | None,
        process: ProcessIdentity,
    ) -> tuple[str | None, str | None, bytes | None]:
        _require_marker_presence(role, marker)
        if marker is None:
            return None, None, None
        relative = _validated_marker_path(marker, process)
        marker_json = _validated_marker_json(marker)
        self._require_marker_file(relative, marker)
        return relative, marker.sha256, marker_json

    def _require_marker_file(self, relative: str, marker: MarkerIdentity) -> None:
        before, content, after = self._read_marker_file(self.state_root / relative)
        _require_marker_match(before, after, content, marker)

    def _read_marker_file(self, path: Path) -> tuple[object, bytes, object]:
        try:
            before = capture_runtime_file_identity(path, state_root=self.state_root)
            content = read_runtime_bytes(
                path,
                self.state_root,
                max_bytes=_MAX_MARKER_BYTES,
                owner_only=False,
            )
            after = capture_runtime_file_identity(path, state_root=self.state_root)
        except (OSError, PermissionError, ValueError) as exc:
            raise OperationalOwnershipError("marker_identity_invalid") from exc
        return before, content, after

    def _lease_marker(self, row: sqlite3.Row, process: ProcessIdentity) -> None:
        marker = _marker_from_row(row)
        role = _validate_role(row["role"])
        self._validate_marker(role, marker, process)

    def _retire_dead_marker(self, row: sqlite3.Row) -> None:
        """Remove a dead owner's marker only while it is still exactly its own.

        The proof of death is the lease and the OS, never this file. A marker
        that is gone, or that someone else has published at the same path
        since, is left alone and does not stand in the way of the reclaim.
        """
        marker = _marker_from_row(row)
        if marker is None:
            return
        path = self.state_root / marker.relative_path
        try:
            before, content, after = self._read_marker_file(path)
            _require_marker_match(before, after, content, marker)
        except OperationalOwnershipError:
            return
        _remove_marker_file(path)

    @staticmethod
    def _row_matches_lease(row: sqlite3.Row, lease: OwnerLease) -> bool:
        return OwnershipRegistry._exact_parameters(row) == (
            lease.role,
            lease.scope,
            lease.actor_id,
            lease.token,
            lease.epoch,
            lease.process.pid,
            lease.process.start_identity,
        )

    @staticmethod
    def _exact_parameters(row: sqlite3.Row) -> tuple[object, ...]:
        return (
            row["role"],
            row["scope"],
            row["actor_id"],
            row["owner_token"],
            row["fencing_epoch"],
            row["process_id"],
            row["process_start_identity"],
        )

    def _delete_row(self, database: sqlite3.Connection, row: sqlite3.Row) -> None:
        result = database.execute(
            """DELETE FROM maintenance_owners
               WHERE role=? AND scope=? AND actor_id=? AND owner_token=?
                 AND fencing_epoch=? AND process_id=? AND process_start_identity=?""",
            self._exact_parameters(row),
        )
        if result.rowcount != 1:
            raise OperationalOwnershipError("owner_fence_lost")

    def _expired_owner_is_dead(self, row: sqlite3.Row, now: datetime) -> bool:
        """Both halves of the reclaim proof: the lease lapsed and the OS agrees."""
        if _parse_timestamp(row["expires_at"]) > now:
            return False
        identity = _row_process(row)
        _validate_process(identity)
        return self._probed_state(identity) == "dead"

    def _probed_state(self, identity: ProcessIdentity) -> ProcessState:
        """Anything the probe cannot settle refuses closed, by name."""
        state = self._process_probe(identity)
        if state not in {"alive", "dead"}:
            raise OperationalOwnershipError("owner_liveness_unknown")
        return state

    def reclaimable_dead_owner(self, row: sqlite3.Row) -> bool:
        """Whether this row may be taken over, on this registry's own clock.

        The single definition of the proof, so a projection living in another
        database asks this question instead of growing a second answer to it.
        """
        return self._expired_owner_is_dead(row, _as_utc(self._clock()))

    def reclaim_dead_marker_owner(
        self, role: OwnerRole, *, scope: str, relative_path: str
    ) -> str:
        """Release the marker a dead owner left behind, with the registry's proof.

        A row for (role, scope) is reclaimed only when its lease lapsed and its
        process is provably dead; the file goes with it only while it is still
        the file the row recorded, and anyone else's file is left in place. A
        marker with no row is removed only when the PID it names no longer
        exists. A live owner refuses by name. No age is read.
        Decision: knowledge/notes/nightly-takes-the-canonical-fence-decision.md
        """
        selected_role = _validate_role(role)
        with contextlib.closing(self._connect()) as database, begin_immediate(database):
            row = database.execute(
                "SELECT * FROM maintenance_owners WHERE role=? AND scope=?",
                (selected_role, scope),
            ).fetchone()
            if row is None:
                return self._remove_orphan_marker(relative_path)
            _require_marker_path_of_row(row, relative_path)
            self._reclaim_or_refuse(database, row, _as_utc(self._clock()), "owner_busy")
            return "reclaimed"

    def _remove_orphan_marker(self, relative_path: str) -> str:
        path = self.state_root / relative_path
        owner = _marker_owner_or_torn(path, self.state_root, relative_path)
        if owner is not None and _marker_owner_alive(*owner):
            raise OperationalOwnershipError("owner_busy")
        _remove_marker_file(path)
        return "orphan_removed"

    def acquire(
        self,
        role: OwnerRole,
        *,
        scope: str,
        actor_id: str | None = None,
        token: str | None = None,
        marker: MarkerIdentity | None = None,
    ) -> OwnerLease:
        with contextlib.closing(self._connect()) as database, begin_immediate(database):
            return self._acquire_in_transaction(
                database,
                role,
                scope=scope,
                actor_id=actor_id,
                token=token,
                marker=marker,
            )

    def _acquire_in_transaction(
        self,
        database: sqlite3.Connection,
        role: OwnerRole,
        *,
        scope: str,
        actor_id: str | None = None,
        token: str | None = None,
        marker: MarkerIdentity | None = None,
    ) -> OwnerLease:
        request = self._validated_request(role, scope, actor_id, token, marker)
        now = _as_utc(self._clock())
        self._settle_deletion_check(database, request, now)
        self._require_admission(database, request, now)
        epoch = _next_owner_epoch(database, request)
        return self._insert_owner(database, request, epoch, now)

    def _validated_request(
        self,
        role: OwnerRole,
        scope: str,
        actor_id: str | None,
        token: str | None,
        marker: MarkerIdentity | None,
    ) -> _AcquireRequest:
        selected_role = _validate_role(role)
        selected_scope = _bounded_text(scope, "scope", 512)
        selected_actor = _bounded_text(
            ownership_actor_identity(selected_role, selected_scope)
            if actor_id is None
            else actor_id,
            "actor_id",
            256,
        )
        selected_token = _bounded_text(
            secrets.token_hex(16) if token is None else token,
            "token",
            256,
        )
        process = current_process_identity()
        _validate_process(process)
        marker_path, marker_sha256, marker_json = self._validate_marker(
            selected_role, marker, process
        )
        return _AcquireRequest(
            role=selected_role,
            scope=selected_scope,
            actor_id=selected_actor,
            token=selected_token,
            process=process,
            marker_path=marker_path,
            marker_sha256=marker_sha256,
            marker_json=marker_json,
        )

    def _settle_deletion_check(
        self, database: sqlite3.Connection, request: _AcquireRequest, now: datetime
    ) -> None:
        """The deletion permit blocks everyone else, and quiescence blocks it."""
        if request.role == "runtime-deletion-check":
            self._require_quiescence(database, request, now)
            return
        deletion = database.execute(
            "SELECT * FROM maintenance_owners WHERE role='runtime-deletion-check'"
        ).fetchone()
        if deletion is not None:
            self._reclaim_or_refuse(
                database, deletion, now, "runtime_deletion_check_active"
            )

    def _require_quiescence(
        self, database: sqlite3.Connection, request: _AcquireRequest, now: datetime
    ) -> None:
        """Every other owner must be gone; a provably dead one is reclaimed, not obeyed."""
        others = database.execute(
            "SELECT * FROM maintenance_owners WHERE NOT (role=? AND scope=?)",
            (request.role, request.scope),
        ).fetchall()
        for other in others:
            self._reclaim_or_refuse(
                database, other, now, "runtime_deletion_check_requires_quiescence"
            )

    def _require_admission(
        self, database: sqlite3.Connection, request: _AcquireRequest, now: datetime
    ) -> None:
        """Nobody else may hold this (role, scope), and nobody else this actor.

        `actor_id` is UNIQUE and, on POSIX, one row per user: a dead row under
        any other role blocks this one just as hard, so it needs the same
        consultation rather than an INSERT that trips the constraint.
        """
        existing = database.execute(
            "SELECT * FROM maintenance_owners WHERE role=? AND scope=?",
            (request.role, request.scope),
        ).fetchone()
        if existing is not None:
            self._reclaim_or_refuse(database, existing, now, "owner_busy")
        conflict = database.execute(
            """SELECT * FROM maintenance_owners
               WHERE actor_id=? AND NOT (role=? AND scope=?)""",
            (request.actor_id, request.role, request.scope),
        ).fetchone()
        if conflict is not None:
            self._reclaim_or_refuse(database, conflict, now, "owner_identity_conflict")

    def _reclaim_or_refuse(
        self,
        database: sqlite3.Connection,
        row: sqlite3.Row,
        now: datetime,
        refusal: str,
    ) -> None:
        """Take over a provably dead owner, or refuse by name — doubt refuses."""
        if not self._expired_owner_is_dead(row, now):
            raise OperationalOwnershipError(refusal)
        self._retire_dead_marker(row)
        _delete_owner_projections(database, row)
        self._delete_row(database, row)

    def _insert_owner(
        self,
        database: sqlite3.Connection,
        request: _AcquireRequest,
        epoch: int,
        now: datetime,
    ) -> OwnerLease:
        ttl_seconds, heartbeat_seconds = _timing(request.role)
        expires = now + timedelta(seconds=ttl_seconds)
        _insert_owner_row(database, request, epoch, now, expires)
        return OwnerLease(
            state_root=self.state_root,
            role=request.role,
            scope=request.scope,
            actor_id=request.actor_id,
            token=request.token,
            epoch=epoch,
            process=request.process,
            acquired_at=now,
            heartbeat_at=now,
            expires_at=expires,
            ttl_seconds=ttl_seconds,
            heartbeat_seconds=heartbeat_seconds,
        )

    def heartbeat(self, lease: OwnerLease) -> OwnerLease:
        with contextlib.closing(self._connect()) as database, begin_immediate(database):
            return self._heartbeat_in_transaction(database, lease)

    def _heartbeat_in_transaction(
        self, database: sqlite3.Connection, lease: OwnerLease
    ) -> OwnerLease:
        self._validate_lease(lease)
        now = _as_utc(self._clock())
        expires = now + timedelta(seconds=lease.ttl_seconds)
        row = database.execute(
            "SELECT * FROM maintenance_owners WHERE role=? AND scope=?",
            (lease.role, lease.scope),
        ).fetchone()
        if row is None or not self._row_matches_lease(row, lease):
            raise OperationalOwnershipError("owner_fence_lost")
        self._lease_marker(row, lease.process)
        result = database.execute(
            """UPDATE maintenance_owners SET heartbeat_at=?,expires_at=?
               WHERE role=? AND scope=? AND actor_id=? AND owner_token=?
                 AND fencing_epoch=? AND process_id=?
                 AND process_start_identity=? AND expires_at>?""",
            (
                _timestamp(now),
                _timestamp(expires),
                lease.role,
                lease.scope,
                lease.actor_id,
                lease.token,
                lease.epoch,
                lease.process.pid,
                lease.process.start_identity,
                _timestamp(now),
            ),
        )
        if result.rowcount != 1:
            raise OperationalOwnershipError("owner_fence_lost")
        return replace(lease, heartbeat_at=now, expires_at=expires)

    def require(self, database: sqlite3.Connection, lease: OwnerLease) -> None:
        self._validate_lease(lease)
        now = _as_utc(self._clock())
        cursor = database.execute(
            """SELECT * FROM maintenance_owners
               WHERE role=? AND scope=? AND actor_id=? AND owner_token=?
                 AND fencing_epoch=? AND process_id=?
                 AND process_start_identity=? AND expires_at>?""",
            (
                lease.role,
                lease.scope,
                lease.actor_id,
                lease.token,
                lease.epoch,
                lease.process.pid,
                lease.process.start_identity,
                _timestamp(now),
            ),
        )
        row = cursor.fetchone()
        if row is None:
            raise OperationalOwnershipError("owner_fence_lost")
        if not isinstance(row, sqlite3.Row):
            row = sqlite3.Row(cursor, row)
        self._lease_marker(row, lease.process)

    def release(self, lease: OwnerLease) -> None:
        with contextlib.closing(self._connect()) as database, begin_immediate(database):
            self._release_in_transaction(database, lease)

    def _release_in_transaction(
        self, database: sqlite3.Connection, lease: OwnerLease
    ) -> None:
        self._validate_lease(lease)
        row = database.execute(
            "SELECT * FROM maintenance_owners WHERE role=? AND scope=?",
            (lease.role, lease.scope),
        ).fetchone()
        if row is None or not self._row_matches_lease(row, lease):
            raise OperationalOwnershipError("owner_fence_lost")
        self._lease_marker(row, lease.process)
        result = database.execute(
            """DELETE FROM maintenance_owners
               WHERE role=? AND scope=? AND actor_id=? AND owner_token=?
                 AND fencing_epoch=? AND process_id=?
                 AND process_start_identity=?""",
            (
                lease.role,
                lease.scope,
                lease.actor_id,
                lease.token,
                lease.epoch,
                lease.process.pid,
                lease.process.start_identity,
            ),
        )
        if result.rowcount != 1:
            raise OperationalOwnershipError("owner_fence_lost")

    def _require_lease_root(self, lease: object) -> None:
        if not isinstance(lease, OwnerLease):
            raise TypeError("lease must be an OwnerLease")
        if Path(lease.state_root) != self.state_root:
            raise ValueError("lease belongs to a different state root")

    def _validate_lease(self, lease: OwnerLease) -> None:
        self._require_lease_root(lease)
        role = _validate_role(lease.role)
        _bounded_text(lease.scope, "scope", 512)
        _bounded_text(lease.actor_id, "actor_id", 256)
        _bounded_text(lease.token, "token", 256)
        _validate_process(lease.process)
        _require_lease_epoch(lease.epoch)
        _require_lease_timing(lease, role)
        for value in (lease.acquired_at, lease.heartbeat_at, lease.expires_at):
            _as_utc(value)


def _publish_marker(state_root: Path, relative_path: str, payload: bytes) -> MarkerIdentity:
    """Write the marker whole beside its name, then link it in: never torn, never replaced.

    The marker used to be created empty and filled afterwards, so a crash in
    between left a file no reader could parse, and every later pass refused on
    it (audit 2026-09-26 B-21). `os.link` fails when the name exists, which is
    the exclusivity `O_EXCL` gave. Research:
    docs/research/2026-09-26-a-marker-is-published-whole.md
    """
    path = Path(state_root) / restricted_relative_path(relative_path, ("run",))
    path.parent.mkdir(parents=True, exist_ok=True)
    staged = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp")
    _write_whole(staged, payload)
    try:
        os.link(staged, path)
    finally:
        staged.unlink(missing_ok=True)
    return MarkerIdentity(
        relative_path=relative_path,
        sha256=sha256_bytes(payload),
        file_identity=capture_runtime_file_identity(path, state_root=Path(state_root)),
        pid=os.getpid(),
    )


def _write_whole(path: Path, payload: bytes) -> None:
    descriptor = os.open(
        path,
        os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0),
        0o600,
    )
    try:
        offset = 0
        while offset < len(payload):
            written = os.write(descriptor, payload[offset:])
            if written <= 0:
                raise OSError("marker write made no progress")
            offset += written
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _require_marker_path_of_row(row: sqlite3.Row, relative_path: str) -> None:
    marker = _marker_from_row(row)
    if marker is None or marker.relative_path != relative_path:
        raise OperationalOwnershipError("marker_identity_invalid")


# Markers only `_publish_marker` writes, which a live writer never leaves torn.
# `run/compile.pid` is not one: the pre-registry compile lock writes it in place.
_WHOLE_MARKERS = frozenset({"run/maintenance.lock"})


def _marker_owner_or_torn(
    path: Path, state_root: Path, relative_path: str
) -> tuple[int, str] | None:
    """The owner an ownerless marker names, or None for a torn one nobody can still be writing."""
    try:
        return _marker_owner(path, state_root)
    except OperationalOwnershipError:
        if relative_path in _WHOLE_MARKERS and _marker_is_torn(path, state_root):
            return None
        raise


def _marker_is_torn(path: Path, state_root: Path) -> bool:
    """Readable bytes that name no PID; an unreadable file is not proof of anything."""
    try:
        read_runtime_bytes(path, state_root, max_bytes=_MAX_MARKER_BYTES, owner_only=False)
    except (OSError, ValueError):
        return False
    return True


def _marker_owner(path: Path, state_root: Path) -> tuple[int, str]:
    """The PID and start identity an ownerless marker names; anything else refuses by name.

    A marker written before 2026-09-17 has one line and names no identity.
    """
    try:
        payload = read_runtime_bytes(
            path, state_root, max_bytes=_MAX_MARKER_BYTES, owner_only=False
        )
        lines = [line.decode("ascii").strip() for line in payload.splitlines()]
        return int(lines[0]), (lines[1:] or [""])[0]
    except (OSError, ValueError, IndexError, UnicodeDecodeError) as exc:
        raise OperationalOwnershipError("marker_identity_invalid") from exc


def _marker_payload() -> bytes:
    """What a scheduled marker records: this PID, and this process.

    The registry's own rows have carried the start identity all along; the
    marker beside them named only a number, which the next process to be given
    that number would have inherited (audit Q-L7). Research:
    docs/research/2026-09-17-a-lock-names-the-process-not-only-its-number.md
    """
    return f"{os.getpid()}\n{_own_start_identity()}\n".encode("ascii", errors="replace")


def _own_start_identity() -> str:
    try:
        return process_start_identity(os.getpid()) or ""
    except (OperationalOwnershipError, OSError, ValueError):
        return ""


def _marker_owner_alive(pid: int, identity: str) -> bool:
    """Whether the process a marker names still runs; doubt refuses by name.

    With the identity the marker recorded, a PID handed to another process reads
    as dead, as the registry rows and the other lock files already read it
    (audit 2026-09-26 C-12). Research:
    docs/research/2026-09-26-a-marker-is-judged-by-the-process-it-names.md
    """
    try:
        observed = process_start_identity(pid)
    except (OSError, PermissionError) as exc:
        raise OperationalOwnershipError("owner_liveness_unknown") from exc
    if observed is None:
        return False
    return not identity or observed == identity


def _remove_marker_file(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise OperationalOwnershipError("marker_not_removed") from exc


def _publish_marker_reclaiming(
    state_root: Path,
    relative_path: str,
    payload: bytes,
    *,
    registry: OwnershipRegistry,
    role: OwnerRole,
    scope: str,
) -> MarkerIdentity:
    """Publish a marker; a marker left by a dead owner is reclaimed first."""
    try:
        return _publish_marker(state_root, relative_path, payload)
    except FileExistsError:
        registry.reclaim_dead_marker_owner(role, scope=scope, relative_path=relative_path)
    try:
        return _publish_marker(state_root, relative_path, payload)
    except FileExistsError as exc:
        raise OperationalOwnershipError("owner_busy") from exc


def _remove_exact_marker(state_root: Path, marker: MarkerIdentity) -> None:
    path = Path(state_root) / marker.relative_path
    try:
        identity = capture_runtime_file_identity(path, state_root=Path(state_root))
        payload = read_runtime_bytes(
            path, Path(state_root), max_bytes=_MAX_MARKER_BYTES, owner_only=False
        )
    except FileNotFoundError:
        return
    if (
        identity != marker.file_identity
        or sha256_bytes(payload) != marker.sha256
        or marker.pid != os.getpid()
    ):
        raise OperationalOwnershipError("marker_identity_invalid")
    path.unlink()


def adopted_ownership_registry(vault: Path, state_root: Path) -> OwnershipRegistry | None:
    """The adopted coordinator's registry, or None on a vault without V3 adoption.

    The same rule the doctor applies: an adopted vault holds its owners in the
    active `markdown-transactions-v3.sqlite3`, not in the pre-adoption candidate.
    """
    coordinator = markdown_transaction.active_or_legacy_coordinator(
        Path(vault), Path(state_root)
    )
    if getattr(coordinator, "_database_contract", None) is None:
        return None
    return coordinator._ownership_registry()  # noqa: SLF001 - the coordinator's own rule


def _pre_adoption_registry(
    state_root: Path, *, clock: Callable[[], datetime] = utc_now
) -> OwnershipRegistry:
    """The candidate registry, which exists only before adoption.

    An adopted vault's registry lives on its adopted coordinator and must be
    passed in; opening the candidate there reads a database that is not the
    vault's (audit C-14,
    docs/research/2026-09-25-an-owner-helper-refuses-the-candidate-on-an-adopted-vault.md).
    """
    if markdown_transaction._reliability_v3_records_present(state_root):
        raise OperationalOwnershipError(
            "adopted_registry_required",
            "an adopted vault's ownership registry must be passed in",
        )
    return OwnershipRegistry(state_root, clock=clock)


def acquire_compile_owner(*, state_root: Path) -> tuple[OwnerLease, MarkerIdentity]:
    now = utc_now().replace(microsecond=0)
    actor_id = ownership_actor_identity("compile", "global")
    token = secrets.token_hex(16)
    payload = (
        f"{os.getpid()}\n{_timestamp(now)}\n{token}\n".encode("ascii", errors="strict")
    )
    marker = _publish_marker(Path(state_root), "run/compile.pid", payload)
    registry = _pre_adoption_registry(Path(state_root), clock=lambda: now)
    try:
        lease = registry.acquire(
            "compile",
            scope="global",
            actor_id=actor_id,
            token=token,
            marker=marker,
        )
    except BaseException:
        _remove_exact_marker(Path(state_root), marker)
        raise
    return lease, marker


def acquire_scheduled_owner(
    role: Literal["nightly", "weekly"],
    *,
    state_root: Path,
    registry: OwnershipRegistry | None = None,
) -> tuple[OwnerLease, MarkerIdentity]:
    """Take the nightly or weekly fence; `registry` is the adopted vault's own.

    Without one the candidate database is used, as before adoption. A marker
    a dead owner left behind is reclaimed with the registry's proof first.
    """
    if role not in {"nightly", "weekly"}:
        raise ValueError("scheduled owner role must be nightly or weekly")
    now = utc_now().replace(microsecond=0)
    actor_id = ownership_actor_identity(role, "global")
    token = secrets.token_hex(16)
    payload = _marker_payload()
    if registry is None:
        registry = _pre_adoption_registry(Path(state_root), clock=lambda: now)
    marker = _publish_marker_reclaiming(
        Path(state_root),
        "run/maintenance.lock",
        payload,
        registry=registry,
        role=role,
        scope="global",
    )
    try:
        lease = registry.acquire(
            role,
            scope="global",
            actor_id=actor_id,
            token=token,
            marker=marker,
        )
    except BaseException:
        _remove_exact_marker(Path(state_root), marker)
        raise
    return lease, marker


def _wait_for_owner_heartbeat(stop: threading.Event, seconds: int) -> bool:
    return stop.wait(seconds)


def _join_owner_heartbeat(thread: threading.Thread, timeout: float) -> None:
    thread.join(timeout=timeout)


def _require_heartbeat_stopped(thread: threading.Thread) -> None:
    if thread.is_alive():
        raise OperationalOwnershipError("owner_heartbeat_stop_timeout")


def _require_heartbeat_finished(
    thread: threading.Thread,
    failure: list[BaseException],
    body_error: BaseException | None,
) -> None:
    """A body that already failed keeps its own error; otherwise the heartbeat's."""
    if body_error is not None:
        return
    _require_heartbeat_stopped(thread)
    if failure:
        raise failure[0]


@contextlib.contextmanager
def heartbeat_owner(
    lease: OwnerLease,
    *,
    registry: OwnershipRegistry | None = None,
    lost: threading.Event | None = None,
) -> Iterator[OwnerLease]:
    """Refresh the lease until the body returns.

    `lost` is set the moment a refresh fails, so a body that checks it between
    steps stops while the loss is fresh instead of learning of it in `finally`
    (client-go's `OnStoppedLeading`; the doctor's own heartbeat does the same).
    """
    if registry is None:
        registry = _pre_adoption_registry(Path(lease.state_root))
    stop = threading.Event()
    failure: list[BaseException] = []
    # The lease is already written; its expiry is counted from here.
    held_since = time.monotonic()

    def heartbeat() -> None:
        # A busy database is retried until the lease expires; a lost fence is
        # final at once. See `docs/research/2026-09-14-a-busy-database-is-not-a-lost-lease.md`.
        from lease_renewal import renew_until_stopped

        ended = renew_until_stopped(
            lambda: registry.heartbeat(lease),
            interval=lease.heartbeat_seconds,
            lease_seconds=lease.ttl_seconds,
            attempt_seconds=DEFAULTS.markdown_busy_ms / 1_000,
            held_since=held_since,
            stop=stop,
            wait=lambda seconds: _wait_for_owner_heartbeat(stop, seconds),
        )
        if ended is not None:
            failure.append(ended)
            _signal_lost(lost)

    thread = threading.Thread(
        target=heartbeat,
        name=f"{lease.role}-owner-heartbeat",
        daemon=True,
    )
    thread.start()
    body_error: BaseException | None = None
    try:
        yield lease
    except BaseException as exc:
        body_error = exc
        raise
    finally:
        stop.set()
        _join_owner_heartbeat(thread, lease.heartbeat_seconds * 2)
        _require_heartbeat_finished(thread, failure, body_error)


def _signal_lost(lost: threading.Event | None) -> None:
    if lost is not None:
        lost.set()


def current_owner_lease(
    lease: OwnerLease, *, registry: OwnershipRegistry | None = None
) -> OwnerLease:
    if registry is None:
        registry = _pre_adoption_registry(Path(lease.state_root))
    with contextlib.closing(registry._connect()) as database:
        row = database.execute(
            """SELECT * FROM maintenance_owners
               WHERE role=? AND scope=? AND actor_id=? AND owner_token=?
                 AND fencing_epoch=? AND process_id=?
                 AND process_start_identity=?""",
            (
                lease.role,
                lease.scope,
                lease.actor_id,
                lease.token,
                lease.epoch,
                lease.process.pid,
                lease.process.start_identity,
            ),
        ).fetchone()
    if row is None:
        raise OperationalOwnershipError("owner_fence_lost")
    return replace(
        lease,
        acquired_at=_parse_timestamp(row["acquired_at"]),
        heartbeat_at=_parse_timestamp(row["heartbeat_at"]),
        expires_at=_parse_timestamp(row["expires_at"]),
    )


def release_marker_owner(
    lease: OwnerLease,
    marker: MarkerIdentity,
    *,
    registry: OwnershipRegistry | None = None,
) -> None:
    if registry is None:
        registry = _pre_adoption_registry(Path(lease.state_root))
    current = current_owner_lease(lease, registry=registry)
    registry.release(current)
    _remove_exact_marker(Path(lease.state_root), marker)
