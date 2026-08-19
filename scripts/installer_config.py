#!/usr/bin/env python3
"""Shared, bounded configuration helpers for the native installers."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import shutil
import tempfile
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from lsp_process_tree import ProcessTree

MAX_CONFIG_BYTES = 2 * 1024 * 1024
MAX_DEBUG_BYTES = 4 * 1024 * 1024
DEBUG_TIMEOUT_SECONDS = 15.0
PROFILE_START = "# >>> LLM-Wiki installer >>>"
PROFILE_END = "# <<< LLM-Wiki installer <<<"
GLOBAL_CONFIG_NAMES = ("opencode.jsonc", "opencode.json", "config.json")


@dataclass(frozen=True, slots=True)
class ConfigMergeResult:
    changed: bool
    config_file: Path
    backup: Path | None


def _without_comments(text: str) -> str:
    output: list[str] = []
    index = 0
    quoted = False
    escaped = False
    while index < len(text):
        current = text[index]
        following = text[index + 1] if index + 1 < len(text) else ""
        if quoted:
            output.append(current)
            if escaped:
                escaped = False
            elif current == "\\":
                escaped = True
            elif current == '"':
                quoted = False
            index += 1
            continue
        if current == '"':
            quoted = True
            output.append(current)
            index += 1
            continue
        if current == "/" and following == "/":
            output.extend((" ", " "))
            index += 2
            while index < len(text) and text[index] not in "\r\n":
                output.append(" ")
                index += 1
            continue
        if current == "/" and following == "*":
            output.extend((" ", " "))
            index += 2
            while index + 1 < len(text) and text[index : index + 2] != "*/":
                output.append(text[index] if text[index] in "\r\n" else " ")
                index += 1
            if index + 1 >= len(text):
                raise ValueError("unterminated JSONC block comment")
            output.extend((" ", " "))
            index += 2
            continue
        output.append(current)
        index += 1
    if quoted:
        raise ValueError("unterminated JSON string")
    return "".join(output)


def _without_trailing_commas(text: str) -> str:
    output: list[str] = []
    quoted = False
    escaped = False
    for index, current in enumerate(text):
        if quoted:
            output.append(current)
            if escaped:
                escaped = False
            elif current == "\\":
                escaped = True
            elif current == '"':
                quoted = False
            continue
        if current == '"':
            quoted = True
            output.append(current)
            continue
        if current == ",":
            lookahead = index + 1
            while lookahead < len(text) and text[lookahead].isspace():
                lookahead += 1
            if lookahead < len(text) and text[lookahead] in "}]":
                continue
        output.append(current)
    return "".join(output)


def parse_jsonc(text: str) -> dict[str, Any]:
    try:
        value = json.loads(
            _without_trailing_commas(_without_comments(text.lstrip("\ufeff")))
        )
    except json.JSONDecodeError as exc:
        raise ValueError("invalid OpenCode JSONC") from exc
    if not isinstance(value, dict):
        raise ValueError("OpenCode config root must be an object")
    return value


def expected_opencode_entry(vault_root: Path) -> dict[str, Any]:
    return {
        "type": "local",
        "command": [
            "uv",
            "run",
            "--locked",
            "--no-sync",
            "--directory",
            str(vault_root),
            "python",
            "scripts/mcp_server.py",
        ],
        "enabled": True,
    }


def build_cron_command(
    *,
    root: Path,
    state_root: Path,
    uv_path: Path,
    kind: str,
    log_path: Path,
) -> str:
    if kind not in {"nightly", "weekly"}:
        raise ValueError("cron kind must be nightly or weekly")
    script = f"scheduled_{kind}.py"
    return " ".join(
        (
            "env",
            f"LLM_WIKI_ROOT={shlex.quote(str(root))}",
            f"LLM_WIKI_STATE_ROOT={shlex.quote(str(state_root))}",
            shlex.quote(str(uv_path)),
            "run",
            "--locked",
            "--no-sync",
            "--directory",
            shlex.quote(str(root)),
            "python",
            f"scripts/{script}",
            ">>",
            shlex.quote(str(log_path)),
            "2>&1",
        )
    )


def opencode_global_dir(home: Path, xdg: str | None, *, platform: str) -> Path:
    if platform not in {"posix", "windows"}:
        raise ValueError("platform must be posix or windows")
    if platform == "posix" and xdg and os.path.isabs(xdg):
        base = Path(xdg)
    else:
        base = Path(home) / ".config"
    return base / "opencode"


def resolve_uv_project_environment(root: Path, override: str | None) -> Path:
    root = Path(root).resolve()
    if override:
        candidate = Path(override)
        if not candidate.is_absolute():
            candidate = root / candidate
    else:
        candidate = root / ".venv"
    return candidate.resolve()


def uv_sync_arguments(root: Path, override: str | None) -> tuple[Path, list[str]]:
    root = Path(root).resolve()
    environment = resolve_uv_project_environment(root, override)
    if environment.exists() and not (environment / "pyvenv.cfg").is_file():
        raise ValueError(
            f"Selected uv project environment is not a virtual environment: {environment}"
        )
    arguments = [
        "--directory",
        str(root),
        "sync",
        "--locked",
        "--no-default-groups",
        "--quiet",
    ]
    if (environment / "pyvenv.cfg").is_file():
        arguments.append("--inexact")
    return environment, arguments


def selected_global_file(config_dir: Path) -> Path:
    for name in GLOBAL_CONFIG_NAMES:
        candidate = Path(config_dir) / name
        if candidate.exists() or candidate.is_symlink():
            return candidate
    return Path(config_dir) / "opencode.jsonc"


def _read_config_bytes(path: Path) -> bytes:
    if path.is_symlink():
        raise ValueError("selected OpenCode config must not be a symlink")
    if not path.exists():
        return b""
    if not path.is_file():
        raise ValueError("selected OpenCode config must be a regular file")
    size = path.stat().st_size
    if size > MAX_CONFIG_BYTES:
        raise ValueError("selected OpenCode config exceeds the size limit")
    value = path.read_bytes()
    if len(value) > MAX_CONFIG_BYTES:
        raise ValueError("selected OpenCode config exceeds the size limit")
    return value


def _create_backup(path: Path, original: bytes) -> Path:
    digest = hashlib.sha256(original).hexdigest()
    backup = path.with_name(f"{path.name}.llm-wiki.{digest}.bak")
    try:
        with backup.open("xb") as handle:
            handle.write(original)
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError:
        if backup.is_symlink() or not backup.is_file() or backup.read_bytes() != original:
            raise ValueError("OpenCode config backup conflicts with source bytes") from None
    return backup


def _atomic_write(path: Path, value: bytes) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def merge_opencode_user_config(
    config_dir: Path, expected: Mapping[str, Any]
) -> ConfigMergeResult:
    config_dir = Path(config_dir)
    config = selected_global_file(config_dir)
    original = _read_config_bytes(config)
    if original:
        try:
            document = parse_jsonc(original.decode("utf-8"))
        except UnicodeDecodeError as exc:
            raise ValueError("selected OpenCode config is not UTF-8") from exc
    else:
        document = {}
    mcp = document.get("mcp")
    if mcp is None:
        mcp = {}
        document["mcp"] = mcp
    if not isinstance(mcp, dict):
        raise ValueError("OpenCode mcp config must be an object")
    if mcp.get("llm-wiki") == expected:
        return ConfigMergeResult(False, config, None)

    mcp["llm-wiki"] = dict(expected)
    normalized = (
        json.dumps(
            document,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")
    if len(normalized) > MAX_CONFIG_BYTES:
        raise ValueError("merged OpenCode config exceeds the size limit")
    config_dir.mkdir(parents=True, exist_ok=True)
    backup = _create_backup(config, original) if original else None
    _atomic_write(config, normalized)
    return ConfigMergeResult(True, config, backup)


def verify_effective_entry(
    config: Mapping[str, Any], expected: Mapping[str, Any]
) -> str:
    mcp = config.get("mcp")
    actual = mcp.get("llm-wiki") if isinstance(mcp, Mapping) else None
    return "active" if actual == expected else "conflict"


def replace_profile_block(profile: Path, root: Path, state: Path) -> None:
    profile = Path(profile)
    existing = profile.read_text(encoding="utf-8") if profile.exists() else ""
    start_count = existing.count(PROFILE_START)
    end_count = existing.count(PROFILE_END)
    if start_count != end_count or start_count > 1:
        raise ValueError("invalid LLM-Wiki profile block ownership")
    block = "\n".join(
        (
            PROFILE_START,
            f"export LLM_WIKI_ROOT={shlex.quote(root.as_posix())}",
            f"export LLM_WIKI_STATE_ROOT={shlex.quote(state.as_posix())}",
            PROFILE_END,
        )
    )
    if PROFILE_START in existing:
        start = existing.index(PROFILE_START)
        end = existing.index(PROFILE_END, start) + len(PROFILE_END)
        updated = existing[:start] + block + existing[end:]
    else:
        separator = "" if not existing else "\n" if existing.endswith("\n") else "\n\n"
        updated = existing + separator + block + "\n"
    if updated == existing:
        return
    profile.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(profile, updated.encode("utf-8"))


def _bounded_process_output(
    command: Sequence[str],
    *,
    cwd: Path,
    environment: Mapping[str, str],
    timeout_seconds: float,
    max_bytes: int,
) -> tuple[int, bytes] | None:
    if timeout_seconds <= 0 or max_bytes <= 0:
        raise ValueError("process bounds must be positive")
    deadline = time.monotonic() + timeout_seconds
    tree = ProcessTree.spawn_with_deadline(
        command,
        cwd=Path(cwd),
        env=environment,
        deadline=deadline,
    )
    process = tree.process
    if process.stdin is not None:
        process.stdin.close()
    chunks: dict[str, bytearray] = {"stdout": bytearray(), "stderr": bytearray()}
    total = 0
    lock = threading.Lock()
    overflow = threading.Event()

    def read_stream(name: str) -> None:
        nonlocal total
        stream = getattr(process, name)
        if stream is None:
            return
        while True:
            data = stream.read(64 * 1024)
            if not data:
                return
            with lock:
                remaining = max_bytes + 1 - total
                if remaining > 0:
                    captured = data[:remaining]
                    chunks[name].extend(captured)
                    total += len(captured)
                if len(data) > remaining or total > max_bytes:
                    overflow.set()
                    return

    readers = [
        threading.Thread(target=read_stream, args=(name,), daemon=True)
        for name in ("stdout", "stderr")
    ]
    for reader in readers:
        reader.start()

    verified = False
    cleanup_deadline: float | None = None
    try:
        while time.monotonic() < deadline:
            if overflow.is_set():
                break
            if process.poll() is not None and all(not reader.is_alive() for reader in readers):
                verified = True
                break
            time.sleep(0.005)
        if not verified:
            cleanup_deadline = time.monotonic() + 2.0
            try:
                tree.terminate(deadline=cleanup_deadline)
            except (OSError, RuntimeError, TimeoutError):
                return None
        for reader in readers:
            remaining = (
                max(0.0, cleanup_deadline - time.monotonic())
                if cleanup_deadline is not None
                else 0.0
            )
            reader.join(timeout=remaining)
        if any(reader.is_alive() for reader in readers):
            return None
        if not verified or overflow.is_set():
            return None
        return process.returncode, bytes(chunks["stdout"])
    finally:
        if process.poll() is None:
            if cleanup_deadline is None:
                cleanup_deadline = time.monotonic() + 2.0
            try:
                tree.terminate(deadline=cleanup_deadline)
            except (OSError, RuntimeError, TimeoutError):
                pass
        try:
            tree.close()
        except (OSError, RuntimeError):
            pass


def probe_effective_entry(
    command: Sequence[str],
    *,
    cwd: Path,
    environment: Mapping[str, str],
    expected: Mapping[str, Any],
    timeout_seconds: float = DEBUG_TIMEOUT_SECONDS,
    max_bytes: int = MAX_DEBUG_BYTES,
) -> str:
    child_environment = os.environ.copy()
    child_environment.update(environment)
    try:
        completed = _bounded_process_output(
            command,
            cwd=cwd,
            environment=child_environment,
            timeout_seconds=timeout_seconds,
            max_bytes=max_bytes,
        )
    except (OSError, RuntimeError, TimeoutError, ValueError):
        return "configured_unverified"
    if completed is None or completed[0] != 0:
        return "configured_unverified"
    try:
        document = json.loads(completed[1].decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return "configured_unverified"
    if not isinstance(document, dict):
        return "configured_unverified"
    return verify_effective_entry(document, expected)


EMBEDDED_ROOT_MARKER = "// llm-wiki:embedded-root"


def plugin_with_embedded_root(source_text: str, vault_root: Path) -> str:
    """Bake the vault root into the plugin the installer publishes.

    OpenCode started from a desktop launcher inherits no shell environment, so
    a plugin that only reads `LLM_WIKI_ROOT` captured nothing and said so only
    on a console nobody reads.
    """
    lines = source_text.splitlines(keepends=True)
    marked = [
        index
        for index, line in enumerate(lines)
        if line.rstrip("\r\n").endswith(EMBEDDED_ROOT_MARKER)
    ]
    if len(marked) != 1:
        raise ValueError("OpenCode plugin source has no single embedded-root marker")
    value = json.dumps(str(vault_root))
    lines[marked[0]] = f"const _EMBEDDED_ROOT = {value}; {EMBEDDED_ROOT_MARKER}\n"
    return "".join(lines)


def _copy_plugin(source: Path, destination: Path, vault_root: Path) -> None:
    if source.is_symlink() or not source.is_file():
        raise ValueError("OpenCode plugin source is unavailable")
    published = plugin_with_embedded_root(source.read_text(encoding="utf-8"), vault_root)
    value = published.encode("utf-8")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() and destination.read_bytes() == value:
        return
    _atomic_write(destination, value)


def configure_opencode(
    *,
    root: Path,
    state_root: Path,
    cwd: Path,
    home: Path,
    xdg_config_home: str | None,
    platform: str,
    debug_command: Sequence[str] | None = None,
    environment: Mapping[str, str] | None = None,
) -> dict[str, str | None]:
    config_dir = opencode_global_dir(home, xdg_config_home, platform=platform)
    executable = shutil.which("opencode")
    detected = debug_command is not None or executable is not None or config_dir.exists()
    if not detected:
        return {"config_file": None, "plugin_file": None, "status": "not_detected"}

    root = Path(root)
    expected = expected_opencode_entry(root)
    merged = merge_opencode_user_config(config_dir, expected)
    plugin = config_dir / "plugins" / "llm-wiki-memory.js"
    _copy_plugin(root / "scripts" / "llm-wiki-memory-opencode.js", plugin, root)
    if debug_command is None and executable is not None:
        debug_command = [executable, "debug", "config"]
    status = "configured_unverified"
    if debug_command is not None:
        child_environment = {
            "LLM_WIKI_ROOT": str(root),
            "LLM_WIKI_STATE_ROOT": str(state_root),
            **dict(environment or {}),
        }
        status = probe_effective_entry(
            debug_command,
            cwd=cwd,
            environment=child_environment,
            expected=expected,
        )
    return {
        "config_file": str(merged.config_file),
        "plugin_file": str(plugin),
        "status": status,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    profile = subparsers.add_parser("profile")
    profile.add_argument("--profile", type=Path, required=True)
    profile.add_argument("--root", type=Path, required=True)
    profile.add_argument("--state-root", type=Path, required=True)
    opencode = subparsers.add_parser("opencode")
    opencode.add_argument("--root", type=Path, required=True)
    opencode.add_argument("--state-root", type=Path, required=True)
    opencode.add_argument("--cwd", type=Path, required=True)
    cron = subparsers.add_parser("cron")
    cron.add_argument("--root", type=Path, required=True)
    cron.add_argument("--state-root", type=Path, required=True)
    cron.add_argument("--uv-path", type=Path, required=True)
    cron.add_argument("--kind", choices=("nightly", "weekly"), required=True)
    cron.add_argument("--log-path", type=Path, required=True)
    sync = subparsers.add_parser("sync-args")
    sync.add_argument("--root", type=Path, required=True)
    sync.add_argument("--environment")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "profile":
        replace_profile_block(args.profile, args.root, args.state_root)
        return 0
    if args.command == "cron":
        print(
            build_cron_command(
                root=args.root,
                state_root=args.state_root,
                uv_path=args.uv_path,
                kind=args.kind,
                log_path=args.log_path,
            )
        )
        return 0
    if args.command == "sync-args":
        environment, arguments = uv_sync_arguments(args.root, args.environment)
        print(
            json.dumps(
                {"environment": str(environment), "arguments": arguments},
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 0
    result = configure_opencode(
        root=args.root,
        state_root=args.state_root,
        cwd=args.cwd,
        home=Path.home(),
        xdg_config_home=os.environ.get("XDG_CONFIG_HOME"),
        platform="windows" if os.name == "nt" else "posix",
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
