"""Content-addressed cache for validated, normalized compile plans."""
from __future__ import annotations

import json
import os
import re
import stat
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Protocol

from markdown_transaction import (
    _acl_output_text,
    _run_acl_command,
    _windows_acl_identity,
)
from reliable_memory import (
    _known_network_path,
    _windows_reparse_point,
    canonical_json_bytes,
    fsync_directory,
    sha256_bytes,
)

CACHE_SCHEMA_VERSION = 1
MAX_CACHE_ENTRY_BYTES = 16 * 1024 * 1024
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_COMPILE_PLAN_SCHEMA_PATH = Path(__file__).resolve().parent / "schemas" / "compile-plan-v2.json"
_COMPILE_PLAN_SCHEMA_BYTES = _COMPILE_PLAN_SCHEMA_PATH.read_bytes()
_COMPILE_PLAN_SCHEMA = json.loads(_COMPILE_PLAN_SCHEMA_BYTES.decode("utf-8"))
COMPILE_PLAN_SCHEMA_VERSION = _COMPILE_PLAN_SCHEMA["properties"]["schema_version"]["const"]
COMPILE_PLAN_SCHEMA_HASH = sha256_bytes(canonical_json_bytes(_COMPILE_PLAN_SCHEMA))


class PlanValidator(Protocol):
    def __call__(self, plan: dict[str, object]) -> bool: ...


@dataclass(frozen=True, order=True)
class SourceOccurrenceBounds:
    first_event_id: str
    last_event_id: str

    def canonical(self) -> dict[str, str]:
        for value in (self.first_event_id, self.last_event_id):
            if not isinstance(value, str) or not 1 <= len(value.encode()) <= 256:
                raise ValueError("source event ID is invalid")
        return {
            "first_event_id": self.first_event_id,
            "last_event_id": self.last_event_id,
        }


@dataclass(frozen=True, order=True)
class SourceDescriptor:
    logical_path: str
    byte_length: int
    sha256: str
    occurrence_bounds: SourceOccurrenceBounds | None = None

    def canonical(self) -> list[object]:
        _validate_logical_path(self.logical_path)
        if not isinstance(self.byte_length, int) or isinstance(self.byte_length, bool):
            raise TypeError("source byte length must be an integer")
        if self.byte_length < 0:
            raise ValueError("source byte length must be non-negative")
        _validate_digest(self.sha256, "source SHA-256")
        return [self.logical_path, self.byte_length, self.sha256]

    def receipt_descriptor(self) -> dict[str, object]:
        self.canonical()
        return {
            "logical_path": self.logical_path,
            "sha256": self.sha256,
            "byte_size": self.byte_length,
            "occurrence_bounds": (
                None
                if self.occurrence_bounds is None
                else self.occurrence_bounds.canonical()
            ),
        }


@dataclass(frozen=True)
class CompileCallDescriptor:
    prompt_program_hash: str
    provider: str
    model: str | None
    capabilities: Mapping[str, object]
    inference_settings: Mapping[str, object]
    structured_output: str
    fallback_from: tuple[str, ...]

    def canonical(self) -> dict[str, object]:
        _validate_digest(self.prompt_program_hash, "prompt program hash")
        self._validate_identity()
        self._validate_modes()
        capabilities = _restricted_mapping(self.capabilities, "capabilities")
        settings = _restricted_mapping(self.inference_settings, "inference settings")
        return {
            "prompt_program_hash": self.prompt_program_hash,
            "provider": self.provider,
            "model": self.model,
            "capabilities": capabilities,
            "inference_settings": settings,
            "structured_output": self.structured_output,
            "fallback_lineage": list(self.fallback_from),
        }

    def _validate_identity(self) -> None:
        if not self.provider or not isinstance(self.provider, str):
            raise ValueError("provider identity is required")
        if self.model is not None and not _nonblank_text(self.model):
            raise ValueError("model identity must be explicit or null")

    def _validate_modes(self) -> None:
        if self.structured_output not in {"native", "prompt"}:
            raise ValueError("structured output mode must be native or prompt")
        if not all(isinstance(item, str) and item for item in self.fallback_from):
            raise ValueError("fallback lineage entries must be non-empty strings")


