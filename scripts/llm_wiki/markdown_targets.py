"""Target validation, bounded capture and stable hashing of knowledge files."""

from __future__ import annotations

import contextlib
import ctypes
import errno
import functools
import hashlib
import os
import re
import sqlite3
import stat
import unicodedata
from collections.abc import Callable, Iterator, Mapping, Sequence
from pathlib import Path

from bounded_io import VerifiedByteRange
from claim_tree_manifest import (
    validate_claim_tree_manifest,
    validate_guardrail_source_manifest,
)
from llm_wiki import markdown_store
from llm_wiki.formats.canonical import canonical_json_bytes
from llm_wiki.markdown_records import (
    MarkdownChange,
    PreconditionChangedError,
    TargetBoundaryFailure,
    TargetPathBoundaryError,
    TargetTooLargeError,
)
from llm_wiki.markdown_store import (
    ABSENT,
    MAX_SQLITE_INT64,
    UINT64_MODULUS,
    close_windows_handle,
    lstat_or_none,
    parse_stored_timestamp,
)
from llm_wiki.restricted_paths import restricted_relative_path
from llm_wiki.windows_native import (
    WindowsFileInformation,
    windows_kernel32,
)
from reliable_memory import (
    fsync_directory,
)

_OVERSIZED_TARGET = "oversized"


_ALLOWED_DIRECTORIES = (
    "knowledge/daily",
    "knowledge/notes",
    "knowledge/projects",
    "knowledge/inbox",
    "knowledge/feedback",
    # Rotated copies of the private vault log (compile_pipeline.LOG_ROTATE_BYTES).
    "knowledge/log-archive",
    # Session records, by the 2026-08-23 retention decision. Only this subtree of
    # `knowledge/raw/` is writable: the rest of raw holds immutable sources that
    # no automatic writer may touch. Without this line every session record was
    # refused as "outside every allowed root" and the writer, which never raises
    # by contract, dropped it silently — 234 transcripts on disk and not one
    # record in the vault.
    "knowledge/raw/sessions",
)
_ALLOWED_FILES = {
    "knowledge/guardrails.md",
    "knowledge/index.md",
    "knowledge/log.md",
    # The vault log since 2026-09-14; the tracked `knowledge/log.md` stays a template
    # (kept above for transactions written before). See `vault_log`.
    "knowledge/log.local.md",
}


# Byte length of a knowledge write target's path and of one name in it: path-safety
# bounds that refuse a malformed target, like the depth below, not a fit to measured pages.
MAX_KNOWLEDGE_PATH_BYTES = 512
MAX_KNOWLEDGE_COMPONENT_BYTES = 128


# Path depth of a knowledge write target; the deepest live path is 5 (2026-09-27), so 12 only
# refuses a malformed target (path-safety bound).
MAX_KNOWLEDGE_DEPTH = 12
_FEEDBACK_JSON_RE = re.compile(r"knowledge/feedback/[0-9a-f]{6,64}\.json")
_BLACKBOARD_JSONL_RE = re.compile(
    r"knowledge/projects/[A-Za-z0-9._-]+/\.blackboard/"
    r"(?:tasks|completed|signals|conflicts)\.jsonl"
)


# SQLite INTEGER is a signed 64-bit value (https://www.sqlite.org/datatype3.html); file identities
# are stored inside that range, unsigned ones folded by UINT64_MODULUS.
_SQLITE_INT64_MIN = -(1 << 63)


def _unsigned_filesystem_id(value: object) -> int:
    """Normalize signed or unsigned OS stat representations to unsigned 64-bit."""
    if type(value) is not int or not _SQLITE_INT64_MIN <= value < UINT64_MODULUS:
        raise ValueError("filesystem identity is not a 64-bit integer")
    return value if value >= 0 else value + UINT64_MODULUS


def _stat_identity(metadata: object) -> tuple[int, int]:
    return (
        _unsigned_filesystem_id(getattr(metadata, "st_dev")),
        _unsigned_filesystem_id(getattr(metadata, "st_ino")),
    )


def decode_filesystem_id(value: object) -> int:
    """Restore an unsigned filesystem ID from its SQLite int64 encoding."""
    if type(value) is not int or not _SQLITE_INT64_MIN <= value <= MAX_SQLITE_INT64:
        raise ValueError("persisted filesystem identity is not a signed 64-bit integer")
    return value if value >= 0 else value + UINT64_MODULUS


def decode_parent_identity(identity: tuple[object, object]) -> tuple[int, int]:
    return (
        decode_filesystem_id(identity[0]),
        decode_filesystem_id(identity[1]),
    )


