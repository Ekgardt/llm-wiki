"""Doctor's checks of language-server owners, leases, failures and Pyright."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
import stat
from collections.abc import Iterator, Sequence
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import MappingProxyType
from typing import Any, NamedTuple

import generation_maintenance  # noqa: E402
import process_liveness
import reliable_memory
from llm_wiki import monotonic_deadline
from llm_wiki.doctor_queue import archive_check
from llm_wiki.doctor_transactions import stop_at
from llm_wiki.formats.canonical import canonical_json_bytes
from reliable_memory import (
    open_readonly_operational_db,
    streamed_rows,
)
from secret_redact import describe_error


def derived_deletion_codes(check: dict) -> list[str]:
    """Deletion codes a check contributes, including its own unreadable state."""
    details = check.get("details", {})
    codes = [str(code) for code in details.get("deletion_codes", [])]
    if codes or not details.get("read_error"):
        return codes
    return [f"{check['id']}_state_unreadable"]


def _snapshot_deletion_codes(
    root_path: Path,
    state_path: Path,
    now: datetime,
    snapshot_deadline: float,
    owner: object,
    validate_reliability_v3_runtime,
) -> list[str]:
    codes = list(
        validate_reliability_v3_runtime(
            root=root_path,
            state_root=state_path,
            now=now,
            deadline=snapshot_deadline,
            excluded_owner=owner,
        )
    )
    for check in (
        archive_check(root_path, state_path, snapshot_deadline),
        lsp_runtime_check(state_path, now, deadline=snapshot_deadline),
    ):
        codes.extend(derived_deletion_codes(check))
    return codes


def _observed_deletion_codes(
    root_path: Path,
    state_path: Path,
    now: datetime,
    snapshot_deadline: float,
    owner: object,
    validate_reliability_v3_runtime,
) -> list[str]:
    codes = _snapshot_deletion_codes(
        root_path,
        state_path,
        now,
        snapshot_deadline,
        owner,
        validate_reliability_v3_runtime,
    )
    if monotonic_deadline.deadline_reached(snapshot_deadline):
        codes.append("run_deletion_state_unknown")
    return codes


class _RuntimeObservationStamp(NamedTuple):
    database_identity: tuple[str, str, str]
    epochs_digest: bytes
    other_owner: bool


def observation_database_identity(path: Path, state_root: Path) -> tuple[str, str, str]:
    identity = reliable_memory.capture_operational_database_identity(path, state_root=state_root)
    return identity.platform, identity.volume, identity.file_id


def _owner_epoch_bytes(row: sqlite3.Row) -> bytes:
    from operational_ownership import require_lease_epoch, validate_role

    validate_role(row["role"])
    require_lease_epoch(row["last_epoch"])
    return canonical_json_bytes(list(row))


def _observation_epoch_digest(database: sqlite3.Connection, deadline: float) -> bytes:
    digest = hashlib.sha256()
    rows = streamed_rows(
        database,
        "SELECT role,scope,last_epoch FROM maintenance_owner_epochs ORDER BY role,scope",
        stop=lambda: stop_at(deadline),
    )
    for row in rows:
        digest.update(_owner_epoch_bytes(row))
    return digest.digest()


def _observation_other_owner(database: sqlite3.Connection, owner, deadline: float) -> bool:
    from reliability_v3_validation import (
        owner_matches,
    )

    rows = streamed_rows(database, "SELECT * FROM maintenance_owners", stop=lambda: stop_at(deadline))
    return any(not owner_matches(row, owner) for row in rows)


def _require_observation_database(expected: tuple, actual: tuple) -> None:
    if actual != expected:
        raise ValueError("runtime observation database changed")


def _runtime_observation_stamp(registry, owner, state_root: Path, deadline: float) -> _RuntimeObservationStamp:
    from markdown_transaction import COORDINATOR_V3_CONTRACT

    stop_at(deadline)
    path = registry.database_path
    identity = observation_database_identity(path, state_root)
    with closing(open_readonly_operational_db(
        path, state_root, max_bytes=None,
        owner_only=True, contract=COORDINATOR_V3_CONTRACT,
    )) as database:
        # One is SQLite's minimum callback interval, not a work/count ceiling.
        database.set_progress_handler(lambda: monotonic_deadline.deadline_reached(deadline), 1)
        database.execute("BEGIN")
        registry.require(database, owner)
        other_owner = _observation_other_owner(database, owner, deadline)
        epochs_digest = _observation_epoch_digest(database, deadline)
        _require_observation_database(identity, observation_database_identity(path, state_root))
    stop_at(deadline)
    return _RuntimeObservationStamp(identity, epochs_digest, other_owner)


def validated_observation_codes(registry, root_path, state_path, now, deadline, owner, validate) -> list[str]:
    before = _runtime_observation_stamp(registry, owner, state_path, deadline)
    if before.other_owner:
        return ["runtime_deletion_check_requires_quiescence"]
    codes = _observed_deletion_codes(root_path, state_path, now, deadline, owner, validate)
    after = _runtime_observation_stamp(registry, owner, state_path, deadline)
    if before != after:
        codes.append("runtime_deletion_snapshot_changed")
    return codes


LSP_FAILURE_RETENTION = timedelta(days=7)


# doctor reads at most this many LSP owner roots; more is reported as an unreadable,
# truncated LSP state, never as a partial healthy one. Normal is far below it: one live
# owner per MCP process and at most `retire_lsp_evidence.KEEP_NEWEST` (20) kept failures.
MAX_LSP_OWNER_ROWS = 128
_LSP_OWNER_NONCE = re.compile(r"[0-9a-f]{32}\Z")
_LSP_TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{6})?Z\Z")
_LSP_OWNER_FIELDS = {
    "command_basename",
    "generation_nonce",
    "owner_nonce",
    "owner_pid",
    "started_at",
    "state",
}


# Written since 2026-09-25 when the platform can name a process start (audit C-39).
_LSP_OWNER_OPTIONAL_FIELDS = {"owner_start_identity"}
_LSP_LEASE_OPTIONAL_FIELDS = {"manager_start_identity", "server_start_identity"}


# Use the writer's canonical bound, including scoped Linux identities.
_LSP_START_IDENTITY_CHARS = process_liveness.MAX_START_IDENTITY_CHARS
_LSP_LEASE_FIELDS = {
    "expires_at",
    "generation_nonce",
    "heartbeat_at",
    "manager_pid",
    "owner_nonce",
    "schema_version",
    "server_pid",
    "state",
}
_LSP_FAILURE_FIELDS = {"code", "generation_nonce", "owner_nonce", "timestamp"}


# `stderr_tail` is the redacted last kilobyte the failed server wrote, bounded
# by its JSON encoding in `lsp_process._STDERR_TAIL_BYTES`.
_LSP_FAILURE_TAIL_CHARS = 1024
_LSP_FAILURE_KNOWN_FIELDS = _LSP_FAILURE_FIELDS | {"server_pid", "stderr_tail"}
LSP_OWNER_ENTRY_NAMES = {"cancellation", "failure.json", "lease.json", "owner.json"}
_LSP_RECORD_NAMES = {"failure.json", "lease.json", "owner.json"}


def _lsp_positive_pid(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _parse_lsp_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or _LSP_TIMESTAMP.fullmatch(value) is None:
        return None
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        return None
    return parsed if _lsp_timestamp_round_trips(parsed, value) else None


def _lsp_timestamp_round_trips(parsed: datetime, value: str) -> bool:
    if parsed.tzinfo != UTC:
        return False
    return parsed.isoformat().replace("+00:00", "Z") == value


def _valid_lsp_command(command: object) -> bool:
    if not isinstance(command, str) or not 0 < len(command) <= 255:
        return False
    return not any(character in command for character in "/\\\x00\r\n")


def _valid_lsp_nonces(record: dict[str, Any], owner_nonce: str) -> bool:
    if record.get("owner_nonce") != owner_nonce:
        return False
    generation = record.get("generation_nonce")
    if not isinstance(generation, str):
        return False
    return _LSP_OWNER_NONCE.fullmatch(generation) is not None


def _valid_lsp_owner_process(record: dict[str, Any]) -> bool:
    if not _lsp_positive_pid(record.get("owner_pid")):
        return False
    if _parse_lsp_timestamp(record.get("started_at")) is None:
        return False
    return record.get("state") == "process_running"


def _lsp_fields_valid(record: dict[str, Any], required: set, optional: set) -> bool:
    """The required fields, and optional start identities that are bounded text."""
    if not required <= set(record) <= required | optional:
        return False
    return all(lsp_start_identity(record[name]) for name in optional & set(record))


def lsp_start_identity(value: object) -> bool:
    return isinstance(value, str) and 0 < len(value) <= _LSP_START_IDENTITY_CHARS


def _valid_lsp_owner(record: dict[str, Any], owner_nonce: str) -> bool:
    if not (
        _lsp_fields_valid(record, _LSP_OWNER_FIELDS, _LSP_OWNER_OPTIONAL_FIELDS)
        and _valid_lsp_command(record.get("command_basename"))
        and _valid_lsp_nonces(record, owner_nonce)
    ):
        return False
    return _valid_lsp_owner_process(record)


def _lsp_schema_version_one(record: dict[str, Any]) -> bool:
    version = record.get("schema_version")
    if isinstance(version, bool) or not isinstance(version, int):
        return False
    return version == 1


def _lsp_generation_nonce_valid(record: dict[str, Any]) -> bool:
    nonce = record.get("generation_nonce")
    if not isinstance(nonce, str):
        return False
    return _LSP_OWNER_NONCE.fullmatch(nonce) is not None


def _lsp_lease_window_valid(record: dict[str, Any]) -> bool:
    heartbeat = _parse_lsp_timestamp(record.get("heartbeat_at"))
    expires = _parse_lsp_timestamp(record.get("expires_at"))
    if heartbeat is None or expires is None:
        return False
    return heartbeat < expires


def _lsp_lease_shape_valid(record: dict[str, Any]) -> bool:
    if not _lsp_fields_valid(record, _LSP_LEASE_FIELDS, _LSP_LEASE_OPTIONAL_FIELDS):
        return False
    return _lsp_schema_version_one(record)


def _lsp_lease_identity_valid(record: dict[str, Any], owner_nonce: str) -> bool:
    if not _lsp_lease_shape_valid(record):
        return False
    if record.get("owner_nonce") != owner_nonce:
        return False
    return _lsp_generation_nonce_valid(record)


def _lsp_lease_process_state(record: dict[str, Any]) -> bool:
    """Both lease pids are positive and the lease still calls itself live."""
    return (
        _lsp_positive_pid(record.get("manager_pid"))
        and _lsp_positive_pid(record.get("server_pid"))
        and record.get("state") == "live"
    )


def _valid_lsp_lease(record: dict[str, Any], owner_nonce: str) -> bool:
    if not _lsp_lease_identity_valid(record, owner_nonce):
        return False
    if not _lsp_lease_process_state(record):
        return False
    return _lsp_lease_window_valid(record)


def _valid_lsp_failure_code(code: object) -> bool:
    if not isinstance(code, str):
        return False
    return re.fullmatch(r"[a-z0-9_]{1,64}", code) is not None


def _valid_lsp_failure_pid(record: dict[str, Any]) -> bool:
    if "server_pid" not in record:
        return True
    return _lsp_positive_pid(record.get("server_pid"))


def _valid_lsp_failure_evidence(record: dict[str, Any], owner_nonce: str) -> bool:
    """The failure code, nonces and timestamp are each present and well formed."""
    return (
        _valid_lsp_failure_code(record.get("code"))
        and _valid_lsp_nonces(record, owner_nonce)
        and _parse_lsp_timestamp(record.get("timestamp")) is not None
    )


def _valid_lsp_failure_tail(record: dict[str, Any]) -> bool:
    """The redacted last words of the failed server, if the writer kept any."""
    if "stderr_tail" not in record:
        return True
    tail = record["stderr_tail"]
    return isinstance(tail, str) and 0 < len(tail) <= _LSP_FAILURE_TAIL_CHARS


def _valid_lsp_failure_shape(record: dict[str, Any]) -> bool:
    """Every required field, and nothing this reader does not know."""
    names = set(record)
    return _LSP_FAILURE_FIELDS <= names and names <= _LSP_FAILURE_KNOWN_FIELDS


def _valid_lsp_failure(record: dict[str, Any], owner_nonce: str) -> bool:
    if not _valid_lsp_failure_shape(record) or not _valid_lsp_failure_tail(record):
        return False
    if not _valid_lsp_failure_evidence(record, owner_nonce):
        return False
    return _valid_lsp_failure_pid(record)


# What actually fixes each way Pyright can fail to qualify. Reinstalling is the
# answer only when the installation itself is wrong; telling an operator to
# reinstall because the repository declares no Pyright settings sends them to
# a command that cannot change the outcome.
_PYRIGHT_ACTIONS = MappingProxyType(
    {
        "pyright_repository_config_ancestor_search": (
            "declare [tool.pyright] in the repository's pyproject.toml so "
            "settings are not read from outside the checkout"
        ),
        "pyright_repository_config_malformed": (
            "fix the repository's [tool.pyright] settings"
        ),
        "pyright_repository_config_too_deep": (
            "flatten the repository's [tool.pyright] settings"
        ),
        # Pyright starts only a regular `node` file on a local path whose parent
        # directories are real directories: a symbolic link or a network path is
        # refused, so what runs is the file that was checked. Named here because the
        # code alone left the owner nothing to act on (2026-09-28).
        "pyright_node_executable_unsafe": (
            "the first `node` on PATH is a symbolic link, sits under a linked or "
            "network directory, or is not a regular file; put a regular node binary "
            "on a local path first on PATH"
        ),
    }
)
_PYRIGHT_INSTALL_ACTION = (
    "uv run python scripts/install_pyright.py --state-root <state-root>"
)


def pyright_recommended_action(codes: Sequence[str]) -> str:
    """The one thing that would change this outcome."""
    for code in codes:
        action = _PYRIGHT_ACTIONS.get(code)
        if action is not None:
            return action
    return _PYRIGHT_INSTALL_ACTION


def pyright_check(
    root: Path,
    state_root: Path,
    *,
    deadline: float,
) -> dict:
    """Report pinned Pyright identity without network access or mutation."""
    from pyright_profile import discover_pyright
    from repository_scope import resolve_repository_scope

    codes: list[str] = []
    details: dict[str, Any] = {
        "status": "qualified",
        "source": None,
        "version": None,
        "node_major": None,
        "node_version": None,
        "package_sha256": None,
        "executable_sha256": None,
        "initialization_options_sha256": None,
        "configuration_sha256": None,
        "qualified": False,
        "codes": codes,
    }
    api_deadline = None if math.isinf(deadline) else deadline
    try:
        scope = resolve_repository_scope(root, deadline=api_deadline)
        identity = discover_pyright(
            scope,
            state_root=state_root,
            deadline=api_deadline,
        )
    except TimeoutError:
        details["status"] = "timeout"
        codes.append("pyright_timeout")
        return generation_maintenance.result(
            "pyright",
            "degraded",
            "Pyright discovery did not complete before the deadline.",
            details,
        )
    except Exception as exc:  # noqa: BLE001 - a health check names the cause and goes on
        details["status"] = "unsafe"
        details["error"] = describe_error(exc)
        codes.append("pyright_unsafe")
        return generation_maintenance.result(
            "pyright",
            "degraded",
            "Pyright discovery could not safely inspect the repository.",
            details,
        )
    details.update(
        {
            "status": identity.status,
            "source": identity.source,
            "version": identity.version,
            "node_major": identity.node_major,
            "node_version": identity.node_version,
            "package_sha256": identity.package_sha256,
            "executable_sha256": identity.executable_sha256,
            "initialization_options_sha256": identity.initialization_options_sha256,
            "configuration_sha256": identity.configuration_sha256,
            "qualified": identity.qualified,
        }
    )
    details["executable_sha256_present"] = identity.executable_sha256 is not None
    if identity.qualified:
        return generation_maintenance.result("pyright", "ok", "Pyright identity is qualified.", details)
    return unqualified_pyright_result(identity, details, codes)


def unqualified_pyright_result(identity, details: dict, codes: list[str]) -> dict:
    """Not installed at all is an optional feature not taken; anything else degrades."""
    _record_pyright_degradation(identity, details, codes)
    details["recommended_action"] = pyright_recommended_action(codes)
    if tuple(identity.degradation_codes) == ("pyright_missing",):
        return generation_maintenance.result("pyright", "skipped", _PYRIGHT_NOT_INSTALLED_MESSAGE, details)
    return generation_maintenance.result("pyright", "degraded", _pyright_degraded_message(codes), details)


# Pyright is installed by a separate, explicit operator action; absent from every
# discovery source with nothing else wrong, it is an optional feature not taken,
# as `models` says of semantic search. Present but wrong still degrades. See
# docs/research/2026-09-27-what-is-not-yet-due-is-not-a-warning.md.
_PYRIGHT_NOT_INSTALLED_MESSAGE = (
    "Pyright is not installed (optional); Python code navigation answers from "
    f"structure alone. To install: {_PYRIGHT_INSTALL_ACTION}"
)


def _extend_unique(codes: list[str], extra) -> None:
    for code in extra:
        if code not in codes:
            codes.append(code)


def _pyright_degraded_message(codes: list[str]) -> str:
    """Name which lookup failed (issue #23): `node_major: null` alone said nothing."""
    named = ", ".join(codes) if codes else "unspecified"
    return f"Pyright identity is degraded or mismatched: {named}."


def _record_pyright_degradation(identity, details: dict, codes: list[str]) -> None:
    if identity.status == "missing":
        codes.append("pyright_missing")
        return
    details["status"] = "degraded"
    _extend_unique(codes, identity.degradation_codes)
    if identity.version != "1.1.411":
        _extend_unique(codes, ("pyright_version_mismatch",))


# Reading an LSP owner, lease or failure record from `run/lsp/` (untrusted JSON). The
# largest such record on the installed vault on 2026-09-27 was 219 bytes; a failure
# record carries at most a 1 KiB stderr tail, so 64 KiB is a guard, refused past it.
# Records are flat objects; a nesting deeper than 32 is not one this runtime wrote.
# The read step is 4 KiB because the whole record is small. Review never.
_LSP_RECORD_BYTES = 64 * 1024
_LSP_READ_CHUNK_BYTES = 4096
LSP_JSON_MAX_DEPTH = 32
_LspOwnerSnapshot = tuple[
    str,
    frozenset[str],
    dict[str, Any] | None,
    dict[str, Any] | None,
    dict[str, Any] | None,
]


def _require_lsp_deadline(deadline: float) -> None:
    if monotonic_deadline.deadline_reached(deadline):
        raise TimeoutError("LSP runtime scan deadline reached")


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    record: dict[str, Any] = {}
    for key, value in pairs:
        if key in record:
            raise ValueError(f"duplicate JSON key: {key}")
        record[key] = value
    return record


def _string_state(character: str, escaped: bool) -> tuple[bool, bool]:
    """Whether the scanner is still inside a string, and whether it is escaped."""
    if escaped:
        return True, False
    if character == "\\":
        return True, True
    return character != '"', False


def _depth_after(character: str, depth: int) -> int:
    if character in "[{":
        return _one_level_deeper(depth)
    if character in "]}":
        return depth - 1
    return depth


def _one_level_deeper(depth: int) -> int:
    if depth + 1 > LSP_JSON_MAX_DEPTH:
        raise ValueError("LSP runtime record is too deeply nested")
    return depth + 1


def _require_lsp_json_depth(text: str) -> None:
    depth = 0
    in_string = False
    escaped = False
    for character in text:
        if in_string:
            in_string, escaped = _string_state(character, escaped)
            continue
        depth = _depth_after(character, depth)
        in_string = character == '"'


def decode_lsp_record(payload: bytes) -> dict[str, Any]:
    text = payload.decode("utf-8", errors="strict")
    _require_lsp_json_depth(text)
    try:
        record = json.loads(
            text,
            object_pairs_hook=_unique_json_object,
        )
    except RecursionError as exc:
        raise ValueError("LSP runtime record is too deeply nested") from exc
    if not isinstance(record, dict):
        raise ValueError("LSP runtime record must be a JSON object")
    return record


def _lsp_posix_directory_flags() -> int:
    if not hasattr(os, "O_DIRECTORY") or not hasattr(os, "O_NOFOLLOW"):
        raise OSError("POSIX no-follow directory handles are unavailable")
    return os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)


