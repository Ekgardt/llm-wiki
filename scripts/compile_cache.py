"""Content-addressed cache for validated, normalized compile plans."""
from __future__ import annotations

import json
import os
import re
import stat
import tempfile
from collections.abc import Callable, Mapping
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
    sha256_bytes,
)

CACHE_SCHEMA_VERSION = 1
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_COMPILE_PLAN_SCHEMA_PATH = Path(__file__).resolve().parent / "schemas" / "compile-plan-v2.json"
_COMPILE_PLAN_SCHEMA_BYTES = _COMPILE_PLAN_SCHEMA_PATH.read_bytes()
_COMPILE_PLAN_SCHEMA = json.loads(_COMPILE_PLAN_SCHEMA_BYTES.decode("utf-8"))
COMPILE_PLAN_SCHEMA_VERSION = _COMPILE_PLAN_SCHEMA["properties"]["schema_version"]["const"]
COMPILE_PLAN_SCHEMA_HASH = sha256_bytes(canonical_json_bytes(_COMPILE_PLAN_SCHEMA))


class PlanValidator(Protocol):
    def __call__(self, plan: dict[str, object]) -> bool | None: ...


@dataclass(frozen=True, order=True)
class SourceDescriptor:
    logical_path: str
    byte_length: int
    sha256: str

    def canonical(self) -> list[object]:
        _validate_logical_path(self.logical_path)
        if not isinstance(self.byte_length, int) or isinstance(self.byte_length, bool):
            raise TypeError("source byte length must be an integer")
        if self.byte_length < 0:
            raise ValueError("source byte length must be non-negative")
        _validate_digest(self.sha256, "source SHA-256")
        return [self.logical_path, self.byte_length, self.sha256]


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
        if not self.provider or not isinstance(self.provider, str):
            raise ValueError("provider identity is required")
        if self.model is not None and (not isinstance(self.model, str) or not self.model.strip()):
            raise ValueError("model identity must be explicit or null")
        if self.structured_output not in {"native", "prompt"}:
            raise ValueError("structured output mode must be native or prompt")
        if not all(isinstance(item, str) and item for item in self.fallback_from):
            raise ValueError("fallback lineage entries must be non-empty strings")
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
        if not isinstance(self.compiler_version, str) or not self.compiler_version:
            raise ValueError("compiler version is required")
        if not isinstance(self.schema_version, str) or not self.schema_version:
            raise ValueError("schema version is required")
        _validate_digest(self.schema_hash, "schema hash")
        if not isinstance(self.normalization_version, str) or not self.normalization_version:
            raise ValueError("normalization version is required")
        flags = _restricted_mapping(self.feature_flags, "feature flags")
        draft_calls = [call.canonical() for call in self.draft_calls]
        critique_calls = [call.canonical() for call in self.critique_calls]
        if not draft_calls:
            raise ValueError("at least one draft call descriptor is required")
        source_manifest = sorted(source.canonical() for source in self.sources)
        paths = [source[0] for source in source_manifest]
        if len(paths) != len(set(paths)):
            raise ValueError("source logical paths must be unique")
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

    @property
    def persistent(self) -> bool:
        calls = (*self.draft_calls, *self.critique_calls)
        return bool(calls) and all(call.model is not None and call.model.strip() for call in calls)


