"""Shared helpers for memory automation state.

Three-zone layout: vault holds code + knowledge + gitignored runtime dirs.

    <vault>/
      run/state.json     # compile hashes, dedupe, heartbeats
      run/compile.pid    # maybe_compile lock
      run/queue/         # deferred LLM tasks
      logs/              # lint / nightly reports
      cache/             # search / QMD indexes
                       # cache/cognee/ — optional semantic graph

`cache/` (incl. `cache/cognee/`), `logs/`, `run/` are gitignored — they live inside the
vault for single-checkout portability but git never tracks their churn.
Override the root via LLM_WIKI_STATE_ROOT (tests use a temp dir).

Written by multiple concurrent processes (flush_memory and compile_memory
may run at the same time). All writers MUST go through `update_state(mutator)`
so the mutation is applied on top of the latest on-disk version under a
cross-platform file lock — otherwise a slow writer will clobber fields
written by a faster one.
"""

from __future__ import annotations

import errno
import hashlib
import hmac
import json
import math
import os
import re
import stat
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import Path
from typing import Any


def _resolve_vault_root(start: Path) -> Path:
    """Resolve the canonical vault root even from inside a git worktree.

    A naive `start.parent.parent` points to the worktree's own root, not
    the main vault. Git exposes the main repo via
    `git rev-parse --git-common-dir`, whose parent is the canonical vault.
    Falls back to the simple behavior if git is unavailable.
    """
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "--git-common-dir"],
            cwd=str(start),
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
        git_common_dir = Path(out) if Path(out).is_absolute() else (start / out).resolve()
        git_common_dir = git_common_dir.resolve()
        if git_common_dir.name == ".git":
            return git_common_dir.parent
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        pass
    return start


# Canonical vault root: prefer LLM_WIKI_ROOT when set (installed instance),
# else resolve from this file's location (worktree-aware).
def _vault_root() -> Path:
    env = os.environ.get("LLM_WIKI_ROOT")
    if env:
        # Keep invalid explicit paths inert so never-fail helpers can import
        # this module and reject them before touching the filesystem.
        return Path(env) if "\0" in env else Path(env).resolve()
    return _resolve_vault_root(Path(__file__).resolve().parent.parent)


ROOT = _vault_root()

# Runtime state lives INSIDE the vault as gitignored dirs (cache/, logs/,
# run/) — keeps everything in one checkout, git ignores the churn.
# Overridable via LLM_WIKI_STATE_ROOT for explicit portability (tests use a
# temp dir; multi-disk setups can point elsewhere).
_state_root_raw = os.environ.get("LLM_WIKI_STATE_ROOT", str(ROOT))
STATE_ROOT = (
    Path(_state_root_raw)
    if "\0" in _state_root_raw
    else Path(_state_root_raw).resolve()
)
STATE_DIR = STATE_ROOT / "run"
REPORTS_DIR = STATE_ROOT / "logs"
STATE_FILE = STATE_DIR / "state.json"
LOCK_FILE = STATE_DIR / "state.json.lock"
_STATE_PROCESS_LOCK = threading.Lock()
_STATE_LOCK_LOCAL = threading.local()
_COMPILE_PROCESS_LOCK = threading.Lock()
_COMPILE_LOCK_LOCAL = threading.local()
_KNOWLEDGE_PUBLICATION_PROCESS_LOCK = threading.Lock()
_KNOWLEDGE_PUBLICATION_LOCK_LOCAL = threading.local()
_BOUND_ATOMIC_DIRECTORY_LOCAL = threading.local()
_ATOMIC_WRITE_REQUIRE_ABSENT_LOCAL = threading.local()
_ATOMIC_WRITE_EXPECTED_TARGET_LOCAL = threading.local()
FILE_HASH_CHUNK_BYTES = 64 * 1024
MAX_RETAINED_CONDITIONAL_ARTIFACTS_PER_TARGET = 32
MAX_RETAINED_CONDITIONAL_ARTIFACT_BYTES_PER_TARGET = 2 * 1024 * 1024
MAX_RETAINED_CONDITIONAL_ARTIFACTS_GLOBAL = 512
MAX_RETAINED_CONDITIONAL_ARTIFACT_BYTES_GLOBAL = 32 * 1024 * 1024
MAX_STATE_JSON_CHARS = 4 * 1024 * 1024
MAX_COMPILE_RECEIPTS = 10_000
MAX_COMPILE_RECEIPT_BYTES = 512 * 1024
MAX_COMPILE_RECEIPT_JOURNALS = 1024
MAX_COMPILE_RECEIPT_EFFECTS = 1024
MAX_COMPILE_RECEIPT_TARGETS = 256
MAX_COMPILE_RECEIPT_EVIDENCE = 4096
MAX_COMPILE_GENERATION_LINEAGE = 16
MAX_COMPILE_RECEIPT_TARGET_BYTES = 64 * 1024
MAX_COMPILE_RECEIPT_INDEX_BYTES = 4 * 1024 * 1024
MAX_INTERNAL_JSON_DEPTH = 128
MAX_JSON_LEXICAL_TOKENS = 1_000_000
MAX_JSON_NUMBER_CHARS = 1_024
MAX_FRONTMATTER_CHARS = 16 * 1024
MAX_HOOK_STDIN_BYTES = 4 * 1024 * 1024


class AtomicWriteConflictError(OSError):
    """The file displaced by conditional publication was not the admitted base."""


class AtomicWriteRecoveryError(OSError):
    """A native conditional-publication failure left recovery files on disk."""

    def __init__(
        self,
        message: str,
        recovery_paths: list[Path],
        recovery_state: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.recovery_paths = tuple(str(path) for path in recovery_paths)
        self.recovery_state = recovery_state or {
            "version": 1,
            "kind": "unresolved",
            "status": "required",
            "owned_paths": [path.name for path in recovery_paths],
        }


class AtomicWriteRollbackError(AtomicWriteRecoveryError):
    """A conflicting publication could not atomically restore its displaced file."""


def _reject_duplicate_json_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate key in JSON object: {key}")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON number: {value}")


def _is_unicode_noncharacter(codepoint: int) -> bool:
    return (
        0xFDD0 <= codepoint <= 0xFDEF
        or 0 <= codepoint <= 0x10FFFF
        and codepoint & 0xFFFF in {0xFFFE, 0xFFFF}
    )


def _contains_invalid_unicode_scalar(value: str) -> bool:
    return any(
        0xD800 <= ord(char) <= 0xDFFF
        or _is_unicode_noncharacter(ord(char))
        for char in value
    )


def _preflight_json_lexical_resources(
    text: str,
    *,
    max_depth: int,
    max_lexical_tokens: int,
) -> None:
    in_string = False
    escaped = False
    container_depth = 0
    structural_tokens = 0
    number_chars = 0
    in_number = False

    for char in text:
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue

        if char == '"':
            in_string = True
            in_number = False
            number_chars = 0
            continue

        if in_number and (char.isdigit() or char in ".eE+-"):
            number_chars += 1
            if number_chars > MAX_JSON_NUMBER_CHARS:
                raise ValueError("JSON number exceeds number length limit")
            continue
        in_number = False
        number_chars = 0
        if char.isdigit() or char == "-":
            in_number = True
            number_chars = 1

        if char in "{}[],:":
            structural_tokens += 1
            if structural_tokens > max_lexical_tokens:
                raise ValueError("JSON object exceeds lexical resource limit")
        if char in "{[":
            container_depth += 1
            if container_depth - 1 > max_depth:
                raise ValueError("JSON object exceeds depth limit")
        elif char in "}]" and container_depth:
            container_depth -= 1


def decode_json_object_strict(
    raw: bytes | bytearray | str,
    *,
    max_bytes: int,
    max_chars: int | None = None,
    max_depth: int = MAX_INTERNAL_JSON_DEPTH,
    max_members: int = MAX_JSON_LEXICAL_TOKENS,
    max_lexical_tokens: int | None = None,
) -> dict[str, Any]:
    """Decode one byte, character, depth, and member bounded JSON object."""
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes <= 0:
        raise ValueError("max_bytes must be a positive integer")
    if max_chars is None:
        max_chars = max_bytes
    if (
        isinstance(max_chars, bool)
        or not isinstance(max_chars, int)
        or max_chars <= 0
    ):
        raise ValueError("max_chars must be a positive integer")
    if (
        isinstance(max_depth, bool)
        or not isinstance(max_depth, int)
        or max_depth < 0
    ):
        raise ValueError("max_depth must be a nonnegative integer")
    if (
        isinstance(max_members, bool)
        or not isinstance(max_members, int)
        or max_members < 0
    ):
        raise ValueError("max_members must be a nonnegative integer")
    if max_lexical_tokens is None:
        max_lexical_tokens = MAX_JSON_LEXICAL_TOKENS
    if (
        isinstance(max_lexical_tokens, bool)
        or not isinstance(max_lexical_tokens, int)
        or max_lexical_tokens < 0
    ):
        raise ValueError("max_lexical_tokens must be a nonnegative integer")
    if isinstance(raw, str):
        encoded = raw.encode("utf-8", errors="strict")
        if len(encoded) > max_bytes:
            raise ValueError("JSON object exceeds byte limit")
        text = raw
    elif isinstance(raw, bytes | bytearray):
        if len(raw) > max_bytes:
            raise ValueError("JSON object exceeds byte limit")
        text = bytes(raw).decode("utf-8", errors="strict")
    else:
        raise TypeError("JSON input must be bytes or text")
    if len(text) > max_chars:
        raise ValueError("JSON object exceeds character limit")

    _preflight_json_lexical_resources(
        text,
        max_depth=max_depth,
        max_lexical_tokens=max_lexical_tokens,
    )
    payload = json.loads(
        text,
        object_pairs_hook=_reject_duplicate_json_pairs,
        parse_constant=_reject_json_constant,
    )
    if not isinstance(payload, dict):
        raise ValueError("JSON root must be an object")

    pending: list[tuple[Any, int]] = [(payload, 0)]
    members = 0
    while pending:
        value, depth = pending.pop()
        if depth > max_depth:
            raise ValueError("JSON object exceeds depth limit")
        if isinstance(value, str):
            if _contains_invalid_unicode_scalar(value):
                raise ValueError("JSON object contains an invalid Unicode scalar")
        elif isinstance(value, float) and not math.isfinite(value):
            raise ValueError("JSON object contains a non-finite number")
        elif isinstance(value, dict):
            members += len(value)
            if members > max_members:
                raise ValueError("JSON object exceeds member limit")
            pending.extend((key, depth + 1) for key in value)
            pending.extend((item, depth + 1) for item in value.values())
        elif isinstance(value, list):
            members += len(value)
            if members > max_members:
                raise ValueError("JSON object exceeds member limit")
            pending.extend((item, depth + 1) for item in value)
    return payload


def read_json_object_bounded_with_status(
    stream: Any,
    *,
    max_bytes: int,
) -> tuple[dict[str, Any] | None, str]:
    """Read one bounded JSON object and distinguish oversize from malformed."""
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes <= 0:
        raise ValueError("max_bytes must be a positive integer")

    try:
        binary_stream = getattr(stream, "buffer", None)
        if binary_stream is not None:
            raw = binary_stream.read(max_bytes + 1)
            if not isinstance(raw, bytes | bytearray):
                return None, "invalid"
            if len(raw) > max_bytes:
                return None, "oversized"
            text = raw.decode("utf-8", errors="strict")
        else:
            text = stream.read(max_bytes + 1)
            if not isinstance(text, str):
                return None, "invalid"
            if len(text) > max_bytes:
                return None, "oversized"
            if len(text.encode("utf-8", errors="strict")) > max_bytes:
                return None, "oversized"
        payload = decode_json_object_strict(text, max_bytes=max_bytes)
    except (OSError, UnicodeError, ValueError, TypeError, RecursionError, MemoryError):
        return None, "invalid"
    return payload, "ok"


def read_json_object_bounded(
    stream: Any,
    *,
    max_bytes: int,
) -> dict[str, Any] | None:
    """Read one UTF-8 JSON object within a byte cap, or return ``None``."""
    payload, _status = read_json_object_bounded_with_status(
        stream,
        max_bytes=max_bytes,
    )
    return payload


def read_json_object_file_bounded(
    path: Path,
    *,
    max_bytes: int,
    max_depth: int,
) -> dict[str, Any] | None:
    """Read one strict UTF-8 JSON object file within byte and depth caps."""
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes <= 0:
        raise ValueError("max_bytes must be a positive integer")
    if (
        isinstance(max_depth, bool)
        or not isinstance(max_depth, int)
        or max_depth < 0
    ):
        raise ValueError("max_depth must be a nonnegative integer")

    try:
        with Path(path).open("rb") as handle:
            raw = handle.read(max_bytes + 1)
    except (OSError, MemoryError):
        return None
    if len(raw) > max_bytes:
        return None

    try:
        payload = decode_json_object_strict(
            raw,
            max_bytes=max_bytes,
            max_depth=max_depth,
        )
    except (
        UnicodeError,
        ValueError,
        TypeError,
        RecursionError,
        MemoryError,
        OverflowError,
    ):
        return None
    return payload


@dataclass(frozen=True)
class FrontmatterScalar:
    present: bool
    value: str | None


@dataclass(frozen=True)
class BoundedPathInventory:
    paths: tuple[Path, ...]
    overflow: bool = False
    error: bool = False

    @property
    def incomplete(self) -> bool:
        return self.overflow or self.error


