"""Structural ownership adapters for supported IDE hook configurations."""

from __future__ import annotations

import hashlib
import json
import os
from collections import Counter
from collections.abc import Mapping, Sequence
from pathlib import Path

from install_control import InstallControlError, ManagedResource
from integration_config_backup import publish_configuration
from reliable_memory import canonical_json_bytes, fsync_directory

MAX_CONFIG_BYTES = 2 * 1024 * 1024
_MISSING = object()
_ROOT_PLACEHOLDER = b"__LLM_WIKI_ROOT__"


def _absolute_destination(path: Path) -> Path:
    return Path(os.path.abspath(Path(path)))


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON key")
        value[key] = item
    return value


def _reject_constant(value: str) -> object:
    raise ValueError(f"invalid JSON constant: {value}")


def _decode_object(raw: bytes) -> dict[str, object]:
    try:
        value = json.loads(
            raw,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise InstallControlError("integration_hook_config_invalid_json") from exc
    if not isinstance(value, dict):
        raise InstallControlError("integration_hook_config_not_object")
    return value


def _existing_config_bytes(path: Path) -> bytes | None:
    if not path.exists():
        return None
    if not path.is_file() or path.stat().st_size > MAX_CONFIG_BYTES:
        raise InstallControlError("integration_hook_config_unsafe")
    return path.read_bytes()


def _read_config(path: Path) -> tuple[dict[str, object], bytes | None]:
    if path.is_symlink():
        raise InstallControlError("integration_hook_config_symlink")
    raw = _existing_config_bytes(path)
    if raw is None:
        return {}, None
    if len(raw) > MAX_CONFIG_BYTES:
        raise InstallControlError("integration_hook_config_oversized")
    return _decode_object(raw), raw


def _template_bytes(root: Path, relative: str) -> bytes:
    path = Path(root) / relative
    if path.is_symlink() or not path.is_file():
        raise InstallControlError("integration_hook_template_missing")
    if path.stat().st_size > MAX_CONFIG_BYTES:
        raise InstallControlError("integration_hook_template_oversized")
    raw = path.read_bytes()
    if _ROOT_PLACEHOLDER not in raw:
        raise InstallControlError("integration_hook_template_placeholder_missing")
    return raw


def _materialized_template(root: Path, relative: str) -> dict[str, object]:
    raw = _template_bytes(root, relative)
    escaped_root = json.dumps(str(Path(root).resolve()), ensure_ascii=True)[1:-1]
    return _decode_object(raw.replace(_ROOT_PLACEHOLDER, escaped_root.encode("ascii")))


def _render_config(value: Mapping[str, object]) -> bytes:
    text = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)
    return f"{text}\n".encode()


def _publish_config(path: Path, updated: Mapping[str, object], original: bytes | None) -> None:
    digest = None
    if original is not None:
        digest = hashlib.sha256(original).hexdigest()
    publish_configuration(
        path,
        _render_config(updated),
        expected_original=original,
        expected_original_sha256=digest,
        max_original_bytes=MAX_CONFIG_BYTES,
    )


def _require_cursor_version(version: object) -> None:
    if type(version) is not int:
        raise InstallControlError("integration_cursor_schema_conflict")
    if version != 1:
        raise InstallControlError("integration_cursor_schema_conflict")


def _cursor_hooks(config: Mapping[str, object]) -> dict[str, object]:
    _require_cursor_version(config.get("version", 1))
    hooks = config.get("hooks", {})
    if not isinstance(hooks, dict):
        raise InstallControlError("integration_cursor_schema_conflict")
    if not _supported_cursor_arrays(hooks):
        raise InstallControlError("integration_cursor_schema_conflict")
    return hooks


def _supported_cursor_arrays(hooks: Mapping[str, object]) -> bool:
    for items in hooks.values():
        if not isinstance(items, list):
            return False
        if any(not isinstance(item, dict) for item in items):
            return False
    return True