_SHA256_HEX = re.compile(r"[0-9a-f]{64}")
_CLAIM_TARGET_FIELDS = {
    "page",
    "claim_id",
    "fingerprint",
    "record_hash",
    "evidence_hash",
}


def _is_sha256_hex(value: object) -> bool:
    return isinstance(value, str) and _SHA256_HEX.fullmatch(value) is not None


def _is_filled_string(value: object) -> bool:
    return isinstance(value, str) and bool(value)


def _valid_positive_epoch(value: object) -> bool:
    if isinstance(value, bool) or not isinstance(value, int):
        return False
    return value >= 1


def _require_bounded_sequence(expected: object, name: str) -> None:
    if not isinstance(expected, Sequence) or isinstance(expected, (str, bytes)):
        raise TypeError(f"{name} precondition must be an array")
    if not 1 <= len(expected) <= 1000:
        raise ValueError(f"{name} precondition must be bounded")


def _require_mapping_fields(expected: object, required: set[str], name: str) -> None:
    if not isinstance(expected, Mapping):
        raise TypeError(f"{name} precondition must be a mapping")
    if set(expected) != required:
        raise ValueError(f"{name} precondition has invalid fields")


def _require_claim_target_hashes(item: Mapping[str, object]) -> None:
    for field in ("fingerprint", "record_hash", "evidence_hash"):
        if not _is_sha256_hex(item[field]):
            raise ValueError("claim_targets precondition has invalid hash")


def _valid_project_lease_values(expected: Mapping[str, object]) -> bool:
    return (
        _is_filled_string(expected["project"])
        and _is_filled_string(expected["lease_token"])
        and isinstance(expected["fencing_epoch"], int)
        and expected["fencing_epoch"] >= 1
        and isinstance(expected["expires_at"], str)
    )


def _validated_project_lease(expected: object) -> dict:
    _require_mapping_fields(
        expected,
        {"project", "lease_token", "fencing_epoch", "expires_at"},
        "project_lease",
    )
    if not _valid_project_lease_values(expected):
        raise ValueError("project_lease precondition has invalid values")
    parse_stored_timestamp(expected["expires_at"])
    return dict(expected)


def _valid_intent_fence_values(expected: Mapping[str, object]) -> bool:
    return (
        _is_sha256_hex(str(expected["intent_id"]))
        and expected["mode"] in {"capture", "worker", "operator"}
        and _is_filled_string(expected["token"])
        and _valid_positive_epoch(expected["fencing_epoch"])
        and isinstance(expected["expires_at"], str)
    )


def _validated_intent_fence(expected: object) -> dict:
    _require_mapping_fields(
        expected,
        {"intent_id", "mode", "token", "fencing_epoch", "expires_at"},
        "intent_fence",
    )
    if not _valid_intent_fence_values(expected):
        raise ValueError("intent_fence precondition has invalid values")
    parse_stored_timestamp(str(expected["expires_at"]))
    return dict(expected)


def _valid_capture_binding_values(expected: Mapping[str, object]) -> bool:
    return (
        _is_sha256_hex(str(expected["intent_id"]))
        and _is_filled_string(expected["task_id"])
        and _is_sha256_hex(str(expected["active_link_digest"]))
        and _is_sha256_hex(str(expected["seal_digest"]))
    )


def _validated_capture_binding(expected: object) -> dict:
    _require_mapping_fields(
        expected,
        {"intent_id", "task_id", "active_link_digest", "seal_digest"},
        "capture_binding",
    )
    if not _valid_capture_binding_values(expected):
        raise ValueError("capture_binding precondition has invalid values")
    return dict(expected)


def _require_hash_or_absent(expected: object) -> None:
    if expected == ABSENT:
        return
    if not _is_sha256_hex(expected):
        raise ValueError("precondition values must be 'absent' or SHA-256 hashes")


_RESERVED_DEVICE_NAMES = {"con", "prn", "aux", "nul"} | {
    f"{prefix}{number}" for prefix in ("com", "lpt") for number in range(1, 10)
}


def _require_normalized_path(value: str) -> None:
    if unicodedata.normalize("NFC", value) != value:
        raise ValueError("path must use NFC Unicode normalization")


def _require_bounded_components(relative: Path) -> None:
    if any(
        len(part.encode("utf-8")) > MAX_KNOWLEDGE_COMPONENT_BYTES
        for part in relative.parts
    ):
        raise ValueError("target path component exceeds length limit")


