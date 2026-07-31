"""Safely merge LLM Wiki lifecycle commands into Codex hooks.json."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import posixpath
import re
import shlex
import stat
import tempfile
import uuid
from datetime import datetime
from pathlib import Path

from memory_state import advisory_file_lock

OUR_SCRIPT_MARKERS = (
    "session_start_context.py",
    "session_start_project_state.py",
    "post_tool_capture.py",
    "precompact_capture.py",
    "session_end_capture.py",
    "session_end_project_tag.py",
)
OWNERSHIP_STATUS_PREFIX = "[LLM Wiki] "
WINDOWS_PATH_RE = re.compile(r"^(?:[a-zA-Z]:/|//)")
MAX_HOOKS_JSON_BYTES = 4 * 1024 * 1024


def default_template() -> Path:
    return Path(__file__).resolve().parent.parent / "integrations" / "codex" / "hooks.template.json"


def default_user_hooks() -> Path:
    return Path.home() / ".codex" / "hooks.json"


def _read_bounded_bytes(path: Path, label: str) -> bytes | None:
    try:
        with path.open("rb") as handle:
            raw = handle.read(MAX_HOOKS_JSON_BYTES + 1)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ValueError(f"cannot safely read {label}: {path}: {exc}") from exc
    if len(raw) > MAX_HOOKS_JSON_BYTES:
        raise ValueError(f"cannot safely read {label}: {path}: exceeds byte limit")
    return raw


def _load_json_document(
    path: Path,
    label: str = "existing Codex hooks",
) -> tuple[dict, bytes | None]:
    raw = _read_bounded_bytes(path, label)
    if raw is None:
        return {}, None
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot safely read {label}: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object: {path}")
    return value, raw


def _load_json(path: Path, label: str = "existing Codex hooks") -> dict:
    return _load_json_document(path, label)[0]


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
                for key in ("type", "command", "commandWindows", "statusMessage"):
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
        raise ValueError("user Codex hooks must be an object")
    if not isinstance(template, dict):
        raise ValueError("template Codex hooks must be an object")
    for settings, label in ((user, "user"), (template, "template")):
        if "hooks" in settings and not isinstance(settings["hooks"], dict):
            raise ValueError(f"{label} hooks must be an object")
    if (
        "description" not in user
        and "description" in template
        and not isinstance(template["description"], str)
    ):
        raise ValueError("template description must be a string when inserted")
    template_hooks = template.get("hooks", {})
    managed_events = set(template_hooks)
    _validate_managed_hook_events(template_hooks, managed_events, "template")
    _validate_managed_hook_events(user.get("hooks", {}), managed_events, "user")


def _normalize_path(value: str) -> str:
    normalized = posixpath.normpath(value.replace("\\", "/"))
    return normalized.casefold() if WINDOWS_PATH_RE.match(normalized) else normalized


def _unquote_token(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def _is_legacy_generated_command(command: str, owned_roots: set[str]) -> bool:
    for posix in (True, False):
        try:
            argv = [_unquote_token(value) for value in shlex.split(command, posix=posix)]
        except ValueError:
            continue
        if len(argv) not in {6, 7} or argv[:3] != ["uv", "run", "--directory"]:
            continue
        if argv[4] != "python":
            continue
        extras = argv[6:]
        script = _normalize_path(argv[5])
        root = _normalize_path(argv[3]).rstrip("/")
        if root not in owned_roots:
            continue
        marker = next(
            (
                candidate
                for candidate in OUR_SCRIPT_MARKERS
                if script == f"{root}/scripts/{candidate}"
            ),
            None,
        )
        if marker is None:
            continue
        if marker == "session_start_context.py":
            if extras in ([], ["--omit-project-state"]):
                return True
        elif not extras:
            return True
    return False


def _strip_ours(
    blocks: list,
    owned_commands: set[str],
    owned_roots: set[str],
) -> list:
    result = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        hooks = block.get("hooks")
        if not isinstance(hooks, list):
            result.append(block)
            continue
        kept = []
        for hook in hooks:
            if not isinstance(hook, dict):
                continue
            commands = {
                command
                for command in (hook.get("command"), hook.get("commandWindows"))
                if isinstance(command, str) and command
            }
            status = hook.get("statusMessage")
            is_ours = (
                isinstance(status, str)
                and status.startswith(OWNERSHIP_STATUS_PREFIX)
            ) or bool(commands & owned_commands) or any(
                _is_legacy_generated_command(command, owned_roots)
                for command in commands
                if command
            )
            if not is_ours:
                kept.append(hook)
        has_unmanaged_metadata = any(key not in {"matcher", "hooks"} for key in block)
        if kept or has_unmanaged_metadata:
            copied = dict(block)
            copied["hooks"] = kept
            result.append(copied)
    return result


def _cmd_quote_path(path: Path) -> str:
    escaped = str(path).replace("%", '"^%"')
    return f'"{escaped}"'


def _materialize(template: dict, vault_root: Path) -> dict:
    root = vault_root.resolve()
    replacements = {
        "VAULT_ROOT": root,
        "SESSION_START_CONTEXT": root / "scripts" / "session_start_context.py",
        "SESSION_START_PROJECT_STATE": root / "scripts" / "session_start_project_state.py",
        "POST_TOOL_CAPTURE": root / "scripts" / "post_tool_capture.py",
        "PRECOMPACT_CAPTURE": root / "scripts" / "precompact_capture.py",
        "SESSION_END_CAPTURE": root / "scripts" / "session_end_capture.py",
        "SESSION_END_PROJECT_TAG": root / "scripts" / "session_end_project_tag.py",
    }
    def replace(value):
        if isinstance(value, dict):
            return {key: replace(item) for key, item in value.items()}
        if isinstance(value, list):
            return [replace(item) for item in value]
        if not isinstance(value, str):
            return value
        for name, path in replacements.items():
            value = value.replace(f"{{{{{name}_POSIX}}}}", shlex.quote(path.as_posix()))
            value = value.replace(f"{{{{{name}_WINDOWS}}}}", _cmd_quote_path(path))
        return value

    return replace(template)


def merge_hooks(user: dict, template: dict, vault_root: Path) -> dict:
    _validate_merge_inputs(user, template)
    result = json.loads(json.dumps(user))
    incoming = _materialize(template, vault_root)
    hooks = result.setdefault("hooks", {})
    owned_roots = {_normalize_path(str(vault_root.resolve())).rstrip("/")}
    owned_commands = {
        command
        for blocks in incoming.get("hooks", {}).values()
        for block in blocks
        for hook in block.get("hooks", [])
        for command in (hook.get("command"), hook.get("commandWindows"))
        if isinstance(command, str) and command
    }
    owned_commands.update(
        command.replace(" --omit-project-state", "")
        for command in tuple(owned_commands)
        if "session_start_context.py" in command
    )
    owned_commands.update(f"uv run python scripts/{marker}" for marker in OUR_SCRIPT_MARKERS)
    for event, blocks in incoming.get("hooks", {}).items():
        existing = hooks.get(event, [])
        hooks[event] = _strip_ours(existing, owned_commands, owned_roots) + blocks
    if "description" not in result and incoming.get("description"):
        result["description"] = incoming["description"]
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
        raise ValueError(f"cannot safely inspect Codex hooks target: {requested}: {exc}") from exc

    if stat.S_ISLNK(metadata.st_mode):
        try:
            link_text = str(requested.readlink())
            resolved = requested.resolve(strict=True)
        except OSError as exc:
            raise ValueError(f"Codex hooks symlink cannot be resolved safely: {requested}") from exc
        if not resolved.is_file():
            raise ValueError(f"Codex hooks symlink must resolve to a regular file: {requested}")
        return resolved, ("symlink", link_text)
    if not stat.S_ISREG(metadata.st_mode):
        raise ValueError(f"Codex hooks target must be a regular file: {requested}")
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
        raise ValueError("Codex hooks target changed during merge") from None
    except OSError as exc:
        raise ValueError("Codex hooks target changed during merge") from exc

    if kind == "missing":
        raise ValueError("Codex hooks target changed during merge")
    if kind == "symlink":
        if not stat.S_ISLNK(metadata.st_mode):
            raise ValueError("Codex hooks target changed during merge")
        try:
            same_link = str(requested.readlink()) == link_text
            same_target = requested.resolve(strict=True) == publish_target
        except OSError as exc:
            raise ValueError("Codex hooks target changed during merge") from exc
        if not same_link or not same_target:
            raise ValueError("Codex hooks target changed during merge")
        return
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise ValueError("Codex hooks target changed during merge")
    try:
        if requested.resolve(strict=True) != publish_target:
            raise ValueError("Codex hooks target changed during merge")
    except OSError as exc:
        raise ValueError("Codex hooks target changed during merge") from exc


def _target_identity(path: Path, raw: bytes | None) -> tuple[int, int, int] | None:
    if raw is None:
        return None
    try:
        metadata = path.stat()
    except OSError as exc:
        raise ValueError("Codex hooks target changed during merge") from exc
    if not stat.S_ISREG(metadata.st_mode):
        raise ValueError(f"Codex hooks target must be a regular file: {path}")
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
    current = _read_bounded_bytes(publish_target, "current Codex hooks")
    current_identity = _target_identity(publish_target, current)
    if current_identity != base_identity:
        raise ValueError("Codex hooks target changed during merge")
    if current is None or base_bytes is None:
        if current is not base_bytes:
            raise ValueError("Codex hooks target changed during merge")
        return
    if hashlib.sha256(current).digest() != hashlib.sha256(base_bytes).digest():
        raise ValueError("Codex hooks target changed during merge")


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


def apply_merge(target: Path, template: Path, vault_root: Path, dry_run: bool = False) -> dict:
    if dry_run:
        source = _load_json(template, "Codex hooks template")
        if not source:
            raise SystemExit(f"merge_codex_hooks: template missing or empty: {template}")
        merged = merge_hooks(_load_json(target), source, vault_root)
        text = json.dumps(merged, indent=2, ensure_ascii=False) + "\n"
        print(text)
        return merged
    target.parent.mkdir(parents=True, exist_ok=True)
    requested = target.absolute()
    requested = requested.parent.resolve(strict=True) / requested.name
    lock_path = requested.with_name(f".{requested.name}.llm-wiki.lock")
    with advisory_file_lock(
        lock_path,
        timeout=30.0,
        description="Codex hooks merge lock",
    ):
        publish_target, binding = _resolve_publish_target(requested)
        user, base_bytes = _load_json_document(publish_target)
        source = _load_json_document(template, "Codex hooks template")[0]
        if not source:
            raise SystemExit(f"merge_codex_hooks: template missing or empty: {template}")
        merged = merge_hooks(user, source, vault_root)
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
        print(f"merge_codex_hooks: backup -> {backup}")
    print(f"merge_codex_hooks: wrote {target}")
    return merged


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--user-hooks", type=Path, default=default_user_hooks())
    parser.add_argument("--template", type=Path, default=default_template())
    parser.add_argument("--vault-root", type=Path, default=Path(__file__).resolve().parent.parent)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    apply_merge(args.user_hooks, args.template, args.vault_root, args.dry_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