@dataclass(frozen=True)
class CompileActionDescriptor:
    compiler_version: str
    schema_version: str
    schema_hash: str
    normalization_version: str
    feature_flags: Mapping[str, object]
    draft_calls: tuple[CompileCallDescriptor, ...]
    critique_calls: tuple[CompileCallDescriptor, ...]
    sources: tuple[SourceDescriptor, ...]

    def canonical(self) -> dict[str, object]:
        self._validate_versions()
        flags = _restricted_mapping(self.feature_flags, "feature flags")
        draft_calls = [call.canonical() for call in self.draft_calls]
        critique_calls = [call.canonical() for call in self.critique_calls]
        if not draft_calls:
            raise ValueError("at least one draft call descriptor is required")
        source_manifest = _source_manifest(self.sources)
        source_manifest_hash = sha256_bytes(canonical_json_bytes(source_manifest))
        return {
            "compiler_version": self.compiler_version,
            "schema_version": self.schema_version,
            "schema_hash": self.schema_hash,
            "normalization_version": self.normalization_version,
            "feature_flags": flags,
            "draft_calls": draft_calls,
            "critique_calls": critique_calls,
            "source_manifest": source_manifest,
            "source_manifest_hash": source_manifest_hash,
        }

    def _validate_versions(self) -> None:
        _require_text(self.compiler_version, "compiler version is required")
        _require_text(self.schema_version, "schema version is required")
        _validate_digest(self.schema_hash, "schema hash")
        _require_text(self.normalization_version, "normalization version is required")

    @property
    def persistent(self) -> bool:
        calls = (*self.draft_calls, *self.critique_calls)
        return bool(calls) and all(call.model is not None and call.model.strip() for call in calls)


# Short compatibility names for callers that treat descriptors as tuple records.
SourceTuple = SourceDescriptor
CallDescriptor = CompileCallDescriptor