def _require_bounded_path(value: str, relative: Path) -> None:
    if len(value.encode("utf-8")) > MAX_KNOWLEDGE_PATH_BYTES:
        raise ValueError("target path exceeds length limit")
    if len(relative.parts) > MAX_KNOWLEDGE_DEPTH:
        raise ValueError("target path depth exceeds limit")
    _require_bounded_components(relative)


def _non_portable_component(part: str) -> bool:
    if part.endswith((" ", ".")):
        return True
    if any(character in '<>:"|?*' or ord(character) < 32 for character in part):
        return True
    return part.rstrip(" .").split(".", 1)[0].casefold() in _RESERVED_DEVICE_NAMES


def _require_portable_components(relative: Path) -> None:
    for part in relative.parts:
        if _non_portable_component(part):
            raise ValueError("path contains a non-portable or reserved component")


def _require_allowed_root(normalized: str) -> None:
    if normalized in _ALLOWED_FILES:
        return
    if any(normalized.startswith(f"{root}/") for root in _ALLOWED_DIRECTORIES):
        return
    raise ValueError("path is outside every allowed Markdown root")


def _require_markdown_target(normalized: str) -> None:
    if normalized.startswith("knowledge/feedback/"):
        raise ValueError("feedback candidates must use JSON")


def _require_feedback_target(normalized: str) -> None:
    if _FEEDBACK_JSON_RE.fullmatch(normalized) is None:
        raise ValueError("JSON targets must be feedback candidates")


def _require_blackboard_target(normalized: str) -> None:
    if _BLACKBOARD_JSONL_RE.fullmatch(normalized) is None:
        raise ValueError("JSONL targets must be project blackboard streams")


def _require_approved_suffix(relative: Path, normalized: str) -> None:
    """Each approved file type belongs in exactly one place."""
    require_target = {
        ".md": _require_markdown_target,
        ".json": _require_feedback_target,
        ".jsonl": _require_blackboard_target,
    }.get(relative.suffix.casefold())
    if require_target is None:
        raise ValueError("transaction targets must use an approved file type")
    require_target(normalized)


_BOUNDARY_ERRNOS = frozenset({errno.ENOENT, errno.ENOTDIR, errno.ELOOP})


def _as_parent_boundary_failure(parent: Path, exc: OSError) -> TargetBoundaryFailure:
    """The parent is gone, is not a directory, or is now a link."""
    if exc.errno not in _BOUNDARY_ERRNOS:
        raise exc
    return TargetBoundaryFailure(
        f"parent identity is not a stable directory: {parent}"
    )


def _open_parent_directory(parent: Path) -> int:
    """A descriptor on the parent itself, never on what a link points at."""
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        return os.open(parent, flags)
    except OSError as exc:
        raise _as_parent_boundary_failure(parent, exc) from exc


_CAPTURE_OPEN_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_BINARY", 0)
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_NONBLOCK", 0)
)


def _required_parent_identity(row: sqlite3.Row) -> object:
    persisted = (row["parent_device"], row["parent_inode"])
    if None in persisted:
        raise RuntimeError(
            f"transaction lacks parent identity for {row['path']}"
        )
    return decode_parent_identity(persisted)


def _close_windows_handles(handles: list[int]) -> None:
    """Every handle is closed; the first failure is raised after the last close."""
    close_error: OSError | None = None
    for handle in reversed(handles):
        close_error = close_error or _closed_windows_handle(handle)
    if close_error is not None:
        raise close_error


def _closed_windows_handle(handle: int) -> OSError | None:
    try:
        close_windows_handle(handle)
    except OSError as exc:
        return exc
    return None


def _ensure_safe_directory(current: Path, value: str) -> None:
    if current.exists():
        _require_safe_parent(
            current,
            f"target traverses an unsafe parent: {value}",
            f"target parent is not a directory: {value}",
        )
        return
    _make_directory(current)
    _require_safe_parent(
        current,
        f"created target parent is unsafe: {value}",
        f"created target parent is not a directory: {value}",
    )
    fsync_directory(current.parent)


def _make_directory(current: Path) -> None:
    try:
        current.mkdir()
    except FileExistsError:
        pass


def _require_safe_parent(
    current: Path, unsafe_message: str, not_directory_message: str
) -> None:
    if current.is_symlink() or is_reparse_point(current):
        raise ValueError(unsafe_message)
    if not current.is_dir():
        raise ValueError(not_directory_message)