# Short compatibility names for callers that treat descriptors as tuple records.
SourceTuple = SourceDescriptor
CallDescriptor = CompileCallDescriptor


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
        validator: PlanValidator | None = None,
    ) -> dict[str, object] | None:
        """Read and deterministically revalidate one cache entry, failing closed."""
        try:
            key = self.key(action)
            if key is None:
                return None
            _validate_action_schema(action)
            self._validate_location(create=False)
            path = self.cache_dir / f"{key}.json"
            if not path.exists() or path.is_symlink() or not path.is_file():
                return None
            _verify_owner_only(path, 0o600)
            raw = path.read_bytes()
            if len(raw) > 16 * 1024 * 1024:
                return None
            record = json.loads(raw.decode("utf-8"))
            if not isinstance(record, dict) or set(record) != {
                "schema_version",
                "action_key",
                "payload_digest",
                "payload",
            }:
                return None
            if canonical_json_bytes(record) != raw:
                return None
            if record["schema_version"] != CACHE_SCHEMA_VERSION or record["action_key"] != key:
                return None
            payload = record["payload"]
            if not isinstance(payload, dict):
                return None
            if sha256_bytes(canonical_json_bytes(payload)) != record["payload_digest"]:
                return None
            _validate_normalized_plan(payload, validator)
            return payload
        except Exception:  # noqa: BLE001 - cache reads and validators fail closed
            return None

    def put(
        self,
        action: CompileActionDescriptor,
        normalized_plan: dict[str, object],
        validator: PlanValidator | None = None,
        *,
        failure_class: str | None = None,
    ) -> Path:
        """Atomically store one successful validated normalized plan."""
        if failure_class is not None:
            raise ValueError("only successful compile plans are cacheable")
        key = self.key(action)
        if key is None:
            raise ValueError("explicit model identity is required for persistent caching")
        _validate_action_schema(action)
        _validate_normalized_plan(normalized_plan, validator)
        self._validate_location(create=True)
        payload = json.loads(canonical_json_bytes(normalized_plan))
        record = {
            "schema_version": CACHE_SCHEMA_VERSION,
            "action_key": key,
            "payload_digest": sha256_bytes(canonical_json_bytes(payload)),
            "payload": payload,
        }
        data = canonical_json_bytes(record)
        target = self.cache_dir / f"{key}.json"
        temporary: Path | None = None
        try:
            descriptor, name = tempfile.mkstemp(prefix=f".{key}.", dir=self.cache_dir)
            temporary = Path(name)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            _restrict_owner_only(temporary, 0o600)
            if temporary.is_symlink() or not temporary.is_file():
                raise PermissionError("cache staging file is not secure")
            os.replace(temporary, target)
            temporary = None
            _verify_owner_only(target, 0o600)
            return target
        finally:
            if temporary is not None:
                try:
                    temporary.unlink()
                except OSError:
                    pass

    def _validate_location(self, *, create: bool) -> None:
        if _known_network_path(self.state_root) or _windows_reparse_point(self.state_root):
            raise PermissionError("compile cache requires a local state root")
        current = self.state_root
        for part in ("cache", "compile"):
            current = current / part
            if create:
                try:
                    current.mkdir(mode=0o700)
                except FileExistsError:
                    pass
            if not current.exists():
                if create:
                    raise PermissionError("compile cache directory could not be created")
                return
            if current.is_symlink() or not current.is_dir():
                raise PermissionError("compile cache directory is not secure")
            if create:
                _restrict_owner_only(current, 0o700)
            _verify_owner_only(current, 0o700)
        try:
            self.cache_dir.resolve(strict=True).relative_to(self.state_root)
        except (OSError, ValueError) as exc:
            raise PermissionError("compile cache escaped the state root") from exc


def _validate_digest(value: str, label: str) -> None:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise ValueError(f"{label} must be a lowercase SHA-256 digest")


def _validate_logical_path(value: str) -> None:
    if not isinstance(value, str) or not value or "\\" in value:
        raise ValueError("source logical path must be relative POSIX syntax")
    path = PurePosixPath(value)
    if (
        not path.parts
        or value == "."
        or path.is_absolute()
        or ".." in path.parts
        or re.match(r"^[A-Za-z]:", value)
    ):
        raise ValueError("source logical path must remain inside the vault")


def _restricted_mapping(value: Mapping[str, object], label: str) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{label} must be a mapping")
    normalized = json.loads(canonical_json_bytes(dict(value)))
    if not isinstance(normalized, dict):
        raise TypeError(f"{label} must be a mapping")
    return normalized


def _validate_normalized_plan(
    plan: dict[str, object],
    validator: Callable[[dict[str, object]], bool | None] | None,
) -> None:
    _validate_schema_value(plan, _COMPILE_PLAN_SCHEMA, "compile plan")
    if json.loads(canonical_json_bytes(plan)) != plan:
        raise ValueError("expected a normalized compile plan in the restricted JSON domain")
    operations = plan["operations"]
    assert isinstance(operations, list)
    seen_paths: set[str] = set()
    for index, operation in enumerate(operations):
        assert isinstance(operation, dict)
        path = operation["path"]
        assert isinstance(path, str)
        try:
            _validate_logical_path(path)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"compile plan operation {index} has an unsafe path") from exc
        if str(PurePosixPath(path)) != path or "\x00" in path:
            raise ValueError(f"compile plan operation {index} path is not normalized")
        if path in seen_paths:
            raise ValueError("compile plan operation paths must be unique")
        seen_paths.add(path)
    if validator is not None and validator(plan) is False:
        raise ValueError("expected a validated normalized compile plan")