def _nonblank_text(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _require_text(value: object, message: str) -> None:
    if not isinstance(value, str) or not value:
        raise ValueError(message)


def _source_manifest(sources: tuple[SourceDescriptor, ...]) -> list[list[object]]:
    source_manifest = sorted(source.canonical() for source in sources)
    paths = [source[0] for source in source_manifest]
    if len(paths) != len(set(paths)):
        raise ValueError("source logical paths must be unique")
    return source_manifest


def action_key(action: CompileActionDescriptor) -> str | None:
    """Return the persistent SHA-256 key, or None for implicit model identity."""
    if not action.persistent:
        return None
    return sha256_bytes(canonical_json_bytes(action.canonical()))


class CompileCache:
    """Local, owner-restricted cache of normalized compile plans."""

    def __init__(self, state_root: Path | None = None) -> None:
        if state_root is None:
            configured = os.environ.get("LLM_WIKI_STATE_ROOT") or os.environ.get("LLM_WIKI_ROOT")
            state_root = Path(configured) if configured else Path(__file__).resolve().parent.parent
        self.state_root = Path(state_root).resolve(strict=False)
        self.cache_dir = self.state_root / "cache" / "compile"

    def key(self, action: CompileActionDescriptor) -> str | None:
        return action_key(action)

    def get(
        self,
        action: CompileActionDescriptor,
        validator: PlanValidator,
    ) -> dict[str, object] | None:
        """Read and deterministically revalidate one cache entry, failing closed."""
        try:
            return self._validated_entry(action, validator)
        except Exception:  # noqa: BLE001 - cache reads and validators fail closed
            return None

    def _validated_entry(
        self,
        action: CompileActionDescriptor,
        validator: PlanValidator,
    ) -> dict[str, object] | None:
        key = self.key(action)
        if key is None:
            return None
        _validate_action_schema(action)
        self._validate_location(create=False)
        raw = _read_cache_entry(self.cache_dir / f"{key}.json")
        return _accepted_payload(_cached_payload(raw, key), validator)

    def put(
        self,
        action: CompileActionDescriptor,
        normalized_plan: dict[str, object],
        *,
        failure_class: str | None = None,
    ) -> Path:
        """Atomically store one successful validated normalized plan."""
        if failure_class is not None:
            raise ValueError("only successful compile plans are cacheable")
        key = self._persistent_key(action)
        _validate_action_schema(action)
        _validate_normalized_plan(normalized_plan)
        data = _bounded_record_bytes(key, normalized_plan)
        self._validate_location(create=True)
        return self._publish(key, data)

    def _persistent_key(self, action: CompileActionDescriptor) -> str:
        key = self.key(action)
        if key is None:
            raise ValueError("explicit model identity is required for persistent caching")
        return key

    def _publish(self, key: str, data: bytes) -> Path:
        """Stage the entry owner-only beside its target, then replace atomically."""
        target = self.cache_dir / f"{key}.json"
        descriptor, name = tempfile.mkstemp(prefix=f".{key}.", dir=self.cache_dir)
        temporary = Path(name)
        try:
            _write_staged_entry(descriptor, temporary, data)
            os.replace(temporary, target)
        except BaseException:
            _discard_staging(temporary)
            raise
        fsync_directory(self.cache_dir)
        _verify_owner_only(target, 0o600)
        return target

    def _validate_location(self, *, create: bool) -> None:
        if _known_network_path(self.state_root) or _windows_reparse_point(self.state_root):
            raise PermissionError("compile cache requires a local state root")
        current = self.state_root
        for part in ("cache", "compile"):
            current = current / part
            if not _secure_cache_directory(current, create=create):
                return
        _require_inside_state_root(self.cache_dir, self.state_root)


_CACHE_RECORD_FIELDS = frozenset({"schema_version", "action_key", "payload_digest", "payload"})


def _cached_payload(raw: bytes, key: str) -> dict[str, object] | None:
    """The payload of a canonical, current and intact record; None otherwise."""
    record = json.loads(raw.decode("utf-8"))
    if not _current_record(record, raw, key):
        return None
    return _intact_payload(record)


def _current_record(record: object, raw: bytes, key: str) -> bool:
    if not isinstance(record, dict) or set(record) != _CACHE_RECORD_FIELDS:
        return False
    if canonical_json_bytes(record) != raw:
        return False
    return record["schema_version"] == CACHE_SCHEMA_VERSION and record["action_key"] == key


def _intact_payload(record: dict) -> dict[str, object] | None:
    payload = record["payload"]
    if not isinstance(payload, dict):
        return None
    if sha256_bytes(canonical_json_bytes(payload)) != record["payload_digest"]:
        return None
    return payload


def _accepted_payload(
    payload: dict[str, object] | None, validator: PlanValidator
) -> dict[str, object] | None:
    if payload is None:
        return None
    _validate_normalized_plan(payload)
    if validator(payload) is not True:
        raise ValueError("application validator rejected cached compile plan")
    return payload


def _bounded_record_bytes(key: str, normalized_plan: dict[str, object]) -> bytes:
    payload = json.loads(canonical_json_bytes(normalized_plan))
    record = {
        "schema_version": CACHE_SCHEMA_VERSION,
        "action_key": key,
        "payload_digest": sha256_bytes(canonical_json_bytes(payload)),
        "payload": payload,
    }
    data = canonical_json_bytes(record)
    if len(data) > MAX_CACHE_ENTRY_BYTES:
        raise ValueError("canonical compile cache entry is too large")
    return data


def _write_staged_entry(descriptor: int, temporary: Path, data: bytes) -> None:
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    _restrict_owner_only(temporary, 0o600)
    if temporary.is_symlink() or not temporary.is_file():
        raise PermissionError("cache staging file is not secure")


def _discard_staging(temporary: Path) -> None:
    try:
        temporary.unlink()
    except OSError:
        pass


def _secure_cache_directory(path: Path, *, create: bool) -> bool:
    """Whether the directory exists and is owner-only; False when absent and not created."""
    if create:
        _make_private_directory(path)
    if not path.exists():
        return _absent_directory(create)
    _require_private_directory(path, create=create)
    return True


def _make_private_directory(path: Path) -> None:
    try:
        path.mkdir(mode=0o700)
    except FileExistsError:
        pass


def _absent_directory(create: bool) -> bool:
    if create:
        raise PermissionError("compile cache directory could not be created")
    return False


def _require_private_directory(path: Path, *, create: bool) -> None:
    if path.is_symlink() or not path.is_dir():
        raise PermissionError("compile cache directory is not secure")
    if create:
        _restrict_owner_only(path, 0o700)
    _verify_owner_only(path, 0o700)


def _require_inside_state_root(cache_dir: Path, state_root: Path) -> None:
    try:
        cache_dir.resolve(strict=True).relative_to(state_root)
    except (OSError, ValueError) as exc:
        raise PermissionError("compile cache escaped the state root") from exc


def _validate_digest(value: str, label: str) -> None:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise ValueError(f"{label} must be a lowercase SHA-256 digest")


def _validate_logical_path(value: str) -> None:
    if not isinstance(value, str) or not value or "\\" in value:
        raise ValueError("source logical path must be relative POSIX syntax")
    if _escapes_vault(value):
        raise ValueError("source logical path must remain inside the vault")


def _escapes_vault(value: str) -> bool:
    path = PurePosixPath(value)
    if not path.parts or value == "." or path.is_absolute():
        return True
    return ".." in path.parts or re.match(r"^[A-Za-z]:", value) is not None


def _restricted_mapping(value: Mapping[str, object], label: str) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{label} must be a mapping")
    normalized = json.loads(canonical_json_bytes(dict(value)))
    if not isinstance(normalized, dict):
        raise TypeError(f"{label} must be a mapping")
    return normalized


def _validate_normalized_plan(
    plan: dict[str, object],
) -> None:
    _validate_schema_value(plan, _COMPILE_PLAN_SCHEMA, "compile plan")
    if json.loads(canonical_json_bytes(plan)) != plan:
        raise ValueError("expected a normalized compile plan in the restricted JSON domain")
    operations = plan["operations"]
    assert isinstance(operations, list)
    seen_paths: set[str] = set()
    for index, operation in enumerate(operations):
        _validate_operation_path(index, operation, seen_paths)


def _validate_operation_path(index: int, operation: object, seen_paths: set[str]) -> None:
    assert isinstance(operation, dict)
    path = operation["path"]
    assert isinstance(path, str)
    _require_normalized_operation_path(index, path)
    if path in seen_paths:
        raise ValueError("compile plan operation paths must be unique")
    seen_paths.add(path)


def _require_normalized_operation_path(index: int, path: str) -> None:
    try:
        _validate_logical_path(path)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"compile plan operation {index} has an unsafe path") from exc
    if str(PurePosixPath(path)) != path or "\x00" in path:
        raise ValueError(f"compile plan operation {index} path is not normalized")