@dataclass(frozen=True)
class _BoundAtomicDirectory:
    path: Path
    identity: tuple[int, int, int]
    descriptor: int | None = None

    def validate_path(self) -> None:
        metadata = self.path.lstat()
        identity = (
            metadata.st_dev,
            metadata.st_ino,
            stat.S_IFMT(metadata.st_mode),
        )
        if (
            identity != self.identity
            or not stat.S_ISDIR(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or _is_reparse_point(metadata)
        ):
            raise OSError("bound atomic-write directory identity changed")


def _comment_or_empty(remainder: str) -> bool:
    if not remainder:
        return True
    if remainder.isspace():
        return True
    return remainder[0].isspace() and remainder.lstrip().startswith("#")


def _single_quoted_scalar(value: str) -> tuple[str, int] | None:
    result: list[str] = []
    index = 1
    while index < len(value):
        char = value[index]
        if char != "'":
            result.append(char)
            index += 1
            continue
        if index + 1 < len(value) and value[index + 1] == "'":
            result.append("'")
            index += 2
            continue
        return "".join(result), index + 1
    return None


_YAML_DOUBLE_QUOTED_ESCAPES = {
    "0": "\0",
    "a": "\a",
    "b": "\b",
    "t": "\t",
    "n": "\n",
    "v": "\v",
    "f": "\f",
    "r": "\r",
    "e": "\x1b",
    " ": " ",
    '"': '"',
    "/": "/",
    "\\": "\\",
    "N": "\x85",
    "_": "\xa0",
    "L": "\u2028",
    "P": "\u2029",
}
_YAML_HEX_ESCAPE_LENGTHS = {"x": 2, "u": 4, "U": 8}


def _scan_yaml_double_quoted_scalar(value: str) -> tuple[str, int, bool]:
    """Decode one bounded, single-line YAML double-quoted scalar."""
    decoded: list[str] = []
    index = 1
    while index < len(value):
        char = value[index]
        if char == '"':
            return "".join(decoded), index + 1, True
        if char != "\\":
            codepoint = ord(char)
            if (
                char in "\r\n"
                or codepoint < 0x20 and char != "\t"
                or 0x7F <= codepoint <= 0x84
                or 0x86 <= codepoint <= 0x9F
                or 0xD800 <= codepoint <= 0xDFFF
                or _is_unicode_noncharacter(codepoint)
            ):
                return "".join(decoded), index, False
            decoded.append(char)
            index += 1
            continue
        if index + 1 >= len(value):
            return "".join(decoded), index, False
        escaped = value[index + 1]
        simple = _YAML_DOUBLE_QUOTED_ESCAPES.get(escaped)
        if simple is not None:
            decoded.append(simple)
            index += 2
            continue
        width = _YAML_HEX_ESCAPE_LENGTHS.get(escaped)
        if width is None:
            return "".join(decoded), index, False
        digits_start = index + 2
        digits_end = digits_start + width
        digits = value[digits_start:digits_end]
        if len(digits) != width or re.fullmatch(r"[0-9A-Fa-f]+", digits) is None:
            return "".join(decoded), index, False
        codepoint = int(digits, 16)
        if (
            codepoint > 0x10FFFF
            or 0xD800 <= codepoint <= 0xDFFF
            or _is_unicode_noncharacter(codepoint)
        ):
            return "".join(decoded), index, False
        decoded.append(chr(codepoint))
        index = digits_end
    return "".join(decoded), index, False


def _double_quoted_scalar(value: str) -> tuple[str, int] | None:
    decoded, end, complete = _scan_yaml_double_quoted_scalar(value)
    return (decoded, end) if complete else None


def _parse_scalar(value: str) -> str | None:
    text = value.strip()
    if not text:
        return None
    if text.startswith('"'):
        parsed = _double_quoted_scalar(text)
        if parsed is None:
            return None
        scalar, end = parsed
        if (
            not scalar
            or any(_is_unicode_noncharacter(ord(char)) for char in scalar)
            or not _comment_or_empty(text[end:])
        ):
            return None
        return scalar
    if text.startswith("'"):
        parsed = _single_quoted_scalar(text)
        if parsed is None:
            return None
        scalar, end = parsed
        if (
            not scalar
            or any(_is_unicode_noncharacter(ord(char)) for char in scalar)
            or not _comment_or_empty(text[end:])
        ):
            return None
        return scalar
    if text[0] in "-?:,[]{}#&*!|>@`\"'":
        return None
    comment = re.search(r"\s+#", text)
    if comment:
        text = text[: comment.start()].rstrip()
    if not text or any(
        ord(char) < 32 or _is_unicode_noncharacter(ord(char)) for char in text
    ):
        return None
    return text


def _decode_frontmatter_key(line: str) -> tuple[str, int] | None:
    if line.startswith('"'):
        return _double_quoted_scalar(line)
    if line.startswith("'"):
        return _single_quoted_scalar(line)
    end = 0
    while end < len(line) and line[end] not in " \t:=":
        end += 1
    return (line[:end], end) if end else None


def parse_frontmatter_scalar(
    content: str,
    key: str,
    *,
    max_chars: int = MAX_FRONTMATTER_CHARS,
) -> FrontmatterScalar:
    """Return absent, valid, or present-invalid for one scalar field."""
    if max_chars <= 0:
        return FrontmatterScalar(True, None)
    bounded = content.removeprefix("\ufeff")[: max_chars + 1]
    lines = bounded.splitlines()
    if not lines:
        return FrontmatterScalar(False, None)
    if lines[0].strip() != "---":
        if re.match(r"^---(?:[ \t]+\S)", lines[0]):
            return FrontmatterScalar(True, None)
        return FrontmatterScalar(False, None)

    closing = next(
        (index for index, line in enumerate(lines[1:], 1) if line.rstrip() == "---"),
        None,
    )
    if closing is None:
        return FrontmatterScalar(True, None)

    root_lines = lines[1:closing]
    substantive = [
        line
        for line in root_lines
        if line.strip() and not line.lstrip(" ").startswith("#")
    ]
    if any(line.startswith("\t") for line in substantive):
        return FrontmatterScalar(True, None)
    root_indent = min(
        (len(line) - len(line.lstrip(" ")) for line in substantive),
        default=0,
    )
    if root_indent:
        lines[1:closing] = [
            line[root_indent:] if line.strip() else line
            for line in root_lines
        ]

    values: list[str | None] = []
    malformed_explicit_entry = False
    unsupported_root = False
    index = 1
    while index < closing:
        line = lines[index]
        if not line or line[0].isspace() or line.startswith("#"):
            index += 1
            continue
        if line.startswith("?"):
            key_text = line[1:]
            explicit_key: str | None = None
            if key_text[:1] in {" ", "\t"}:
                key_text = key_text.lstrip(" \t")
                decoded = _decode_frontmatter_key(key_text)
                if decoded is not None:
                    candidate_key, key_end = decoded
                    if (
                        candidate_key
                        and key_text[0] not in "-?:,[]{}#&*!|>@`"
                        and _comment_or_empty(key_text[key_end:])
                    ):
                        explicit_key = candidate_key
            value_line = lines[index + 1] if index + 1 < closing else None
            explicit_value: str | None = None
            valid_value_line = (
                value_line is not None
                and value_line.startswith(":")
                and value_line[1:2] in {"", " ", "\t"}
            )
            if valid_value_line:
                explicit_value = _parse_scalar(value_line[1:])
            if explicit_key is None or not valid_value_line:
                malformed_explicit_entry = True
            elif explicit_key == key:
                values.append(explicit_value)
            index += 2 if value_line is not None and value_line.startswith(":") else 1
            continue
        if line.startswith(":"):
            malformed_explicit_entry = True
            index += 1
            continue
        if line[0] in "-[{!&*%":
            unsupported_root = True
            index += 1
            continue
        if line[:1] in {"'", '"'}:
            decoded = _decode_frontmatter_key(line)
        else:
            separator = next(
                (
                    position
                    for position, char in enumerate(line)
                    if char == ":"
                    and line[position + 1 : position + 2] in {"", " ", "\t"}
                ),
                None,
            )
            plain_key = line[:separator].rstrip(" \t") if separator is not None else ""
            decoded = (plain_key, len(plain_key)) if plain_key else None
        if decoded is None:
            unsupported_root = True
            index += 1
            continue
        decoded_key, end = decoded
        entry_match = re.fullmatch(
            r"[ \t]*:(?:[ \t]+(.*?)[ \t]*|[ \t]*)",
            line[end:],
        )
        if entry_match is None:
            unsupported_root = True
            index += 1
            continue
        raw_value = (entry_match.group(1) or "").lstrip()
        if decoded_key == "<<" or raw_value.startswith(("&", "*", "!")):
            unsupported_root = True
        if decoded_key != key:
            if decoded_key.rstrip("\"'") == key:
                values.append(None)
            index += 1
            continue
        values.append(_parse_scalar(entry_match.group(1) or ""))
        index += 1
    if malformed_explicit_entry or unsupported_root:
        return FrontmatterScalar(True, None)
    if not values:
        return FrontmatterScalar(False, None)
    if len(values) != 1 or values[0] is None:
        return FrontmatterScalar(True, None)
    return FrontmatterScalar(True, values[0])


def parse_project_scope(content: str) -> FrontmatterScalar:
    scope = parse_frontmatter_scalar(content, "project")
    if not scope.present or scope.value is None:
        return scope
    from session_start_project_state import is_canonical_project_slug

    if not is_canonical_project_slug(scope.value):
        return FrontmatterScalar(True, None)
    return scope


def _is_reparse_point(metadata) -> bool:
    attributes = getattr(metadata, "st_file_attributes", 0)
    return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


_USE_WINDOWS_DIRECTORY_HANDLES = os.name == "nt"
_WINDOWS_FILE_ATTRIBUTE_DIRECTORY = 0x10
_WINDOWS_FILE_ATTRIBUTE_REPARSE_POINT = 0x400


def _load_windows_kernel32() -> Any:
    import ctypes

    return ctypes.WinDLL("kernel32", use_last_error=True)


def _open_windows_directory_handle(path: Path) -> tuple[Any, Any]:
    import ctypes
    from ctypes import wintypes

    kernel32 = _load_windows_kernel32()
    create_file = kernel32.CreateFileW
    create_file.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    create_file.restype = wintypes.HANDLE

    path_text = os.path.abspath(path)
    if not path_text.startswith("\\\\?\\"):
        path_text = (
            "\\\\?\\UNC\\" + path_text[2:]
            if path_text.startswith("\\\\")
            else "\\\\?\\" + path_text
        )
    file_list_directory = 0x0001
    share_read_write = 0x00000001 | 0x00000002
    open_existing = 3
    open_directory_no_follow = 0x02000000 | 0x00200000
    # FILE_LIST_DIRECTORY is the minimum listing right; CreateFileW shares last
    # until CloseHandle, and no FILE_SHARE_DELETE blocks rename/delete:
    # https://learn.microsoft.com/windows/win32/api/fileapi/nf-fileapi-createfilew
    handle = create_file(
        path_text,
        file_list_directory,
        share_read_write,
        None,
        open_existing,
        open_directory_no_follow,
        None,
    )
    if handle == ctypes.c_void_p(-1).value:
        error = ctypes.get_last_error()
        raise OSError(f"CreateFileW failed with Windows error {error}")
    return kernel32, handle


def _windows_directory_handle_metadata(handle_state: tuple[Any, Any]) -> tuple[int, int, int]:
    import ctypes
    from ctypes import wintypes

    class ByHandleFileInformation(ctypes.Structure):
        _fields_ = [
            ("dwFileAttributes", wintypes.DWORD),
            ("ftCreationTime", wintypes.FILETIME),
            ("ftLastAccessTime", wintypes.FILETIME),
            ("ftLastWriteTime", wintypes.FILETIME),
            ("dwVolumeSerialNumber", wintypes.DWORD),
            ("nFileSizeHigh", wintypes.DWORD),
            ("nFileSizeLow", wintypes.DWORD),
            ("nNumberOfLinks", wintypes.DWORD),
            ("nFileIndexHigh", wintypes.DWORD),
            ("nFileIndexLow", wintypes.DWORD),
        ]

    kernel32, handle = handle_state
    get_information = kernel32.GetFileInformationByHandle
    get_information.argtypes = (
        wintypes.HANDLE,
        ctypes.POINTER(ByHandleFileInformation),
    )
    get_information.restype = wintypes.BOOL
    information = ByHandleFileInformation()
    if not get_information(handle, ctypes.byref(information)):
        error = ctypes.get_last_error()
        raise OSError(
            f"GetFileInformationByHandle failed with Windows error {error}"
        )
    file_index = (information.nFileIndexHigh << 32) | information.nFileIndexLow
    return (
        information.dwVolumeSerialNumber,
        file_index,
        information.dwFileAttributes,
    )


def _close_windows_directory_handle(handle_state: tuple[Any, Any]) -> bool:
    from ctypes import wintypes

    kernel32, handle = handle_state
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = (wintypes.HANDLE,)
    close_handle.restype = wintypes.BOOL
    return bool(close_handle(handle))


@contextmanager
def _held_windows_directory(path: Path, expected_metadata) -> Iterator[None]:
    if not _USE_WINDOWS_DIRECTORY_HANDLES:
        yield
        return

    handle_state = _open_windows_directory_handle(path)
    try:
        _device, inode, attributes = _windows_directory_handle_metadata(handle_state)
        if (
            inode != expected_metadata.st_ino
            or not attributes & _WINDOWS_FILE_ATTRIBUTE_DIRECTORY
            or attributes & _WINDOWS_FILE_ATTRIBUTE_REPARSE_POINT
        ):
            raise OSError("opened directory handle failed identity validation")
        yield
    finally:
        if not _close_windows_directory_handle(handle_state):
            raise OSError("CloseHandle failed for directory handle")


@contextmanager
def bind_atomic_writes_to_directory(path: Path) -> Iterator[_BoundAtomicDirectory]:
    """Bind direct-child atomic writes to one validated directory identity."""
    root = Path(os.path.abspath(path))
    metadata = root.lstat()
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or _is_reparse_point(metadata)
    ):
        raise OSError("atomic-write directory is not a regular directory")
    identity = (
        metadata.st_dev,
        metadata.st_ino,
        stat.S_IFMT(metadata.st_mode),
    )
    previous = getattr(_BOUND_ATOMIC_DIRECTORY_LOCAL, "current", None)
    descriptor: int | None = None
    windows_context = None
    windows_entered = False
    try:
        if _USE_WINDOWS_DIRECTORY_HANDLES:
            windows_context = _held_windows_directory(root, metadata)
            windows_context.__enter__()
            windows_entered = True
        elif os.name == "posix":
            flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(
                os, "O_NOFOLLOW", 0
            )
            descriptor = os.open(root, flags)
            opened = os.fstat(descriptor)
            opened_identity = (
                opened.st_dev,
                opened.st_ino,
                stat.S_IFMT(opened.st_mode),
            )
            if opened_identity != identity or not stat.S_ISDIR(opened.st_mode):
                raise OSError("opened atomic-write directory identity changed")
        else:
            raise OSError("identity-bound atomic writes are unsupported")
        bound = _BoundAtomicDirectory(root, identity, descriptor)
        _BOUND_ATOMIC_DIRECTORY_LOCAL.current = bound
        bound.validate_path()
        yield bound
    finally:
        if previous is None:
            try:
                del _BOUND_ATOMIC_DIRECTORY_LOCAL.current
            except AttributeError:
                pass
        else:
            _BOUND_ATOMIC_DIRECTORY_LOCAL.current = previous
        if descriptor is not None:
            os.close(descriptor)
        if windows_entered:
            windows_context.__exit__(None, None, None)


@contextmanager
def require_absent_atomic_target() -> Iterator[None]:
    previous = getattr(_ATOMIC_WRITE_REQUIRE_ABSENT_LOCAL, "enabled", False)
    _ATOMIC_WRITE_REQUIRE_ABSENT_LOCAL.enabled = True
    try:
        yield
    finally:
        if previous:
            _ATOMIC_WRITE_REQUIRE_ABSENT_LOCAL.enabled = True
        else:
            try:
                del _ATOMIC_WRITE_REQUIRE_ABSENT_LOCAL.enabled
            except AttributeError:
                pass


def _validated_expected_atomic_target(
    expected: dict[str, Any],
) -> tuple[tuple[int, int, int], str, int, int, int]:
    identity = expected.get("identity") if isinstance(expected, dict) else None
    digest = expected.get("sha256") if isinstance(expected, dict) else None
    size = expected.get("size") if isinstance(expected, dict) else None
    mode = expected.get("mode") if isinstance(expected, dict) else None
    file_attributes = (
        expected.get("file_attributes") if isinstance(expected, dict) else None
    )
    nlink = expected.get("nlink") if isinstance(expected, dict) else None
    if (
        not isinstance(identity, list)
        or len(identity) != 3
        or any(isinstance(item, bool) or not isinstance(item, int) for item in identity)
        or not isinstance(digest, str)
        or re.fullmatch(r"[0-9a-f]{64}", digest) is None
        or isinstance(size, bool)
        or not isinstance(size, int)
        or size < 0
        or isinstance(mode, bool)
        or not isinstance(mode, int)
        or mode < 0
        or isinstance(file_attributes, bool)
        or not isinstance(file_attributes, int)
        or file_attributes < 0
        or nlink != 1
    ):
        raise ValueError("atomic target precondition is invalid")
    return tuple(identity), digest, size, mode, file_attributes


@contextmanager
def require_matching_atomic_target(expected: dict[str, Any]) -> Iterator[None]:
    """Route the next atomic write through expected-base conditional publication."""
    validated = _validated_expected_atomic_target(expected)
    previous = getattr(_ATOMIC_WRITE_EXPECTED_TARGET_LOCAL, "current", None)
    _ATOMIC_WRITE_EXPECTED_TARGET_LOCAL.current = validated
    try:
        yield
    finally:
        if previous is None:
            try:
                del _ATOMIC_WRITE_EXPECTED_TARGET_LOCAL.current
            except AttributeError:
                pass
        else:
            _ATOMIC_WRITE_EXPECTED_TARGET_LOCAL.current = previous


def bounded_path_inventory(
    directory: Path,
    pattern: str,
    limit: int,
    *,
    recursive: bool,
    kind: str,
    required_root: bool = False,
) -> BoundedPathInventory:
    """Enumerate a directory with a hard entry cap and explicit uncertainty."""
    if limit < 0 or kind not in {"file", "directory"}:
        return BoundedPathInventory((), error=True)

    root = Path(directory)
    pending = [root]
    matched: list[Path] = []
    scanned = 0
    current = root
    try:
        while pending:
            current = pending.pop()
            current_metadata = current.lstat()
            if (
                not stat.S_ISDIR(current_metadata.st_mode)
                or stat.S_ISLNK(current_metadata.st_mode)
                or _is_reparse_point(current_metadata)
            ):
                return BoundedPathInventory((), error=True)
            current_identity = (
                current_metadata.st_dev,
                current_metadata.st_ino,
                stat.S_IFMT(current_metadata.st_mode),
            )
            with _held_windows_directory(current, current_metadata):
                try:
                    entries = os.scandir(current)
                except OSError:
                    return BoundedPathInventory((), error=True)
                try:
                    verified_metadata = current.lstat()
                except OSError:
                    entries.close()
                    return BoundedPathInventory((), error=True)
                verified_identity = (
                    verified_metadata.st_dev,
                    verified_metadata.st_ino,
                    stat.S_IFMT(verified_metadata.st_mode),
                )
                if (
                    current_identity != verified_identity
                    or not stat.S_ISDIR(verified_metadata.st_mode)
                    or stat.S_ISLNK(verified_metadata.st_mode)
                    or _is_reparse_point(verified_metadata)
                ):
                    entries.close()
                    return BoundedPathInventory((), error=True)
                with entries:
                    for entry in entries:
                        scanned += 1
                        if scanned > limit:
                            return BoundedPathInventory(
                                tuple(sorted(matched)),
                                overflow=True,
                            )
                        metadata = entry.stat(follow_symlinks=False)
                        path = Path(entry.path)
                        lexical_metadata = path.lstat()
                        bound_device = metadata.st_dev
                        bound_inode = metadata.st_ino
                        if os.name == "nt" and not bound_device:
                            # Windows scandir stat results leave st_dev/st_ino at zero.
                            bound_device = current_metadata.st_dev
                        if os.name == "nt" and not bound_inode:
                            bound_inode = entry.inode()
                        bound_identity = (
                            bound_device,
                            bound_inode,
                            stat.S_IFMT(metadata.st_mode),
                            _is_reparse_point(metadata),
                        )
                        lexical_identity = (
                            lexical_metadata.st_dev,
                            lexical_metadata.st_ino,
                            stat.S_IFMT(lexical_metadata.st_mode),
                            _is_reparse_point(lexical_metadata),
                        )
                        if (
                            bound_identity != lexical_identity
                            or _is_reparse_point(metadata)
                        ):
                            return BoundedPathInventory((), error=True)
                        is_directory = stat.S_ISDIR(metadata.st_mode)
                        is_file = stat.S_ISREG(metadata.st_mode)
                        if recursive and is_directory:
                            pending.append(path)
                        if not fnmatch(entry.name, pattern):
                            continue
                        if (kind == "file" and is_file) or (
                            kind == "directory" and is_directory
                        ):
                            matched.append(path)
    except FileNotFoundError:
        if current == root and scanned == 0:
            return BoundedPathInventory((), error=required_root)
        return BoundedPathInventory((), error=True)
    except OSError:
        return BoundedPathInventory((), error=True)
    return BoundedPathInventory(tuple(sorted(matched)))


def _lock_file_descriptor(handle) -> None:
    handle.seek(0)
    if sys.platform == "win32":
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock_file_descriptor(handle) -> None:
    handle.seek(0)
    if sys.platform == "win32":
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def advisory_file_lock(
    lock_path: Path,
    timeout: float = 30.0,
    poll: float = 0.05,
    description: str = "file lock",
) -> Iterator[None]:
    """Hold a cross-platform advisory lock until the descriptor closes."""
    deadline = time.monotonic() + max(0.0, timeout)
    handle = None
    locked = False
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = lock_path.open("a+b")
        # Windows byte-range locks may extend beyond EOF, so an empty lock file
        # needs no racy initialization write before contenders begin locking.
        while True:
            try:
                _lock_file_descriptor(handle)
                locked = True
                break
            except OSError as exc:
                if exc.errno not in {errno.EACCES, errno.EAGAIN}:
                    raise
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(f"Could not acquire {description}: {lock_path}") from None
                time.sleep(min(poll, max(remaining, 0.0)))
        yield
    finally:
        if locked and handle is not None:
            try:
                _unlock_file_descriptor(handle)
            except OSError:
                pass
        if handle is not None:
            handle.close()


