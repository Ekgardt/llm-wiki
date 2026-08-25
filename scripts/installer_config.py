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


class _StringState:
    """Tracks whether the scanner currently sits inside a JSON string."""

    def __init__(self) -> None:
        self.quoted = False
        self._escaped = False

    def consume(self, character: str) -> bool:
        """Report whether this character belongs to a string, and advance."""
        if self.quoted:
            self._advance_quoted(character)
            return True
        if character == '"':
            self.quoted = True
            return True
        return False

    def _advance_quoted(self, character: str) -> None:
        if self._escaped:
            self._escaped = False
            return
        if character == "\\":
            self._escaped = True
            return
        if character == '"':
            self.quoted = False


class _JsoncScanner:
    """Blank out JSONC comments while preserving offsets and string contents."""

    def __init__(self, text: str) -> None:
        self._text = text
        self._output: list[str] = []
        self._index = 0
        self._state = _StringState()

    def run(self) -> str:
        while self._index < len(self._text):
            self._step()
        if self._state.quoted:
            raise ValueError("unterminated JSON string")
        return "".join(self._output)

    def _step(self) -> None:
        current = self._text[self._index]
        if self._state.consume(current):
            self._emit(current)
            return
        if self._at("//"):
            self._skip_line_comment()
            return
        if self._at("/*"):
            self._skip_block_comment()
            return
        self._emit(current)

    def _emit(self, character: str) -> None:
        self._output.append(character)
        self._index += 1

    def _at(self, marker: str) -> bool:
        return self._text[self._index : self._index + 2] == marker

    def _skip_line_comment(self) -> None:
        self._output.extend((" ", " "))
        self._index += 2
        while self._index < len(self._text) and self._text[self._index] not in "\r\n":
            self._output.append(" ")
            self._index += 1

    def _skip_block_comment(self) -> None:
        self._output.extend((" ", " "))
        self._index += 2
        while self._index + 1 < len(self._text) and not self._at("*/"):
            character = self._text[self._index]
            self._output.append(character if character in "\r\n" else " ")
            self._index += 1
        if self._index + 1 >= len(self._text):
            raise ValueError("unterminated JSONC block comment")
        self._output.extend((" ", " "))
        self._index += 2


def _without_comments(text: str) -> str:
    return _JsoncScanner(text).run()


def _trailing_comma_at(text: str, index: int) -> bool:
    lookahead = index + 1
    while lookahead < len(text) and text[lookahead].isspace():
        lookahead += 1
    return lookahead < len(text) and text[lookahead] in "}]"


def _without_trailing_commas(text: str) -> str:
    output: list[str] = []
    state = _StringState()
    for index, current in enumerate(text):
        if state.consume(current):
            output.append(current)
            continue
        if current == "," and _trailing_comma_at(text, index):
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


def _require_regular_config(path: Path) -> None:
    if not path.is_file():
        raise ValueError("selected OpenCode config must be a regular file")


def _require_config_size(size: int) -> None:
    if size > MAX_CONFIG_BYTES:
        raise ValueError("selected OpenCode config exceeds the size limit")


def _read_config_bytes(path: Path) -> bytes:
    """The size is checked twice: the file may grow between stat and read."""
    if path.is_symlink():
        raise ValueError("selected OpenCode config must not be a symlink")
    if not path.exists():
        return b""
    _require_regular_config(path)
    _require_config_size(path.stat().st_size)
    value = path.read_bytes()
    _require_config_size(len(value))
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
    document = _config_document(original)
    mcp = _mcp_section(document)
    if mcp.get("llm-wiki") == expected:
        return ConfigMergeResult(False, config, None)
    mcp["llm-wiki"] = dict(expected)
    normalized = _normalized_config_bytes(document)
    config_dir.mkdir(parents=True, exist_ok=True)
    backup = _create_backup(config, original) if original else None
    _atomic_write(config, normalized)
    return ConfigMergeResult(True, config, backup)


def _config_document(original: bytes) -> dict[str, Any]:
    if not original:
        return {}
    try:
        return parse_jsonc(original.decode("utf-8"))
    except UnicodeDecodeError as exc:
        raise ValueError("selected OpenCode config is not UTF-8") from exc


def _mcp_section(document: dict[str, Any]) -> dict[str, Any]:
    mcp = document.get("mcp")
    if mcp is None:
        mcp = {}
        document["mcp"] = mcp
    if not isinstance(mcp, dict):
        raise ValueError("OpenCode mcp config must be an object")
    return mcp