def _read_cache_entry(path: Path) -> bytes:
    metadata = path.lstat()
    if path.is_symlink() or not stat.S_ISREG(metadata.st_mode):
        raise PermissionError("cache entry must be a regular non-symlink file")
    _verify_owner_only(path, 0o600)
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        return _read_verified_descriptor(path, descriptor, _file_identity(metadata))
    finally:
        os.close(descriptor)


def _file_identity(info: os.stat_result) -> tuple[int, int, int, int]:
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)


def _read_verified_descriptor(path: Path, descriptor: int, metadata_identity: tuple) -> bytes:
    """Read the opened entry, proving it is the file that was checked before and after."""
    opened = os.fstat(descriptor)
    _require_bounded_regular_entry(opened)
    opened_identity = _file_identity(opened)
    if metadata_identity != opened_identity:
        raise PermissionError("cache entry changed before open")
    _require_owner_only_descriptor(opened)
    raw = _read_bounded_entry(descriptor)
    _require_unchanged_during_read(path, descriptor, opened_identity)
    _verify_owner_only(path, 0o600)
    verified = path.lstat()
    if opened_identity[:2] != (verified.st_dev, verified.st_ino):
        raise PermissionError("cache entry changed during permission verification")
    return raw


def _require_bounded_regular_entry(opened: os.stat_result) -> None:
    if not stat.S_ISREG(opened.st_mode) or opened.st_size > MAX_CACHE_ENTRY_BYTES:
        raise PermissionError("cache entry descriptor is not a bounded regular file")