def _require_absolute_lsp_root(state_root: Path, anchor: Path) -> None:
    if not state_root.is_absolute() or not anchor.anchor:
        raise ValueError("LSP state root must be an absolute local path")


def _opened_lsp_anchor(anchor: Path, deadline: float) -> int:
    _require_lsp_deadline(deadline)
    current = os.open(anchor, _lsp_posix_directory_flags())
    try:
        _require_lsp_deadline(deadline)
        if not stat.S_ISDIR(os.fstat(current).st_mode):
            raise PermissionError("LSP path anchor is not a directory")
        _require_lsp_deadline(deadline)
        return current
    except BaseException:
        os.close(current)
        raise


def _opened_lsp_component(parent: int, component: str, deadline: float) -> int:
    opened = os.open(component, _lsp_posix_directory_flags(), dir_fd=parent)
    try:
        _require_lsp_deadline(deadline)
        if not stat.S_ISDIR(os.fstat(opened).st_mode):
            raise PermissionError("LSP path component is not a directory")
        _require_lsp_deadline(deadline)
        return opened
    except BaseException:
        os.close(opened)
        raise


def _descend_lsp_components(current: int, components, deadline: float) -> int:
    """Owns `current`: closes it on any failure and returns the final descriptor."""
    try:
        for component in components:
            _require_lsp_deadline(deadline)
            opened = _opened_lsp_component(current, component, deadline)
            os.close(current)
            current = opened
        return current
    except BaseException:
        os.close(current)
        raise