@contextmanager
def compile_file_lock(lock_path: Path, timeout: float = 30.0, poll: float = 0.05) -> Iterator[None]:
    """Hold the reentrant process-wide compile lock until context exit."""
    depth = getattr(_COMPILE_LOCK_LOCAL, "depth", 0)
    if depth:
        _COMPILE_LOCK_LOCAL.depth = depth + 1
        try:
            yield
        finally:
            _COMPILE_LOCK_LOCAL.depth -= 1
        return

    deadline = time.monotonic() + max(0.0, timeout)
    remaining = max(0.0, deadline - time.monotonic())
    if not _COMPILE_PROCESS_LOCK.acquire(timeout=remaining):
        raise TimeoutError(f"Could not acquire compile lock: {lock_path}")

    try:
        remaining = max(0.0, deadline - time.monotonic())
        with advisory_file_lock(
            lock_path,
            timeout=remaining,
            poll=poll,
            description="compile lock",
        ):
            _COMPILE_LOCK_LOCAL.depth = 1
            try:
                yield
            finally:
                _COMPILE_LOCK_LOCAL.depth = 0
    finally:
        _COMPILE_PROCESS_LOCK.release()


@contextmanager
def knowledge_publication_lock(
    timeout: float = 30.0,
    poll: float = 0.05,
) -> Iterator[None]:
    """Serialize knowledge validation and publication across writers."""
    depth = getattr(_KNOWLEDGE_PUBLICATION_LOCK_LOCAL, "depth", 0)
    if depth:
        _KNOWLEDGE_PUBLICATION_LOCK_LOCAL.depth = depth + 1
        try:
            yield
        finally:
            _KNOWLEDGE_PUBLICATION_LOCK_LOCAL.depth -= 1
        return

    lock_path = STATE_ROOT / "run" / "knowledge-publication.lock"
    deadline = time.monotonic() + max(0.0, timeout)
    remaining = max(0.0, deadline - time.monotonic())
    if not _KNOWLEDGE_PUBLICATION_PROCESS_LOCK.acquire(timeout=remaining):
        raise TimeoutError(f"Could not acquire knowledge publication lock: {lock_path}")

    try:
        remaining = max(0.0, deadline - time.monotonic())
        with advisory_file_lock(
            lock_path,
            timeout=remaining,
            poll=poll,
            description="knowledge publication lock",
        ):
            _KNOWLEDGE_PUBLICATION_LOCK_LOCAL.depth = 1
            try:
                yield
            finally:
                _KNOWLEDGE_PUBLICATION_LOCK_LOCAL.depth = 0
    finally:
        _KNOWLEDGE_PUBLICATION_PROCESS_LOCK.release()


def _load_state_while_locked(*, degrade_on_read_error: bool) -> dict[str, Any]:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        with STATE_FILE.open("rb") as handle:
            raw = handle.read(MAX_STATE_JSON_CHARS + 1)
    except FileNotFoundError:
        return {}
    except OSError:
        if degrade_on_read_error:
            return {}
        raise
    else:
        try:
            state = decode_json_object_strict(
                raw,
                max_bytes=MAX_STATE_JSON_CHARS,
            )
        except (UnicodeError, ValueError, TypeError, RecursionError, MemoryError):
            state = None
    if not isinstance(state, dict):
        quarantine = STATE_FILE.with_name(
            f"{STATE_FILE.name}.corrupt-{uuid.uuid4().hex}"
        )
        try:
            os.replace(STATE_FILE, quarantine)
            _sync_parent_directory(STATE_FILE)
        except FileNotFoundError:
            quarantine = None
        except OSError:
            quarantine = None
        try:
            err_log = REPORTS_DIR / "hook-errors.log"
            REPORTS_DIR.mkdir(parents=True, exist_ok=True)
            with err_log.open("a", encoding="utf-8") as f:
                outcome = (
                    f"quarantined as {quarantine.name}"
                    if quarantine is not None
                    else "quarantine unavailable"
                )
                f.write(
                    f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] "
                    f"state.json invalid; {outcome}\n"
                )
        except OSError:
            pass
        return {}
    return state


def load_state() -> dict[str, Any]:
    """Read state for read-only callers, degrading on state-file read errors.

    Invalid JSON or UTF-8 is quarantined under the state lock. A missing file
    and a transient read failure both appear as empty state to read-only
    callers, but neither can cause an existing file to be rewritten.
    """
    with _state_lock():
        return _load_state_while_locked(degrade_on_read_error=True)


def save_state(state: dict[str, Any]) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(state, indent=2, ensure_ascii=False)
    if _contains_invalid_unicode_scalar(serialized):
        raise ValueError("state JSON contains an invalid Unicode scalar")
    if len(serialized.encode("utf-8", errors="strict")) > MAX_STATE_JSON_CHARS:
        raise ValueError(
            f"state JSON exceeds {MAX_STATE_JSON_CHARS}-byte limit"
        )
    atomic_write(
        STATE_FILE,
        serialized,
        encoding="utf-8",
    )


@contextmanager
def _state_lock(timeout: float = 10.0, poll: float = 0.05) -> Iterator[None]:
    """Hold the reentrant process-wide and cross-process state lock."""
    depth = getattr(_STATE_LOCK_LOCAL, "depth", 0)
    if depth:
        _STATE_LOCK_LOCAL.depth = depth + 1
        try:
            yield
        finally:
            _STATE_LOCK_LOCAL.depth -= 1
        return

    deadline = time.monotonic() + max(0.0, timeout)
    remaining = max(0.0, deadline - time.monotonic())
    if not _STATE_PROCESS_LOCK.acquire(timeout=remaining):
        raise TimeoutError(f"Could not acquire state lock: {LOCK_FILE}")

    try:
        remaining = max(0.0, deadline - time.monotonic())
        with advisory_file_lock(
            LOCK_FILE,
            timeout=remaining,
            poll=poll,
            description="state lock",
        ):
            _STATE_LOCK_LOCAL.depth = 1
            try:
                yield
            finally:
                _STATE_LOCK_LOCAL.depth = 0
    finally:
        _STATE_PROCESS_LOCK.release()