def _require_owner_only_descriptor(opened: os.stat_result) -> None:
    if os.name == "posix" and stat.S_IMODE(opened.st_mode) != 0o600:
        raise PermissionError("cache entry descriptor is not owner-only")


def _read_bounded_entry(descriptor: int) -> bytes:
    with os.fdopen(descriptor, "rb", closefd=False) as handle:
        raw = handle.read(MAX_CACHE_ENTRY_BYTES + 1)
    if len(raw) > MAX_CACHE_ENTRY_BYTES:
        raise ValueError("cache entry is too large")
    return raw


def _require_unchanged_during_read(path: Path, descriptor: int, opened_identity: tuple) -> None:
    after = _file_identity(os.fstat(descriptor))
    current = path.lstat()
    if opened_identity != after or opened_identity[:2] != (current.st_dev, current.st_ino):
        raise PermissionError("cache entry changed during read")


def _validate_action_schema(action: CompileActionDescriptor) -> None:
    if (
        action.schema_version != COMPILE_PLAN_SCHEMA_VERSION
        or action.schema_hash != COMPILE_PLAN_SCHEMA_HASH
    ):
        raise ValueError("action does not identify the committed compile-plan-v2 schema")


def _validate_schema_value(value: object, schema: Mapping[str, object], location: str) -> None:
    validator = _TYPE_VALIDATORS.get(schema.get("type"))
    if validator is not None:
        validator(value, schema, location)
    _validate_schema_choice(value, schema, location)


def _validate_schema_choice(value: object, schema: Mapping[str, object], location: str) -> None:
    if "const" in schema and value != schema["const"]:
        raise ValueError(f"{location} does not match the committed compile plan schema")
    choices = schema.get("enum")
    if isinstance(choices, list) and value not in choices:
        raise ValueError(f"{location} is not an allowed value")


def _validate_schema_object(value: object, schema: Mapping[str, object], location: str) -> None:
    if not isinstance(value, dict):
        raise ValueError(f"{location} must be an object")
    properties = _checked_schema_properties(value, schema, location)
    for name, item in value.items():
        _validate_schema_child(item, properties.get(name), f"{location}.{name}")


def _checked_schema_properties(value: dict, schema: Mapping[str, object], location: str) -> dict:
    """The schema's properties, after the required and additional-field checks."""
    _require_schema_fields(value, schema, location)
    properties = schema.get("properties", {})
    assert isinstance(properties, dict)
    if schema.get("additionalProperties") is False:
        _require_no_extra_fields(value, properties, location)
    return properties


def _require_schema_fields(value: dict, schema: Mapping[str, object], location: str) -> None:
    required = schema.get("required", [])
    assert isinstance(required, list)
    missing = [name for name in required if name not in value]
    if missing:
        raise ValueError(f"{location} is missing required fields: {', '.join(missing)}")


def _require_no_extra_fields(value: dict, properties: dict, location: str) -> None:
    extras = set(value) - set(properties)
    if extras:
        raise ValueError(f"{location} has unsupported fields: {', '.join(sorted(extras))}")


def _validate_schema_child(item: object, child_schema: object, location: str) -> None:
    if isinstance(child_schema, dict):
        _validate_schema_value(item, child_schema, location)


def _validate_schema_array(value: object, schema: Mapping[str, object], location: str) -> None:
    if not isinstance(value, list):
        raise ValueError(f"{location} must be an array")
    items = schema.get("items")
    if not isinstance(items, dict):
        return
    for index, item in enumerate(value):
        _validate_schema_value(item, items, f"{location}[{index}]")