def _validate_cursor_projection(
    handlers: Mapping[str, Sequence[Mapping[str, object]]],
) -> dict[str, list[dict[str, object]]]:
    return {
        _validate_cursor_event(event, items): [dict(item) for item in items]
        for event, items in handlers.items()
    }


def _validate_cursor_event(event: object, items: Sequence[object]) -> str:
    if not isinstance(event, str) or not event or not items:
        raise ValueError("Cursor owned hook projection is invalid")
    return event


def _handler_counts(items: Sequence[object]) -> Counter[bytes]:
    if any(not isinstance(item, dict) for item in items):
        raise InstallControlError("integration_cursor_schema_conflict")
    return Counter(canonical_json_bytes(item) for item in items)


def _expected_counts_match(actual: Counter[bytes], expected: Counter[bytes]) -> bool:
    return all(actual[key] == count for key, count in expected.items())


def _cursor_event_state(current: Sequence[object], expected: Sequence[object]) -> str:
    actual_counts = _handler_counts(current)
    expected_counts = _handler_counts(expected)
    matched = sum(min(actual_counts[key], count) for key, count in expected_counts.items())
    total = sum(expected_counts.values())
    if matched == 0:
        return "absent"
    if matched != total:
        return "conflict"
    if not _expected_counts_match(actual_counts, expected_counts):
        return "conflict"
    return "present"


def _cursor_projection(
    config: Mapping[str, object], expected: Mapping[str, Sequence[object]]
) -> bytes | None:
    hooks = _cursor_hooks(config)
    states = [_cursor_event_state(hooks.get(event, []), items) for event, items in expected.items()]
    handlers: dict[frozenset[str], object] = {
        frozenset({"absent"}): None,
        frozenset({"present"}): expected,
    }
    try:
        projection = handlers[frozenset(states)]
    except KeyError as exc:
        raise InstallControlError("integration_cursor_ownership_conflict") from exc
    if projection is None:
        return None
    return canonical_json_bytes({"hooks": projection, "version": 1})


def _cursor_projection_handlers(raw: bytes) -> dict[str, object]:
    value = _decode_object(raw)
    if set(value) != {"hooks", "version"}:
        raise InstallControlError("integration_cursor_projection_invalid")
    return _cursor_hooks(value)


def _cursor_projection_any(
    config: Mapping[str, object], candidates: Sequence[bytes]
) -> bytes | None:
    matches: list[bytes] = []
    for candidate in candidates:
        handlers = _cursor_projection_handlers(candidate)
        if _cursor_projection(config, handlers) is not None:
            matches.append(candidate)
    if not matches:
        return None
    if len(matches) != 1:
        raise InstallControlError("integration_cursor_ownership_conflict")
    return matches[0]


def _without_handlers(current: Sequence[object], owned: Sequence[object]) -> list[object]:
    remaining = _handler_counts(owned)
    result: list[object] = []
    for item in current:
        fingerprint = canonical_json_bytes(item)
        if remaining[fingerprint] > 0:
            remaining[fingerprint] -= 1
            continue
        result.append(item)
    return result


def _merge_cursor_handlers(
    config: dict[str, object],
    owned: Mapping[str, Sequence[object]],
    replacement: Mapping[str, Sequence[object]],
) -> dict[str, object]:
    hooks = _cursor_hooks(config)
    updated = dict(hooks)
    for event in set(owned) | set(replacement):
        unrelated = _without_handlers(updated.get(event, []), owned.get(event, []))
        combined = [*unrelated, *replacement.get(event, [])]
        if combined:
            updated[event] = combined
            continue
        updated.pop(event, None)
    result = dict(config)
    result["hooks"] = updated
    result["version"] = 1
    return result