def open_posix_lsp_root(state_root: Path, deadline: float) -> int:
    state_root = Path(os.path.abspath(state_root))
    anchor = Path(state_root.anchor)
    _require_absolute_lsp_root(state_root, anchor)
    components = (*state_root.relative_to(anchor).parts, "run", "lsp")
    return _descend_lsp_components(
        _opened_lsp_anchor(anchor, deadline), components, deadline
    )


def _list_posix_lsp_names(
    directory_fd: int,
    *,
    observed_limit: int,
    deadline: float,
) -> tuple[list[str], bool]:
    _require_lsp_deadline(deadline)
    iterator = os.scandir(directory_fd)
    _require_lsp_deadline(deadline)
    with iterator:
        return _read_lsp_names(iterator, observed_limit, deadline)


def _read_lsp_names(
    iterator: Iterator[os.DirEntry[str]], observed_limit: int, deadline: float
) -> tuple[list[str], bool]:
    names: list[str] = []
    while len(names) < observed_limit:
        _require_lsp_deadline(deadline)
        name = _next_lsp_name(iterator, deadline)
        if name is None:
            return names, False
        names.append(name)
    return names, len(names) == observed_limit


def _next_lsp_name(iterator: Iterator[os.DirEntry[str]], deadline: float) -> str | None:
    try:
        entry = next(iterator)
    except StopIteration:
        _require_lsp_deadline(deadline)
        return None
    _require_lsp_deadline(deadline)
    return entry.name