def _validate_schema_string(value: object, schema: Mapping[str, object], location: str) -> None:
    if not isinstance(value, str):
        raise ValueError(f"{location} must be a string")
    minimum = schema.get("minLength")
    if isinstance(minimum, int) and len(value) < minimum:
        raise ValueError(f"{location} is too short")


_TYPE_VALIDATORS = {
    "object": _validate_schema_object,
    "array": _validate_schema_array,
    "string": _validate_schema_string,
}


def _restrict_owner_only(path: Path, mode: int) -> None:
    if os.name == "posix":
        path.chmod(mode)
        _verify_owner_only(path, mode)
        return
    if os.name != "nt":
        raise PermissionError("owner-only cache permissions are unsupported")
    _harden_cache_windows_acl(path)
    _verify_owner_only(path, mode)


def _verify_owner_only(path: Path, mode: int) -> None:
    if path.is_symlink():
        raise PermissionError("cache path must not be a symlink")
    _require_cache_path_type(path)
    if not _is_owner_only(path, mode):
        raise PermissionError("cache path is not owner-only")


def _require_cache_path_type(path: Path) -> None:
    info = path.stat(follow_symlinks=False)
    expected_type = stat.S_IFDIR if path.is_dir() else stat.S_IFREG
    if stat.S_IFMT(info.st_mode) != expected_type:
        raise PermissionError("cache path has an invalid type")


def _is_owner_only(path: Path, mode: int) -> bool:
    if os.name == "nt":
        return _windows_acl_is_owner_only(path)
    return os.name == "posix" and stat.S_IMODE(path.stat().st_mode) == mode


def _windows_acl_is_owner_only(path: Path) -> bool:
    try:
        verified = _run_acl_command(["icacls", str(path)])
    except Exception:  # noqa: BLE001 - ACL validation is fail-closed
        return False
    if verified.returncode != 0:
        return False
    identity = _windows_acl_identity()
    return _sole_full_owner_entry(path, identity, _acl_output_text(verified.stdout))


def _sole_full_owner_entry(path: Path, identity: str, output: str) -> bool:
    """Exactly one ACL entry, held by the owner, full control and not inherited."""
    acl_lines = [line.strip() for line in output.splitlines() if ":(" in line]
    if len(acl_lines) != 1:
        return False
    return _full_owner_entry(path, identity, acl_lines[0])


def _full_owner_entry(path: Path, identity: str, line: str) -> bool:
    if _acl_principal(path, line).casefold() != identity.casefold():
        return False
    return "(F)" in line and "(I)" not in line


def _acl_principal(path: Path, line: str) -> str:
    principal = line.split(":(", 1)[0].strip()
    path_text = str(path)
    if principal.casefold().startswith(path_text.casefold()):
        principal = principal[len(path_text) :].strip()
    return principal


_BROAD_SIDS = (
    "*S-1-1-0",  # Everyone
    "*S-1-3-0",  # Creator Owner
    "*S-1-3-4",  # Owner Rights
    "*S-1-5-18",  # Local System
    "*S-1-5-32-544",  # Administrators
    "*S-1-5-32-545",  # Users
    "*S-1-15-2-1",  # All application packages
    "*S-1-15-2-2",  # All restricted application packages
)


def _harden_cache_windows_acl(path: Path) -> None:
    identity = _windows_acl_identity()
    permission = f"{identity}:(OI)(CI)(F)" if path.is_dir() else f"{identity}:(F)"
    commands = [
        ["icacls", str(path), "/inheritance:r", "/grant:r", permission],
        ["icacls", str(path), "/remove:g", *_BROAD_SIDS],
        ["icacls", str(path), "/remove:d", *_BROAD_SIDS],
    ]
    try:
        results = [_run_acl_command(command) for command in commands]
    except Exception as exc:  # noqa: BLE001 - ACL enforcement is fail-closed
        raise PermissionError("owner-only cache permissions are unavailable") from exc
    _require_hardened_acl(path, results)


def _require_hardened_acl(path: Path, results: list) -> None:
    if any(result.returncode != 0 for result in results) or not _windows_acl_is_owner_only(path):
        raise PermissionError("owner-only cache ACL enforcement failed")