def update_state(mutator: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
    """Atomically read-modify-write state under a file lock.

    `mutator` receives the freshly-loaded state dict and mutates it
    in place. The updated dict is written back atomically. Returns the
    state that was written, so callers can inspect the post-merge result.
    """
    with _state_lock():
        state = _load_state_while_locked(degrade_on_read_error=False)
        mutator(state)
        save_state(state)
        return state


def read_state_snapshot_strict() -> tuple[bytes, dict[str, Any]]:
    """Read state without quarantine or other recovery side effects."""
    metadata = STATE_FILE.lstat()
    if (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or _is_reparse_point(metadata)
        or metadata.st_nlink != 1
        or metadata.st_size > MAX_STATE_JSON_CHARS
    ):
        raise ValueError("state.json is not a safe bounded regular file")
    with STATE_FILE.open("rb") as handle:
        opened = os.fstat(handle.fileno())
        if not os.path.samestat(metadata, opened):
            raise ValueError("state.json changed before it could be read")
        raw = handle.read(MAX_STATE_JSON_CHARS + 1)
        finished = os.fstat(handle.fileno())
    current = STATE_FILE.lstat()
    stable_fields = ("st_mode", "st_nlink", "st_size", "st_mtime_ns")
    if (
        len(raw) > MAX_STATE_JSON_CHARS
        or not os.path.samestat(opened, finished)
        or not os.path.samestat(finished, current)
        or any(getattr(opened, field) != getattr(finished, field) for field in stable_fields)
        or any(getattr(finished, field) != getattr(current, field) for field in stable_fields)
    ):
        raise ValueError("state.json changed or exceeded its bound while reading")
    state = decode_json_object_strict(raw, max_bytes=MAX_STATE_JSON_CHARS)
    return raw, state


def update_state_if_unchanged(
    expected_raw: bytes,
    mutator: Callable[[dict[str, Any]], None],
) -> tuple[bytes, dict[str, Any]]:
    """Atomically update state only when its byte-exact preimage still matches."""
    with _state_lock():
        current_raw, state = read_state_snapshot_strict()
        if not hmac.compare_digest(current_raw, expected_raw):
            raise ValueError("state.json drifted after recovery review")
        mutator(state)
        save_state(state)
        return read_state_snapshot_strict()


def file_hash(path: Path) -> str:
    if not path.exists():
        return ""
    h = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(FILE_HASH_CHUNK_BYTES):
            h.update(chunk)
    return h.hexdigest()


def _valid_receipt_snapshot(value: object) -> bool:
    if not isinstance(value, dict) or set(value) != {
        "identity",
        "sha256",
        "size",
        "mode",
        "file_attributes",
        "nlink",
    }:
        return False
    identity = value.get("identity")
    return (
        isinstance(identity, list)
        and len(identity) == 3
        and all(isinstance(item, int) and not isinstance(item, bool) for item in identity)
        and isinstance(value.get("sha256"), str)
        and re.fullmatch(r"[0-9a-f]{64}", value["sha256"]) is not None
        and isinstance(value.get("size"), int)
        and not isinstance(value.get("size"), bool)
        and 0 <= value["size"] <= MAX_COMPILE_RECEIPT_TARGET_BYTES
        and isinstance(value.get("mode"), int)
        and not isinstance(value.get("mode"), bool)
        and isinstance(value.get("file_attributes"), int)
        and not isinstance(value.get("file_attributes"), bool)
        and value.get("nlink") == 1
    )


def _file_identity_matches(expected: object, current: object) -> bool:
    if expected == current:
        return True
    if (
        os.name != "nt"
        or not isinstance(expected, list)
        or not isinstance(current, list)
        or len(expected) != 3
        or len(current) != 3
        or expected[1:] != current[1:]
    ):
        return False
    expected_device = expected[0]
    current_device = current[0]
    if any(
        not isinstance(device, int) or isinstance(device, bool) or device < 0
        for device in (expected_device, current_device)
    ):
        return False
    # Python 3.12 widened Windows st_dev; older receipts retain its low 32 bits.
    legacy_max = 0xFFFFFFFF
    expected_is_legacy = (
        expected_device <= legacy_max < current_device
        and current_device & legacy_max == expected_device
    )
    current_is_legacy = (
        current_device <= legacy_max < expected_device
        and expected_device & legacy_max == current_device
    )
    return expected_is_legacy or current_is_legacy


def _file_snapshot_matches_exact(
    expected: dict[str, Any],
    current: dict[str, Any],
) -> bool:
    return _file_identity_matches(expected.get("identity"), current.get("identity")) and all(
        expected.get(field) == current.get(field)
        for field in ("sha256", "size", "mode", "file_attributes", "nlink")
    )


def _read_receipt_regular_file(path: Path, max_bytes: int) -> tuple[bytes, dict[str, Any]]:
    target = Path(path)
    metadata = target.lstat()
    if (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or _is_reparse_point(metadata)
        or metadata.st_size > max_bytes
    ):
        raise OSError("compile receipt target is unsafe or oversized")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(
        os, "O_NOFOLLOW", 0
    )
    descriptor = os.open(target, flags)
    try:
        opened = os.fstat(descriptor)
        if not os.path.samestat(metadata, opened):
            raise OSError("compile receipt target changed while opening")
        chunks: list[bytes] = []
        remaining = max_bytes + 1
        while remaining:
            chunk = os.read(descriptor, min(FILE_HASH_CHUNK_BYTES, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        opened_after = os.fstat(descriptor)
        current = target.lstat()
    finally:
        os.close(descriptor)
    if (
        len(raw) > max_bytes
        or not os.path.samestat(opened, opened_after)
        or not os.path.samestat(opened_after, current)
        or opened.st_size != len(raw)
        or opened_after.st_size != len(raw)
        or current.st_size != len(raw)
        or getattr(opened, "st_mtime_ns", None)
        != getattr(opened_after, "st_mtime_ns", None)
        or getattr(opened_after, "st_mtime_ns", None)
        != getattr(current, "st_mtime_ns", None)
        or stat.S_IMODE(opened.st_mode) != stat.S_IMODE(opened_after.st_mode)
        or stat.S_IMODE(opened_after.st_mode) != stat.S_IMODE(current.st_mode)
        or getattr(opened, "st_file_attributes", 0)
        != getattr(opened_after, "st_file_attributes", 0)
        or getattr(opened_after, "st_file_attributes", 0)
        != getattr(current, "st_file_attributes", 0)
        or opened.st_nlink != 1
        or opened_after.st_nlink != 1
        or current.st_nlink != 1
    ):
        raise OSError("compile receipt target changed while reading")
    snapshot = {
        "identity": [opened.st_dev, opened.st_ino, stat.S_IFMT(opened.st_mode)],
        "sha256": hashlib.sha256(raw).hexdigest(),
        "size": len(raw),
        "mode": stat.S_IMODE(opened_after.st_mode),
        "file_attributes": getattr(opened_after, "st_file_attributes", 0),
        "nlink": 1,
    }
    return raw, snapshot


def _receipt_snapshot_matches(
    expected: dict[str, Any],
    raw: bytes,
    current: dict[str, Any],
) -> bool:
    if _file_snapshot_matches_exact(expected, current):
        return True
    prefix_size = expected["size"]
    return (
        _file_identity_matches(expected["identity"], current["identity"])
        and current["mode"] == expected["mode"]
        and current["file_attributes"] == expected["file_attributes"]
        and current["nlink"] == expected["nlink"]
        and current["size"] >= prefix_size
        and hashlib.sha256(raw[:prefix_size]).hexdigest() == expected["sha256"]
    )


def is_compile_receipt_valid(
    receipt: object,
    daily_name: str,
    daily_sha256: str,
    *,
    root: Path | None = None,
) -> bool:
    """Validate one self-contained compiled-hash receipt against live effects."""
    try:
        from session_start_project_state import _same_native_project_root

        if not isinstance(receipt, dict):
            return False
        receipt_version = receipt.get("version")
        receipt_fields = {
                "version",
                "daily_sha256",
                "generation_id",
                "journal_ids",
                "effects",
                "targets",
                "index",
            }
        if receipt_version == 2:
            receipt_fields.update({"consumed_evidence", "generation_lineage"})
        if (
            set(receipt) != receipt_fields
            or receipt_version not in {1, 2}
            or not isinstance(daily_name, str)
            or Path(daily_name).name != daily_name
            or not daily_name.endswith(".md")
            or re.fullmatch(r"[0-9a-f]{64}", daily_sha256 or "") is None
            or receipt.get("daily_sha256") != daily_sha256
            or re.fullmatch(r"[0-9a-f]{64}", receipt.get("generation_id", ""))
            is None
        ):
            return False
        encoded = json.dumps(
            receipt,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8", errors="strict")
        if len(encoded) > MAX_COMPILE_RECEIPT_BYTES:
            return False

        journal_ids = receipt["journal_ids"]
        effects = receipt["effects"]
        targets = receipt["targets"]
        index = receipt["index"]
        consumed_evidence = receipt.get("consumed_evidence", [])
        generation_lineage = receipt.get("generation_lineage", [])
        if (
            not isinstance(journal_ids, list)
            or len(journal_ids) > MAX_COMPILE_RECEIPT_JOURNALS
            or len(set(journal_ids)) != len(journal_ids)
            or any(
                not isinstance(item, str)
                or re.fullmatch(r"[0-9a-f]{64}", item) is None
                for item in journal_ids
            )
            or not isinstance(effects, list)
            or len(effects) > MAX_COMPILE_RECEIPT_EFFECTS
            or not isinstance(targets, list)
            or len(targets) > MAX_COMPILE_RECEIPT_TARGETS
            or not isinstance(index, dict)
            or set(index) != {"generation_id", "entries"}
            or index.get("generation_id") != receipt["generation_id"]
            or not isinstance(consumed_evidence, list)
            or len(consumed_evidence) > MAX_COMPILE_RECEIPT_EVIDENCE
            or any(
                not isinstance(token, str)
                or re.fullmatch(r"[0-9a-f]{64}", token) is None
                for token in consumed_evidence
            )
            or len(set(consumed_evidence)) != len(consumed_evidence)
            or not isinstance(generation_lineage, list)
            or len(generation_lineage) > MAX_COMPILE_GENERATION_LINEAGE
            or any(
                not isinstance(generation_id, str)
                or re.fullmatch(r"[0-9a-f]{64}", generation_id) is None
                for generation_id in generation_lineage
            )
            or len(set(generation_lineage)) != len(generation_lineage)
        ):
            return False

        target_records: dict[str, dict[str, Any]] = {}
        for item in targets:
            if not isinstance(item, dict) or set(item) != {"target", "current"}:
                return False
            target_name = item.get("target")
            if (
                not isinstance(target_name, str)
                or Path(target_name).name != target_name
                or not target_name.endswith(".md")
                or target_name in target_records
                or not _valid_receipt_snapshot(item.get("current"))
            ):
                return False
            target_records[target_name] = item["current"]

        effect_records: dict[str, list[dict[str, Any]]] = {}
        effect_ids: set[tuple[str, int]] = set()
        for effect in effects:
            base_effect_fields = {
                "journal_id",
                "operation_index",
                "target",
                "after",
                "marker",
                "fingerprint",
            }
            scoped_effect_fields = {
                *base_effect_fields,
                "project_slug",
                "project_root",
            }
            unscoped_legacy_effect_fields = {
                *base_effect_fields,
                "source_scope",
            }
            effect_fields = (
                frozenset(effect) if isinstance(effect, dict) else frozenset()
            )
            scoped = effect_fields == scoped_effect_fields
            unscoped_legacy = effect_fields == unscoped_legacy_effect_fields
            if (
                not isinstance(effect, dict)
                or effect_fields
                not in (
                    {frozenset(base_effect_fields)}
                    if receipt_version == 1
                    else {
                        frozenset(base_effect_fields),
                        frozenset(scoped_effect_fields),
                        frozenset(unscoped_legacy_effect_fields),
                    }
                )
            ):
                return False
            journal_id = effect.get("journal_id")
            operation_index = effect.get("operation_index")
            target_name = effect.get("target")
            marker = effect.get("marker")
            fingerprint = effect.get("fingerprint")
            effect_id = (journal_id, operation_index)
            if (
                journal_id not in journal_ids
                or not isinstance(operation_index, int)
                or isinstance(operation_index, bool)
                or operation_index < 0
                or effect_id in effect_ids
                or target_name not in target_records
                or not _valid_receipt_snapshot(effect.get("after"))
                or not isinstance(marker, str)
                or re.fullmatch(
                    r"<!-- llm-wiki-compile-op:[0-9a-f]{64} -->", marker
                )
                is None
                or not isinstance(fingerprint, str)
                or re.fullmatch(
                    r"<!-- llm-wiki-compile-content:[0-9a-f]{64} -->",
                    fingerprint,
                )
                is None
                or scoped
                and (
                    not isinstance(effect.get("project_slug"), str)
                    or not effect["project_slug"]
                    or not isinstance(effect.get("project_root"), str)
                    or not effect["project_root"]
                )
                or unscoped_legacy
                and effect.get("source_scope") != "unscoped"
            ):
                return False
            effect_ids.add(effect_id)
            effect_records.setdefault(target_name, []).append(effect)
        if set(effect_records) != set(target_records):
            return False

        entries = index.get("entries")
        expected_entries = sorted(
            f"knowledge/notes/{Path(name).stem}" for name in target_records
        )
        if (
            not isinstance(entries, list)
            or entries != expected_entries
            or len(set(entries)) != len(entries)
        ):
            return False

        vault_root = Path(root if root is not None else ROOT)
        notes_root = vault_root / "knowledge" / "notes"
        for target_name, expected_current in target_records.items():
            raw, current = _read_receipt_regular_file(
                notes_root / target_name,
                MAX_COMPILE_RECEIPT_TARGET_BYTES,
            )
            if not _receipt_snapshot_matches(expected_current, raw, current):
                return False
            content = raw.decode("utf-8", errors="strict")
            target_project = parse_project_scope(content)
            target_project_root = parse_frontmatter_scalar(content, "project_root")
            for effect in effect_records[target_name]:
                after = effect["after"]
                scoped = "project_slug" in effect
                unscoped_legacy = effect.get("source_scope") == "unscoped"
                if (
                    len(raw) < after["size"]
                    or hashlib.sha256(raw[: after["size"]]).hexdigest()
                    != after["sha256"]
                    or effect["marker"].encode("ascii") not in raw
                    or effect["fingerprint"].encode("ascii") not in raw
                    or scoped
                    and (
                        target_project.value != effect["project_slug"]
                        or not _same_native_project_root(
                            target_project_root.value,
                            effect["project_root"],
                        )
                    )
                    or not scoped
                    and not unscoped_legacy
                    and (target_project.present or target_project_root.present)
                ):
                    return False

        index_raw, _index_snapshot = _read_receipt_regular_file(
            vault_root / "knowledge" / "index.md",
            MAX_COMPILE_RECEIPT_INDEX_BYTES,
        )
        index_text = index_raw.decode("utf-8", errors="strict")
        live_entries = {
            match.group("target")
            for line in index_text.splitlines()
            if (
                match := re.match(
                    r"^[ \t]*-[ \t]+\[\[(?P<target>[^\]|\r\n]+)"
                    r"(?:\|[^\]\r\n]*)?\]\](?=[ \t]|$)",
                    line,
                )
            )
        }
        return all(entry in live_entries for entry in entries)
    except (
        KeyError,
        MemoryError,
        OSError,
        OverflowError,
        RecursionError,
        TypeError,
        UnicodeError,
        ValueError,
    ):
        return False


def trusted_compiled_daily_hashes(
    state: object,
    *,
    root: Path | None = None,
) -> dict[str, str]:
    """Return only hashes whose bounded receipts still match live effects."""
    if not isinstance(state, dict):
        return {}
    hashes = state.get("compiled_daily_hashes")
    receipts = state.get("compiled_daily_receipts")
    if (
        not isinstance(hashes, dict)
        or not isinstance(receipts, dict)
        or len(hashes) > MAX_COMPILE_RECEIPTS
        or len(receipts) > MAX_COMPILE_RECEIPTS
    ):
        return {}
    trusted: dict[str, str] = {}
    for daily_name, digest in hashes.items():
        if (
            isinstance(daily_name, str)
            and isinstance(digest, str)
            and is_compile_receipt_valid(
                receipts.get(daily_name),
                daily_name,
                digest,
                root=root,
            )
        ):
            trusted[daily_name] = digest
    return trusted


def _sync_parent_directory(path: Path) -> None:
    """Persist a replaced directory entry on POSIX when the FS supports it."""
    if os.name != "posix":
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        fd = os.open(str(path.parent), flags)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def sync_file_strict(path: Path) -> None:
    """Flush one regular file and propagate every durability error."""
    target = Path(path)
    bound = getattr(_BOUND_ATOMIC_DIRECTORY_LOCAL, "current", None)
    bound_matches = (
        bound is not None
        and Path(os.path.abspath(target.parent)) == bound.path
    )
    if bound_matches:
        bound.validate_path()
    flags = os.O_WRONLY | getattr(os, "O_BINARY", 0) | getattr(
        os, "O_NOFOLLOW", 0
    )
    descriptor = (
        os.open(target.name, flags, dir_fd=bound.descriptor)
        if bound_matches and bound.descriptor is not None
        else os.open(target, flags)
    )
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or _is_reparse_point(metadata):
            raise OSError("strict sync target is not a regular file")
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    if bound_matches:
        bound.validate_path()


def sync_parent_directory_strict(path: Path) -> None:
    """Flush a POSIX parent directory and propagate open/fsync errors."""
    if os.name != "posix":
        return
    target = Path(path)
    bound = getattr(_BOUND_ATOMIC_DIRECTORY_LOCAL, "current", None)
    if (
        bound is not None
        and bound.descriptor is not None
        and Path(os.path.abspath(target.parent)) == bound.path
    ):
        bound.validate_path()
        metadata = os.fstat(bound.descriptor)
        if not stat.S_ISDIR(metadata.st_mode):
            raise OSError("strict sync parent is not a directory")
        os.fsync(bound.descriptor)
        bound.validate_path()
        return
    flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor = os.open(target.parent, flags)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISDIR(metadata.st_mode):
            raise OSError("strict sync parent is not a directory")
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _sync_conditional_parent(path: Path, dir_fd: int | None) -> None:
    if dir_fd is None:
        _sync_parent_directory(path)
    else:
        os.fsync(dir_fd)


def _snapshot_regular_file(
    path: Path,
    *,
    dir_fd: int | None,
) -> tuple[tuple[int, int, int], str, int, int, int]:
    if dir_fd is None:
        metadata = path.lstat()
    else:
        metadata = os.stat(path.name, dir_fd=dir_fd, follow_symlinks=False)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or _is_reparse_point(metadata)
    ):
        raise OSError("conditional replacement is not a regular file")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(
        os, "O_NOFOLLOW", 0
    )
    if dir_fd is None:
        descriptor = os.open(path, flags)
    else:
        descriptor = os.open(path.name, flags, dir_fd=dir_fd)
    try:
        opened = os.fstat(descriptor)
        if not os.path.samestat(metadata, opened):
            raise OSError("conditional replacement changed while opening")
        digest = hashlib.sha256()
        size = 0
        while chunk := os.read(descriptor, FILE_HASH_CHUNK_BYTES):
            digest.update(chunk)
            size += len(chunk)
        opened_after = os.fstat(descriptor)
        if dir_fd is None:
            current = path.lstat()
        else:
            current = os.stat(path.name, dir_fd=dir_fd, follow_symlinks=False)
    finally:
        os.close(descriptor)
    if (
        not os.path.samestat(opened, opened_after)
        or not os.path.samestat(opened_after, current)
        or opened.st_size != opened_after.st_size
        or opened_after.st_size != size
        or getattr(opened, "st_mtime_ns", None)
        != getattr(opened_after, "st_mtime_ns", None)
        or stat.S_IMODE(opened.st_mode) != stat.S_IMODE(opened_after.st_mode)
        or stat.S_IMODE(opened_after.st_mode) != stat.S_IMODE(current.st_mode)
        or getattr(opened, "st_file_attributes", 0)
        != getattr(opened_after, "st_file_attributes", 0)
        or getattr(opened_after, "st_file_attributes", 0)
        != getattr(current, "st_file_attributes", 0)
        or opened.st_nlink != 1
        or opened_after.st_nlink != 1
        or os.name == "posix"
        and current.st_nlink != 1
    ):
        raise OSError("conditional replacement changed while hashing")
    return (
        (opened.st_dev, opened.st_ino, stat.S_IFMT(opened.st_mode)),
        digest.hexdigest(),
        size,
        stat.S_IMODE(opened_after.st_mode),
        getattr(opened_after, "st_file_attributes", 0),
    )


def _snapshot_record(
    snapshot: tuple[tuple[int, int, int], str, int, int, int],
) -> dict[str, Any]:
    identity, digest, size, mode, file_attributes = snapshot
    return {
        "identity": list(identity),
        "sha256": digest,
        "size": size,
        "mode": mode,
        "file_attributes": file_attributes,
        "nlink": 1,
    }


def _assert_expected_base_file(
    path: Path,
    expected: tuple[tuple[int, int, int], str, int, int, int],
    *,
    dir_fd: int | None,
) -> None:
    """Validate the exact regular file displaced by a native exchange."""
    (
        expected_identity,
        expected_digest,
        expected_size,
        expected_mode,
        expected_attributes,
    ) = expected
    try:
        if dir_fd is not None:
            metadata = os.stat(path.name, dir_fd=dir_fd, follow_symlinks=False)
            flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(
                os, "O_NOFOLLOW", 0
            )
            descriptor = os.open(path.name, flags, dir_fd=dir_fd)
            try:
                opened = os.fstat(descriptor)
                if (
                    not os.path.samestat(metadata, opened)
                    or not stat.S_ISREG(opened.st_mode)
                    or _is_reparse_point(opened)
                    or (
                        opened.st_dev,
                        opened.st_ino,
                        stat.S_IFMT(opened.st_mode),
                    )
                    != expected_identity
                    or opened.st_size != expected_size
                    or stat.S_IMODE(opened.st_mode) != expected_mode
                    or getattr(opened, "st_file_attributes", 0)
                    != expected_attributes
                    or opened.st_nlink != 1
                ):
                    raise OSError("displaced target does not match admitted base")
                digest = hashlib.sha256()
                total = 0
                while total <= expected_size:
                    chunk = os.read(
                        descriptor,
                        min(FILE_HASH_CHUNK_BYTES, expected_size + 1 - total),
                    )
                    if not chunk:
                        break
                    digest.update(chunk)
                    total += len(chunk)
                opened_after = os.fstat(descriptor)
                current = os.stat(
                    path.name,
                    dir_fd=dir_fd,
                    follow_symlinks=False,
                )
            finally:
                os.close(descriptor)
        else:
            metadata = path.lstat()
            if (
                not stat.S_ISREG(metadata.st_mode)
                or stat.S_ISLNK(metadata.st_mode)
                or _is_reparse_point(metadata)
                or (
                    metadata.st_dev,
                    metadata.st_ino,
                    stat.S_IFMT(metadata.st_mode),
                )
                != expected_identity
                or metadata.st_size != expected_size
                or stat.S_IMODE(metadata.st_mode) != expected_mode
                or getattr(metadata, "st_file_attributes", 0)
                != expected_attributes
            ):
                raise OSError("displaced target does not match admitted base")
            with path.open("rb") as handle:
                opened = os.fstat(handle.fileno())
                if not os.path.samestat(metadata, opened):
                    raise OSError("displaced target does not match admitted base")
                digest = hashlib.sha256()
                total = 0
                while total <= expected_size:
                    chunk = handle.read(
                        min(FILE_HASH_CHUNK_BYTES, expected_size + 1 - total)
                    )
                    if not chunk:
                        break
                    digest.update(chunk)
                    total += len(chunk)
                opened_after = os.fstat(handle.fileno())
                current = path.lstat()
        if (
            total != expected_size
            or digest.hexdigest() != expected_digest
            or not os.path.samestat(opened, opened_after)
            or opened.st_size != opened_after.st_size
            or getattr(opened, "st_mtime_ns", None)
            != getattr(opened_after, "st_mtime_ns", None)
            or stat.S_IMODE(opened_after.st_mode) != expected_mode
            or getattr(opened_after, "st_file_attributes", 0)
            != expected_attributes
            or opened_after.st_nlink != 1
            or not stat.S_ISREG(current.st_mode)
            or stat.S_ISLNK(current.st_mode)
            or _is_reparse_point(current)
            or (
                current.st_dev,
                current.st_ino,
                stat.S_IFMT(current.st_mode),
            )
            != expected_identity
            or stat.S_IMODE(current.st_mode) != expected_mode
            or getattr(current, "st_file_attributes", 0)
            != expected_attributes
            or os.name == "posix"
            and current.st_nlink != 1
        ):
            raise OSError("displaced target does not match admitted base")
    except FileNotFoundError as exc:
        raise OSError("displaced target does not match admitted base") from exc


def _windows_extended_path(path: Path) -> str:
    path_text = os.path.abspath(path)
    if path_text.startswith("\\\\?\\"):
        return path_text
    if path_text.startswith("\\\\"):
        return "\\\\?\\UNC\\" + path_text[2:]
    return "\\\\?\\" + path_text


def _replace_file_windows(replaced: Path, replacing: Path, backup: Path) -> None:
    import ctypes
    from ctypes import wintypes

    kernel32 = _load_windows_kernel32()
    replace_file = kernel32.ReplaceFileW
    replace_file.argtypes = (
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.c_void_p,
        ctypes.c_void_p,
    )
    replace_file.restype = wintypes.BOOL
    ctypes.set_last_error(0)
    if not replace_file(
        _windows_extended_path(replaced),
        _windows_extended_path(replacing),
        _windows_extended_path(backup),
        0,
        None,
        None,
    ):
        error = ctypes.get_last_error()
        raise OSError(error, f"ReplaceFileW failed with Windows error {error}")


def _rename_exchange_posix(dir_fd: int, left: str, right: str) -> None:
    import ctypes

    libc = ctypes.CDLL(None, use_errno=True)
    if sys.platform == "darwin":
        rename_exchange = getattr(libc, "renameatx_np", None)
        exchange_flag = 0x00000002  # RENAME_SWAP
    else:
        rename_exchange = getattr(libc, "renameat2", None)
        exchange_flag = 0x00000002  # RENAME_EXCHANGE
    if rename_exchange is None:
        raise OSError(errno.ENOTSUP, "atomic rename exchange is unavailable")
    rename_exchange.argtypes = (
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    )
    rename_exchange.restype = ctypes.c_int
    if rename_exchange(
        dir_fd,
        os.fsencode(left),
        dir_fd,
        os.fsencode(right),
        exchange_flag,
    ) != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))


def _rename_noreplace_posix(
    dir_fd: int,
    source: str,
    destination: str,
    *,
    destination_dir_fd: int | None = None,
) -> None:
    """Rename one bound child without ever replacing the destination."""
    import ctypes

    libc = ctypes.CDLL(None, use_errno=True)
    if sys.platform == "darwin":
        rename_noreplace = getattr(libc, "renameatx_np", None)
        noreplace_flag = 0x00000004  # RENAME_EXCL
    else:
        rename_noreplace = getattr(libc, "renameat2", None)
        noreplace_flag = 0x00000001  # RENAME_NOREPLACE
    if rename_noreplace is None:
        raise OSError(errno.ENOTSUP, "atomic no-replace rename is unavailable")
    rename_noreplace.argtypes = (
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    )
    rename_noreplace.restype = ctypes.c_int
    target_dir_fd = dir_fd if destination_dir_fd is None else destination_dir_fd
    if rename_noreplace(
        dir_fd,
        os.fsencode(source),
        target_dir_fd,
        os.fsencode(destination),
        noreplace_flag,
    ) != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))


def _exchange_expected_base_files(
    target: Path,
    replacement: Path,
    backup: Path,
    *,
    dir_fd: int | None,
) -> Path:
    """Perform one native exchange and return the displaced file's path."""
    if sys.platform == "win32":
        _replace_file_windows(target, replacement, backup)
        return backup
    if os.name == "posix" and dir_fd is not None:
        _rename_exchange_posix(dir_fd, target.name, replacement.name)
        return replacement
    raise OSError(errno.ENOTSUP, "expected-base conditional publication is unsupported")


def _conditional_token_from_path(path: Path) -> str:
    _prefix, separator, suffix = path.name.rpartition(".")
    if not separator or suffix not in {
        "replacement",
        "displaced",
        "rejected",
        "cleanup",
    }:
        raise ValueError("conditional retirement path is invalid")
    token = _prefix.rpartition(".")[2]
    if re.fullmatch(r"[0-9a-f]{32}", token) is None:
        raise ValueError("conditional retirement token is invalid")
    return token


def _resolved_tombstone_bytes(path: Path) -> bytes:
    token = _conditional_token_from_path(path)
    return f"llm-wiki-resolved:{token}\n".encode("ascii")