def _open_posix_lsp_directory(
    parent_fd: int,
    name: str,
    deadline: float,
) -> int:
    _require_lsp_deadline(deadline)
    expected = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    _require_lsp_deadline(deadline)
    _require_real_lsp_directory(expected)
    opened = os.open(
        name,
        _lsp_posix_directory_flags(),
        dir_fd=parent_fd,
    )
    try:
        _require_lsp_deadline(deadline)
        _require_same_directory(opened, expected)
        _require_lsp_deadline(deadline)
        return opened
    except BaseException:
        os.close(opened)
        raise


def _require_real_lsp_directory(expected: os.stat_result) -> None:
    if stat.S_ISLNK(expected.st_mode) or not stat.S_ISDIR(expected.st_mode):
        raise PermissionError("LSP runtime member is not a real directory")


def _require_same_directory(opened: int, expected: os.stat_result) -> None:
    current = os.fstat(opened)
    if not stat.S_ISDIR(current.st_mode) or not os.path.samestat(expected, current):
        raise PermissionError("LSP runtime directory changed before open")


def read_posix_lsp_record(
    owner_fd: int,
    name: str,
    deadline: float,
) -> dict[str, Any]:
    _require_lsp_deadline(deadline)
    expected = os.stat(name, dir_fd=owner_fd, follow_symlinks=False)
    _require_lsp_deadline(deadline)
    _require_safe_lsp_record(expected)
    descriptor = os.open(
        name,
        os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
        dir_fd=owner_fd,
    )
    try:
        return _read_opened_lsp_record(descriptor, expected, deadline)
    finally:
        os.close(descriptor)


def _require_safe_lsp_record(expected: os.stat_result) -> None:
    if stat.S_ISLNK(expected.st_mode) or not stat.S_ISREG(expected.st_mode):
        raise PermissionError("LSP runtime record is unsafe or oversized")
    if expected.st_size > _LSP_RECORD_BYTES:
        raise PermissionError("LSP runtime record is unsafe or oversized")


def _read_lsp_chunks(descriptor: int, deadline: float) -> tuple[list[bytes], int]:
    chunks: list[bytes] = []
    total = 0
    while total <= _LSP_RECORD_BYTES:
        _require_lsp_deadline(deadline)
        chunk = os.read(
            descriptor,
            min(_LSP_READ_CHUNK_BYTES, _LSP_RECORD_BYTES + 1 - total),
        )
        _require_lsp_deadline(deadline)
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
    if total > _LSP_RECORD_BYTES:
        raise ValueError("LSP runtime record exceeds its byte bound")
    return chunks, total


def _require_stable_lsp_record(
    descriptor: int, opened: os.stat_result, identity: tuple, total: int, deadline: float
) -> None:
    after = os.fstat(descriptor)
    _require_lsp_deadline(deadline)
    changed = identity != (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    )
    if total != opened.st_size or changed:
        raise PermissionError("LSP runtime record changed during read")


