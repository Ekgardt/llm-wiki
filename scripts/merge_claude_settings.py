"""Merge LLM-wiki Claude Code hooks into user settings.json safely.

Does NOT wipe the user's existing hooks/permissions. Strategy:
  - Backup existing settings to settings.json.bak-llm-wiki-<timestamp>
  - For each hook event we own: drop exact legacy commands and current-vault
    exec forms, then append the template entries
  - Union permissions.allow / permissions.deny
  - Ensure env.LLM_WIKI_ROOT / LLM_WIKI_STATE_ROOT are set
  - Write merged JSON with trailing newline

Usage:
    uv run python scripts/merge_claude_settings.py
    uv run python scripts/merge_claude_settings.py --user-settings PATH --template PATH
    uv run python scripts/merge_claude_settings.py --dry-run
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import posixpath
import re
import stat
import subprocess
import tempfile
import uuid
from datetime import datetime
from pathlib import Path

from memory_state import advisory_file_lock

OUR_SCRIPT_MARKERS = (
    "session_start_context.py",
    "precompact_capture.py",
    "session_end_capture.py",
    "user_prompt_capture.py",
    "post_tool_capture.py",
    "session_start_project_state.py",
    "session_end_project_tag.py",
)
LEGACY_OWNED_COMMANDS = frozenset(
    command
    for marker in OUR_SCRIPT_MARKERS
    for command in (
        f"uv run python scripts/{marker}",
        f'uv run --directory "$LLM_WIKI_ROOT" python scripts/{marker}',
    )
) | {
    "uv run python scripts/session_start_context.py --omit-project-state",
    'uv run --directory "$LLM_WIKI_ROOT" python '
    "scripts/session_start_context.py --omit-project-state",
}
WINDOWS_PATH_RE = re.compile(r"^(?:[a-zA-Z]:/|//)")
CLAUDE_VERSION_PATTERNS = (
    re.compile(
        r"^(\d+)\.(\d+)\.(\d+)(?:-[0-9A-Za-z.-]+)?\s+\(Claude Code\)$",
        re.IGNORECASE,
    ),
    re.compile(
        r"^claude(?: code)?(?: version)?\s+v?(\d+)\.(\d+)\.(\d+)"
        r"(?:-[0-9A-Za-z.-]+)?$",
        re.IGNORECASE,
    ),
    re.compile(
        r"^claude-code/(\d+)\.(\d+)\.(\d+)(?:-[0-9A-Za-z.-]+)?$",
        re.IGNORECASE,
    ),
)
EXEC_HOOK_MIN_VERSION = (2, 1, 139)
OWNERSHIP_STATUS_PREFIX = "[LLM Wiki] "
MAX_SETTINGS_JSON_BYTES = 4 * 1024 * 1024


def _default_template() -> Path:
    return Path(__file__).resolve().parent.parent / "integrations" / "claude-code" / "settings.json"


def _default_user_settings() -> Path:
    home = Path.home()
    return home / ".claude" / "settings.json"


def parse_claude_version(raw: str | None) -> tuple[int, int, int] | None:
    versions = {
        tuple(int(part) for part in match.groups())
        for line in str(raw or "").splitlines()
        for pattern in CLAUDE_VERSION_PATTERNS
        if (match := pattern.fullmatch(line.strip()))
    }
    return next(iter(versions)) if len(versions) == 1 else None


def detect_claude_version() -> str | None:
    """Return bounded CLI version output, or None when detection fails."""
    try:
        result = subprocess.run(
            ["claude", "--version"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
            check=False,
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    output = f"{result.stdout}\n{result.stderr}".strip()
    return output[:512] or None


def _read_bounded_bytes(path: Path, label: str) -> bytes | None:
    try:
        with path.open("rb") as handle:
            raw = handle.read(MAX_SETTINGS_JSON_BYTES + 1)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ValueError(f"cannot safely read {label}: {path}: {exc}") from exc
    if len(raw) > MAX_SETTINGS_JSON_BYTES:
        raise ValueError(f"cannot safely read {label}: {path}: exceeds byte limit")
    return raw


def _load_json_document(
    path: Path,
    label: str = "existing Claude settings",
) -> tuple[dict, bytes | None]:
    raw = _read_bounded_bytes(path, label)
    if raw is None:
        return {}, None
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot safely read {label}: {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"{label} must be a JSON object: {path}")
    return data, raw


def _load_json(path: Path) -> dict:
    return _load_json_document(path)[0]


def _validate_permissions(settings: dict, label: str) -> None:
    if "permissions" not in settings:
        return
    permissions = settings["permissions"]
    if not isinstance(permissions, dict):
        raise ValueError(f"{label} permissions must be an object")
    for key in ("allow", "deny"):
        if key not in permissions:
            continue
        values = permissions[key]
        if not isinstance(values, list) or not all(
            isinstance(value, str) for value in values
        ):
            raise ValueError(f"{label} permissions.{key} must be a list of strings")


def _validate_managed_hook_events(
    hooks: dict,
    managed_events: set[str],
    label: str,
) -> None:
    for event in managed_events:
        if event not in hooks:
            continue
        blocks = hooks[event]
        if not isinstance(blocks, list):
            raise ValueError(f"{label} hooks.{event} must be a list")
        for block_index, block in enumerate(blocks):
            block_label = f"{label} hooks.{event} block {block_index}"
            if not isinstance(block, dict):
                raise ValueError(f"{block_label} must be an object")
            if "matcher" in block and not isinstance(block["matcher"], str):
                raise ValueError(f"{block_label} matcher must be a string")
            handlers = block.get("hooks")
            if not isinstance(handlers, list):
                raise ValueError(f"{block_label} hooks must be a list")
            for handler_index, handler in enumerate(handlers):
                handler_label = (
                    f"{label} hooks.{event} handler {block_index}:{handler_index}"
                )
                if not isinstance(handler, dict):
                    raise ValueError(f"{handler_label} must be an object")
                for key in ("type", "command", "statusMessage", "shell"):
                    if key in handler and not isinstance(handler[key], str):
                        raise ValueError(f"{handler_label} {key} must be a string")
                if "args" in handler and (
                    not isinstance(handler["args"], list)
                    or not all(isinstance(arg, str) for arg in handler["args"])
                ):
                    raise ValueError(f"{handler_label} args must be a list of strings")
                if "timeout" in handler:
                    timeout = handler["timeout"]
                    if (
                        isinstance(timeout, bool)
                        or not isinstance(timeout, int)
                        or timeout < 0
                    ):
                        raise ValueError(f"{handler_label} timeout must be a nonnegative integer")
                if "async" in handler and not isinstance(handler["async"], bool):
                    raise ValueError(f"{handler_label} async must be a boolean")


def _validate_merge_inputs(user: dict, template: dict) -> None:
    if not isinstance(user, dict):
        raise ValueError("user Claude settings must be an object")
    if not isinstance(template, dict):
        raise ValueError("template Claude settings must be an object")
    for settings, label in ((user, "user"), (template, "template")):
        if "env" in settings and not isinstance(settings["env"], dict):
            raise ValueError(f"{label} env must be an object")
        _validate_permissions(settings, label)
        if "hooks" in settings and not isinstance(settings["hooks"], dict):
            raise ValueError(f"{label} hooks must be an object")

    user_env = user.get("env", {})
    for key in ("LLM_WIKI_ROOT", "LLM_WIKI_STATE_ROOT"):
        if key in user_env and not isinstance(user_env[key], str):
            raise ValueError(f"user env.{key} must be a string")
    if (
        "$schema" not in user
        and "$schema" in template
        and not isinstance(template["$schema"], str)
    ):
        raise ValueError("template $schema must be a string when inserted")
    if (
        "autoMemoryEnabled" not in user
        and "autoMemoryEnabled" in template
        and not isinstance(template["autoMemoryEnabled"], bool)
    ):
        raise ValueError("template autoMemoryEnabled must be a boolean when inserted")

    template_hooks = template.get("hooks", {})
    managed_events = set(template_hooks)
    _validate_managed_hook_events(template_hooks, managed_events, "template")
    _validate_managed_hook_events(user.get("hooks", {}), managed_events, "user")


def _normalize_hook_path(value: str) -> str:
    normalized = posixpath.normpath(value.replace("\\", "/"))
    return normalized.casefold() if WINDOWS_PATH_RE.match(normalized) else normalized


def _owned_script_marker(
    value: str,
    vault_root: str,
    *,
    allow_relative: bool,
) -> str | None:
    candidate = _normalize_hook_path(value)
    vault = _normalize_hook_path(vault_root)
    for marker in OUR_SCRIPT_MARKERS:
        relative = _normalize_hook_path(f"scripts/{marker}")
        absolute = _normalize_hook_path(f"{vault}/{relative}")
        if candidate == absolute or (allow_relative and candidate == relative):
            return marker
    return None


def _absolute_script_path(vault_root: str, marker: str, shell: str) -> str:
    if shell == "powershell":
        root = vault_root.rstrip("/\\").replace("/", "\\")
        return f"{root}\\scripts\\{marker}"
    root = vault_root.rstrip("/\\").replace("\\", "/")
    return f"{root}/scripts/{marker}"


def _quote_bash_literal(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"


def _quote_powershell_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _shell_command(argv: list[str], shell: str) -> str:
    if shell == "powershell":
        return "& " + " ".join(_quote_powershell_literal(arg) for arg in argv)
    return " ".join(_quote_bash_literal(arg) for arg in argv)


def _legacy_argv(
    command: str,
    args: list[str],
    vault_root: str,
    shell: str,
) -> list[str]:
    materialized: list[str] = []
    for arg in args:
        value = vault_root if arg == "$LLM_WIKI_ROOT" else arg
        marker = _owned_script_marker(value, vault_root, allow_relative=True)
        materialized.append(
            _absolute_script_path(vault_root, marker, shell)
            if marker is not None
            else value
        )
    return [command, *materialized]


def _owned_shell_commands(vault_root: str) -> set[str]:
    commands: set[str] = set()
    for marker in OUR_SCRIPT_MARKERS:
        extras = ["--omit-project-state"] if marker == "session_start_context.py" else []
        args = [
            "run",
            "--directory",
            "$LLM_WIKI_ROOT",
            "python",
            f"scripts/{marker}",
            *extras,
        ]
        for shell in ("bash", "powershell"):
            commands.add(
                _shell_command(_legacy_argv("uv", args, vault_root, shell), shell)
            )
    return commands


def _hook_matches_vault(hook: dict, vault_root: str) -> bool:
    command = hook.get("command")
    if not isinstance(command, str):
        return False
    args = hook.get("args")
    if args is None:
        return command in LEGACY_OWNED_COMMANDS or command in _owned_shell_commands(
            vault_root
        )
    if not isinstance(args, list) or not all(isinstance(arg, str) for arg in args):
        return False

    marker: str | None = None
    extra: list[str] = []
    if command in {"uv", "uv.exe"}:
        if (
            len(args) >= 5
            and args[:2] == ["run", "--directory"]
            and args[3] == "python"
            and _normalize_hook_path(args[2]) == _normalize_hook_path(vault_root)
        ):
            marker = _owned_script_marker(
                args[4], vault_root, allow_relative=True
            )
            extra = args[5:]
        elif len(args) >= 3 and args[:2] == ["run", "python"]:
            marker = _owned_script_marker(
                args[2], vault_root, allow_relative=False
            )
            extra = args[3:]
    elif command in {"python", "python.exe", "python3", "python3.exe"} and args:
        marker = _owned_script_marker(args[0], vault_root, allow_relative=False)
        extra = args[1:]

    if marker == "session_start_context.py":
        return extra in ([], ["--omit-project-state"])
    return marker is not None and not extra


def _hook_is_ours(hook: dict, vault_roots: tuple[str, ...]) -> bool:
    status = hook.get("statusMessage")
    if isinstance(status, str) and status.startswith(OWNERSHIP_STATUS_PREFIX):
        return True
    return any(_hook_matches_vault(hook, root) for root in vault_roots if root)


def _materialize_hook_blocks(
    blocks: list,
    vault_root: str,
    *,
    exec_form: bool,
    legacy_shell: str,
) -> list:
    materialized = json.loads(json.dumps(blocks))
    for block in materialized:
        if not isinstance(block, dict):
            continue
        hooks = block.get("hooks")
        if not isinstance(hooks, list):
            continue
        for hook in hooks:
            if not isinstance(hook, dict) or not isinstance(hook.get("args"), list):
                continue
            args = [
                vault_root if arg == "$LLM_WIKI_ROOT" else arg
                for arg in hook["args"]
            ]
            if exec_form:
                hook["args"] = args
                continue
            command = hook.get("command")
            if not isinstance(command, str) or not all(
                isinstance(arg, str) for arg in args
            ):
                continue
            hook["command"] = _shell_command(
                _legacy_argv(command, args, vault_root, legacy_shell),
                legacy_shell,
            )
            hook.pop("args", None)
            hook["shell"] = legacy_shell
    return materialized


def _strip_our_hooks(blocks: list, vault_roots: tuple[str, ...]) -> list:
    """Remove matcher-blocks that only (or partly) contain our commands."""
    out: list = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        hooks = block.get("hooks")
        if not isinstance(hooks, list):
            out.append(block)
            continue
        kept = [
            h
            for h in hooks
            if isinstance(h, dict) and not _hook_is_ours(h, vault_roots)
        ]
        has_unmanaged_metadata = any(key not in {"matcher", "hooks"} for key in block)
        if kept or has_unmanaged_metadata:
            new_block = dict(block)
            new_block["hooks"] = kept
            out.append(new_block)
        # If all hooks were ours, drop the whole matcher block.
    return out


def merge_settings(
    user: dict,
    template: dict,
    vault_root: str,
    state_root: str,
    *,
    claude_version: str | tuple[int, int, int] | None = EXEC_HOOK_MIN_VERSION,
    legacy_shell: str | None = None,
) -> dict:
    """Return a new merged settings dict."""
    _validate_merge_inputs(user, template)
    result = json.loads(json.dumps(user))  # deep copy via JSON
    user_env = user.get("env", {})
    previous_root = user_env.get("LLM_WIKI_ROOT")
    owned_roots = tuple(
        dict.fromkeys(
            root
            for root in (
                vault_root,
                previous_root if isinstance(previous_root, str) else "",
            )
            if root
        )
    )
    version = (
        claude_version
        if isinstance(claude_version, tuple)
        else parse_claude_version(claude_version)
    )
    exec_form = version is not None and version >= EXEC_HOOK_MIN_VERSION
    shell = legacy_shell or ("powershell" if os.name == "nt" else "bash")
    if shell not in {"bash", "powershell"}:
        raise ValueError(f"unsupported legacy shell: {shell}")

    # Schema / flag from template ONLY if not already set by the user
    # (non-destructive merge — do NOT clobber existing values).
    if "$schema" in template and "$schema" not in result:
        result["$schema"] = template["$schema"]
    if template.get("autoMemoryEnabled") is not None and "autoMemoryEnabled" not in result:
        result["autoMemoryEnabled"] = template["autoMemoryEnabled"]

    # Permissions: union lists
    t_perm = template.get("permissions", {})
    u_perm = result.setdefault("permissions", {})
    for key in ("allow", "deny"):
        existing = u_perm.get(key, [])
        incoming = t_perm.get(key, [])
        merged: list[str] = []
        seen: set[str] = set()
        for item in list(existing) + list(incoming):
            if item not in seen:
                seen.add(item)
                merged.append(item)
        if merged:
            u_perm[key] = merged

    # Hooks: per event, strip ours then append template blocks
    t_hooks = template.get("hooks", {})
    u_hooks = result.setdefault("hooks", {})
    for event, t_blocks in t_hooks.items():
        existing = u_hooks.get(event, [])
        cleaned = _strip_our_hooks(list(existing), owned_roots)
        u_hooks[event] = cleaned + _materialize_hook_blocks(
            t_blocks,
            vault_root,
            exec_form=exec_form,
            legacy_shell=shell,
        )

    # Env: set vault roots without clobbering unrelated keys
    env = result.setdefault("env", {})
    if vault_root:
        env["LLM_WIKI_ROOT"] = vault_root
    if state_root:
        env["LLM_WIKI_STATE_ROOT"] = state_root

    return result


def _sync_parent_directory(directory: Path) -> None:
    if os.name != "posix":
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(str(directory), flags)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def _resolve_publish_target(requested: Path) -> tuple[Path, tuple[str, str | None]]:
    try:
        metadata = requested.lstat()
    except FileNotFoundError:
        return requested, ("missing", None)
    except OSError as exc:
        raise ValueError(f"cannot safely inspect Claude settings target: {requested}: {exc}") from exc

    if stat.S_ISLNK(metadata.st_mode):
        try:
            link_text = str(requested.readlink())
            resolved = requested.resolve(strict=True)
        except OSError as exc:
            raise ValueError(f"Claude settings symlink cannot be resolved safely: {requested}") from exc
        if not resolved.is_file():
            raise ValueError(f"Claude settings symlink must resolve to a regular file: {requested}")
        return resolved, ("symlink", link_text)
    if not stat.S_ISREG(metadata.st_mode):
        raise ValueError(f"Claude settings target must be a regular file: {requested}")
    return requested.resolve(strict=True), ("regular", None)


def _assert_target_binding(
    requested: Path,
    publish_target: Path,
    binding: tuple[str, str | None],
) -> None:
    kind, link_text = binding
    try:
        metadata = requested.lstat()
    except FileNotFoundError:
        if kind == "missing":
            return
        raise ValueError("Claude settings target changed during merge") from None
    except OSError as exc:
        raise ValueError("Claude settings target changed during merge") from exc

    if kind == "missing":
        raise ValueError("Claude settings target changed during merge")
    if kind == "symlink":
        if not stat.S_ISLNK(metadata.st_mode):
            raise ValueError("Claude settings target changed during merge")
        try:
            same_link = str(requested.readlink()) == link_text
            same_target = requested.resolve(strict=True) == publish_target
        except OSError as exc:
            raise ValueError("Claude settings target changed during merge") from exc
        if not same_link or not same_target:
            raise ValueError("Claude settings target changed during merge")
        return
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise ValueError("Claude settings target changed during merge")
    try:
        if requested.resolve(strict=True) != publish_target:
            raise ValueError("Claude settings target changed during merge")
    except OSError as exc:
        raise ValueError("Claude settings target changed during merge") from exc


def _target_identity(path: Path, raw: bytes | None) -> tuple[int, int, int] | None:
    if raw is None:
        return None
    try:
        metadata = path.stat()
    except OSError as exc:
        raise ValueError("Claude settings target changed during merge") from exc
    if not stat.S_ISREG(metadata.st_mode):
        raise ValueError(f"Claude settings target must be a regular file: {path}")
    return (
        metadata.st_dev,
        metadata.st_ino,
        stat.S_IMODE(metadata.st_mode),
    )


def _assert_merge_base_unchanged(
    requested: Path,
    publish_target: Path,
    binding: tuple[str, str | None],
    base_bytes: bytes | None,
    base_identity: tuple[int, int, int] | None,
) -> None:
    _assert_target_binding(requested, publish_target, binding)
    current = _read_bounded_bytes(publish_target, "current Claude settings")
    current_identity = _target_identity(publish_target, current)
    if current_identity != base_identity:
        raise ValueError("Claude settings target changed during merge")
    if current is None or base_bytes is None:
        if current is not base_bytes:
            raise ValueError("Claude settings target changed during merge")
        return
    if hashlib.sha256(current).digest() != hashlib.sha256(base_bytes).digest():
        raise ValueError("Claude settings target changed during merge")


def _write_exclusive_file(path: Path, data: bytes, mode: int) -> None:
    descriptor = os.open(
        str(path),
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        mode,
    )
    try:
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            os.chmod(path, mode)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        raise
    _sync_parent_directory(path.parent)


def _create_backup(requested: Path, base_bytes: bytes, mode: int) -> Path:
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S.%f")
    while True:
        backup = requested.with_name(
            f"{requested.name}.bak-llm-wiki-{stamp}-{uuid.uuid4().hex}"
        )
        try:
            _write_exclusive_file(backup, base_bytes, mode)
        except FileExistsError:
            continue
        return backup


def _write_temporary_file(target: Path, data: bytes, mode: int) -> Path:
    descriptor, raw_path = tempfile.mkstemp(
        prefix=f".{target.name}.llm-wiki-",
        suffix=".tmp",
        dir=target.parent,
    )
    temporary = Path(raw_path)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            os.chmod(temporary, mode)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise
    return temporary


def _publish_merge(
    requested: Path,
    publish_target: Path,
    binding: tuple[str, str | None],
    base_bytes: bytes | None,
    base_identity: tuple[int, int, int] | None,
    data: bytes,
    mode: int,
) -> Path | None:
    """Publish with optimistic protection against writers ignoring our advisory lock.

    The final expected-base check is adjacent to ``os.replace``. Portable filesystems
    do not provide compare-and-swap replacement, so a noncooperating writer can still
    race in the instruction gap between that check and replacement.
    """
    temporary = _write_temporary_file(publish_target, data, mode)
    backup = None
    try:
        _assert_merge_base_unchanged(
            requested,
            publish_target,
            binding,
            base_bytes,
            base_identity,
        )
        if base_bytes is not None:
            backup = _create_backup(requested, base_bytes, mode)
        _assert_merge_base_unchanged(
            requested,
            publish_target,
            binding,
            base_bytes,
            base_identity,
        )
        os.replace(temporary, publish_target)
        temporary = None
        _sync_parent_directory(publish_target.parent)
        return backup
    finally:
        if temporary is not None:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass


def apply_merge(
    user_settings: Path,
    template: Path,
    vault_root: str,
    state_root: str,
    dry_run: bool = False,
    *,
    claude_version: str | tuple[int, int, int] | None = EXEC_HOOK_MIN_VERSION,
    legacy_shell: str | None = None,
) -> dict:
    if dry_run:
        user = _load_json(user_settings)
        tmpl = _load_json_document(template, "Claude settings template")[0]
        if not tmpl:
            raise SystemExit(f"merge_claude_settings: template missing or empty: {template}")
        merged = merge_settings(
            user,
            tmpl,
            vault_root,
            state_root,
            claude_version=claude_version,
            legacy_shell=legacy_shell,
        )
        text = json.dumps(merged, indent=2, ensure_ascii=False) + "\n"
        print(text)
        return merged

    user_settings.parent.mkdir(parents=True, exist_ok=True)
    requested = user_settings.absolute()
    requested = requested.parent.resolve(strict=True) / requested.name
    lock_path = requested.with_name(f".{requested.name}.llm-wiki.lock")
    with advisory_file_lock(
        lock_path,
        timeout=30.0,
        description="Claude settings merge lock",
    ):
        publish_target, binding = _resolve_publish_target(requested)
        user, base_bytes = _load_json_document(publish_target)
        tmpl = _load_json_document(template, "Claude settings template")[0]
        if not tmpl:
            raise SystemExit(f"merge_claude_settings: template missing or empty: {template}")
        merged = merge_settings(
            user,
            tmpl,
            vault_root,
            state_root,
            claude_version=claude_version,
            legacy_shell=legacy_shell,
        )
        text = json.dumps(merged, indent=2, ensure_ascii=False) + "\n"
        base_identity = _target_identity(publish_target, base_bytes)
        mode = base_identity[2] if base_identity is not None else 0o600
        backup = _publish_merge(
            requested,
            publish_target,
            binding,
            base_bytes,
            base_identity,
            text.encode("utf-8"),
            mode,
        )
    if backup is not None:
        print(f"merge_claude_settings: backup → {backup}")
    print(f"merge_claude_settings: wrote {user_settings}")
    return merged


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--user-settings", type=Path, default=None)
    p.add_argument("--template", type=Path, default=None)
    p.add_argument(
        "--vault-root",
        default=os.environ.get("LLM_WIKI_ROOT", ""),
        help="Value for env.LLM_WIKI_ROOT (default: $LLM_WIKI_ROOT)",
    )
    p.add_argument(
        "--state-root",
        default=os.environ.get("LLM_WIKI_STATE_ROOT", ""),
        help="Value for env.LLM_WIKI_STATE_ROOT",
    )
    p.add_argument("--dry-run", action="store_true")
    p.add_argument(
        "--claude-version",
        default=None,
        help="Claude Code version output; auto-detected when omitted",
    )
    p.add_argument(
        "--legacy-shell",
        choices=("bash", "powershell"),
        default=None,
        help="Shell used by legacy command-string hooks",
    )
    return p.parse_args()


def main() -> int:
    args = parse_args()
    template = args.template or _default_template()
    user_settings = args.user_settings or _default_user_settings()
    vault = args.vault_root or str(Path(__file__).resolve().parent.parent)
    state = args.state_root
    if not state:
        state = str(Path(vault).resolve())
    claude_version = args.claude_version or detect_claude_version()

    apply_merge(
        user_settings=user_settings,
        template=template,
        vault_root=str(Path(vault).resolve()),
        state_root=str(Path(state).resolve()),
        dry_run=args.dry_run,
        claude_version=claude_version,
        legacy_shell=args.legacy_shell,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