def _is_resolved_conditional_tombstone(
    path: Path,
    snapshot: tuple[tuple[int, int, int], str, int, int, int] | None,
) -> bool:
    if snapshot is None:
        return False
    expected = _resolved_tombstone_bytes(path)
    return (
        snapshot[1] == hashlib.sha256(expected).hexdigest()
        and snapshot[2] == len(expected)
    )


def _open_windows_delete_descriptor(path: Path) -> int:
    import ctypes
    import msvcrt
    from ctypes import wintypes

    kernel32 = _load_windows_kernel32()
    create_file = kernel32.CreateFileW
    create_file.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    create_file.restype = wintypes.HANDLE
    handle = create_file(
        _windows_extended_path(path),
        0x80000000 | 0x40000000 | 0x00010000,  # GENERIC_READ|WRITE|DELETE
        0x00000001 | 0x00000002 | 0x00000004,
        None,
        3,  # OPEN_EXISTING
        0x00200000,  # FILE_FLAG_OPEN_REPARSE_POINT
        None,
    )
    invalid = ctypes.c_void_p(-1).value
    if handle in {None, invalid}:
        error = ctypes.get_last_error()
        raise OSError(error, f"CreateFileW failed with Windows error {error}")
    try:
        return msvcrt.open_osfhandle(
            int(handle),
            os.O_RDWR | getattr(os, "O_BINARY", 0),
        )
    except BaseException:
        kernel32.CloseHandle(handle)
        raise


def _retire_open_descriptor(
    path: Path,
    descriptor: int,
    replacement: bytes | None,
) -> None:
    if sys.platform == "win32":
        import ctypes
        import msvcrt
        from ctypes import wintypes

        class FileDispositionInfoEx(ctypes.Structure):
            _fields_ = [("Flags", wintypes.ULONG)]

        set_information = _load_windows_kernel32().SetFileInformationByHandle
        set_information.argtypes = (
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
        )
        set_information.restype = wintypes.BOOL
        disposition = FileDispositionInfoEx(
            0x00000001 | 0x00000002  # DELETE | POSIX_SEMANTICS
        )
        ctypes.set_last_error(0)
        if not set_information(
            msvcrt.get_osfhandle(descriptor),
            21,  # FileDispositionInfoEx
            ctypes.byref(disposition),
            ctypes.sizeof(disposition),
        ):
            error = ctypes.get_last_error()
            raise OSError(
                error,
                f"POSIX handle deletion failed with Windows error {error}",
            )
        return
    if os.name == "posix":
        return
    if replacement is not None:
        raise OSError(errno.ENOTSUP, "handle-safe retirement is unavailable")
    raise OSError(errno.ENOTSUP, "handle-safe retirement is unavailable")


def _retained_conditional_artifact_record(
    path: Path,
    dir_fd: int | None,
    expected: tuple[tuple[int, int, int], str, int, int, int],
) -> dict[str, Any]:
    identity, expected_digest, expected_size, expected_mode, expected_attributes = (
        expected
    )
    metadata = (
        path.lstat()
        if dir_fd is None
        else os.stat(path.name, dir_fd=dir_fd, follow_symlinks=False)
    )
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(
        os, "O_NOFOLLOW", 0
    )
    descriptor = (
        os.open(path, flags)
        if dir_fd is None
        else os.open(path.name, flags, dir_fd=dir_fd)
    )
    try:
        opened = os.fstat(descriptor)
        digest = hashlib.sha256()
        size = 0
        while size <= expected_size:
            chunk = os.read(
                descriptor,
                min(FILE_HASH_CHUNK_BYTES, expected_size + 1 - size),
            )
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
        opened_after = os.fstat(descriptor)
        current = (
            path.lstat()
            if dir_fd is None
            else os.stat(path.name, dir_fd=dir_fd, follow_symlinks=False)
        )
    finally:
        os.close(descriptor)
    current_identity = (
        current.st_dev,
        current.st_ino,
        stat.S_IFMT(current.st_mode),
    )
    if (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or _is_reparse_point(metadata)
        or not os.path.samestat(metadata, opened)
        or not os.path.samestat(opened, opened_after)
        or not os.path.samestat(opened_after, current)
        or current_identity != identity
        or size != expected_size
        or digest.hexdigest() != expected_digest
        or opened.st_size != expected_size
        or opened_after.st_size != expected_size
        or current.st_size != expected_size
        or getattr(opened, "st_mtime_ns", None)
        != getattr(opened_after, "st_mtime_ns", None)
        or getattr(opened_after, "st_mtime_ns", None)
        != getattr(current, "st_mtime_ns", None)
        or stat.S_IMODE(opened.st_mode) != expected_mode
        or stat.S_IMODE(opened_after.st_mode) != expected_mode
        or stat.S_IMODE(current.st_mode) != expected_mode
        or getattr(opened, "st_file_attributes", 0) != expected_attributes
        or getattr(opened_after, "st_file_attributes", 0) != expected_attributes
        or getattr(current, "st_file_attributes", 0) != expected_attributes
        or min(opened.st_nlink, opened_after.st_nlink, current.st_nlink) < 1
    ):
        raise OSError("retained conditional artifact changed")
    return {
        "path": path.name,
        "snapshot": {
            "identity": list(identity),
            "sha256": expected_digest,
            "size": expected_size,
            "mode": expected_mode,
            "file_attributes": expected_attributes,
            "nlink": current.st_nlink,
        },
    }


def _validate_retained_conditional_artifact_record(
    record: object,
    path: Path,
    dir_fd: int | None,
    expected: tuple[tuple[int, int, int], str, int, int, int],
) -> dict[str, Any]:
    if not isinstance(record, dict) or set(record) != {"path", "snapshot"}:
        raise ValueError("retained conditional artifact record is invalid")
    snapshot = record.get("snapshot")
    if (
        record.get("path") != path.name
        or not isinstance(snapshot, dict)
        or set(snapshot)
        != {"identity", "sha256", "size", "mode", "file_attributes", "nlink"}
        or snapshot.get("identity") != list(expected[0])
        or snapshot.get("sha256") != expected[1]
        or snapshot.get("size") != expected[2]
        or snapshot.get("mode") != expected[3]
        or snapshot.get("file_attributes") != expected[4]
        or isinstance(snapshot.get("nlink"), bool)
        or not isinstance(snapshot.get("nlink"), int)
        or snapshot["nlink"] < 1
    ):
        raise ValueError("retained conditional artifact snapshot is invalid")
    actual = _retained_conditional_artifact_record(path, dir_fd, expected)
    actual_snapshot = actual["snapshot"]
    for field in ("identity", "sha256", "size", "mode", "file_attributes"):
        if actual_snapshot[field] != snapshot[field]:
            raise OSError("retained conditional artifact no longer matches its record")
    return record