def _read_opened_lsp_record(
    descriptor: int, expected: os.stat_result, deadline: float
) -> dict[str, Any]:
    _require_lsp_deadline(deadline)
    opened = os.fstat(descriptor)
    _require_lsp_deadline(deadline)
    if not stat.S_ISREG(opened.st_mode) or not os.path.samestat(expected, opened):
        raise PermissionError("LSP runtime record changed before open")
    identity = (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns)
    chunks, total = _read_lsp_chunks(descriptor, deadline)
    _require_lsp_deadline(deadline)
    _require_stable_lsp_record(descriptor, opened, identity, total, deadline)
    return decode_lsp_record(b"".join(chunks))


class _PosixOwnerReading(NamedTuple):
    snapshot: tuple
    unreadable: bool
    stop: bool


def _posix_owner_snapshot(
    owner_name: str, present: frozenset[str], records: dict
) -> tuple:
    return (
        owner_name,
        present,
        records["owner.json"],
        records["lease.json"],
        records["failure.json"],
    )


def _read_posix_child(
    owner_fd: int, child_name: str, deadline: float, records: dict
) -> bool:
    """True when this child could not be read."""
    if child_name not in LSP_OWNER_ENTRY_NAMES:
        return True
    if child_name == "cancellation":
        os.close(_open_posix_lsp_directory(owner_fd, child_name, deadline))
        return False
    try:
        records[child_name] = read_posix_lsp_record(owner_fd, child_name, deadline)
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
        return True
    return False


def _read_posix_owner_children(
    owner_fd: int, deadline: float, records: dict
) -> tuple[frozenset[str], bool]:
    child_names, truncated = _list_posix_lsp_names(
        owner_fd,
        observed_limit=len(LSP_OWNER_ENTRY_NAMES) + 1,
        deadline=deadline,
    )
    bounded_names = child_names[: len(LSP_OWNER_ENTRY_NAMES)]
    present = frozenset(bounded_names)
    unreadable = truncated or "cancellation" not in present
    for child_name in bounded_names:
        unreadable = _read_posix_child(owner_fd, child_name, deadline, records) or unreadable
    return present, unreadable


def _read_posix_owner(
    lsp_fd: int, owner_name: str, deadline: float
) -> _PosixOwnerReading:
    records: dict[str, dict[str, Any] | None] = {
        name: None for name in _LSP_RECORD_NAMES
    }
    present, unreadable, stop = _read_posix_owner_records(
        lsp_fd, owner_name, deadline, records
    )
    snapshot = _posix_owner_snapshot(owner_name, present, records)
    return _PosixOwnerReading(snapshot, unreadable, stop)


def _read_posix_owner_records(
    lsp_fd: int, owner_name: str, deadline: float, records: dict
) -> tuple[frozenset[str], bool, bool]:
    owner_fd: int | None = None
    try:
        owner_fd = _open_posix_lsp_directory(lsp_fd, owner_name, deadline)
        present, unreadable = _read_posix_owner_children(owner_fd, deadline, records)
        return present, unreadable, False
    except TimeoutError:
        return frozenset(), True, True
    except (OSError, ValueError):
        return frozenset(), True, False
    finally:
        if owner_fd is not None:
            os.close(owner_fd)


def _scan_posix_owners(
    lsp_fd: int, owner_names: list[str], deadline: float
) -> tuple[list, bool]:
    snapshots: list = []
    unreadable = False
    for owner_name in sorted(owner_names[:MAX_LSP_OWNER_ROWS]):
        if _LSP_OWNER_NONCE.fullmatch(owner_name) is None:
            unreadable = True
            continue
        reading = _read_posix_owner(lsp_fd, owner_name, deadline)
        unreadable = unreadable or reading.unreadable
        snapshots.append(reading.snapshot)
        if reading.stop:
            return snapshots, True
    return snapshots, unreadable


def _posix_lsp_snapshots(lsp_fd: int, deadline: float) -> tuple[list, bool, bool]:
    try:
        owner_names, truncated = _list_posix_lsp_names(
            lsp_fd,
            observed_limit=MAX_LSP_OWNER_ROWS + 1,
            deadline=deadline,
        )
    except (OSError, ValueError, TimeoutError):
        return [], True, False
    snapshots: list = []
    try:
        snapshots, unreadable = _scan_posix_owners(lsp_fd, owner_names, deadline)
        _require_lsp_deadline(deadline)
    except TimeoutError:
        return snapshots, True, False
    return snapshots, unreadable or truncated, False


def snapshot_posix_lsp(
    state_root: Path,
    deadline: float,
) -> tuple[list[_LspOwnerSnapshot], bool, bool]:
    try:
        lsp_fd = open_posix_lsp_root(state_root, deadline)
    except FileNotFoundError:
        if monotonic_deadline.deadline_reached(deadline):
            return [], True, False
        return [], False, True
    except (OSError, ValueError, TimeoutError):
        return [], True, False
    try:
        return _posix_lsp_snapshots(lsp_fd, deadline)
    finally:
        os.close(lsp_fd)


def _windows_lsp_identity(workspace, handle: int, *, directory: bool) -> bytes:
    _volume, file_id, _kind = workspace.identity(handle, directory=directory)
    if not isinstance(file_id, bytes) or not any(file_id):
        raise OSError("Windows LSP identity is unavailable")
    return file_id


def read_windows_lsp_record(
    workspace,
    owner_handle: int,
    entry,
    deadline: float,
) -> dict[str, Any]:
    if entry.kind != "file" or entry.size > _LSP_RECORD_BYTES:
        raise PermissionError("Windows LSP record is unsafe or oversized")
    _require_lsp_deadline(deadline)
    handle = workspace.open_file(owner_handle, entry.name)
    try:
        return _read_windows_record_body(workspace, handle, entry, deadline)
    finally:
        workspace.close_handle(handle)


def _require_windows_record_size(size: int, entry) -> None:
    if size != entry.size or size > _LSP_RECORD_BYTES:
        raise PermissionError("Windows LSP record size changed before read")


def _read_windows_chunks(workspace, handle: int, deadline: float) -> tuple[list[bytes], int]:
    chunks: list[bytes] = []
    total = 0
    iterator = iter(
        workspace.read_chunks(
            handle,
            chunk_bytes=_LSP_READ_CHUNK_BYTES,
            max_bytes=_LSP_RECORD_BYTES,
        )
    )
    while True:
        _require_lsp_deadline(deadline)
        try:
            chunk = next(iterator)
        except StopIteration:
            _require_lsp_deadline(deadline)
            return chunks, total
        _require_lsp_deadline(deadline)
        chunks.append(chunk)
        total += len(chunk)