def _retire_new_cursor_file(
    path: Path,
    updated: Mapping[str, object],
    replacement: bytes | None,
    config_existed: bool,
) -> bool:
    if replacement is not None or config_existed:
        return False
    if updated != {"hooks": {}, "version": 1}:
        return False
    path.unlink()
    fsync_directory(path.parent)
    return True


def _cursor_replacement_handlers(replacement: bytes | None) -> dict[str, object]:
    if replacement is None:
        return {}
    return _cursor_projection_handlers(replacement)


def _require_cursor_expected(
    config: Mapping[str, object], expected: bytes | None, replacement: bytes | None
) -> None:
    if expected is not None:
        if _cursor_projection_any(config, (expected,)) != expected:
            raise InstallControlError("integration_hook_config_changed")
        return
    candidates = () if replacement is None else (replacement,)
    if _cursor_projection_any(config, candidates) is not None:
        raise InstallControlError("integration_hook_config_changed")


def _write_cursor_projection(
    path: Path,
    expected: bytes | None,
    replacement: bytes | None,
    metadata: Mapping[str, object],
) -> None:
    config, original = _read_config(path)
    _require_cursor_expected(config, expected, replacement)
    owned = {} if expected is None else _cursor_projection_handlers(expected)
    updated = _merge_cursor_handlers(config, owned, _cursor_replacement_handlers(replacement))
    existed = bool(metadata.get("config_existed", True))
    if _retire_new_cursor_file(path, updated, replacement, existed):
        return
    _publish_config(path, updated, original)


def _write_cursor(
    path: Path,
    desired: Mapping[str, Sequence[object]],
    replacement_bytes: bytes | None,
    config_existed: bool,
) -> None:
    config, _original = _read_config(path)
    current = _cursor_projection(config, desired)
    if current == replacement_bytes:
        return
    _write_cursor_projection(
        path,
        current,
        replacement_bytes,
        {"config_existed": config_existed},
    )


def cursor_hooks_resource(
    destination: Path,
    handlers: Mapping[str, Sequence[Mapping[str, object]]],
) -> ManagedResource:
    """Own exact Cursor handlers while preserving all unrelated configuration."""
    path = _absolute_destination(destination)
    desired_handlers = _validate_cursor_projection(handlers)
    desired = canonical_json_bytes({"hooks": desired_handlers, "version": 1})
    config_existed = path.exists()
    return ManagedResource(
        resource_id="cursor-user-hooks",
        kind="cursor_hooks_fragment",
        locator=str(path),
        desired=desired,
        read_owned=lambda: _cursor_projection(_read_config(path)[0], desired_handlers),
        write_owned=lambda value: _write_cursor(path, desired_handlers, value, config_existed),
        recognizes=lambda current: current == desired,
        read_projections=lambda candidates: _cursor_projection_any(
            _read_config(path)[0], candidates
        ),
        write_projection=lambda expected, replacement, metadata: _write_cursor_projection(
            path, expected, replacement, metadata
        ),
        metadata={"config_existed": config_existed},
        adopt_as_absent=False,
    )


def _antigravity_projection(
    config: Mapping[str, object], expected: Mapping[str, object]
) -> bytes | None:
    current = config.get("llm-wiki", _MISSING)
    if current is _MISSING:
        return None
    if not isinstance(current, dict):
        raise InstallControlError("integration_antigravity_ownership_conflict")
    if canonical_json_bytes(current) != canonical_json_bytes(expected):
        raise InstallControlError("integration_antigravity_ownership_conflict")
    return canonical_json_bytes(expected)


def _antigravity_projection_any(
    config: Mapping[str, object], candidates: Sequence[bytes]
) -> bytes | None:
    current = config.get("llm-wiki", _MISSING)
    if current is _MISSING:
        return None
    if not isinstance(current, dict):
        raise InstallControlError("integration_antigravity_ownership_conflict")
    encoded = canonical_json_bytes(current)
    if encoded not in candidates:
        raise InstallControlError("integration_antigravity_ownership_conflict")
    return encoded


