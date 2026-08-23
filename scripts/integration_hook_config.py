"""Structural ownership adapters for supported IDE hook configurations."""

from __future__ import annotations

import hashlib
import json
import os
from collections import Counter
from collections.abc import Mapping, Sequence
from pathlib import Path

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


# --- Claude Code user settings ------------------------------------------------
#
# The installer merged these by content and outside the ownership transaction, so
# an uninstall left our hooks running against a vault that no longer existed.
# The owned projection is exactly two things: the hook blocks whose commands are
# ours, and the two environment keys. Permissions are deliberately not owned —
# `deny` and `allow` entries are unioned into lists the user also edits, and we
# cannot tell our copy of an entry from theirs, so taking them back at uninstall
# would remove a setting we never added.

CLAUDE_ENV_KEYS = ("LLM_WIKI_ROOT", "LLM_WIKI_STATE_ROOT")


def _claude_hooks(config: Mapping[str, object]) -> dict[str, list]:
    hooks = config.get("hooks", {})
    if not isinstance(hooks, dict):
        raise InstallControlError("integration_claude_settings_invalid")
    return {
        event: list(blocks) for event, blocks in hooks.items() if isinstance(blocks, list)
    }


def _command_of(hook: object) -> str:
    if not isinstance(hook, dict):
        return ""
    return str(hook.get("command") or "")


def _block_hooks(block: object) -> list[object]:
    if not isinstance(block, dict):
        return []
    hooks = block.get("hooks")
    if not isinstance(hooks, list):
        return []
    return hooks


def _hook_commands(block: object) -> list[str]:
    return [_command_of(hook) for hook in _block_hooks(block) if isinstance(hook, dict)]


def _block_ownership(block: object) -> str:
    """`ours`, `theirs`, or `mixed` — a mixed block has no single owner."""
    commands = _hook_commands(block)
    ours = [command for command in commands if _claude_command_is_ours(command)]
    if not ours:
        return "theirs"
    if len(ours) == len(commands):
        return "ours"
    return "mixed"


def _claude_command_is_ours(command: str) -> bool:
    from merge_claude_settings import OUR_SCRIPT_MARKERS

    return any(marker in command for marker in OUR_SCRIPT_MARKERS)


def _owned_blocks(blocks: Sequence[object]) -> list[object]:
    states = [_block_ownership(block) for block in blocks]
    if "mixed" in states:
        # One block carrying our command next to the user's own is ambiguous:
        # stripping it rewrites their block, keeping it double-fires the hook.
        raise InstallControlError("integration_claude_ownership_conflict")
    return [block for block, state in zip(blocks, states) if state == "ours"]


def _claude_owned_hooks(config: Mapping[str, object]) -> dict[str, list]:
    owned: dict[str, list] = {}
    for event, blocks in _claude_hooks(config).items():
        ours = _owned_blocks(blocks)
        if ours:
            owned[event] = ours
    return owned


def _claude_owned_env(config: Mapping[str, object]) -> dict[str, str]:
    env = config.get("env", {})
    if not isinstance(env, dict):
        raise InstallControlError("integration_claude_settings_invalid")
    return {key: str(env[key]) for key in CLAUDE_ENV_KEYS if key in env}


def _claude_projection(config: Mapping[str, object]) -> bytes | None:
    owned = {"env": _claude_owned_env(config), "hooks": _claude_owned_hooks(config)}
    if not owned["env"] and not owned["hooks"]:
        return None
    return canonical_json_bytes(owned)


def _claude_projection_any(
    config: Mapping[str, object], candidates: Sequence[bytes]
) -> bytes | None:
    """The projection is derived from our own markers, so it is unique."""
    current = _claude_projection(config)
    if current is None or current not in candidates:
        return None
    return current


def _claude_desired(template: Mapping[str, object], env: Mapping[str, str]) -> bytes:
    hooks = _claude_hooks(template)
    return canonical_json_bytes({"env": dict(env), "hooks": hooks})


def _without_owned_blocks(config: Mapping[str, object]) -> dict[str, list]:
    remaining: dict[str, list] = {}
    for event, blocks in _claude_hooks(config).items():
        kept = [block for block in blocks if _block_ownership(block) != "ours"]
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
    """Permissions are added at install and never taken back — see the note above."""
    incoming = _mapping_or_empty(template.get("permissions"))
    permissions = _mapping_or_empty(config.get("permissions"))
    permissions.update(_permission_lists(permissions, incoming))
    config["permissions"] = permissions


