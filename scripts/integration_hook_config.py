"""Structural ownership adapters for supported IDE hook configurations."""

from __future__ import annotations

import hashlib
import json
import os
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import NamedTuple

from install_control import InstallControlError, ManagedResource, file_resource
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


def _template_raw(root: Path, relative: str) -> bytes:
    path = Path(root) / relative
    if path.is_symlink() or not path.is_file():
        raise InstallControlError("integration_hook_template_missing")
    if path.stat().st_size > MAX_CONFIG_BYTES:
        raise InstallControlError("integration_hook_template_oversized")
    return path.read_bytes()


def _template_bytes(root: Path, relative: str) -> bytes:
    raw = _template_raw(root, relative)
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


def _plugin_source_bytes(source: Path) -> str:
    if source.is_symlink() or not source.is_file():
        raise InstallControlError("integration_opencode_plugin_unavailable")
    return source.read_text(encoding="utf-8")


def opencode_plugin_resource(root: Path, destination: Path) -> ManagedResource:
    """Own the published OpenCode plugin as one whole file.

    The installer copied it outside the ownership transaction, so an uninstall
    left the plugin behind, still pointing at a vault that no longer existed,
    and a rollback could not put back what was there before.
    """
    from installer_config import plugin_with_embedded_root

    root = _absolute_destination(root)
    source = root / "scripts" / "llm-wiki-memory-opencode.js"
    desired = plugin_with_embedded_root(_plugin_source_bytes(source), root)
    return file_resource(
        resource_id="opencode-plugin",
        kind="opencode_plugin",
        path=_absolute_destination(destination),
        desired=desired.encode("utf-8"),
        mode=0o644,
    )


# --- Owned hook blocks in a shared JSON config --------------------------------
#
# Claude and Codex both keep hooks as {event: [block, ...]}, where a block holds a
# list of handlers. Ownership asks the same question of both: which blocks are
# entirely ours. What differs is how a handler is recognised as ours, what else in
# the file we own, and what the refusals are called.
#
# Both were merged by separate scripts outside the ownership transaction, so an
# uninstall left our hooks in place, still launching a vault that was gone.


class _HookFamily(NamedTuple):
    """One host's hook configuration: how to recognise our handlers, and our env."""

    name: str
    handler_is_ours: Callable[[object], bool]
    env_keys: tuple[str, ...] = ()


CLAUDE_ENV_KEYS = ("LLM_WIKI_ROOT", "LLM_WIKI_STATE_ROOT")


def _family_error(family: _HookFamily, suffix: str) -> InstallControlError:
    return InstallControlError(f"integration_{family.name}_{suffix}")


def _family_hooks(config: Mapping[str, object], family: _HookFamily) -> dict[str, list]:
    hooks = config.get("hooks", {})
    if not isinstance(hooks, dict):
        raise _family_error(family, "settings_invalid")
    return {
        event: list(blocks) for event, blocks in hooks.items() if isinstance(blocks, list)
    }


def _block_hooks(block: object) -> list[object]:
    if not isinstance(block, dict):
        return []
    hooks = block.get("hooks")
    if not isinstance(hooks, list):
        return []
    return hooks


def _block_ownership(block: object, family: _HookFamily) -> str:
    """`ours`, `theirs`, or `mixed` — a mixed block has no single owner."""
    handlers = _block_hooks(block)
    ours = [handler for handler in handlers if family.handler_is_ours(handler)]
    if not ours:
        return "theirs"
    if len(ours) == len(handlers):
        return "ours"
    return "mixed"


def _owned_blocks(blocks: Sequence[object], family: _HookFamily) -> list[object]:
    states = [_block_ownership(block, family) for block in blocks]
    if "mixed" in states:
        # One block carrying our handler next to the user's own is ambiguous:
        # stripping it rewrites their block, keeping it double-fires our hook.
        raise _family_error(family, "ownership_conflict")
    return [block for block, state in zip(blocks, states) if state == "ours"]


def _owned_hooks(config: Mapping[str, object], family: _HookFamily) -> dict[str, list]:
    owned: dict[str, list] = {}
    for event, blocks in _family_hooks(config, family).items():
        ours = _owned_blocks(blocks, family)
        if ours:
            owned[event] = ours
    return owned


def _owned_env(config: Mapping[str, object], family: _HookFamily) -> dict[str, str]:
    env = config.get("env", {})
    if not isinstance(env, dict):
        raise _family_error(family, "settings_invalid")
    return {key: str(env[key]) for key in family.env_keys if key in env}


def _family_projection(config: Mapping[str, object], family: _HookFamily) -> bytes | None:
    owned = {"env": _owned_env(config, family), "hooks": _owned_hooks(config, family)}
    if not owned["env"] and not owned["hooks"]:
        return None
    return canonical_json_bytes(owned)


def _family_projection_any(
    config: Mapping[str, object], candidates: Sequence[bytes], family: _HookFamily
) -> bytes | None:
    """The projection is derived from our own markers, so it is unique."""
    current = _family_projection(config, family)
    if current is None or current not in candidates:
        return None
    return current