def _require_written_content(change: MarkdownChange) -> None:
    if not isinstance(change.content, bytes):
        raise TypeError("create and replace content must be bytes")
    if len(change.content) > markdown_store.MAX_KNOWLEDGE_TARGET_BYTES:
        raise ValueError("transaction target size exceeds limit")


def _require_change_shape(change: object) -> None:
    """A change must be a MarkdownChange naming one of the three known kinds."""
    if not isinstance(change, MarkdownChange):
        raise TypeError("changes must contain MarkdownChange values")
    if change.kind not in {"create", "replace", "delete"}:
        raise ValueError(f"unsupported change kind: {change.kind}")


def _require_change_content(change: MarkdownChange) -> None:
    if change.kind == "delete":
        if change.content is not None:
            raise ValueError("delete content must be absent")
        return
    _require_written_content(change)


def _require_before_bound(max_before_bytes: object) -> None:
    if max_before_bytes is None:
        return
    if (
        isinstance(max_before_bytes, bool)
        or not isinstance(max_before_bytes, int)
        or max_before_bytes < 0
    ):
        raise ValueError("max_before_bytes must be a non-negative integer or None")


def _stat_at_or_none(parent_descriptor: int, name: str) -> os.stat_result | None:
    try:
        return os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
    except FileNotFoundError:
        return None


def _open_capture_descriptor(
    target: object, label: object, dir_fd: int | None = None
) -> int:
    try:
        if dir_fd is None:
            return os.open(target, _CAPTURE_OPEN_FLAGS)
        return os.open(target, _CAPTURE_OPEN_FLAGS, dir_fd=dir_fd)
    except (OSError, ValueError) as exc:
        raise ValueError(
            f"transaction target cannot be opened safely: {label}"
        ) from exc


def _is_reparse_metadata(metadata: os.stat_result) -> bool:
    attributes = getattr(metadata, "st_file_attributes", 0)
    return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def _require_regular_file(metadata: os.stat_result, target: Path) -> None:
    if stat.S_ISLNK(metadata.st_mode) or _is_reparse_metadata(metadata):
        raise ValueError(f"transaction target is a link: {target}")
    if not stat.S_ISREG(metadata.st_mode):
        raise ValueError(f"transaction target is not a regular file: {target}")


def _bounded_chunks(descriptor: int, remaining: int) -> Iterator[bytes]:
    """One byte past the bound is read, so the caller can see the overflow."""
    while remaining:
        chunk = os.read(descriptor, min(remaining, 1024 * 1024))
        if not chunk:
            return
        yield chunk
        remaining -= len(chunk)


def descriptor_bytes(descriptor: int, max_bytes: int | None) -> bytes:
    if max_bytes is None:
        with os.fdopen(os.dup(descriptor), "rb") as handle:
            return handle.read()
    return b"".join(_bounded_chunks(descriptor, max_bytes + 1))


def _require_read_size(
    content: bytes, after: os.stat_result, target: Path, max_bytes: int | None
) -> None:
    if len(content) != after.st_size:
        raise PreconditionChangedError(f"transaction target changed while reading: {target}")
    if max_bytes is not None and len(content) > max_bytes:
        raise ValueError(f"transaction target exceeds {max_bytes} bytes: {target}")