def _antigravity_replacement(replacement: bytes | None) -> dict[str, object] | None:
    if replacement is None:
        return None
    return _decode_object(replacement)


def _expected_candidates(expected: bytes | None) -> tuple[bytes, ...]:
    if expected is None:
        return ()
    return (expected,)


def _merge_antigravity(
    config: Mapping[str, object], replacement: bytes | None
) -> dict[str, object]:
    updated = dict(config)
    value = _antigravity_replacement(replacement)
    if value is None:
        updated.pop("llm-wiki", None)
        return updated
    updated["llm-wiki"] = value
    return updated


def _retire_new_antigravity_file(
    path: Path, updated: Mapping[str, object], metadata: Mapping[str, object]
) -> bool:
    if updated or bool(metadata.get("config_existed", True)):
        return False
    path.unlink()
    fsync_directory(path.parent)
    return True


def _write_antigravity_projection(
    path: Path,
    expected: bytes | None,
    replacement: bytes | None,
    metadata: Mapping[str, object],
) -> None:
    config, original = _read_config(path)
    candidates = _expected_candidates(expected)
    if _antigravity_projection_any(config, candidates) != expected:
        raise InstallControlError("integration_hook_config_changed")
    updated = _merge_antigravity(config, replacement)
    if _retire_new_antigravity_file(path, updated, metadata):
        return
    _publish_config(path, updated, original)


def _write_antigravity(
    path: Path,
    desired: Mapping[str, object],
    replacement: bytes | None,
    config_existed: bool,
) -> None:
    config, _original = _read_config(path)
    current = _antigravity_projection(config, desired)
    if current == replacement:
        return
    _write_antigravity_projection(path, current, replacement, {"config_existed": config_existed})


def antigravity_hooks_resource(
    destination: Path, owned_config: Mapping[str, object]
) -> ManagedResource:
    """Own only Antigravity's exact top-level ``llm-wiki`` object."""
    path = _absolute_destination(destination)
    desired_config = dict(owned_config)
    desired = canonical_json_bytes(desired_config)
    config_existed = path.exists()
    return ManagedResource(
        resource_id="antigravity-user-hooks",
        kind="antigravity_hooks_fragment",
        locator=str(path),
        desired=desired,
        read_owned=lambda: _antigravity_projection(_read_config(path)[0], desired_config),
        write_owned=lambda value: _write_antigravity(path, desired_config, value, config_existed),
        recognizes=lambda current: current == desired,
        read_projections=lambda candidates: _antigravity_projection_any(
            _read_config(path)[0], candidates
        ),
        write_projection=lambda expected, replacement, metadata: _write_antigravity_projection(
            path, expected, replacement, metadata
        ),
        metadata={"config_existed": config_existed},
        adopt_as_absent=False,
    )


def _cursor_template_handlers(root: Path) -> dict[str, object]:
    template = _materialized_template(root, "integrations/cursor/hooks.json")
    if set(template) != {"hooks", "version"}:
        raise InstallControlError("integration_cursor_template_invalid")
    return _cursor_hooks(template)


def _antigravity_template_config(root: Path) -> dict[str, object]:
    template = _materialized_template(root, "integrations/antigravity/hooks.json")
    if set(template) != {"llm-wiki"}:
        raise InstallControlError("integration_antigravity_template_invalid")
    owned = template["llm-wiki"]
    if not isinstance(owned, dict):
        raise InstallControlError("integration_antigravity_template_invalid")
    return owned


def managed_ide_hook_resources(root: Path, home: Path) -> list[ManagedResource]:
    """Build exact user-hook resources from the tracked host templates."""
    home = _absolute_destination(home)
    return [
        cursor_hooks_resource(home / ".cursor" / "hooks.json", _cursor_template_handlers(root)),
        antigravity_hooks_resource(
            home / ".gemini" / "config" / "hooks.json",
            _antigravity_template_config(root),
        ),
    ]