def _family_desired(
    template: Mapping[str, object], env: Mapping[str, str], family: _HookFamily
) -> bytes:
    return canonical_json_bytes({"env": dict(env), "hooks": _family_hooks(template, family)})


def _without_owned_blocks(config: Mapping[str, object], family: _HookFamily) -> dict[str, list]:
    remaining: dict[str, list] = {}
    for event, blocks in _family_hooks(config, family).items():
        kept = [block for block in blocks if _block_ownership(block, family) != "ours"]
        if kept:
            remaining[event] = kept
    return remaining


def _unioned(existing: object, incoming: object) -> list[str]:
    values = list(existing) if isinstance(existing, list) else []
    additions = list(incoming) if isinstance(incoming, list) else []
    merged: list[str] = []
    for item in values + additions:
        text = str(item)
        if text not in merged:
            merged.append(text)
    return merged


def _mapping_or_empty(value: object) -> dict[str, object]:
    if isinstance(value, dict):
        return dict(value)
    return {}


def _permission_lists(
    permissions: Mapping[str, object], incoming: Mapping[str, object]
) -> dict[str, list[str]]:
    merged = {key: _unioned(permissions.get(key), incoming.get(key)) for key in ("allow", "deny")}
    return {key: value for key, value in merged.items() if value}


def _merged_permissions(config: dict[str, object], template: Mapping[str, object]) -> None:
    """Permissions are added at install and never taken back — see the note below."""
    incoming = _mapping_or_empty(template.get("permissions"))
    permissions = _mapping_or_empty(config.get("permissions"))
    permissions.update(_permission_lists(permissions, incoming))
    config["permissions"] = permissions


def _merged_defaults(config: dict[str, object], template: Mapping[str, object]) -> None:
    """Template scalars fill gaps; a value the user already set is never clobbered."""
    for key in ("$schema", "autoMemoryEnabled"):
        if key in template and key not in config:
            config[key] = template[key]


def _env_applied(config: dict[str, object], env: Mapping[str, str], family: _HookFamily) -> None:
    current = config.get("env")
    values = dict(current) if isinstance(current, dict) else {}
    for key in family.env_keys:
        values.pop(key, None)
    values.update(env)
    if values:
        config["env"] = values
        return
    config.pop("env", None)


def _hooks_applied(
    config: dict[str, object], added: Mapping[str, Sequence[object]], family: _HookFamily
) -> None:
    hooks = _without_owned_blocks(config, family)
    for event, blocks in added.items():
        hooks[event] = [*hooks.get(event, []), *blocks]
    if hooks:
        config["hooks"] = hooks
        return
    config.pop("hooks", None)


def _replacement_parts(
    replacement: bytes | None, family: _HookFamily
) -> tuple[dict[str, str], dict[str, list]]:
    if replacement is None:
        return {}, {}
    value = _decode_object(replacement)
    if set(value) != {"env", "hooks"}:
        raise _family_error(family, "projection_invalid")
    env = value["env"]
    if not isinstance(env, dict):
        raise _family_error(family, "projection_invalid")
    return {key: str(item) for key, item in env.items()}, _family_hooks(value, family)


def _require_family_expected(
    config: Mapping[str, object],
    expected: bytes | None,
    replacement: bytes | None,
    family: _HookFamily,
) -> None:
    current = _family_projection(config, family)
    if expected is not None:
        if current != expected:
            raise InstallControlError("integration_hook_config_changed")
        return
    if current is not None and current != replacement:
        raise InstallControlError("integration_hook_config_changed")


def _retire_new_hook_file(path: Path, replacement: bytes | None, config_existed: bool) -> bool:
    """A config file we created ourselves is ours entirely, so it goes away whole.

    Everything left in it at uninstall — the permissions we unioned, the schema we
    filled in — is ours too, and leaving behind a file the user never had is the
    litter this ownership work exists to stop.
    """
    if replacement is not None or config_existed:
        return False
    path.unlink()
    fsync_directory(path.parent)
    return True


def _write_family_projection(
    path: Path,
    expected: bytes | None,
    replacement: bytes | None,
    metadata: Mapping[str, object],
    family: _HookFamily,
    finish: Callable[[dict[str, object], bytes | None], None],
) -> None:
    config, original = _read_config(path)
    _require_family_expected(config, expected, replacement, family)
    env, hooks = _replacement_parts(replacement, family)
    updated = dict(config)
    _hooks_applied(updated, hooks, family)
    _env_applied(updated, env, family)
    finish(updated, replacement)
    if _retire_new_hook_file(path, replacement, bool(metadata.get("config_existed", True))):
        return
    _publish_config(path, updated, original)


def _write_family(
    path: Path,
    replacement: bytes | None,
    metadata: Mapping[str, object],
    family: _HookFamily,
    finish: Callable[[dict[str, object], bytes | None], None],
) -> None:
    config, _original = _read_config(path)
    current = _family_projection(config, family)
    if current == replacement:
        return
    _write_family_projection(path, current, replacement, metadata, family, finish)