def _unlink_owned_bound_file(
    path: Path,
    dir_fd: int | None,
    expected: tuple[tuple[int, int, int], str, int, int, int],
) -> bool:
    identity, expected_digest, expected_size, expected_mode, expected_attributes = (
        expected
    )
    replacement = None
    flags = (
        (os.O_RDWR if sys.platform == "win32" else os.O_RDONLY)
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        if sys.platform == "win32":
            descriptor = _open_windows_delete_descriptor(path)
        elif dir_fd is None:
            descriptor = os.open(path, flags)
        else:
            descriptor = os.open(path.name, flags, dir_fd=dir_fd)
    except FileNotFoundError:
        return False
    try:
        metadata = os.fstat(descriptor)
        current_identity = (
            metadata.st_dev,
            metadata.st_ino,
            stat.S_IFMT(metadata.st_mode),
        )
        if (
            current_identity != identity
            or not stat.S_ISREG(metadata.st_mode)
            or _is_reparse_point(metadata)
            or metadata.st_size != expected_size
            or stat.S_IMODE(metadata.st_mode) != expected_mode
            or getattr(metadata, "st_file_attributes", 0) != expected_attributes
            or metadata.st_nlink != 1
        ):
            return False
        digest = hashlib.sha256()
        size = 0
        os.lseek(descriptor, 0, os.SEEK_SET)
        while chunk := os.read(descriptor, FILE_HASH_CHUNK_BYTES):
            digest.update(chunk)
            size += len(chunk)
        checked = os.fstat(descriptor)
        if (
            not os.path.samestat(metadata, checked)
            or checked.st_size != expected_size
            or size != expected_size
            or digest.hexdigest() != expected_digest
            or getattr(metadata, "st_mtime_ns", None)
            != getattr(checked, "st_mtime_ns", None)
            or stat.S_IMODE(checked.st_mode) != expected_mode
            or getattr(checked, "st_file_attributes", 0) != expected_attributes
            or checked.st_nlink != 1
        ):
            return False
        os.lseek(descriptor, 0, os.SEEK_SET)
        _retire_open_descriptor(path, descriptor, replacement)
        retired = os.fstat(descriptor)
        if (
            not os.path.samestat(metadata, retired)
            or not stat.S_ISREG(retired.st_mode)
            or _is_reparse_point(retired)
            or sys.platform == "win32"
            and retired.st_nlink != 0
            or sys.platform != "win32"
            and (retired.st_nlink < 1 or retired.st_size != expected_size)
        ):
            raise OSError("conditional retirement descriptor changed")
    finally:
        os.close(descriptor)

    try:
        current = (
            path.lstat()
            if dir_fd is None
            else os.stat(path.name, dir_fd=dir_fd, follow_symlinks=False)
        )
    except FileNotFoundError:
        return sys.platform == "win32"
    if sys.platform == "win32":
        return False
    current_identity = (
        current.st_dev,
        current.st_ino,
        stat.S_IFMT(current.st_mode),
    )
    if current_identity != identity or current.st_size != expected_size:
        return False
    try:
        _retained_conditional_artifact_record(path, dir_fd, expected)
    except OSError:
        return False
    return True


def _bound_path_exists(path: Path, dir_fd: int | None) -> bool:
    try:
        if dir_fd is None:
            path.lstat()
        else:
            os.stat(path.name, dir_fd=dir_fd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    return True


def _conditional_artifact_inventory(
    path: Path,
    dir_fd: int | None,
    excluded_names: frozenset[str] = frozenset(),
) -> tuple[int, int, int, int]:
    prefix = f".{path.name}."
    location: Path | int = path.parent if dir_fd is None else dir_fd
    target_count = 0
    target_bytes = 0
    global_count = 0
    global_bytes = 0
    with os.scandir(location) as entries:
        for entry in entries:
            name = entry.name
            if name in excluded_names:
                continue
            if re.fullmatch(
                r"\..+\.[0-9a-f]{32}\.(?:replacement|displaced|rejected|cleanup)",
                name,
            ) is None:
                continue
            metadata = entry.stat(follow_symlinks=False)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or stat.S_ISLNK(metadata.st_mode)
                or _is_reparse_point(metadata)
            ):
                raise OSError("conditional artifact inventory is unsafe")
            global_count += 1
            global_bytes += metadata.st_size
            if name.startswith(prefix):
                target_count += 1
                target_bytes += metadata.st_size
    return target_count, target_bytes, global_count, global_bytes


def _require_retained_conditional_artifact_capacity(
    path: Path,
    dir_fd: int | None,
    prospective_bytes: int,
    *,
    excluded_names: frozenset[str] = frozenset(),
) -> None:
    target_count, target_bytes, global_count, global_bytes = (
        _conditional_artifact_inventory(path, dir_fd, excluded_names)
    )
    limits = (
        (
            target_count + 1,
            MAX_RETAINED_CONDITIONAL_ARTIFACTS_PER_TARGET,
            "per-target retained artifact count limit reached",
        ),
        (
            target_bytes + prospective_bytes,
            MAX_RETAINED_CONDITIONAL_ARTIFACT_BYTES_PER_TARGET,
            "per-target retained artifact byte limit reached",
        ),
        (
            global_count + 1,
            MAX_RETAINED_CONDITIONAL_ARTIFACTS_GLOBAL,
            "global retained artifact count limit reached",
        ),
        (
            global_bytes + prospective_bytes,
            MAX_RETAINED_CONDITIONAL_ARTIFACT_BYTES_GLOBAL,
            "global retained artifact byte limit reached",
        ),
    )
    for usage, limit, message in limits:
        if usage > limit:
            raise OSError(errno.ENOSPC, f"conditional {message}")


def _conditional_atomic_write_bound(
    bound: _BoundAtomicDirectory,
    path: Path,
    content: str,
    encoding: str,
    expected: tuple[tuple[int, int, int], str, int, int, int],
) -> None:
    dir_fd = bound.descriptor
    if sys.platform != "win32" and dir_fd is None:
        raise OSError(
            errno.ENOTSUP,
            "expected-base conditional publication requires a native exchange",
        )

    try:
        if dir_fd is None:
            target_metadata = path.lstat()
        else:
            target_metadata = os.stat(
                path.name,
                dir_fd=dir_fd,
                follow_symlinks=False,
            )
    except FileNotFoundError as exc:
        raise AtomicWriteConflictError("conditional update target is missing") from exc
    if (
        not stat.S_ISREG(target_metadata.st_mode)
        or stat.S_ISLNK(target_metadata.st_mode)
        or _is_reparse_point(target_metadata)
    ):
        raise AtomicWriteConflictError("conditional update target is not a regular file")

    _require_retained_conditional_artifact_capacity(path, dir_fd, expected[2])
    token = uuid.uuid4().hex
    replacement = path.with_name(f".{path.name}.{token}.replacement")
    backup = path.with_name(f".{path.name}.{token}.displaced")
    rejected = path.with_name(f".{path.name}.{token}.rejected")
    for candidate in (replacement, backup, rejected):
        if _bound_path_exists(candidate, dir_fd):
            raise FileExistsError(
                errno.EEXIST,
                "conditional recovery path already exists",
                str(candidate),
            )
    flags = os.O_WRONLY | getattr(os, "O_BINARY", 0) | getattr(
        os, "O_NOFOLLOW", 0
    ) | os.O_CREAT | os.O_EXCL
    descriptor: int | None = None
    replacement_owned = False
    replacement_expected: tuple[tuple[int, int, int], str, int, int, int] | None = None
    exchange_completed = False
    preserve_recovery = False

    def owned_recovery_paths() -> list[Path]:
        owned: list[Path] = []
        if replacement_owned and _bound_path_exists(replacement, dir_fd):
            owned.append(replacement)
        for candidate in (backup, rejected):
            if _bound_path_exists(candidate, dir_fd):
                owned.append(candidate)
        return owned

    def unresolved_recovery_state() -> dict[str, Any]:
        return {
            "version": 1,
            "kind": "unresolved",
            "status": "required",
            "target": path.name,
            "token": token,
            "owned_paths": [item.name for item in owned_recovery_paths()],
        }

    try:
        if dir_fd is None:
            descriptor = os.open(replacement, flags, 0o666)
        else:
            descriptor = os.open(
                replacement.name,
                flags,
                0o666,
                dir_fd=dir_fd,
            )
        replacement_owned = True
        if os.name == "posix":
            os.fchmod(descriptor, stat.S_IMODE(target_metadata.st_mode))
        elif dir_fd is None:
            os.chmod(replacement, stat.S_IMODE(target_metadata.st_mode))
        os.ftruncate(descriptor, 0)
        handle = os.fdopen(descriptor, "w", encoding=encoding, newline="")
        descriptor = None
        with handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        replacement_expected = _snapshot_regular_file(
            replacement,
            dir_fd=dir_fd,
        )
        published_expected = (
            replacement_expected[:3] + expected[3:]
            if sys.platform == "win32"
            else replacement_expected
        )

        bound.validate_path()
        _assert_expected_base_file(
            replacement,
            replacement_expected,
            dir_fd=dir_fd,
        )
        _require_retained_conditional_artifact_capacity(
            path,
            dir_fd,
            expected[2],
            excluded_names=frozenset({replacement.name}),
        )
        try:
            displaced = _exchange_expected_base_files(
                path,
                replacement,
                backup,
                dir_fd=dir_fd,
            )
            exchange_completed = True
        except OSError as exc:
            preserve_recovery = True
            _sync_conditional_parent(path, dir_fd)
            error = getattr(exc, "winerror", None) or exc.errno
            detail = (
                f"documented ReplaceFileW partial state {error}"
                if error in {1175, 1176, 1177}
                else f"native exchange failed: {exc}"
            )
            raise AtomicWriteRecoveryError(
                detail,
                owned_recovery_paths(),
                unresolved_recovery_state(),
            ) from exc

        try:
            _assert_expected_base_file(displaced, expected, dir_fd=dir_fd)
            _assert_expected_base_file(path, published_expected, dir_fd=dir_fd)
        except OSError as conflict:
            try:
                attempted_before_rollback = _snapshot_regular_file(
                    path,
                    dir_fd=dir_fd,
                )
                rejected_replacement = _exchange_expected_base_files(
                    path,
                    displaced,
                    rejected,
                    dir_fd=dir_fd,
                )
            except OSError as rollback_error:
                preserve_recovery = True
                _sync_conditional_parent(path, dir_fd)
                try:
                    restore_snapshot = _snapshot_regular_file(
                        displaced,
                        dir_fd=dir_fd,
                    )
                except OSError:
                    restore_snapshot = None
                recovery_state = {
                    "version": 1,
                    "kind": "rollback",
                    "status": "required",
                    "target": path.name,
                    "token": token,
                    "displaced_path": displaced.name,
                    "rollback_backup_path": rejected.name,
                    "attempted": _snapshot_record(attempted_before_rollback),
                    "restore": (
                        _snapshot_record(restore_snapshot)
                        if restore_snapshot is not None
                        else None
                    ),
                    "attempted_artifact_path": None,
                    "owned_paths": [
                        item.name for item in owned_recovery_paths()
                    ],
                }
                raise AtomicWriteRollbackError(
                    f"conditional publication rollback failed: {rollback_error}",
                    owned_recovery_paths(),
                    recovery_state,
                ) from rollback_error
            _sync_conditional_parent(path, dir_fd)
            try:
                _assert_expected_base_file(
                    rejected_replacement,
                    attempted_before_rollback,
                    dir_fd=dir_fd,
                )
            except OSError as intervening_error:
                preserve_recovery = True
                raise AtomicWriteRecoveryError(
                    "conditional publication restored the displaced target and "
                    "preserved intervening writer bytes",
                    owned_recovery_paths(),
                    unresolved_recovery_state(),
                ) from intervening_error
            try:
                if not _unlink_owned_bound_file(
                    rejected_replacement,
                    dir_fd,
                    attempted_before_rollback,
                ):
                    raise OSError(
                        "rejected conditional identity changed during retirement"
                    )
            except OSError as cleanup_error:
                preserve_recovery = True
                raise AtomicWriteRecoveryError(
                    f"conditional publication restored target but retained rejected file: "
                    f"{cleanup_error}",
                    owned_recovery_paths(),
                    unresolved_recovery_state(),
                ) from cleanup_error
            _sync_conditional_parent(path, dir_fd)
            raise AtomicWriteConflictError(
                "conditional update target changed since admission"
            ) from conflict

        try:
            if not _unlink_owned_bound_file(displaced, dir_fd, expected):
                raise OSError(
                    "displaced conditional identity changed during retirement"
                )
        except OSError as cleanup_error:
            preserve_recovery = True
            raise AtomicWriteRecoveryError(
                f"conditional publication retained displaced base: {cleanup_error}",
                owned_recovery_paths(),
                unresolved_recovery_state(),
            ) from cleanup_error
        _sync_conditional_parent(path, dir_fd)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if (
            replacement_owned
            and replacement_expected is not None
            and not preserve_recovery
            and not exchange_completed
        ):
            try:
                if _unlink_owned_bound_file(
                    replacement,
                    dir_fd,
                    replacement_expected,
                ):
                    _sync_conditional_parent(path, dir_fd)
            except FileNotFoundError:
                pass


def prepare_conditional_atomic_write(
    path: Path,
    content: str,
    expected: dict[str, Any],
    operation_fingerprint: str,
    encoding: str = "utf-8",
) -> dict[str, Any]:
    """Reserve every name needed by a future journaled exchange."""
    target = Path(path)
    validated = _validated_expected_atomic_target(expected)
    bound = getattr(_BOUND_ATOMIC_DIRECTORY_LOCAL, "current", None)
    if bound is None or Path(os.path.abspath(target.parent)) != bound.path:
        raise OSError(
            errno.ENOTSUP,
            "expected-base conditional publication requires a bound directory",
        )
    bound.validate_path()
    if not isinstance(operation_fingerprint, str) or not operation_fingerprint:
        raise ValueError("conditional operation fingerprint is required")
    if len(operation_fingerprint) > 1024:
        raise ValueError("conditional operation fingerprint is too long")
    current = _snapshot_regular_file(target, dir_fd=bound.descriptor)
    if current != validated:
        raise AtomicWriteConflictError("conditional update target changed before prepare")
    _require_retained_conditional_artifact_capacity(
        target,
        bound.descriptor,
        validated[2],
    )
    token = uuid.uuid4().hex
    replacement = target.with_name(f".{target.name}.{token}.replacement")
    displaced = target.with_name(f".{target.name}.{token}.displaced")
    rollback_backup = target.with_name(f".{target.name}.{token}.rejected")
    cleanup_restore = target.with_name(f".{target.name}.{token}.cleanup")
    for candidate in (replacement, displaced, rollback_backup, cleanup_restore):
        if _bound_path_exists(candidate, bound.descriptor):
            raise FileExistsError(
                errno.EEXIST,
                "conditional recovery path already exists",
                str(candidate),
            )
    try:
        encoded_content = content.encode(encoding)
    except (LookupError, UnicodeError) as exc:
        raise ValueError("conditional replacement encoding is invalid") from exc

    exchange_displaced = displaced if sys.platform == "win32" else replacement
    cleanup_placeholder = replacement if sys.platform == "win32" else displaced
    cleanup_displaced = rollback_backup if sys.platform == "win32" else displaced
    cleanup_restored = cleanup_restore if sys.platform == "win32" else displaced
    cleanup_content = f"llm-wiki-cleanup:{token}\n"
    encoded_cleanup = cleanup_content.encode("utf-8")
    bound.validate_path()
    return {
        "version": 2,
        "kind": "conditional_update",
        "status": "prepared",
        "target": str(target.absolute()),
        "token": token,
        "operation_fingerprint": operation_fingerprint,
        "expected": _snapshot_record(validated),
        "replacement_content": content,
        "replacement_encoding": encoding,
        "replacement_sha256": hashlib.sha256(encoded_content).hexdigest(),
        "replacement_size": len(encoded_content),
        "attempted": None,
        "replacement_snapshot": None,
        "reusable_tombstone_snapshot": None,
        "replacement_path": replacement.name,
        "displaced_path": displaced.name,
        "rollback_backup_path": rollback_backup.name,
        "exchange_displaced_path": exchange_displaced.name,
        "cleanup_placeholder_path": cleanup_placeholder.name,
        "cleanup_displaced_path": cleanup_displaced.name,
        "cleanup_restore_path": cleanup_restore.name,
        "cleanup_restored_path": cleanup_restored.name,
        "cleanup_content": cleanup_content,
        "cleanup_sha256": hashlib.sha256(encoded_cleanup).hexdigest(),
        "cleanup_size": len(encoded_cleanup),
        "cleanup_snapshot": None,
        "retained_artifact": None,
        "attempted_artifact_path": None,
        "owned_paths": [],
    }


def _validated_conditional_reservation_metadata(
    target: Path,
    recovery_state: dict[str, Any],
) -> tuple[
    tuple[tuple[int, int, int], str, int, int, int],
    str,
    str,
    Path,
    Path,
    Path,
    Path,
    Path,
]:
    if not isinstance(recovery_state, dict):
        raise ValueError("conditional recovery reservation is invalid")
    token = recovery_state.get("token")
    replacement_name = recovery_state.get("replacement_path")
    displaced_name = recovery_state.get("displaced_path")
    rollback_name = recovery_state.get("rollback_backup_path")
    exchange_displaced_name = recovery_state.get("exchange_displaced_path")
    cleanup_placeholder_name = recovery_state.get("cleanup_placeholder_path")
    cleanup_displaced_name = recovery_state.get("cleanup_displaced_path")
    cleanup_restore_name = recovery_state.get("cleanup_restore_path")
    cleanup_restored_name = recovery_state.get("cleanup_restored_path")
    expected_replacement = f".{target.name}.{token}.replacement"
    expected_displaced = f".{target.name}.{token}.displaced"
    expected_rollback = f".{target.name}.{token}.rejected"
    expected_exchange_displaced = (
        expected_displaced if sys.platform == "win32" else expected_replacement
    )
    expected_cleanup_placeholder = (
        expected_replacement if sys.platform == "win32" else expected_displaced
    )
    expected_cleanup_displaced = (
        expected_rollback if sys.platform == "win32" else expected_displaced
    )
    expected_cleanup_restore = f".{target.name}.{token}.cleanup"
    expected_cleanup_restored = (
        expected_cleanup_restore if sys.platform == "win32" else expected_displaced
    )
    if (
        recovery_state.get("version") != 2
        or recovery_state.get("kind") != "conditional_update"
        or recovery_state.get("status") not in {"prepared", "cleanup_pending"}
        or recovery_state.get("target") != str(target.absolute())
        or not isinstance(token, str)
        or re.fullmatch(r"[0-9a-f]{32}", token) is None
        or replacement_name != expected_replacement
        or displaced_name != expected_displaced
        or rollback_name != expected_rollback
        or exchange_displaced_name != expected_exchange_displaced
        or cleanup_placeholder_name != expected_cleanup_placeholder
        or cleanup_displaced_name != expected_cleanup_displaced
        or cleanup_restore_name != expected_cleanup_restore
        or cleanup_restored_name != expected_cleanup_restored
        or not isinstance(recovery_state.get("operation_fingerprint"), str)
        or not recovery_state["operation_fingerprint"]
    ):
        raise ValueError("conditional recovery reservation metadata is invalid")
    try:
        expected = _validated_expected_atomic_target(recovery_state["expected"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("conditional recovery reservation target is invalid") from exc
    content = recovery_state.get("replacement_content")
    encoding = recovery_state.get("replacement_encoding")
    digest = recovery_state.get("replacement_sha256")
    size = recovery_state.get("replacement_size")
    if not isinstance(content, str) or not isinstance(encoding, str) or not encoding:
        raise ValueError("conditional recovery replacement content is invalid")
    try:
        encoded_content = content.encode(encoding)
    except (LookupError, UnicodeError) as exc:
        raise ValueError("conditional recovery replacement encoding is invalid") from exc
    if (
        digest != hashlib.sha256(encoded_content).hexdigest()
        or isinstance(size, bool)
        or not isinstance(size, int)
        or size != len(encoded_content)
    ):
        raise ValueError("conditional recovery replacement digest is invalid")
    cleanup_content = recovery_state.get("cleanup_content")
    cleanup_digest = recovery_state.get("cleanup_sha256")
    cleanup_size = recovery_state.get("cleanup_size")
    if not isinstance(cleanup_content, str):
        raise ValueError("conditional recovery cleanup content is invalid")
    encoded_cleanup = cleanup_content.encode("utf-8")
    if (
        cleanup_digest != hashlib.sha256(encoded_cleanup).hexdigest()
        or isinstance(cleanup_size, bool)
        or not isinstance(cleanup_size, int)
        or cleanup_size != len(encoded_cleanup)
    ):
        raise ValueError("conditional recovery cleanup digest is invalid")
    replacement = target.with_name(replacement_name)
    displaced = target.with_name(displaced_name)
    rollback_backup = target.with_name(rollback_name)
    exchange_displaced = target.with_name(exchange_displaced_name)
    cleanup_restore = target.with_name(cleanup_restore_name)
    reusable_record = recovery_state.get("reusable_tombstone_snapshot")
    if reusable_record is not None:
        try:
            reusable_snapshot = _validated_expected_atomic_target(reusable_record)
        except (TypeError, ValueError) as exc:
            raise ValueError("conditional reusable tombstone is invalid") from exc
        tombstone = _resolved_tombstone_bytes(replacement)
        if (
            reusable_snapshot[1] != hashlib.sha256(tombstone).hexdigest()
            or reusable_snapshot[2] != len(tombstone)
        ):
            raise ValueError("conditional reusable tombstone content is invalid")
    return (
        expected,
        content,
        encoding,
        replacement,
        displaced,
        rollback_backup,
        exchange_displaced,
        cleanup_restore,
    )


def _validated_conditional_reservation(
    target: Path,
    recovery_state: dict[str, Any],
) -> tuple[
    tuple[tuple[int, int, int], str, int, int, int],
    tuple[tuple[int, int, int], str, int, int, int],
    tuple[tuple[int, int, int], str, int, int, int],
    Path,
    Path,
    Path,
    Path,
]:
    (
        expected,
        _content,
        _encoding,
        replacement,
        displaced,
        rollback_backup,
        exchange_displaced,
        _cleanup_restore,
    ) = _validated_conditional_reservation_metadata(target, recovery_state)
    try:
        attempted = _validated_expected_atomic_target(recovery_state["attempted"])
        replacement_snapshot = _validated_expected_atomic_target(
            recovery_state["replacement_snapshot"]
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("conditional recovery reservation snapshots are invalid") from exc
    if (
        replacement_snapshot[1] != recovery_state["replacement_sha256"]
        or replacement_snapshot[2] != recovery_state["replacement_size"]
    ):
        raise ValueError("conditional recovery replacement snapshot is invalid")
    expected_attempted = (
        replacement_snapshot[:3] + expected[3:]
        if sys.platform == "win32"
        else replacement_snapshot
    )
    if attempted != expected_attempted:
        raise ValueError("conditional recovery attempted snapshot is invalid")
    return (
        expected,
        attempted,
        replacement_snapshot,
        replacement,
        displaced,
        rollback_backup,
        exchange_displaced,
    )


def _materialize_conditional_atomic_write_reservation(
    bound: _BoundAtomicDirectory,
    target: Path,
    recovery_state: dict[str, Any],
) -> bool:
    (
        expected,
        content,
        encoding,
        replacement,
        displaced,
        rollback_backup,
        _exchange_displaced,
        _cleanup_restore,
    ) = _validated_conditional_reservation_metadata(target, recovery_state)
    if recovery_state.get("attempted") is not None or recovery_state.get(
        "replacement_snapshot"
    ) is not None:
        _validated_conditional_reservation(target, recovery_state)
        return False
    dir_fd = bound.descriptor
    current = _snapshot_regular_file(target, dir_fd=dir_fd)
    if current != expected:
        raise AtomicWriteConflictError(
            "conditional update target changed before materialization"
        )
    for candidate in (displaced, rollback_backup):
        if _bound_path_exists(candidate, dir_fd):
            raise AtomicWriteRecoveryError(
                "reserved conditional recovery path is occupied",
                _prepared_recovery_paths((replacement, displaced, rollback_backup), dir_fd),
                recovery_state,
            )
    reusable_record = recovery_state.get("reusable_tombstone_snapshot")
    reusable_snapshot = (
        _validated_expected_atomic_target(reusable_record)
        if reusable_record is not None
        else None
    )
    _require_retained_conditional_artifact_capacity(
        target,
        dir_fd,
        expected[2],
        excluded_names=(
            frozenset({replacement.name})
            if reusable_snapshot is not None
            else frozenset()
        ),
    )
    flags = os.O_WRONLY | getattr(os, "O_BINARY", 0) | getattr(
        os, "O_NOFOLLOW", 0
    )
    descriptor: int | None = None
    created_expected: tuple[tuple[int, int, int], str, int, int, int] | None = None
    created_here = False
    try:
        if reusable_snapshot is not None:
            if dir_fd is None:
                descriptor = os.open(replacement, flags)
            else:
                descriptor = os.open(
                    replacement.name,
                    flags,
                    dir_fd=dir_fd,
                )
            opened = os.fstat(descriptor)
            if (
                opened.st_dev,
                opened.st_ino,
                stat.S_IFMT(opened.st_mode),
            ) != reusable_snapshot[0]:
                raise OSError("resolved conditional tombstone identity changed")
            created_here = True
        elif not _bound_path_exists(replacement, dir_fd):
            create_flags = flags | os.O_CREAT | os.O_EXCL
            if dir_fd is None:
                descriptor = os.open(replacement, create_flags, 0o666)
            else:
                descriptor = os.open(
                    replacement.name,
                    create_flags,
                    0o666,
                    dir_fd=dir_fd,
            )
            created_here = True
        if descriptor is not None:
            if os.name == "posix":
                os.fchmod(descriptor, expected[3])
            elif dir_fd is None:
                os.chmod(replacement, expected[3])
            os.ftruncate(descriptor, 0)
            prepared = os.fstat(descriptor)
            created_expected = (
                (
                    prepared.st_dev,
                    prepared.st_ino,
                    stat.S_IFMT(prepared.st_mode),
                ),
                recovery_state["replacement_sha256"],
                recovery_state["replacement_size"],
                stat.S_IMODE(prepared.st_mode),
                getattr(prepared, "st_file_attributes", 0),
            )
            handle = os.fdopen(descriptor, "w", encoding=encoding, newline="")
            descriptor = None
            with handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            _sync_conditional_parent(target, dir_fd)
        replacement_snapshot = _snapshot_regular_file(replacement, dir_fd=dir_fd)
        if (
            replacement_snapshot[1] != recovery_state["replacement_sha256"]
            or replacement_snapshot[2] != recovery_state["replacement_size"]
            or replacement_snapshot[3] != expected[3]
        ):
            raise OSError("reserved conditional replacement content changed")
        published_snapshot = (
            replacement_snapshot[:3] + expected[3:]
            if sys.platform == "win32"
            else replacement_snapshot
        )
        recovery_state["attempted"] = _snapshot_record(published_snapshot)
        recovery_state["replacement_snapshot"] = _snapshot_record(
            replacement_snapshot
        )
        recovery_state["attempted_artifact_path"] = replacement.name
        recovery_state["owned_paths"] = [replacement.name]
        return True
    except BaseException:
        if descriptor is not None:
            os.close(descriptor)
        if created_here and created_expected is not None:
            try:
                if _unlink_owned_bound_file(
                    replacement,
                    dir_fd,
                    created_expected,
                ):
                    _sync_conditional_parent(target, dir_fd)
            except OSError:
                pass
        raise


def _prepared_recovery_paths(
    candidates: tuple[Path, ...],
    dir_fd: int | None,
) -> list[Path]:
    return [candidate for candidate in candidates if _bound_path_exists(candidate, dir_fd)]


def _consume_conditional_atomic_write_reservation(
    bound: _BoundAtomicDirectory,
    target: Path,
    recovery_state: dict[str, Any],
) -> None:
    (
        expected,
        attempted,
        replacement_snapshot,
        replacement,
        displaced_name,
        rollback_backup,
        exchange_displaced,
    ) = _validated_conditional_reservation(target, recovery_state)
    cleanup_restore = target.with_name(recovery_state["cleanup_restore_path"])
    candidates = (replacement, displaced_name, rollback_backup, cleanup_restore)
    dir_fd = bound.descriptor
    try:
        _assert_expected_base_file(
            replacement,
            replacement_snapshot,
            dir_fd=dir_fd,
        )
        bound.validate_path()
        _require_retained_conditional_artifact_capacity(
            target,
            dir_fd,
            expected[2],
            excluded_names=frozenset({replacement.name}),
        )
        displaced = _exchange_expected_base_files(
            target,
            replacement,
            displaced_name,
            dir_fd=dir_fd,
        )
    except OSError as exc:
        try:
            _sync_conditional_parent(target, dir_fd)
        except OSError:
            pass
        raise AtomicWriteRecoveryError(
            f"conditional native exchange failed: {exc}",
            _prepared_recovery_paths(candidates, dir_fd),
            recovery_state,
        ) from exc
    if displaced != exchange_displaced:
        raise AtomicWriteRecoveryError(
            "conditional native exchange returned an undisclosed artifact",
            _prepared_recovery_paths(candidates, dir_fd),
            recovery_state,
        )
    recovery_state["owned_paths"] = [displaced.name]

    displaced_snapshot = None
    attempted_target_snapshot = None
    try:
        displaced_snapshot = _snapshot_regular_file(displaced, dir_fd=dir_fd)
        attempted_target_snapshot = _snapshot_regular_file(target, dir_fd=dir_fd)
        _assert_expected_base_file(displaced, expected, dir_fd=dir_fd)
        if attempted_target_snapshot != attempted:
            raise OSError("conditional attempted target snapshot changed")
        _assert_expected_base_file(target, attempted, dir_fd=dir_fd)
        _sync_conditional_parent(target, dir_fd)
        bound.validate_path()
        return
    except OSError as post_exchange_error:
        try:
            restored_attempt = _exchange_expected_base_files(
                target,
                displaced,
                rollback_backup,
                dir_fd=dir_fd,
            )
            _sync_conditional_parent(target, dir_fd)
            if displaced_snapshot is not None:
                _assert_expected_base_file(
                    target,
                    displaced_snapshot,
                    dir_fd=dir_fd,
                )
            _assert_expected_base_file(
                restored_attempt,
                attempted,
                dir_fd=dir_fd,
            )
            recovery_state["attempted_artifact_path"] = restored_attempt.name
            if restored_attempt.name not in recovery_state["owned_paths"]:
                recovery_state["owned_paths"].append(restored_attempt.name)
        except OSError as rollback_error:
            try:
                _sync_conditional_parent(target, dir_fd)
            except OSError:
                pass
            if displaced_snapshot is not None:
                operation_fingerprint = recovery_state["operation_fingerprint"]
                recovery_state.update(
                    {
                        "version": 1,
                        "kind": "rollback",
                        "status": "required",
                        "target": target.name,
                        "displaced_path": displaced.name,
                        "rollback_backup_path": rollback_backup.name,
                        "attempted": _snapshot_record(attempted),
                        "restore": _snapshot_record(displaced_snapshot),
                        "attempted_artifact_path": None,
                        "owned_paths": [
                            item.name
                            for item in _prepared_recovery_paths(candidates, dir_fd)
                        ],
                        "operation_fingerprint": operation_fingerprint,
                    }
                )
                raise AtomicWriteRollbackError(
                    f"conditional publication rollback failed: {rollback_error}",
                    _prepared_recovery_paths(candidates, dir_fd),
                    recovery_state,
                ) from rollback_error
            raise AtomicWriteRecoveryError(
                f"conditional post-exchange recovery failed: {rollback_error}",
                _prepared_recovery_paths(candidates, dir_fd),
                recovery_state,
            ) from rollback_error
        raise AtomicWriteRecoveryError(
            f"conditional publication rolled back after post-exchange error: "
            f"{post_exchange_error}",
            _prepared_recovery_paths(candidates, dir_fd),
            recovery_state,
        ) from post_exchange_error


def conditional_atomic_write(
    path: Path,
    content_or_reservation: str | dict[str, Any],
    expected: dict[str, Any] | None = None,
    encoding: str = "utf-8",
    *,
    persist_recovery: Callable[[dict[str, Any]], None] | None = None,
) -> None:
    """Consume a prepared reservation, or support the low-level legacy helper."""
    target = Path(path)
    bound = getattr(_BOUND_ATOMIC_DIRECTORY_LOCAL, "current", None)
    if bound is None or Path(os.path.abspath(target.parent)) != bound.path:
        raise OSError(
            errno.ENOTSUP,
            "expected-base conditional publication requires a bound directory",
        )
    bound.validate_path()
    if expected is not None:
        if not isinstance(content_or_reservation, str):
            raise TypeError("conditional content must be text")
        validated = _validated_expected_atomic_target(expected)
        _conditional_atomic_write_bound(
            bound,
            target,
            content_or_reservation,
            encoding,
            validated,
        )
    else:
        if not isinstance(content_or_reservation, dict):
            raise TypeError("conditional recovery reservation must be an object")
        needs_materialization = (
            content_or_reservation.get("attempted") is None
            and content_or_reservation.get("replacement_snapshot") is None
        )
        if needs_materialization and persist_recovery is None:
            raise ValueError(
                "conditional reservation materialization requires durable recovery"
            )
        materialized = _materialize_conditional_atomic_write_reservation(
            bound,
            target,
            content_or_reservation,
        )
        if materialized:
            persist_recovery(content_or_reservation)
        _consume_conditional_atomic_write_reservation(
            bound,
            target,
            content_or_reservation,
        )
    bound.validate_path()


def finalize_conditional_atomic_write(
    path: Path,
    recovery_state: dict[str, Any],
    *,
    persist_recovery: Callable[[dict[str, Any]], None] | None = None,
) -> None:
    """Quarantine and clean a displaced base after cleanup intent is durable."""
    target = Path(path)
    bound = getattr(_BOUND_ATOMIC_DIRECTORY_LOCAL, "current", None)
    if bound is None or Path(os.path.abspath(target.parent)) != bound.path:
        raise OSError(errno.ENOTSUP, "conditional finalize requires a bound directory")
    (
        expected,
        attempted,
        _replacement_snapshot,
        replacement,
        displaced,
        rollback_backup,
        exchange_displaced,
    ) = _validated_conditional_reservation(target, recovery_state)
    (
        _expected_metadata,
        _content,
        _encoding,
        _replacement,
        _displaced,
        _rollback_backup,
        _exchange_displaced,
        cleanup_restore,
    ) = _validated_conditional_reservation_metadata(target, recovery_state)
    dir_fd = bound.descriptor
    cleanup_placeholder = target.with_name(
        recovery_state["cleanup_placeholder_path"]
    )
    cleanup_displaced = target.with_name(recovery_state["cleanup_displaced_path"])
    cleanup_restored = target.with_name(recovery_state["cleanup_restored_path"])
    candidates = tuple(
        dict.fromkeys(
            (
                replacement,
                displaced,
                rollback_backup,
                cleanup_restore,
            )
        )
    )

    def snapshot_if_present(candidate: Path):
        try:
            return _snapshot_regular_file(candidate, dir_fd=dir_fd)
        except FileNotFoundError:
            return None

    def cleanup_payload_matches(snapshot) -> bool:
        return (
            snapshot is not None
            and snapshot[1] == recovery_state["cleanup_sha256"]
            and snapshot[2] == recovery_state["cleanup_size"]
            and snapshot[3] == expected[3]
        )

    try:
        _assert_expected_base_file(target, attempted, dir_fd=dir_fd)
        if os.name == "posix":
            retained_record = recovery_state.get("retained_artifact")
            if retained_record is not None:
                _validate_retained_conditional_artifact_record(
                    retained_record,
                    exchange_displaced,
                    dir_fd,
                    expected,
                )
                recovery_state["owned_paths"] = []
                _sync_conditional_parent(target, dir_fd)
                _assert_expected_base_file(target, attempted, dir_fd=dir_fd)
                bound.validate_path()
                return
            existing_paths = [
                candidate
                for candidate in candidates
                if _bound_path_exists(candidate, dir_fd)
            ]
            if existing_paths == [exchange_displaced] and _unlink_owned_bound_file(
                exchange_displaced,
                dir_fd,
                expected,
            ):
                if persist_recovery is None:
                    raise OSError(
                        "retained artifact recording requires durable recovery"
                    )
                retained_record = _retained_conditional_artifact_record(
                    exchange_displaced,
                    dir_fd,
                    expected,
                )
                recovery_state["owned_paths"] = []
                recovery_state["retained_artifact"] = retained_record
                _sync_conditional_parent(target, dir_fd)
                persist_recovery(recovery_state)
                _assert_expected_base_file(target, attempted, dir_fd=dir_fd)
                bound.validate_path()
                return
            existing = [
                (candidate, snapshot_if_present(candidate))
                for candidate in existing_paths
            ]
            if existing and all(
                _is_resolved_conditional_tombstone(candidate, snapshot)
                for candidate, snapshot in existing
            ):
                recovery_state["owned_paths"] = []
                _sync_conditional_parent(target, dir_fd)
                _assert_expected_base_file(target, attempted, dir_fd=dir_fd)
                bound.validate_path()
                return
        if not any(_bound_path_exists(candidate, dir_fd) for candidate in candidates):
            _sync_conditional_parent(target, dir_fd)
            bound.validate_path()
            return

        cleanup_snapshot_record = recovery_state.get("cleanup_snapshot")
        if cleanup_snapshot_record is None:
            if persist_recovery is None:
                raise OSError("cleanup materialization requires durable recovery")
            for candidate in (cleanup_displaced, cleanup_restore):
                if candidate != cleanup_placeholder and _bound_path_exists(
                    candidate, dir_fd
                ):
                    raise OSError("unexpected cleanup artifact exists before exchange")
            cleanup_snapshot = snapshot_if_present(cleanup_placeholder)
            if cleanup_snapshot is None:
                flags = (
                    os.O_WRONLY
                    | os.O_CREAT
                    | os.O_EXCL
                    | getattr(os, "O_BINARY", 0)
                )
                if dir_fd is None:
                    descriptor = os.open(cleanup_placeholder, flags, 0o666)
                else:
                    descriptor = os.open(
                        cleanup_placeholder.name,
                        flags,
                        0o666,
                        dir_fd=dir_fd,
                    )
                try:
                    if os.name == "posix":
                        os.fchmod(descriptor, expected[3])
                    elif dir_fd is None:
                        os.chmod(cleanup_placeholder, expected[3])
                    handle = os.fdopen(
                        descriptor,
                        "w",
                        encoding="utf-8",
                        newline="",
                    )
                    descriptor = -1
                    with handle:
                        handle.write(recovery_state["cleanup_content"])
                        handle.flush()
                        os.fsync(handle.fileno())
                finally:
                    if descriptor >= 0:
                        os.close(descriptor)
                _sync_conditional_parent(target, dir_fd)
                cleanup_snapshot = _snapshot_regular_file(
                    cleanup_placeholder,
                    dir_fd=dir_fd,
                )
            if not cleanup_payload_matches(cleanup_snapshot):
                raise OSError("reserved cleanup placeholder content changed")
            recovery_state["cleanup_snapshot"] = _snapshot_record(cleanup_snapshot)
            recovery_state["owned_paths"] = list(
                dict.fromkeys(
                    [*recovery_state.get("owned_paths", []), cleanup_placeholder.name]
                )
            )
            persist_recovery(recovery_state)
        else:
            cleanup_snapshot = _validated_expected_atomic_target(
                cleanup_snapshot_record
            )
            if not cleanup_payload_matches(cleanup_snapshot):
                raise OSError("persisted cleanup placeholder is invalid")

        def exact_cleanup(snapshot) -> bool:
            return snapshot is not None and snapshot[:3] == cleanup_snapshot[:3]

        public_snapshot = snapshot_if_present(exchange_displaced)
        displaced_snapshot = snapshot_if_present(cleanup_displaced)
        restored_snapshot = snapshot_if_present(cleanup_restored)
        placeholder_snapshot = snapshot_if_present(cleanup_placeholder)

        if public_snapshot is None:
            if not any(_bound_path_exists(candidate, dir_fd) for candidate in candidates):
                _sync_conditional_parent(target, dir_fd)
                _assert_expected_base_file(target, attempted, dir_fd=dir_fd)
                bound.validate_path()
                return
            raise OSError("public cleanup artifact disappeared")

        if (
            exact_cleanup(restored_snapshot)
            and not exact_cleanup(public_snapshot)
            and public_snapshot != expected
        ):
            raise OSError("foreign cleanup artifact was restored and retained")

        if not exact_cleanup(public_snapshot):
            if not exact_cleanup(placeholder_snapshot):
                raise OSError("cleanup placeholder identity changed before exchange")
            for candidate in (cleanup_displaced, cleanup_restore):
                if candidate != cleanup_placeholder and _bound_path_exists(
                    candidate, dir_fd
                ):
                    raise OSError("cleanup exchange destination is occupied")
            exchanged = _exchange_expected_base_files(
                exchange_displaced,
                cleanup_placeholder,
                rollback_backup,
                dir_fd=dir_fd,
            )
            if exchanged != cleanup_displaced:
                raise OSError("cleanup exchange returned an undisclosed artifact")
            _sync_conditional_parent(target, dir_fd)
            public_snapshot = _snapshot_regular_file(
                exchange_displaced,
                dir_fd=dir_fd,
            )
            displaced_snapshot = _snapshot_regular_file(
                cleanup_displaced,
                dir_fd=dir_fd,
            )
            if not exact_cleanup(public_snapshot):
                raise OSError("cleanup placeholder changed during exchange")

        if displaced_snapshot is not None and displaced_snapshot != expected:
            foreign_snapshot = displaced_snapshot
            restored = _exchange_expected_base_files(
                exchange_displaced,
                cleanup_displaced,
                cleanup_restore,
                dir_fd=dir_fd,
            )
            if restored != cleanup_restored:
                raise OSError("cleanup restore returned an undisclosed artifact")
            _sync_conditional_parent(target, dir_fd)
            _assert_expected_base_file(
                exchange_displaced,
                foreign_snapshot,
                dir_fd=dir_fd,
            )
            restored_cleanup = _snapshot_regular_file(
                cleanup_restored,
                dir_fd=dir_fd,
            )
            if not exact_cleanup(restored_cleanup):
                raise OSError("cleanup restore did not preserve its placeholder")
            recovery_state["owned_paths"] = [cleanup_restored.name]
            raise OSError("foreign cleanup artifact was restored without deletion")

        if displaced_snapshot == expected:
            if not _unlink_owned_bound_file(
                cleanup_displaced,
                dir_fd,
                expected,
            ):
                raise OSError("displaced cleanup identity changed before removal")
        elif _is_resolved_conditional_tombstone(
            cleanup_displaced,
            displaced_snapshot,
        ):
            pass
        elif displaced_snapshot is not None:
            raise OSError("displaced cleanup artifact is invalid")

        public_snapshot = snapshot_if_present(exchange_displaced)
        if public_snapshot is not None:
            if exact_cleanup(public_snapshot):
                if not _unlink_owned_bound_file(
                    exchange_displaced,
                    dir_fd,
                    cleanup_snapshot,
                ):
                    raise OSError("public cleanup placeholder changed before removal")
            elif not _is_resolved_conditional_tombstone(
                exchange_displaced,
                public_snapshot,
            ):
                raise OSError("public cleanup placeholder changed before removal")
        for candidate in candidates:
            candidate_snapshot = snapshot_if_present(candidate)
            if candidate_snapshot is not None and not (
                os.name == "posix"
                and _is_resolved_conditional_tombstone(candidate, candidate_snapshot)
            ):
                raise OSError("unexpected conditional artifact remains after cleanup")
        recovery_state["owned_paths"] = []
        _sync_conditional_parent(target, dir_fd)
        _assert_expected_base_file(target, attempted, dir_fd=dir_fd)
        bound.validate_path()
    except OSError as exc:
        raise AtomicWriteRecoveryError(
            f"conditional applied cleanup failed: {exc}",
            _prepared_recovery_paths(candidates, dir_fd),
            recovery_state,
        ) from exc


def _reconcile_prepared_conditional_write(
    target: Path,
    recovery_state: dict[str, Any],
    phase: str,
    persist_recovery: Callable[[dict[str, Any]], None] | None = None,
) -> str:
    bound = getattr(_BOUND_ATOMIC_DIRECTORY_LOCAL, "current", None)
    if bound is None or Path(os.path.abspath(target.parent)) != bound.path:
        raise OSError(errno.ENOTSUP, "conditional recovery requires a bound directory")
    (
        expected,
        _content,
        _encoding,
        replacement,
        displaced,
        rollback_backup,
        exchange_displaced,
        cleanup_restore,
    ) = _validated_conditional_reservation_metadata(target, recovery_state)
    if phase == "cleanup":
        finalize_conditional_atomic_write(
            target,
            recovery_state,
            persist_recovery=persist_recovery,
        )
        return "applied"
    if phase != "inspect":
        raise ValueError("prepared conditional recovery phase is invalid")

    dir_fd = bound.descriptor
    candidates = (replacement, displaced, rollback_backup, cleanup_restore)

    def fail(message: str, cause: BaseException | None = None):
        error = AtomicWriteRecoveryError(
            message,
            _prepared_recovery_paths(candidates, dir_fd),
            recovery_state,
        )
        if cause is None:
            raise error
        raise error from cause

    try:
        target_snapshot = _snapshot_regular_file(target, dir_fd=dir_fd)
    except OSError as exc:
        fail("prepared conditional target is unavailable", exc)

    unmaterialized = (
        recovery_state.get("attempted") is None
        and recovery_state.get("replacement_snapshot") is None
    )
    if unmaterialized:
        if target_snapshot != expected:
            fail("prepared conditional target changed before materialization")
        if _bound_path_exists(displaced, dir_fd) or _bound_path_exists(
            rollback_backup, dir_fd
        ):
            fail("prepared conditional recovery path exists before materialization")
        bound.validate_path()
        return "pending"
    try:
        (
            _expected,
            attempted,
            replacement_snapshot,
            _replacement,
            _displaced,
            _rollback_backup,
            _exchange_displaced,
        ) = _validated_conditional_reservation(target, recovery_state)
    except ValueError as exc:
        fail("prepared conditional materialization metadata is invalid", exc)

    if target_snapshot == attempted:
        try:
            displaced_snapshot = _snapshot_regular_file(
                exchange_displaced,
                dir_fd=dir_fd,
            )
        except OSError as exc:
            fail("prepared conditional displaced artifact is unavailable", exc)
        if displaced_snapshot == expected:
            return "applied"
        if _bound_path_exists(rollback_backup, dir_fd):
            fail("prepared conditional rollback path is occupied")
        try:
            restored_attempt = _exchange_expected_base_files(
                target,
                exchange_displaced,
                rollback_backup,
                dir_fd=dir_fd,
            )
            _sync_conditional_parent(target, dir_fd)
            _assert_expected_base_file(
                target,
                displaced_snapshot,
                dir_fd=dir_fd,
            )
            _assert_expected_base_file(
                restored_attempt,
                attempted,
                dir_fd=dir_fd,
            )
            if _unlink_owned_bound_file(
                restored_attempt,
                dir_fd,
                attempted,
            ):
                _sync_conditional_parent(target, dir_fd)
        except OSError as exc:
            fail("prepared conditional concurrent target restore failed", exc)
        bound.validate_path()
        return "pending"

    for candidate in (replacement, rollback_backup):
        if not _bound_path_exists(candidate, dir_fd):
            continue
        try:
            candidate_snapshot = _snapshot_regular_file(candidate, dir_fd=dir_fd)
        except OSError as exc:
            fail("prepared conditional scratch artifact is unreadable", exc)
        if candidate_snapshot not in {attempted, replacement_snapshot}:
            fail("prepared conditional scratch ownership changed")
        try:
            if _unlink_owned_bound_file(
                candidate,
                dir_fd,
                candidate_snapshot,
            ):
                _sync_conditional_parent(target, dir_fd)
        except OSError as exc:
            fail("prepared conditional scratch cleanup failed", exc)
    if (
        exchange_displaced not in {replacement, rollback_backup}
        and _bound_path_exists(exchange_displaced, dir_fd)
    ):
        fail("prepared conditional displaced artifact exists without its effect")
    bound.validate_path()
    return "pending"


def reconcile_conditional_write_recovery(
    path: Path,
    recovery_state: dict[str, Any],
    phase: str,
    *,
    persist_recovery: Callable[[dict[str, Any]], None] | None = None,
) -> str | None:
    """Idempotently restore or clean one journal-owned rollback failure."""
    target = Path(path)
    if (
        isinstance(recovery_state, dict)
        and recovery_state.get("kind") == "conditional_update"
    ):
        return _reconcile_prepared_conditional_write(
            target,
            recovery_state,
            phase,
            persist_recovery,
        )
    bound = getattr(_BOUND_ATOMIC_DIRECTORY_LOCAL, "current", None)
    if bound is None or Path(os.path.abspath(target.parent)) != bound.path:
        raise OSError(
            errno.ENOTSUP,
            "conditional recovery requires a bound directory",
        )
    bound.validate_path()
    if phase not in {"restore", "cleanup"}:
        raise ValueError("conditional recovery phase is invalid")
    if not isinstance(recovery_state, dict):
        raise ValueError("conditional recovery state is invalid")
    token = recovery_state.get("token")
    status = recovery_state.get("status")
    displaced_name = recovery_state.get("displaced_path")
    rollback_backup_name = recovery_state.get("rollback_backup_path")
    expected_displaced_names = {
        f".{target.name}.{token}.replacement",
        f".{target.name}.{token}.displaced",
    }
    expected_backup_name = f".{target.name}.{token}.rejected"
    if (
        recovery_state.get("version") != 1
        or recovery_state.get("kind") != "rollback"
        or recovery_state.get("target") != target.name
        or not isinstance(token, str)
        or re.fullmatch(r"[0-9a-f]{32}", token) is None
        or status not in {"required", "restoring", "restored", "resolved"}
        or displaced_name not in expected_displaced_names
        or rollback_backup_name != expected_backup_name
    ):
        raise AtomicWriteRecoveryError(
            "conditional rollback recovery metadata is invalid",
            [],
            recovery_state,
        )
    try:
        attempted = _validated_expected_atomic_target(recovery_state["attempted"])
        restore = _validated_expected_atomic_target(recovery_state["restore"])
    except (KeyError, TypeError, ValueError) as exc:
        raise AtomicWriteRecoveryError(
            "conditional rollback recovery snapshots are invalid",
            [],
            recovery_state,
        ) from exc
    owned_names = recovery_state.get("owned_paths")
    allowed_names = expected_displaced_names | {expected_backup_name}
    if (
        not isinstance(owned_names, list)
        or any(not isinstance(name, str) or name not in allowed_names for name in owned_names)
        or len(set(owned_names)) != len(owned_names)
    ):
        raise AtomicWriteRecoveryError(
            "conditional rollback recovery ownership is invalid",
            [],
            recovery_state,
        )
    attempted_artifact = recovery_state.get("attempted_artifact_path")
    if attempted_artifact is not None and attempted_artifact not in allowed_names:
        raise AtomicWriteRecoveryError(
            "conditional rollback attempted artifact is invalid",
            [],
            recovery_state,
        )

    dir_fd = bound.descriptor
    displaced = target.with_name(displaced_name)
    rollback_backup = target.with_name(rollback_backup_name)
    candidates = (displaced, rollback_backup)

    def snapshot_if_present(candidate: Path):
        try:
            return _snapshot_regular_file(candidate, dir_fd=dir_fd)
        except FileNotFoundError:
            return None

    def existing_recovery_paths() -> list[Path]:
        return [
            candidate
            for candidate in candidates
            if _bound_path_exists(candidate, dir_fd)
        ]

    def fail(message: str, cause: BaseException | None = None):
        error = AtomicWriteRecoveryError(
            message,
            existing_recovery_paths(),
            recovery_state,
        )
        if cause is None:
            raise error
        raise error from cause

    try:
        target_snapshot = _snapshot_regular_file(target, dir_fd=dir_fd)
    except OSError as exc:
        fail("conditional rollback target is unavailable", exc)

    if phase == "restore":
        if target_snapshot == restore:
            for candidate in candidates:
                try:
                    candidate_snapshot = snapshot_if_present(candidate)
                except OSError as exc:
                    fail("conditional rollback artifact is unreadable", exc)
                if candidate_snapshot == attempted:
                    recovery_state["attempted_artifact_path"] = candidate.name
                    if candidate.name not in owned_names:
                        owned_names.append(candidate.name)
                    return
            if attempted_artifact is not None and not _bound_path_exists(
                target.with_name(attempted_artifact),
                dir_fd,
            ):
                return
            fail("conditional rollback attempted artifact is missing")
        if target_snapshot != attempted:
            fail("conditional rollback target diverged before restore")
        try:
            displaced_snapshot = _snapshot_regular_file(displaced, dir_fd=dir_fd)
        except OSError as exc:
            fail("conditional rollback displaced target is unavailable", exc)
        if displaced_snapshot != restore:
            fail("conditional rollback displaced target changed before restore")
        if _bound_path_exists(rollback_backup, dir_fd):
            fail("conditional rollback backup path is already occupied")
        try:
            restored_attempt = _exchange_expected_base_files(
                target,
                displaced,
                rollback_backup,
                dir_fd=dir_fd,
            )
            _sync_conditional_parent(target, dir_fd)
            _assert_expected_base_file(target, restore, dir_fd=dir_fd)
            _assert_expected_base_file(
                restored_attempt,
                attempted,
                dir_fd=dir_fd,
            )
        except OSError as exc:
            try:
                _sync_conditional_parent(target, dir_fd)
            except OSError:
                pass
            fail("conditional rollback restore failed", exc)
        recovery_state["attempted_artifact_path"] = restored_attempt.name
        if restored_attempt.name not in owned_names:
            owned_names.append(restored_attempt.name)
        bound.validate_path()
        return

    if target_snapshot != restore:
        fail("conditional rollback target diverged after restore")
    if attempted_artifact is None:
        for candidate in candidates:
            try:
                if snapshot_if_present(candidate) == attempted:
                    attempted_artifact = candidate.name
                    recovery_state["attempted_artifact_path"] = attempted_artifact
                    if attempted_artifact not in owned_names:
                        owned_names.append(attempted_artifact)
                    break
            except OSError as exc:
                fail("conditional rollback artifact is unreadable", exc)
    if attempted_artifact is not None:
        artifact = target.with_name(attempted_artifact)
        if _bound_path_exists(artifact, dir_fd):
            try:
                artifact_snapshot = _snapshot_regular_file(artifact, dir_fd=dir_fd)
                if artifact_snapshot == attempted:
                    if not _unlink_owned_bound_file(
                        artifact,
                        dir_fd,
                        attempted,
                    ):
                        raise OSError(
                            "conditional rollback identity changed before retirement"
                        )
                elif not (
                    os.name == "posix"
                    and _is_resolved_conditional_tombstone(
                        artifact,
                        artifact_snapshot,
                    )
                ):
                    raise OSError("conditional rollback artifact changed before cleanup")
                _sync_conditional_parent(target, dir_fd)
            except OSError as exc:
                fail("conditional rollback cleanup failed", exc)
    try:
        _assert_expected_base_file(target, restore, dir_fd=dir_fd)
    except OSError as exc:
        fail("conditional rollback target changed during cleanup", exc)
    bound.validate_path()


def _atomic_write_path(path: Path, content: str, encoding: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        target_mode = stat.S_IMODE(path.stat().st_mode)
    except FileNotFoundError:
        target_mode = None

    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    descriptor: int | None = None
    tmp: Path | None = None
    try:
        while descriptor is None:
            candidate = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
            try:
                descriptor = os.open(candidate, flags, 0o666)
                tmp = candidate
            except FileExistsError:
                continue
        if target_mode is not None:
            os.chmod(tmp, target_mode)
        handle = os.fdopen(descriptor, "w", encoding=encoding, newline="")
        descriptor = None
        with handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        replace_deadline = time.monotonic() + 1.0
        while True:
            try:
                if os.name == "nt":
                    _move_file_write_through_windows(tmp, path, replace=True)
                else:
                    os.replace(str(tmp), str(path))
                break
            except PermissionError:
                if os.name != "nt" or time.monotonic() >= replace_deadline:
                    raise
                time.sleep(0.01)
        _sync_parent_directory(path)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if tmp is not None:
            try:
                tmp.unlink()
            except FileNotFoundError:
                pass


def _atomic_write_bound_posix(
    bound: _BoundAtomicDirectory,
    path: Path,
    content: str,
    encoding: str,
) -> None:
    descriptor: int | None = None
    tmp_name: str | None = None
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    try:
        try:
            target_metadata = os.stat(
                path.name,
                dir_fd=bound.descriptor,
                follow_symlinks=False,
            )
            target_mode = stat.S_IMODE(target_metadata.st_mode)
        except FileNotFoundError:
            target_mode = None
        while descriptor is None:
            candidate = f".{path.name}.{uuid.uuid4().hex}.tmp"
            try:
                descriptor = os.open(
                    candidate,
                    flags,
                    0o666,
                    dir_fd=bound.descriptor,
                )
                tmp_name = candidate
            except FileExistsError:
                continue
        if target_mode is not None:
            os.fchmod(descriptor, target_mode)
        handle = os.fdopen(descriptor, "w", encoding=encoding, newline="")
        descriptor = None
        with handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(
            tmp_name,
            path.name,
            src_dir_fd=bound.descriptor,
            dst_dir_fd=bound.descriptor,
        )
        try:
            os.fsync(bound.descriptor)
        except OSError:
            pass
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if tmp_name is not None:
            try:
                os.unlink(tmp_name, dir_fd=bound.descriptor)
            except FileNotFoundError:
                pass


def _atomic_create_path(path: Path, content: str, encoding: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    descriptor: int | None = None
    tmp: Path | None = None
    try:
        while descriptor is None:
            candidate = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
            try:
                descriptor = os.open(candidate, flags, 0o666)
                tmp = candidate
            except FileExistsError:
                continue
        handle = os.fdopen(descriptor, "w", encoding=encoding, newline="")
        descriptor = None
        with handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        if os.name == "nt":
            _move_file_write_through_windows(tmp, path)
        else:
            os.link(str(tmp), str(path), follow_symlinks=False)
        _sync_parent_directory(path)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if tmp is not None:
            try:
                tmp.unlink()
            except FileNotFoundError:
                pass


def _move_file_write_through_windows(
    source: Path,
    target: Path,
    *,
    replace: bool = False,
) -> None:
    import ctypes
    from ctypes import wintypes

    def extended(path: Path) -> str:
        value = os.path.abspath(path)
        if value.startswith("\\\\?\\"):
            return value
        return (
            "\\\\?\\UNC\\" + value[2:]
            if value.startswith("\\\\")
            else "\\\\?\\" + value
        )

    kernel32 = _load_windows_kernel32()
    move_file = kernel32.MoveFileExW
    move_file.argtypes = (wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD)
    move_file.restype = wintypes.BOOL
    flags = 0x00000008 | (0x00000001 if replace else 0)
    if not move_file(extended(source), extended(target), flags):
        error = ctypes.get_last_error()
        raise ctypes.WinError(error)


def _atomic_create_bound_posix(
    bound: _BoundAtomicDirectory,
    path: Path,
    content: str,
    encoding: str,
) -> None:
    descriptor: int | None = None
    tmp_name: str | None = None
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    try:
        while descriptor is None:
            candidate = f".{path.name}.{uuid.uuid4().hex}.tmp"
            try:
                descriptor = os.open(
                    candidate,
                    flags,
                    0o666,
                    dir_fd=bound.descriptor,
                )
                tmp_name = candidate
            except FileExistsError:
                continue
        handle = os.fdopen(descriptor, "w", encoding=encoding, newline="")
        descriptor = None
        with handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.link(
            tmp_name,
            path.name,
            src_dir_fd=bound.descriptor,
            dst_dir_fd=bound.descriptor,
            follow_symlinks=False,
        )
        try:
            os.fsync(bound.descriptor)
        except OSError:
            pass
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if tmp_name is not None:
            try:
                os.unlink(tmp_name, dir_fd=bound.descriptor)
            except FileNotFoundError:
                pass


def atomic_write(path: Path, content: str, encoding: str = "utf-8") -> None:
    """Write content atomically without redirecting identity-bound writes."""
    target = Path(path)
    bound = getattr(_BOUND_ATOMIC_DIRECTORY_LOCAL, "current", None)
    require_absent = getattr(_ATOMIC_WRITE_REQUIRE_ABSENT_LOCAL, "enabled", False)
    expected_target = getattr(_ATOMIC_WRITE_EXPECTED_TARGET_LOCAL, "current", None)
    if require_absent and expected_target is not None:
        raise ValueError("atomic target cannot be both absent and preconditioned")
    if expected_target is not None:
        if bound is None or Path(os.path.abspath(target.parent)) != bound.path:
            raise OSError(
                errno.ENOTSUP,
                "expected-base conditional publication requires a bound directory",
            )
        bound.validate_path()
        _conditional_atomic_write_bound(
            bound,
            target,
            content,
            encoding,
            expected_target,
        )
        bound.validate_path()
        return
    if (
        bound is None
        or Path(os.path.abspath(target.parent)) != bound.path
    ):
        if require_absent:
            _atomic_create_path(target, content, encoding)
        else:
            _atomic_write_path(target, content, encoding)
        return

    bound.validate_path()
    if require_absent and bound.descriptor is not None:
        _atomic_create_bound_posix(bound, target, content, encoding)
    elif require_absent:
        _atomic_create_path(target, content, encoding)
    elif bound.descriptor is None:
        _atomic_write_path(target, content, encoding)
    else:
        _atomic_write_bound_posix(bound, target, content, encoding)
    bound.validate_path()


def spawn_detached(
    args: list[str],
    stdout_path: Path | None = None,
    stderr_path: Path | None = None,
    cwd: Path | None = None,
) -> int | None:
    """Spawn a subprocess that outlives the caller.

    Used by hook wrappers to kick off flush/compile without blocking the
    hook timeout. Safe on Windows (DETACHED_PROCESS) and POSIX (start_new_session).

    If `stdout_path` / `stderr_path` are given, stdout/stderr are redirected
    there (truncated on each spawn) instead of DEVNULL — this is how we
    keep observability into a detached compile. `cwd` overrides the default
    vault working directory. Returns the spawned PID, or None if spawn failed.
    """
    out_f = err_f = None
    try:
        kwargs: dict[str, Any] = {
            "stdin": subprocess.DEVNULL,
            "close_fds": True,
            "cwd": str(cwd or ROOT),
        }
        if stdout_path is not None:
            stdout_path.parent.mkdir(parents=True, exist_ok=True)
            out_f = open(stdout_path, "wb")
            kwargs["stdout"] = out_f
        else:
            kwargs["stdout"] = subprocess.DEVNULL
        if stderr_path is not None:
            stderr_path.parent.mkdir(parents=True, exist_ok=True)
            err_f = open(stderr_path, "wb")
            kwargs["stderr"] = err_f
        else:
            kwargs["stderr"] = subprocess.DEVNULL
        env = os.environ.copy()
        env["CLAUDE_INVOKED_BY"] = env.get("CLAUDE_INVOKED_BY", "memory-automation")
        kwargs["env"] = env
        if sys.platform == "win32":
            DETACHED_PROCESS = 0x00000008
            CREATE_NEW_PROCESS_GROUP = 0x00000200
            CREATE_NO_WINDOW = 0x08000000
            kwargs["creationflags"] = (
                DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW
            )
        else:
            kwargs["start_new_session"] = True
        proc = subprocess.Popen(args, **kwargs)
        return proc.pid
    except (OSError, ValueError):
        return None
    finally:
        # Parent can close its handles; a child inherited its own on success.
        if out_f is not None:
            try:
                out_f.close()
            except OSError:
                pass
        if err_f is not None:
            try:
                err_f.close()
            except OSError:
                pass