def _bounded_digest(descriptor: int, target: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    total = 0
    for chunk in _bounded_chunks(descriptor, markdown_store.MAX_KNOWLEDGE_TARGET_BYTES + 1):
        total += len(chunk)
        _require_hash_size(total, target)
        digest.update(chunk)
    return digest.hexdigest(), total


def _require_hash_size(total: int, target: Path) -> None:
    if total > markdown_store.MAX_KNOWLEDGE_TARGET_BYTES:
        raise TargetTooLargeError(
            f"transaction target exceeds {markdown_store.MAX_KNOWLEDGE_TARGET_BYTES} bytes: {target}"
        )


def _traversable_target(current: Path, value: str) -> bool:
    """False once the walk reaches a path that does not exist yet."""
    if current.is_symlink():
        raise TargetPathBoundaryError(f"target traverses a symlink: {value}")
    if is_reparse_point(current):
        raise TargetPathBoundaryError(
            f"target traverses a Windows reparse point: {value}"
        )
    return current.exists()


def is_reparse_point(path: Path) -> bool:
    try:
        attributes = getattr(path.stat(follow_symlinks=False), "st_file_attributes", 0)
    except FileNotFoundError:
        return False
    return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def use_posix_dir_fd() -> bool:
    return os.name == "posix"


def _require_windows_directory_identity(handle: int, path: Path) -> None:
    """Refuse a handle that cannot be identified or that is a reparse point."""
    kernel32 = windows_kernel32()
    information = WindowsFileInformation()
    if not kernel32.GetFileInformationByHandle(handle, ctypes.byref(information)):
        error = ctypes.get_last_error()
        kernel32.CloseHandle(handle)
        raise OSError(error, f"cannot identify Windows directory: {path}")
    if information.file_attributes & 0x400:
        kernel32.CloseHandle(handle)
        raise TargetBoundaryFailure(
            f"Windows directory handle resolves to a reparse point: {path}"
        )


def _open_windows_directory(path: Path) -> int:
    kernel32 = windows_kernel32()
    file_read_attributes = 0x80
    file_share_read = 0x1
    file_share_write = 0x2
    open_existing = 3
    file_flag_backup_semantics = 0x02000000
    file_flag_open_reparse_point = 0x00200000
    handle = kernel32.CreateFileW(
        str(path),
        file_read_attributes,
        file_share_read | file_share_write,
        None,
        open_existing,
        file_flag_backup_semantics | file_flag_open_reparse_point,
        None,
    )
    invalid_handle = ctypes.c_void_p(-1).value
    if handle == invalid_handle:
        raise _windows_directory_failure(path, ctypes.get_last_error())
    _require_windows_directory_identity(handle, path)
    return handle


# ERROR_FILE_NOT_FOUND, ERROR_PATH_NOT_FOUND, ERROR_DIRECTORY: the three ways
# Windows says the parent this transaction prepared against is not there any more.
_WINDOWS_BOUNDARY_ERRORS = frozenset({2, 3, 267})


def _windows_directory_failure(path: Path, code: int) -> BaseException:
    """The boundary failure, or the raw error that is nobody's to name here."""
    if code in _WINDOWS_BOUNDARY_ERRORS:
        return TargetBoundaryFailure(
            f"parent identity is not a stable directory: {path}"
        )
    return OSError(code, f"cannot lock Windows directory: {path}")


class TargetCaptureOperations:
    def ensure_target_parent(self, value: str) -> Path:
        """Create a covered target's parent without traversing unsafe directories."""
        relative = restricted_relative_path(
            value, (*_ALLOWED_DIRECTORIES, *_ALLOWED_FILES)
        )
        if len(relative.parts) > MAX_KNOWLEDGE_DEPTH:
            raise ValueError("target path depth exceeds limit")
        current = self.vault
        for part in relative.parts[:-1]:
            current = current / part
            _ensure_safe_directory(current, value)
        return self._target(value)

    def _validate_change(self, change: MarkdownChange) -> MarkdownChange:
        _require_change_shape(change)
        _require_change_content(change)
        _require_before_bound(change.max_before_bytes)
        self._target(change.path)
        if change.max_before_bytes is None:
            return MarkdownChange(
                change.kind,
                change.path,
                change.content,
                markdown_store.MAX_KNOWLEDGE_TARGET_BYTES,
            )
        return change

    def _require_no_reparse_traversal(self, relative: Path, value: str) -> None:
        current = self.vault
        for part in relative.parts:
            current = current / part
            if not _traversable_target(current, value):
                return

    def _require_canonical_parent(self, target: Path, value: str) -> None:
        if target.parent.resolve(strict=False) != target.parent:
            raise TargetPathBoundaryError(
                f"target has a non-canonical parent: {value}"
            )
        try:
            target.relative_to(self.vault)
        except ValueError as exc:
            raise TargetPathBoundaryError("target escapes the vault") from exc
        if not target.parent.is_dir():
            raise TargetPathBoundaryError(f"target parent does not exist: {value}")

    def _target(self, value: str) -> Path:
        relative = restricted_relative_path(
            value, (*_ALLOWED_DIRECTORIES, *_ALLOWED_FILES)
        )
        _require_normalized_path(value)
        _require_bounded_path(value, relative)
        _require_portable_components(relative)
        normalized = relative.as_posix()
        _require_allowed_root(normalized)
        _require_approved_suffix(relative, normalized)
        target = self.vault.joinpath(*relative.parts)
        self._require_no_reparse_traversal(relative, value)
        self._require_canonical_parent(target, value)
        return target

    def _require_claim_target_identity(self, item: object) -> None:
        if not isinstance(item, Mapping) or set(item) != _CLAIM_TARGET_FIELDS:
            raise ValueError("claim_targets precondition has invalid fields")
        self._target(str(item["page"]))
        if not _is_filled_string(item["claim_id"]):
            raise ValueError("claim_targets precondition has invalid claim id")

    def _require_claim_target(self, item: object, identities: set) -> None:
        self._require_claim_target_identity(item)
        _require_claim_target_hashes(item)
        identity = (str(item["page"]), item["claim_id"])
        if identity in identities:
            raise ValueError("claim_targets precondition contains duplicates")
        identities.add(identity)

    def _validated_claim_targets(self, expected: object) -> list[dict]:
        _require_bounded_sequence(expected, "claim_targets")
        targets: list[dict] = []
        identities: set = set()
        for item in expected:
            self._require_claim_target(item, identities)
            targets.append(dict(item))
        return sorted(targets, key=lambda item: (item["page"], item["claim_id"]))

    def _validated_fence_precondition(self, path: str, expected: object) -> object:
        validate = {
            "project_lease": _validated_project_lease,
            "intent_fence": _validated_intent_fence,
            "capture_binding": _validated_capture_binding,
        }.get(path)
        if validate is not None:
            return validate(expected)
        self._target(path)
        _require_hash_or_absent(expected)
        return expected

    def _validated_precondition(self, path: str, expected: object) -> object:
        """One precondition value, validated by the kind its key names."""
        if path == "claim_targets":
            return self._validated_claim_targets(expected)
        validate = {
            "claim_tree_manifest": validate_claim_tree_manifest,
            "guardrails_source_manifest": validate_guardrail_source_manifest,
        }.get(path)
        if validate is not None:
            return validate(expected)
        return self._validated_fence_precondition(path, expected)

    def _validate_preconditions(self, preconditions: Mapping[str, object]) -> dict[str, object]:
        if not isinstance(preconditions, Mapping):
            raise TypeError("preconditions must be a mapping")
        result: dict[str, object] = {}
        for path, expected in preconditions.items():
            result[path] = self._validated_precondition(path, expected)
        canonical_json_bytes(result)
        return result

    def _read_target(self, target: Path) -> bytes | None:
        return self._read_bounded_target(target, markdown_store.MAX_KNOWLEDGE_TARGET_BYTES)

    def _parent_identity(self, parent: Path) -> tuple[int, int]:
        metadata = self._parent_metadata(parent)
        if not stat.S_ISDIR(metadata.st_mode) or is_reparse_point(parent):
            raise TargetBoundaryFailure(
                f"parent identity is not a stable directory: {parent}"
            )
        return _stat_identity(metadata)

    @staticmethod
    def _parent_metadata(parent: Path) -> os.stat_result:
        """A parent that is gone or replaced is a boundary failure, not an ENOENT."""
        try:
            return parent.stat(follow_symlinks=False)
        except OSError as exc:
            raise _as_parent_boundary_failure(parent, exc) from exc

    def _capture_target_on_windows(self, target, max_before_bytes):
        with self._hold_windows_parent(target.parent):
            identity = self._parent_identity(target.parent)
            current = functools.partial(lstat_or_none, target)
            with self._capture_file_range(target, current, max_before_bytes) as source:
                yield source, identity
            self._require_parent_identity(target.parent, identity, str(target))

    @contextlib.contextmanager
    def _capture_target(self, target, *, max_before_bytes=None):
        if not use_posix_dir_fd():
            yield from self._capture_target_on_windows(target, max_before_bytes)
            return
        descriptor = _open_parent_directory(target.parent)
        try:
            identity = _stat_identity(os.fstat(descriptor))
            current = functools.partial(_stat_at_or_none, descriptor, target.name)
            with self._capture_file_range(
                target.name, current, max_before_bytes, dir_fd=descriptor
            ) as source:
                yield source, identity
            self._require_parent_identity(target.parent, identity, str(target))
        finally:
            os.close(descriptor)

    @contextlib.contextmanager
    def _capture_file_range(self, name, current, max_bytes, *, dir_fd=None):
        before = current()
        if before is None:
            yield None
            if current() is not None:
                raise PreconditionChangedError(f"transaction target changed while reading: {name}")
            return
        self._validate_capture_metadata(before, Path(name), max_bytes)
        descriptor = _open_capture_descriptor(name, name, dir_fd=dir_fd)
        try:
            with self._capture_descriptor_range(descriptor, before, name, current) as source:
                yield source
        finally:
            os.close(descriptor)

    @contextlib.contextmanager
    def _capture_descriptor_range(self, descriptor, before, name, current):
        if not self._same_capture_identity(before, os.fstat(descriptor)):
            raise PreconditionChangedError(f"transaction target changed before open: {name}")
        check = functools.partial(self._require_capture_range_live,
                                  descriptor, before, name, current)
        check()
        with os.fdopen(descriptor, 'rb', buffering=0, closefd=False) as stream:
            source = VerifiedByteRange(stream, before.st_size, None, owner_check=check)
            yield source
            source.verify_content(label=f"transaction target {name}")

    def _require_capture_range_live(self, descriptor, before, name, current):
        self._require_unchanged_read(before, os.fstat(descriptor), name)
        self._require_unchanged_read(before, current(), name)

    def _read_bounded_target(self, target: Path, max_bytes: int | None) -> bytes | None:
        before = lstat_or_none(target)
        if before is None:
            return None
        self._validate_capture_metadata(before, target, max_bytes)
        descriptor = _open_capture_descriptor(target, target)
        try:
            content, after = self._read_stable_descriptor(
                descriptor, before, target, max_bytes
            )
            self._require_unchanged_read(after, lstat_or_none(target), target)
            return content
        finally:
            os.close(descriptor)

    @staticmethod
    def _validate_capture_metadata(
        metadata: os.stat_result, target: Path, max_bytes: int | None
    ) -> None:
        _require_regular_file(metadata, target)
        if max_bytes is not None and metadata.st_size > max_bytes:
            raise TargetTooLargeError(
                f"transaction target exceeds {max_bytes} bytes: {target}"
            )

    def _read_stable_descriptor(
        self,
        descriptor: int,
        before: os.stat_result,
        target: Path,
        max_bytes: int | None,
    ) -> tuple[bytes, os.stat_result]:
        opened = os.fstat(descriptor)
        if not self._same_capture_identity(before, opened):
            raise PreconditionChangedError(f"transaction target changed before open: {target}")
        self._validate_capture_metadata(opened, target, max_bytes)
        content = descriptor_bytes(descriptor, max_bytes)
        after = os.fstat(descriptor)
        if not self.same_capture_snapshot(opened, after):
            raise PreconditionChangedError(f"transaction target changed while reading: {target}")
        _require_read_size(content, after, target, max_bytes)
        return content, after

    @staticmethod
    def _same_capture_identity(left: os.stat_result, right: os.stat_result) -> bool:
        return _stat_identity(left) == _stat_identity(right)

    @classmethod
    def same_capture_snapshot(cls, left: os.stat_result, right: os.stat_result) -> bool:
        metadata_matches = (
            left.st_mode,
            left.st_size,
            left.st_mtime_ns,
        ) == (
            right.st_mode,
            right.st_size,
            right.st_mtime_ns,
        )
        # Python exposes creation time as st_ctime on Windows, where descriptor and
        # path views may differ. It is not a content-change signal on that platform.
        change_time_matches = os.name == "nt" or left.st_ctime_ns == right.st_ctime_ns
        return (
            cls._same_capture_identity(left, right)
            and metadata_matches
            and change_time_matches
        )

    @contextlib.contextmanager
    def _stable_parent(self, row: sqlite3.Row) -> Iterator[tuple[Path, int | None]]:
        target = self._target(row["path"])
        expected = _required_parent_identity(row)
        if not use_posix_dir_fd():
            yield from self._windows_stable_parent(target, expected, row["path"])
            return
        yield from self._posix_stable_parent(target, expected, row["path"])

    def _windows_stable_parent(
        self, target: Path, expected: object, path: str
    ) -> Iterator[tuple[Path, int | None]]:
        with self._hold_windows_parent(target.parent):
            self._require_parent_identity(target.parent, expected, path)
            try:
                yield target, None
            finally:
                self._require_parent_identity(target.parent, expected, path)

    def _posix_stable_parent(
        self, target: Path, expected: object, path: str
    ) -> Iterator[tuple[Path, int | None]]:
        descriptor = _open_parent_directory(target.parent)
        try:
            if _stat_identity(os.fstat(descriptor)) != expected:
                raise TargetBoundaryFailure(f"parent identity mismatch for {path}")
            yield target, descriptor
            self._require_parent_identity(target.parent, expected, path)
        finally:
            os.close(descriptor)

    def _require_parent_identity(
        self, parent: Path, expected: object, path: str
    ) -> None:
        if self._parent_identity(parent) != expected:
            raise TargetBoundaryFailure(f"parent identity mismatch for {path}")

    @contextlib.contextmanager
    def _hold_windows_parent(self, parent: Path) -> Iterator[None]:
        if os.name != "nt":
            raise RuntimeError(
                "safe non-POSIX mutation requires Windows directory handles"
            )
        paths = self._vault_chain(parent)
        handles: list[int] = []
        previous_parent_handle = getattr(self._local, "windows_parent_handle", None)
        try:
            for path in paths:
                handles.append(_open_windows_directory(path))
            self._local.windows_parent_handle = handles[-1]
            yield
        finally:
            self._local.windows_parent_handle = previous_parent_handle
            _close_windows_handles(handles)

    def _vault_chain(self, parent: Path) -> list[Path]:
        """Every directory from the vault down to the parent, vault first."""
        try:
            relative = parent.relative_to(self.vault)
        except ValueError as exc:
            raise TargetBoundaryFailure("target parent is outside the vault") from exc
        paths = [self.vault]
        current = self.vault
        for part in relative.parts:
            current = current / part
            paths.append(current)
        return paths

    def _hash_bounded_target(self, target: Path) -> str:
        before = lstat_or_none(target)
        if before is None:
            return ABSENT
        if not self._within_capture_bound(before, target):
            return _OVERSIZED_TARGET
        descriptor = os.open(target, _CAPTURE_OPEN_FLAGS)
        return self._hash_opened_target(
            descriptor, before, target, lambda: lstat_or_none(target), target
        )

    def _hash_opened_target(
        self,
        descriptor: int,
        before: os.stat_result,
        target: Path,
        current: Callable[[], os.stat_result | None],
        label: object,
    ) -> str:
        """Hash an opened bounded target and close it; `current` re-reads its metadata."""
        try:
            hashed = self._hashed_or_none(descriptor, before, target)
            if hashed is None:
                return _OVERSIZED_TARGET
            self._require_unchanged_hash(hashed[1], current(), label)
            return hashed[0]
        finally:
            os.close(descriptor)

    def _hash_bounded_from_parent(self, parent_descriptor: int, name: str) -> str:
        before = _stat_at_or_none(parent_descriptor, name)
        if before is None:
            return ABSENT
        if not self._within_capture_bound(before, Path(name)):
            return _OVERSIZED_TARGET
        descriptor = os.open(name, _CAPTURE_OPEN_FLAGS, dir_fd=parent_descriptor)
        return self._hash_opened_target(
            descriptor, before, Path(name), lambda: _stat_at_or_none(parent_descriptor, name), name
        )

    def _within_capture_bound(self, metadata: os.stat_result, target: Path) -> bool:
        """False means the target is too large to be captured at all."""
        try:
            self._validate_capture_metadata(
                metadata, target, markdown_store.MAX_KNOWLEDGE_TARGET_BYTES
            )
        except TargetTooLargeError:
            return False
        return True

    def _hashed_or_none(
        self, descriptor: int, before: os.stat_result, target: Path
    ) -> tuple[str, os.stat_result] | None:
        """None means the target grew past the bound while it was open."""
        try:
            return self._hash_stable_descriptor(descriptor, before, target)
        except TargetTooLargeError:
            return None

    def _require_unchanged_read(
        self, after: os.stat_result, current: os.stat_result | None, label: object
    ) -> None:
        if current is None or not self.same_capture_snapshot(after, current):
            raise PreconditionChangedError(f"transaction target changed while reading: {label}")

    def _require_unchanged_hash(
        self, after: os.stat_result, current: os.stat_result | None, label: object
    ) -> None:
        if current is None or not self.same_capture_snapshot(after, current):
            raise PreconditionChangedError(f"transaction target changed while hashing: {label}")

    def _hash_stable_descriptor(
        self, descriptor: int, before: os.stat_result, target: Path
    ) -> tuple[str, os.stat_result]:
        opened = os.fstat(descriptor)
        if not self._same_capture_identity(before, opened):
            raise PreconditionChangedError(f"transaction target changed before hash: {target}")
        self._validate_capture_metadata(
            opened, target, markdown_store.MAX_KNOWLEDGE_TARGET_BYTES
        )
        digest, total = _bounded_digest(descriptor, target)
        after = os.fstat(descriptor)
        if not self.same_capture_snapshot(opened, after) or total != after.st_size:
            raise PreconditionChangedError(f"transaction target changed while hashing: {target}")
        return digest, after

    def _hash_operation_target(
        self, target: Path, parent_descriptor: int | None
    ) -> str:
        if parent_descriptor is None:
            return self._hash_bounded_target(target)
        return self._hash_bounded_from_parent(parent_descriptor, target.name)