def _read_windows_record_body(workspace, handle: int, entry, deadline: float) -> dict[str, Any]:
    _require_lsp_deadline(deadline)
    if _windows_lsp_identity(workspace, handle, directory=False) != entry.file_id:
        raise PermissionError("Windows LSP record changed before open")
    _require_lsp_deadline(deadline)
    size = workspace.file_size(handle)
    _require_lsp_deadline(deadline)
    _require_windows_record_size(size, entry)
    chunks, total = _read_windows_chunks(workspace, handle, deadline)
    _require_lsp_deadline(deadline)
    after_size = workspace.file_size(handle)
    _require_lsp_deadline(deadline)
    if total != size or after_size != size:
        raise PermissionError("Windows LSP record changed during read")
    return decode_lsp_record(b"".join(chunks))


def snapshot_windows_lsp(
    state_root: Path,
    deadline: float,
) -> tuple[list[_LspOwnerSnapshot], bool, bool]:
    import windows_workspace as workspace

    lsp_root = Path(os.path.abspath(state_root)) / "run" / "lsp"
    try:
        _require_lsp_deadline(deadline)
        lsp_handle = workspace.open_directory_path(lsp_root)
    except FileNotFoundError:
        return _missing_windows_lsp_root(deadline)
    except (OSError, ValueError, RuntimeError, TimeoutError):
        return [], True, False
    try:
        return _windows_lsp_owner_snapshots(workspace, lsp_handle, deadline)
    finally:
        workspace.close_handle(lsp_handle)


def _missing_windows_lsp_root(deadline: float):
    if monotonic_deadline.deadline_reached(deadline):
        return [], True, False
    return [], False, True


def _valid_windows_owner_entry(owner_entry) -> bool:
    if owner_entry.kind != "directory":
        return False
    return _LSP_OWNER_NONCE.fullmatch(owner_entry.name) is not None


def _windows_lsp_owner_entries(workspace, lsp_handle: int, deadline: float):
    _require_lsp_deadline(deadline)
    entries = workspace.list_directory(lsp_handle, max_entries=MAX_LSP_OWNER_ROWS)
    _require_lsp_deadline(deadline)
    return entries


def _verify_windows_cancellation(workspace, owner_handle: int, child_entry, deadline: float) -> bool:
    """True when this child leaves the owner unreadable."""
    if child_entry.kind != "directory":
        return True
    _require_lsp_deadline(deadline)
    cancellation = workspace.open_directory(owner_handle, child_entry.name)
    try:
        _require_lsp_deadline(deadline)
        if (
            _windows_lsp_identity(workspace, cancellation, directory=True)
            != child_entry.file_id
        ):
            raise PermissionError("Windows LSP cancellation directory changed")
        _require_lsp_deadline(deadline)
    finally:
        workspace.close_handle(cancellation)
    return False


def _read_windows_child(workspace, owner_handle: int, child_entry, deadline: float, records: dict) -> bool:
    """True when this child leaves the owner unreadable."""
    if child_entry.name not in LSP_OWNER_ENTRY_NAMES:
        return True
    if child_entry.name == "cancellation":
        return _verify_windows_cancellation(workspace, owner_handle, child_entry, deadline)
    try:
        records[child_entry.name] = read_windows_lsp_record(
            workspace, owner_handle, child_entry, deadline
        )
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError, RuntimeError):
        return True
    return False


def _read_windows_owner_children(
    workspace, owner_handle: int, owner_entry, deadline: float, records: dict
) -> tuple[frozenset[str], bool]:
    _require_lsp_deadline(deadline)
    if (
        _windows_lsp_identity(workspace, owner_handle, directory=True)
        != owner_entry.file_id
    ):
        raise PermissionError("Windows LSP owner changed before open")
    _require_lsp_deadline(deadline)
    child_entries = workspace.list_directory(
        owner_handle, max_entries=len(LSP_OWNER_ENTRY_NAMES)
    )
    _require_lsp_deadline(deadline)
    present = frozenset(entry.name for entry in child_entries)
    unreadable = "cancellation" not in present
    for child_entry in child_entries:
        child_unreadable = _read_windows_child(
            workspace, owner_handle, child_entry, deadline, records
        )
        unreadable = unreadable or child_unreadable
    return present, unreadable


def _read_windows_owner_records(
    workspace, lsp_handle: int, owner_entry, deadline: float, records: dict
) -> tuple[frozenset[str], bool, bool]:
    owner_handle: int | None = None
    try:
        _require_lsp_deadline(deadline)
        owner_handle = workspace.open_directory(lsp_handle, owner_entry.name)
        present, unreadable = _read_windows_owner_children(
            workspace, owner_handle, owner_entry, deadline, records
        )
        return present, unreadable, False
    except TimeoutError:
        return frozenset(), True, True
    except (OSError, ValueError, RuntimeError):
        return frozenset(), True, False
    finally:
        if owner_handle is not None:
            workspace.close_handle(owner_handle)


def _windows_owner_reading(workspace, lsp_handle: int, owner_entry, deadline: float):
    """One owner directory: its snapshot, whether it was unreadable, whether to stop."""
    records: dict[str, dict[str, Any] | None] = {
        name: None for name in _LSP_RECORD_NAMES
    }
    present, unreadable, stop = _read_windows_owner_records(
        workspace, lsp_handle, owner_entry, deadline, records
    )
    snapshot = (
        owner_entry.name,
        present,
        records["owner.json"],
        records["lease.json"],
        records["failure.json"],
    )
    return snapshot, unreadable, stop


def _finished_windows_snapshots(snapshots: list, unreadable: bool, deadline: float):
    try:
        _require_lsp_deadline(deadline)
    except TimeoutError:
        return snapshots, True, False
    return snapshots, unreadable, False


def _collected_windows_snapshots(workspace, lsp_handle: int, owner_entries, deadline: float):
    snapshots: list[_LspOwnerSnapshot] = []
    unreadable = False
    for owner_entry in owner_entries:
        if not _valid_windows_owner_entry(owner_entry):
            unreadable = True
            continue
        snapshot, entry_unreadable, stop = _windows_owner_reading(
            workspace, lsp_handle, owner_entry, deadline
        )
        unreadable = unreadable or entry_unreadable
        snapshots.append(snapshot)
        if stop:
            return snapshots, True, False
    return _finished_windows_snapshots(snapshots, unreadable, deadline)


def _windows_lsp_owner_snapshots(workspace, lsp_handle: int, deadline: float):
    try:
        owner_entries = _windows_lsp_owner_entries(workspace, lsp_handle, deadline)
    except (OSError, ValueError, RuntimeError, TimeoutError):
        return [], True, False
    return _collected_windows_snapshots(workspace, lsp_handle, owner_entries, deadline)