def _hook_family_resource(
    *,
    resource_id: str,
    kind: str,
    path: Path,
    family: _HookFamily,
    desired: bytes,
    config_existed: bool,
    finish: Callable[[dict[str, object], bytes | None], None],
) -> ManagedResource:
    metadata = {"config_existed": config_existed}
    return ManagedResource(
        resource_id=resource_id,
        kind=kind,
        locator=str(path),
        desired=desired,
        read_owned=lambda: _family_projection(_read_config(path)[0], family),
        write_owned=lambda value: _write_family(path, value, metadata, family, finish),
        recognizes=lambda current: current == desired,
        read_projections=lambda candidates: _family_projection_any(
            _read_config(path)[0], candidates, family
        ),
        write_projection=lambda expected, replacement, data: _write_family_projection(
            path, expected, replacement, data, family, finish
        ),
        metadata=metadata,
        adopt_as_absent=False,
    )


# --- Claude Code user settings ------------------------------------------------
#
# The owned projection is exactly two things: the hook blocks whose commands are
# ours, and the two environment keys. Permissions are deliberately not owned —
# `deny` and `allow` entries are unioned into lists the user also edits, and we
# cannot tell our copy of an entry from theirs, so taking them back at uninstall
# would remove a setting we never added.


def _claude_command_is_ours(handler: object) -> bool:
    from merge_claude_settings import OUR_SCRIPT_MARKERS

    if not isinstance(handler, dict):
        return False
    command = str(handler.get("command") or "")
    return any(marker in command for marker in OUR_SCRIPT_MARKERS)


CLAUDE_FAMILY = _HookFamily("claude", _claude_command_is_ours, CLAUDE_ENV_KEYS)


def _claude_finish(template: Mapping[str, object]):
    def finish(config: dict[str, object], replacement: bytes | None) -> None:
        if replacement is None:
            return
        _merged_defaults(config, template)
        _merged_permissions(config, template)

    return finish


def claude_settings_resource(
    destination: Path,
    template: Mapping[str, object],
    vault_root: Path,
    state_root: Path,
    *,
    config_existed: bool | None = None,
) -> ManagedResource:
    """Own our Claude hook blocks and the two environment keys, nothing else.

    `config_existed` comes from the recorded manifest when there is one. Deciding
    it afresh at uninstall would always say "it existed" — we are the ones who
    created it — and the file we made would outlive the installation.
    """
    path = _absolute_destination(destination)
    env = {
        "LLM_WIKI_ROOT": str(_absolute_destination(vault_root)),
        "LLM_WIKI_STATE_ROOT": str(_absolute_destination(state_root)),
    }
    return _hook_family_resource(
        resource_id="claude-user-settings",
        kind="claude_settings_fragment",
        path=path,
        family=CLAUDE_FAMILY,
        desired=_family_desired(template, env, CLAUDE_FAMILY),
        config_existed=path.exists() if config_existed is None else config_existed,
        finish=_claude_finish(template),
    )


def claude_settings_template(root: Path) -> dict[str, object]:
    """Claude's template needs no root substitution: its commands expand
    `$LLM_WIKI_ROOT` in the shell at hook time, from the env we own above."""
    template = _decode_object(_template_raw(root, "integrations/claude-code/settings.json"))
    if "hooks" not in template:
        raise InstallControlError("integration_claude_template_invalid")
    return template


# --- Codex hooks --------------------------------------------------------------
#
# Codex owns no environment: its handlers reach the vault through the profile
# fragment the same transaction writes. Whether the hooks may be written at all
# is a separate question — inline hooks in `config.toml` can disable, duplicate,
# or contradict the file ones — and that check stays where it is, in
# `codex_memory`, ahead of the install.


def _codex_handler_is_ours(handler: object) -> bool:
    from codex_memory import _is_llm_wiki_hook

    return _is_llm_wiki_hook(handler)


CODEX_FAMILY = _HookFamily("codex", _codex_handler_is_ours)


def _no_finish(_config: dict[str, object], _replacement: bytes | None) -> None:
    return None


def codex_hooks_resource(
    destination: Path,
    template: Mapping[str, object],
    *,
    config_existed: bool | None = None,
) -> ManagedResource:
    """Own exactly the Codex hook blocks whose handlers are all ours."""
    path = _absolute_destination(destination)
    return _hook_family_resource(
        resource_id="codex-user-hooks",
        kind="codex_hooks_fragment",
        path=path,
        family=CODEX_FAMILY,
        desired=_family_desired(template, {}, CODEX_FAMILY),
        config_existed=path.exists() if config_existed is None else config_existed,
        finish=_no_finish,
    )


def codex_hooks_template(root: Path) -> dict[str, object]:
    template = _decode_object(_template_raw(root, "integrations/codex/hooks.json"))
    if "hooks" not in template:
        raise InstallControlError("integration_codex_template_invalid")
    return template


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