def _normalized_config_bytes(document: dict[str, Any]) -> bytes:
    value = (
        json.dumps(
            document,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")
    if len(value) > MAX_CONFIG_BYTES:
        raise ValueError("merged OpenCode config exceeds the size limit")
    return value


def verify_effective_entry(
    config: Mapping[str, Any], expected: Mapping[str, Any]
) -> str:
    mcp = config.get("mcp")
    actual = mcp.get("llm-wiki") if isinstance(mcp, Mapping) else None
    return "active" if actual == expected else "conflict"


def replace_profile_block(profile: Path, root: Path, state: Path) -> None:
    profile = Path(profile)
    existing = profile.read_text(encoding="utf-8") if profile.exists() else ""
    _require_single_profile_block(existing)
    updated = _profile_with_block(existing, _profile_block(root, state))
    if updated == existing:
        return
    profile.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(profile, updated.encode("utf-8"))


def _require_single_profile_block(existing: str) -> None:
    start_count = existing.count(PROFILE_START)
    end_count = existing.count(PROFILE_END)
    if start_count != end_count or start_count > 1:
        raise ValueError("invalid LLM-Wiki profile block ownership")


def _profile_block(root: Path, state: Path) -> str:
    return "\n".join(
        (
            PROFILE_START,
            f"export LLM_WIKI_ROOT={shlex.quote(root.as_posix())}",
            f"export LLM_WIKI_STATE_ROOT={shlex.quote(state.as_posix())}",
            PROFILE_END,
        )
    )


def _appended_separator(existing: str) -> str:
    if not existing:
        return ""
    if existing.endswith("\n"):
        return "\n"
    return "\n\n"


def _profile_with_block(existing: str, block: str) -> str:
    if PROFILE_START not in existing:
        return existing + _appended_separator(existing) + block + "\n"
    start = existing.index(PROFILE_START)
    end = existing.index(PROFILE_END, start) + len(PROFILE_END)
    return existing[:start] + block + existing[end:]


CLEANUP_SECONDS = 2.0

READ_CHUNK_BYTES = 64 * 1024

POLL_SECONDS = 0.005


class _BoundedReader:
    """Reads both streams into one buffer and stops at the byte ceiling."""

    def __init__(self, process: object, max_bytes: int) -> None:
        self._process = process
        self._max_bytes = max_bytes
        self._lock = threading.Lock()
        self._overflow = threading.Event()
        self.chunks: dict[str, bytearray] = {
            "stdout": bytearray(),
            "stderr": bytearray(),
        }
        self.total = 0

    def overflowed(self) -> bool:
        return self._overflow.is_set()

    def read(self, name: str) -> None:
        stream = getattr(self._process, name)
        if stream is None:
            return
        while True:
            data = stream.read(READ_CHUNK_BYTES)
            if not data:
                return
            if self._store(name, data):
                return

    def _store(self, name: str, data: bytes) -> bool:
        """Return True when the ceiling is reached and reading must stop."""
        with self._lock:
            remaining = self._max_bytes + 1 - self.total
            if remaining > 0:
                captured = data[:remaining]
                self.chunks[name].extend(captured)
                self.total += len(captured)
            if len(data) > remaining or self.total > self._max_bytes:
                self._overflow.set()
                return True
        return False


def _close_stdin(process: object) -> None:
    if process.stdin is not None:
        process.stdin.close()


def _started_readers(reader: _BoundedReader) -> list[threading.Thread]:
    threads = [
        threading.Thread(target=reader.read, args=(name,), daemon=True)
        for name in ("stdout", "stderr")
    ]
    for thread in threads:
        thread.start()
    return threads


def _awaited_process(
    process: object,
    threads: list[threading.Thread],
    reader: _BoundedReader,
    deadline: float,
) -> bool:
    """True only when the process exited and both readers finished in time."""
    while time.monotonic() < deadline:
        if reader.overflowed():
            return False
        if _process_settled(process, threads):
            return True
        time.sleep(POLL_SECONDS)
    return False


def _readers_finished(threads: list[threading.Thread]) -> bool:
    return all(not thread.is_alive() for thread in threads)


def _process_settled(process: object, threads: list[threading.Thread]) -> bool:
    return process.poll() is not None and _readers_finished(threads)


def _remaining_cleanup(state: dict[str, float | None]) -> float:
    cleanup_deadline = state["cleanup_deadline"]
    if cleanup_deadline is None:
        return 0.0
    return max(0.0, cleanup_deadline - time.monotonic())


def _terminated_tree(tree: object, state: dict[str, float | None]) -> bool:
    state["cleanup_deadline"] = time.monotonic() + CLEANUP_SECONDS
    try:
        tree.terminate(deadline=state["cleanup_deadline"])
    except (OSError, RuntimeError, TimeoutError):
        return False
    return True


def _joined_readers(
    threads: list[threading.Thread], state: dict[str, float | None]
) -> None:
    for thread in threads:
        thread.join(timeout=_remaining_cleanup(state))


def _bounded_result(
    process: object,
    reader: _BoundedReader,
    threads: list[threading.Thread],
    verified: bool,
) -> tuple[int, bytes] | None:
    if any(thread.is_alive() for thread in threads):
        return None
    if not verified or reader.overflowed():
        return None
    return process.returncode, bytes(reader.chunks["stdout"])


def _terminate_quietly(tree: object, state: dict[str, float | None]) -> None:
    if state["cleanup_deadline"] is None:
        state["cleanup_deadline"] = time.monotonic() + CLEANUP_SECONDS
    try:
        tree.terminate(deadline=state["cleanup_deadline"])
    except (OSError, RuntimeError, TimeoutError):
        pass


def _closed_tree(
    tree: object, process: object, state: dict[str, float | None]
) -> None:
    if process.poll() is None:
        _terminate_quietly(tree, state)
    try:
        tree.close()
    except (OSError, RuntimeError):
        pass


def _require_process_bounds(timeout_seconds: float, max_bytes: int) -> None:
    if timeout_seconds <= 0 or max_bytes <= 0:
        raise ValueError("process bounds must be positive")


def _bounded_process_output(
    command: Sequence[str],
    *,
    cwd: Path,
    environment: Mapping[str, str],
    timeout_seconds: float,
    max_bytes: int,
) -> tuple[int, bytes] | None:
    _require_process_bounds(timeout_seconds, max_bytes)
    deadline = time.monotonic() + timeout_seconds
    tree = ProcessTree.spawn_with_deadline(
        command,
        cwd=Path(cwd),
        env=environment,
        deadline=deadline,
    )
    process = tree.process
    _close_stdin(process)
    reader = _BoundedReader(process, max_bytes)
    threads = _started_readers(reader)
    state: dict[str, float | None] = {"cleanup_deadline": None}
    try:
        verified = _awaited_process(process, threads, reader, deadline)
        if not verified and not _terminated_tree(tree, state):
            return None
        _joined_readers(threads, state)
        return _bounded_result(process, reader, threads, verified)
    finally:
        _closed_tree(tree, process, state)


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
    document = _probe_document(completed)
    if document is None:
        return "configured_unverified"
    return verify_effective_entry(document, expected)


def _probe_document(completed: tuple[int, bytes] | None) -> dict[str, Any] | None:
    if completed is None or completed[0] != 0:
        return None
    try:
        document = json.loads(completed[1].decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(document, dict):
        return None
    return document


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
    if not _opencode_detected(config_dir, executable, debug_command):
        return {"config_file": None, "plugin_file": None, "status": "not_detected"}

    root = Path(root)
    expected = expected_opencode_entry(root)
    merged = merge_opencode_user_config(config_dir, expected)
    plugin = config_dir / "plugins" / "llm-wiki-memory.js"
    _copy_plugin(root / "scripts" / "llm-wiki-memory-opencode.js", plugin, root)
    return {
        "config_file": str(merged.config_file),
        "plugin_file": str(plugin),
        "status": _opencode_status(
            _opencode_debug_command(debug_command, executable),
            cwd=cwd,
            root=root,
            state_root=state_root,
            environment=environment,
            expected=expected,
        ),
    }


def _opencode_detected(
    config_dir: Path, executable: str | None, debug_command: Sequence[str] | None
) -> bool:
    return debug_command is not None or executable is not None or config_dir.exists()


def _opencode_debug_command(
    debug_command: Sequence[str] | None, executable: str | None
) -> Sequence[str] | None:
    if debug_command is not None:
        return debug_command
    if executable is None:
        return None
    return [executable, "debug", "config"]


def _opencode_status(
    debug_command: Sequence[str] | None,
    *,
    cwd: Path,
    root: Path,
    state_root: Path,
    environment: Mapping[str, str] | None,
    expected: Mapping[str, Any],
) -> str:
    if debug_command is None:
        return "configured_unverified"
    child_environment = {
        "LLM_WIKI_ROOT": str(root),
        "LLM_WIKI_STATE_ROOT": str(state_root),
        **dict(environment or {}),
    }
    return probe_effective_entry(
        debug_command,
        cwd=cwd,
        environment=child_environment,
        expected=expected,
    )


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