def snapshot_lsp_runtime(
    state_root: Path,
    deadline: float,
) -> tuple[list[_LspOwnerSnapshot], bool, bool]:
    if os.name == "posix":
        return snapshot_posix_lsp(state_root, deadline)
    if os.name == "nt":
        return snapshot_windows_lsp(state_root, deadline)
    return [], True, False


class _LspLiveness(NamedTuple):
    live: bool
    heartbeat_at: datetime | None
    unreadable: bool
    stop: bool


class _LspOwnerReading(NamedTuple):
    record: dict
    codes: list[str]
    unreadable: bool
    stop: bool


def _lsp_records_missing(child_names: set[str], owner: object, lease: object) -> bool:
    if "owner.json" not in child_names or owner is None:
        return True
    return "lease.json" in child_names and lease is None


def _validated_lsp_owner_record(
    owner: dict | None, entry_name: str, now: datetime
) -> tuple[dict | None, bool]:
    if owner is None:
        return None, False
    if not _valid_lsp_owner(owner, entry_name):
        return None, True
    started_at = _parse_lsp_timestamp(owner.get("started_at"))
    return owner, started_at is not None and started_at > now


def _validated_lsp_lease_record(
    lease: dict | None, entry_name: str
) -> tuple[dict | None, bool]:
    if lease is None:
        return None, False
    if not _valid_lsp_lease(lease, entry_name):
        return None, True
    return lease, False


def _validated_lsp_records(
    entry_name: str,
    child_names: set[str],
    owner: dict | None,
    lease: dict | None,
    now: datetime,
) -> tuple[dict | None, dict | None, bool]:
    missing = _lsp_records_missing(child_names, owner, lease)
    owner, owner_invalid = _validated_lsp_owner_record(owner, entry_name, now)
    lease, lease_invalid = _validated_lsp_lease_record(lease, entry_name)
    return owner, lease, missing or owner_invalid or lease_invalid


def _lsp_nonces_match(owner: dict, lease: dict, entry_name: str) -> bool:
    """Both records belong to this owner directory.

    The generation nonces are deliberately not compared. `owner.json` is
    immutable create-only and names the generation the owner started with; a
    recovery restart installs a second generation and republishes the lease,
    and no record may be rewritten to agree with it.
    """
    owned = (owner.get("owner_nonce"), lease.get("owner_nonce"))
    return owned == (entry_name, entry_name)


def _lsp_start_within_window(
    owner: dict, now: datetime, heartbeat_at: datetime | None
) -> bool:
    """The owner started no later than its own heartbeat, which is not in the future."""
    started_at = _parse_lsp_timestamp(owner.get("started_at"))
    if started_at is None or heartbeat_at is None:
        return False
    return started_at <= heartbeat_at <= now


def _lsp_records_match(
    owner: dict,
    lease: dict,
    entry_name: str,
    now: datetime,
    heartbeat_at: datetime | None,
) -> bool:
    if not _lsp_nonces_match(owner, lease, entry_name):
        return False
    return _lsp_start_within_window(owner, now, heartbeat_at)


def _lsp_pid_states(processes: tuple, deadline: float) -> list[str]:
    states: list[str] = []
    for pid, identity in processes:
        _require_lsp_deadline(deadline)
        try:
            pid_state = lsp_pid_state(pid, identity)
        finally:
            _require_lsp_deadline(deadline)
        states.append(pid_state)
    return states


def _lsp_pid_liveness(
    pids: tuple, heartbeat_at: datetime | None, deadline: float
) -> _LspLiveness:
    try:
        pid_states = _lsp_pid_states(pids, deadline)
    except TimeoutError:
        return _LspLiveness(False, heartbeat_at, True, monotonic_deadline.deadline_reached(deadline))
    except Exception:  # noqa: BLE001
        return _LspLiveness(False, heartbeat_at, True, False)
    live = any(state == "alive" for state in pid_states)
    return _LspLiveness(live, heartbeat_at, "unknown" in pid_states, False)


def lsp_liveness(
    owner: dict | None,
    lease: dict | None,
    entry_name: str,
    now: datetime,
    deadline: float,
) -> _LspLiveness:
    """Whether this owner still holds live processes, and what that cost to learn."""
    if not isinstance(owner, dict):
        return _LspLiveness(False, None, True, False)
    heartbeat_at = _lsp_heartbeat(lease)
    processes = _lsp_recorded_processes(owner, lease)
    result = _lsp_pid_liveness(processes, heartbeat_at, deadline)
    if isinstance(lease, dict) and not _lsp_records_match(owner, lease, entry_name, now, heartbeat_at):
        return result._replace(unreadable=True)
    return result


def _lsp_heartbeat(lease: dict | None) -> datetime | None:
    if not isinstance(lease, dict):
        return None
    return _parse_lsp_timestamp(lease.get("heartbeat_at"))


def _lsp_recorded_processes(owner: dict, lease: dict | None) -> tuple:
    recorded = ((owner.get("owner_pid"), owner.get("owner_start_identity")),)
    if not isinstance(lease, dict):
        return recorded
    return recorded + (
        (lease.get("manager_pid"), lease.get("manager_start_identity")),
        (lease.get("server_pid"), lease.get("server_start_identity")),
    )


def _lsp_missing_start_identity_pids(owner: object, lease: dict | None) -> list[int]:
    if not isinstance(owner, dict):
        return []
    return [pid for pid, identity in _lsp_recorded_processes(owner, lease) if not identity]


def _failure_time_invalid(owner: dict, failure: dict, now: datetime) -> bool:
    owner_started_at = _parse_lsp_timestamp(owner.get("started_at"))
    failed_at = _parse_lsp_timestamp(failure.get("timestamp"))
    if owner_started_at is None or failed_at is None:
        return True
    return not owner_started_at <= failed_at <= now


def _failure_contradicts_owner(
    owner: object, failure: object, now: datetime
) -> bool:
    if not isinstance(owner, dict) or not isinstance(failure, dict):
        return False
    return _failure_time_invalid(owner, failure, now)


def _validated_failure_record(
    failure: dict | None, entry_name: str
) -> tuple[dict | None, bool]:
    if failure is None:
        return None, True
    if not _valid_lsp_failure(failure, entry_name):
        return None, True
    return failure, False


def _failure_age_days(failure: object, now: datetime) -> float | None:
    if not isinstance(failure, dict):
        return None
    failed_at = _parse_lsp_timestamp(failure.get("timestamp"))
    if failed_at is None:
        return None
    return (now - failed_at).total_seconds() / 86400.0