def _validate_action_schema(action: CompileActionDescriptor) -> None:
    if (
        action.schema_version != COMPILE_PLAN_SCHEMA_VERSION
        or action.schema_hash != COMPILE_PLAN_SCHEMA_HASH
    ):
        raise ValueError("action does not identify the committed compile-plan-v2 schema")


def _validate_schema_value(value: object, schema: Mapping[str, object], location: str) -> None:
    expected_type = schema.get("type")
    if expected_type == "object":
        if not isinstance(value, dict):
            raise ValueError(f"{location} must be an object")
        required = schema.get("required", [])
        assert isinstance(required, list)
        missing = [name for name in required if name not in value]
        if missing:
            raise ValueError(f"{location} is missing required fields: {', '.join(missing)}")
        properties = schema.get("properties", {})
        assert isinstance(properties, dict)
        if schema.get("additionalProperties") is False:
            extras = set(value) - set(properties)
            if extras:
                raise ValueError(f"{location} has unsupported fields: {', '.join(sorted(extras))}")
        for name, item in value.items():
            child_schema = properties.get(name)
            if isinstance(child_schema, dict):
                _validate_schema_value(item, child_schema, f"{location}.{name}")
    elif expected_type == "array":
        if not isinstance(value, list):
            raise ValueError(f"{location} must be an array")
        items = schema.get("items")
        if isinstance(items, dict):
            for index, item in enumerate(value):
                _validate_schema_value(item, items, f"{location}[{index}]")
    elif expected_type == "string":
        if not isinstance(value, str):
            raise ValueError(f"{location} must be a string")
        minimum = schema.get("minLength")
        if isinstance(minimum, int) and len(value) < minimum:
            raise ValueError(f"{location} is too short")
    if "const" in schema and value != schema["const"]:
        raise ValueError(f"{location} does not match the committed compile plan schema")
    choices = schema.get("enum")
    if isinstance(choices, list) and value not in choices:
        raise ValueError(f"{location} is not an allowed value")


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
    info = path.stat(follow_symlinks=False)
    expected_type = stat.S_IFDIR if path.is_dir() else stat.S_IFREG
    if stat.S_IFMT(info.st_mode) != expected_type:
        raise PermissionError("cache path has an invalid type")
    if not _is_owner_only(path, mode):
        raise PermissionError("cache path is not owner-only")


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
    acl_lines = [
        line.strip()
        for line in _acl_output_text(verified.stdout).splitlines()
        if ":(" in line
    ]
    owner_lines = [
        line
        for line in acl_lines
        if _acl_principal(path, line).casefold() == identity.casefold()
    ]
    return (
        len(acl_lines) == 1
        and len(owner_lines) == 1
        and "(F)" in owner_lines[0]
        and "(I)" not in owner_lines[0]
    )


def _acl_principal(path: Path, line: str) -> str:
    principal = line.split(":(", 1)[0].strip()
    path_text = str(path)
    if principal.casefold().startswith(path_text.casefold()):
        principal = principal[len(path_text) :].strip()
    return principal


def _harden_cache_windows_acl(path: Path) -> None:
    identity = _windows_acl_identity()
    permission = f"{identity}:(OI)(CI)(F)" if path.is_dir() else f"{identity}:(F)"
    broad_sids = [
        "*S-1-1-0",  # Everyone
        "*S-1-3-0",  # Creator Owner
        "*S-1-3-4",  # Owner Rights
        "*S-1-5-18",  # Local System
        "*S-1-5-32-544",  # Administrators
        "*S-1-5-32-545",  # Users
        "*S-1-15-2-1",  # All application packages
        "*S-1-15-2-2",  # All restricted application packages
    ]
    commands = [
        ["icacls", str(path), "/inheritance:r", "/grant:r", permission],
        ["icacls", str(path), "/remove:g", *broad_sids],
        ["icacls", str(path), "/remove:d", *broad_sids],
    ]
    try:
        results = [_run_acl_command(command) for command in commands]
    except Exception as exc:  # noqa: BLE001 - ACL enforcement is fail-closed
        raise PermissionError("owner-only cache permissions are unavailable") from exc
    if any(result.returncode != 0 for result in results) or not _windows_acl_is_owner_only(path):
        raise PermissionError("owner-only cache ACL enforcement failed")
