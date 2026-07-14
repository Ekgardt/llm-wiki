"""Content-addressed cache for validated, normalized compile plans."""
from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Protocol

from reliable_memory import (
    _known_network_path,
    _windows_reparse_point,
    canonical_json_bytes,
    sha256_bytes,
)

CACHE_SCHEMA_VERSION = 1
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


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
    schema_hash: str
    normalization_version: str
    feature_flags: Mapping[str, object]
    draft_calls: tuple[CompileCallDescriptor, ...]
    critique_calls: tuple[CompileCallDescriptor, ...]
    sources: tuple[SourceDescriptor, ...]

    def canonical(self) -> dict[str, object]:
        if not isinstance(self.compiler_version, str) or not self.compiler_version:
            raise ValueError("compiler version is required")
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
    if path.is_absolute() or ".." in path.parts or re.match(r"^[A-Za-z]:", value):
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
    if not isinstance(plan, dict) or not isinstance(plan.get("operations"), list):
        raise ValueError("expected a normalized compile plan with operations")
    if json.loads(canonical_json_bytes(plan)) != plan:
        raise ValueError("expected a normalized compile plan in the restricted JSON domain")
    if validator is not None and validator(plan) is False:
        raise ValueError("expected a validated normalized compile plan")


def _restrict_owner_only(path: Path, mode: int) -> None:
    if os.name == "posix":
        path.chmod(mode)
        _verify_owner_only(path, mode)
        return
    if os.name != "nt":
        raise PermissionError("owner-only cache permissions are unsupported")
    username = os.environ.get("USERNAME")
    if not username:
        raise PermissionError("owner-only cache permissions are unavailable")
    permission = "(OI)(CI)F" if path.is_dir() else "F"
    result = subprocess.run(
        [
            "icacls",
            str(path),
            "/inheritance:r",
            "/grant:r",
            f"{username}:{permission}",
        ],
        capture_output=True,
        check=False,
        timeout=5,
    )
    if result.returncode != 0:
        raise PermissionError("owner-only cache permissions are unavailable")


def _verify_owner_only(path: Path, mode: int) -> None:
    if path.is_symlink():
        raise PermissionError("cache path must not be a symlink")
    info = path.stat(follow_symlinks=False)
    expected_type = stat.S_IFDIR if path.is_dir() else stat.S_IFREG
    if stat.S_IFMT(info.st_mode) != expected_type:
        raise PermissionError("cache path has an invalid type")
    if os.name == "posix" and stat.S_IMODE(info.st_mode) != mode:
        raise PermissionError("cache path is not owner-only")