def _record_explicit_failure(
    record: dict,
    codes: list[str],
    failure: dict | None,
    owner: object,
    entry_name: str,
    now: datetime,
) -> bool:
    failure, unreadable = _validated_failure_record(failure, entry_name)
    if _failure_contradicts_owner(owner, failure, now):
        unreadable = True
    age_days = _failure_age_days(failure, now)
    record["failure_evidence"] = True
    record["failure_age_days"] = age_days
    retention_days = LSP_FAILURE_RETENTION.total_seconds() / 86400.0
    if age_days is None or age_days < retention_days:
        codes.append("lsp_failure_evidence_retained")
    return unreadable


def _crash_timestamp(owner: object, heartbeat_at: datetime | None):
    if heartbeat_at is not None:
        return heartbeat_at
    if not isinstance(owner, dict):
        return None
    return _parse_lsp_timestamp(owner.get("started_at"))


def _record_crash_evidence(
    record: dict,
    codes: list[str],
    owner: object,
    heartbeat_at: datetime | None,
    now: datetime,
) -> None:
    crash_at = _crash_timestamp(owner, heartbeat_at)
    if crash_at is not None:
        record["failure_evidence"] = True
        record["failure_age_days"] = (now - crash_at).total_seconds() / 86400.0
    crash_age = record["failure_age_days"]
    if crash_age is None or crash_age < 7:
        codes.append("lsp_failure_evidence_retained")


def _record_failure_evidence(
    record: dict,
    codes: list[str],
    child_names: set[str],
    failure: dict | None,
    owner: object,
    entry_name: str,
    now: datetime,
    heartbeat_at: datetime | None,
) -> bool:
    if "failure.json" in child_names:
        return _record_explicit_failure(record, codes, failure, owner, entry_name, now)
    if not record["processes_dead"]:
        return False
    _record_crash_evidence(record, codes, owner, heartbeat_at, now)
    return False


def read_lsp_owner(snapshot: tuple, now: datetime, deadline: float) -> _LspOwnerReading:
    entry_name, child_names, owner, lease, failure = snapshot
    record: dict[str, Any] = {
        "owner_nonce": entry_name,
        "live": False,
        "failure_evidence": False,
        "failure_age_days": None,
    }
    codes: list[str] = []
    owner, lease, unreadable = _validated_lsp_records(
        entry_name, child_names, owner, lease, now
    )
    record["missing_start_identity_pids"] = _lsp_missing_start_identity_pids(owner, lease)
    liveness = lsp_liveness(owner, lease, entry_name, now, deadline)
    record["live"] = liveness.live
    # Only positive death may imply a crash; explicit failure evidence is
    # validated below before publishing the final deletion-relevant verdict.
    record["processes_dead"] = _lsp_death_proven(liveness, unreadable, liveness.stop)
    if liveness.live:
        codes.append("lsp_owner_live")
    failure_unreadable = _record_failure_evidence(
        record,
        codes,
        child_names,
        failure,
        owner,
        entry_name,
        now,
        liveness.heartbeat_at,
    )
    stop = liveness.stop or monotonic_deadline.deadline_reached(deadline)
    unreadable = unreadable or liveness.unreadable or failure_unreadable
    record["processes_dead"] = _lsp_death_proven(liveness, unreadable, stop)
    return _LspOwnerReading(record, codes, unreadable, stop)


def _lsp_death_proven(liveness: _LspLiveness, unreadable: bool, stop: bool) -> bool:
    return not (liveness.live or liveness.unreadable or unreadable or stop)


def scan_lsp_owners(
    snapshots: list[tuple], now: datetime, deadline: float
) -> tuple[list[dict], list[str], bool]:
    owners: list[dict] = []
    codes: list[str] = []
    unreadable = False
    for snapshot in snapshots:
        if monotonic_deadline.deadline_reached(deadline):
            return owners, codes, True
        reading = read_lsp_owner(snapshot, now, deadline)
        unreadable = unreadable or reading.unreadable
        if reading.stop:
            return owners, codes, True
        owners.append(reading.record)
        codes.extend(reading.codes)
    return owners, codes, unreadable


def _lsp_retained_message(owners: list[dict], unreadable: bool) -> str:
    if unreadable and any(owner.get("missing_start_identity_pids") for owner in owners):
        return (
            "Retained LSP records lack process-start identity; "
            "process death cannot be verified. Cleanup remains blocked."
        )
    return "LSP runtime owners are live or retained."


def lsp_result(owners: list[dict], codes: list[str], *, unreadable: bool) -> dict:
    if unreadable and "lsp_state_unreadable" not in codes:
        codes.append("lsp_state_unreadable")
    message = _lsp_retained_message(owners, unreadable)
    status = "degraded"
    if not codes:
        message, status = "LSP runtime owners are bounded.", "ok"
    return generation_maintenance.result(
        "lsp",
        status,
        message,
        {
            "codes": codes,
            "owners": owners,
            "deletion_codes": list(codes),
            "read_error": unreadable,
        },
    )


def lsp_runtime_check(
    state_root: Path,
    now: datetime,
    *,
    deadline: float = float("inf"),
) -> dict:
    """Bound the live and retained LSP owner evidence under run/lsp."""
    snapshots, unreadable, absent = snapshot_lsp_runtime(state_root, deadline)
    if monotonic_deadline.deadline_reached(deadline):
        unreadable = True
        absent = False
    if absent:
        return generation_maintenance.result(
            "lsp",
            "ok",
            "No LSP runtime owners are present.",
            {"codes": [], "owners": [], "deletion_codes": [], "read_error": False},
        )
    owners, codes, scan_unreadable = scan_lsp_owners(snapshots, now, deadline)
    return lsp_result(owners, codes, unreadable=unreadable or scan_unreadable)


def generation_result(
    status: str,
    message: str,
    **details: object,
) -> dict:
    baseline = {
        "catalog": "unknown",
        "active_generation": None,
        "catalog_schema": "unknown",
        "generation_schema": None,
        "source_manifest": "unknown",
        "evidence_integrity": "unknown",
        "search_index": "unknown",
        "search_schema": None,
        "search_integrity": "unknown",
        "vector_state": "unknown",
        "vector_model": None,
        "vector_dimensions": None,
        "freshness": "unknown",
        "unindexed_delta": None,
        "unresolved_observations": None,
        "age_seconds": None,
        "age_source": None,
        "capacity_usage": None,
        "repairable": status == "degraded",
    }
    baseline.update(details)
    return generation_maintenance.result("generation", status, message, baseline)


def lsp_pid_state(pid: int, identity: str | None = None) -> str:
    return process_liveness.recorded_process_state(pid, identity)