def _merged_defaults(config: dict[str, object], template: Mapping[str, object]) -> None:
    """Template scalars fill gaps; a value the user already set is never clobbered."""
    for key in ("$schema", "autoMemoryEnabled"):
        if key in template and key not in config:
            config[key] = template[key]


def _claude_env_applied(config: dict[str, object], env: Mapping[str, str]) -> None:
    current = config.get("env")
    values = dict(current) if isinstance(current, dict) else {}
    for key in CLAUDE_ENV_KEYS:
        values.pop(key, None)
    values.update(env)
    if values:
        config["env"] = values
        return
    config.pop("env", None)


def _claude_hooks_applied(config: dict[str, object], added: Mapping[str, Sequence[object]]) -> None:
    hooks = _without_owned_blocks(config)
    for event, blocks in added.items():
        hooks[event] = [*hooks.get(event, []), *blocks]
    if hooks:
        config["hooks"] = hooks
        return
    config.pop("hooks", None)


def _claude_replacement(replacement: bytes | None) -> tuple[dict[str, str], dict[str, list]]:
    if replacement is None:
        return {}, {}
    value = _decode_object(replacement)
    if set(value) != {"env", "hooks"}:
        raise InstallControlError("integration_claude_projection_invalid")
    env = value["env"]
    if not isinstance(env, dict):
        raise InstallControlError("integration_claude_projection_invalid")
    return {key: str(item) for key, item in env.items()}, _claude_hooks(value)


def _require_claude_expected(
    config: Mapping[str, object], expected: bytes | None, replacement: bytes | None
) -> None:
    current = _claude_projection(config)
    if expected is not None:
        if current != expected:
            raise InstallControlError("integration_hook_config_changed")
        return
    if current is not None and current != replacement:
        raise InstallControlError("integration_hook_config_changed")


def _retire_new_claude_file(
    path: Path, replacement: bytes | None, config_existed: bool
) -> bool:
    """A settings file we created ourselves is ours entirely, so it goes away whole.

    Everything left in it at uninstall — the permissions we unioned, the schema we
    filled in — is ours too, and leaving a file behind that the user never had is
    the litter this ownership work exists to stop.
    """
    if replacement is not None or config_existed:
        return False
    path.unlink()
    fsync_directory(path.parent)
    return True


def _write_claude_projection(
    path: Path,
    expected: bytes | None,
    replacement: bytes | None,
    metadata: Mapping[str, object],
    template: Mapping[str, object],
) -> None:
    config, original = _read_config(path)
    _require_claude_expected(config, expected, replacement)
    env, hooks = _claude_replacement(replacement)
    updated = dict(config)
    _claude_hooks_applied(updated, hooks)
    _claude_env_applied(updated, env)
    _apply_claude_template(updated, template, replacement)
    existed = bool(metadata.get("config_existed", True))
    if _retire_new_claude_file(path, replacement, existed):
        return
    _publish_config(path, updated, original)


def _apply_claude_template(
    config: dict[str, object], template: Mapping[str, object], replacement: bytes | None
) -> None:
    if replacement is None:
        return
    _merged_defaults(config, template)
    _merged_permissions(config, template)


def _write_claude(
    path: Path,
    replacement: bytes | None,
    metadata: Mapping[str, object],
    template: Mapping[str, object],
) -> None:
    config, _original = _read_config(path)
    current = _claude_projection(config)
    if current == replacement:
        return
    _write_claude_projection(path, current, replacement, metadata, template)


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
    desired = _claude_desired(template, env)
    existed = path.exists() if config_existed is None else config_existed
    metadata = {"config_existed": existed}
    return ManagedResource(
        resource_id="claude-user-settings",
        kind="claude_settings_fragment",
        locator=str(path),
        desired=desired,
        read_owned=lambda: _claude_projection(_read_config(path)[0]),
        write_owned=lambda value: _write_claude(path, value, metadata, template),
        recognizes=lambda current: current == desired,
        read_projections=lambda candidates: _claude_projection_any(
            _read_config(path)[0], candidates
        ),
        write_projection=lambda expected, replacement, data: _write_claude_projection(
            path, expected, replacement, data, template
        ),
        metadata=metadata,
        adopt_as_absent=False,
    )


def claude_settings_template(root: Path) -> dict[str, object]:
    """Claude's template needs no root substitution: its commands expand
    `$LLM_WIKI_ROOT` in the shell at hook time, from the env we own below."""
    template = _decode_object(_template_raw(root, "integrations/claude-code/settings.json"))
    if "hooks" not in template:
        raise InstallControlError("integration_claude_template_invalid")
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
