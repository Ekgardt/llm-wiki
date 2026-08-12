"""Helper for OpenCode plugin: append a pre-built block to today's daily log.

Reads JSON from stdin: {"slug": "...", "sessionId": "...", "block": "...",
"captureId": "<optional lowercase SHA-256>"}
Appends `block` to $LLM_WIKI_ROOT/knowledge/daily/<date>.md.

Why this exists: the OpenCode plugin does LLM work in JS (via OpenCode SDK),
then needs to write the result to a markdown file. Calling Python for the
file I/O keeps path handling cross-platform and reuses the canonical
daily-log location without re-implementing it in JS.

Invalid input exits without an acknowledgement. Errors go to stderr.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import re
import stat
import sys
from datetime import datetime
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, io.UnsupportedOperation):
        pass

from memory_state import (  # noqa: E402
    STATE_ROOT,
    advisory_file_lock,
    atomic_write,
    bind_atomic_writes_to_directory,
    read_json_object_bounded,
)
from secret_redact import redact_secrets  # noqa: E402

MAX_STDIN_BYTES = 256 * 1024
MAX_DAILY_MARKER_SCAN_BYTES = 4 * 1024 * 1024
MAX_DAILY_MARKER_SCAN_TOTAL_BYTES = 64 * 1024 * 1024
DAILY_MARKER_SCAN_CHUNK_BYTES = 64 * 1024
MAX_DAILY_MARKER_SCAN_FILES = 4_096
IDEMPOTENCY_MARKER_RE = re.compile(
    r"<!-- llm-wiki-(?:queue-task|direct-flush|capture): [0-9a-f]{64} -->"
)
CAPTURE_ID_RE = re.compile(r"[0-9a-f]{64}")
CAPTURE_MARKER_RE = re.compile(r"<!-- llm-wiki-capture: [0-9a-f]{64} -->")
CAPTURE_MARKER_PREFIX = "<!-- llm-wiki-capture:"
ESCAPED_CAPTURE_MARKER_PREFIX = "&lt;!-- llm-wiki-capture:"


def neutralize_capture_marker_prefix(value: object) -> str:
    return str(value).replace(CAPTURE_MARKER_PREFIX, ESCAPED_CAPTURE_MARKER_PREFIX)


def _reject_nul_path(path: Path, label: str) -> None:
    if "\0" in os.fspath(path):
        raise ValueError(f"{label} contains NUL")


@contextlib.contextmanager
def _daily_lock(
    timeout: float = 10.0,
    poll: float = 0.05,
    state_root: Path | None = None,
):
    """Hold the stable daily append lock by its open file descriptor."""
    lock_file = (state_root or STATE_ROOT) / "run" / "daily-append.lock"
    with advisory_file_lock(
        lock_file,
        timeout=timeout,
        poll=poll,
        description="daily-log lock",
    ):
        yield


def _append_unlocked(daily_path: Path, text: str) -> None:
    if not daily_path.exists():
        day = daily_path.stem
        daily_path.write_text(f"# Daily Session Memory — {day}\n", encoding="utf-8")
    with daily_path.open("a", encoding="utf-8") as handle:
        handle.write(text)
        if not text.endswith("\n"):
            handle.write("\n")


def _is_reparse_point(metadata) -> bool:
    attributes = getattr(metadata, "st_file_attributes", 0)
    return bool(
        attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )


def _daily_candidate_metadata(path: Path, *, allow_missing: bool):
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        if allow_missing:
            return None
        raise RuntimeError("daily marker scan candidate disappeared") from None
    except OSError as exc:
        raise RuntimeError("daily marker scan candidate is unreadable") from exc

    if (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or _is_reparse_point(metadata)
    ):
        raise RuntimeError("daily marker scan candidate is not a regular file")
    if metadata.st_size > MAX_DAILY_MARKER_SCAN_BYTES:
        raise RuntimeError("daily marker scan candidate exceeds byte limit")
    return metadata


def _open_windows_daily_candidate(path: Path) -> int:
    import ctypes
    import msvcrt
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
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
    handle = create_file(
        path_text,
        0x80000000,  # GENERIC_READ
        0x00000001 | 0x00000002,  # FILE_SHARE_READ | FILE_SHARE_WRITE
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
            os.O_RDONLY | getattr(os, "O_BINARY", 0),
        )
    except BaseException:
        kernel32.CloseHandle(handle)
        raise


def _open_daily_candidate_descriptor(path: Path, directory_bound) -> int:
    if os.name == "nt":
        return _open_windows_daily_candidate(path)
    if os.name != "posix":
        raise OSError("no-follow daily reads are unsupported on this platform")
    flags = (
        os.O_RDONLY
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    if directory_bound.descriptor is None:
        return os.open(path, flags)
    return os.open(path.name, flags, dir_fd=directory_bound.descriptor)


def _marker_in_daily(
    path: Path,
    marker: str,
    *,
    allow_missing: bool,
    directory_bound,
    aggregate_remaining: int,
    collected: bytearray | None = None,
    standalone_marker: bool = False,
) -> tuple[bool, int]:
    metadata = _daily_candidate_metadata(path, allow_missing=allow_missing)
    if metadata is None:
        return False, 0
    needle = marker.encode("ascii")
    overlap = b""
    pending_line = bytearray()
    total = 0
    found = False
    descriptor: int | None = None
    try:
        directory_bound.validate_path()
        descriptor = _open_daily_candidate_descriptor(path, directory_bound)
        opened = os.fstat(descriptor)
        if (
            not os.path.samestat(metadata, opened)
            or not stat.S_ISREG(opened.st_mode)
            or _is_reparse_point(opened)
            or opened.st_size > MAX_DAILY_MARKER_SCAN_BYTES
        ):
            raise RuntimeError("daily marker scan candidate changed while opening")
        while total < opened.st_size:
            per_file_remaining = MAX_DAILY_MARKER_SCAN_BYTES - total
            aggregate_file_remaining = aggregate_remaining - total
            if aggregate_file_remaining <= 0:
                if total < opened.st_size:
                    raise RuntimeError("daily marker scan aggregate byte limit exceeded")
                break
            read_size = min(
                DAILY_MARKER_SCAN_CHUNK_BYTES,
                per_file_remaining,
                aggregate_file_remaining,
                opened.st_size - total,
            )
            if read_size <= 0:
                raise RuntimeError("daily marker scan candidate exceeds byte limit")
            chunk = os.read(descriptor, read_size)
            if not chunk:
                break
            if len(chunk) > read_size or total + len(chunk) > opened.st_size:
                raise RuntimeError("daily marker scan candidate changed while reading")
            window = overlap + chunk
            total += len(chunk)
            if collected is not None:
                collected.extend(chunk)
            if standalone_marker:
                parts = chunk.split(b"\n")
                if len(parts) == 1:
                    pending_line.extend(chunk)
                else:
                    first_line = bytes(pending_line) + parts[0]
                    complete_lines = [first_line, *parts[1:-1]]
                    found = any(
                        line == needle or line == needle + b"\r"
                        for line in complete_lines
                    )
                    pending_line = bytearray(parts[-1])
                    if found:
                        break
            else:
                if needle in window:
                    found = True
                    break
                overlap = window[-(len(needle) - 1):]
        if (
            standalone_marker
            and not found
            and total == opened.st_size
            and bytes(pending_line) in {needle, needle + b"\r"}
        ):
            found = True
        opened_after = os.fstat(descriptor)
        current = path.lstat()
        directory_bound.validate_path()
    except OSError as exc:
        raise RuntimeError("daily marker scan candidate is unreadable") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
    if (
        not os.path.samestat(opened, opened_after)
        or not os.path.samestat(opened_after, current)
        or not stat.S_ISREG(opened_after.st_mode)
        or _is_reparse_point(opened_after)
        or _is_reparse_point(current)
        or opened.st_size != opened_after.st_size
        or opened_after.st_size != current.st_size
        or getattr(opened, "st_mtime_ns", None)
        != getattr(opened_after, "st_mtime_ns", None)
        or getattr(opened_after, "st_mtime_ns", None)
        != getattr(current, "st_mtime_ns", None)
    ):
        raise RuntimeError("daily marker scan candidate changed while reading")
    if not found and total != opened_after.st_size:
        raise RuntimeError("daily marker scan candidate changed while reading")
    return found, total


def _global_capture_marker_scan(
    daily_path: Path,
    marker: str,
    directory_bound,
) -> tuple[Path | None, bytes | None]:
    try:
        candidates: list[Path] = []
        inventory = (
            directory_bound.descriptor
            if directory_bound.descriptor is not None
            else directory_bound.path
        )
        target_name = daily_path.name
        target_casefold = target_name.casefold()
        with os.scandir(inventory) as entries:
            for count, entry in enumerate(entries, start=1):
                if count > MAX_DAILY_MARKER_SCAN_FILES:
                    raise RuntimeError("daily marker scan inventory limit exceeded")
                if entry.name.casefold() == target_casefold and entry.name != target_name:
                    raise RuntimeError(
                        "daily marker scan target has case-insensitive filename collision"
                    )
                if entry.name.endswith(".md"):
                    candidates.append(directory_bound.path / entry.name)
        directory_bound.validate_path()
        candidates.sort(key=lambda path: path.name)
        total = 0
        target_content: bytes | None = None
        for candidate in candidates:
            collected = bytearray() if candidate.name == daily_path.name else None
            found, consumed = _marker_in_daily(
                candidate,
                marker,
                allow_missing=False,
                directory_bound=directory_bound,
                aggregate_remaining=MAX_DAILY_MARKER_SCAN_TOTAL_BYTES - total,
                collected=collected,
                standalone_marker=True,
            )
            total += consumed
            if found:
                return candidate, None
            if collected is not None:
                target_content = bytes(collected)
        return None, target_content
    except RuntimeError:
        raise
    except OSError as exc:
        raise RuntimeError("daily marker scan inventory is unreadable") from exc


def _capture_append_content(
    daily_path: Path,
    target_content: bytes | None,
    text: str,
) -> str:
    if target_content is None:
        existing = f"# Daily Session Memory — {daily_path.stem}\n"
    else:
        try:
            existing = target_content.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise RuntimeError("daily capture target is not valid UTF-8") from exc
    final = existing + text + ("" if text.endswith("\n") else "\n")
    try:
        final_bytes = final.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise RuntimeError("daily capture content is not valid UTF-8") from exc
    if len(final_bytes) > MAX_DAILY_MARKER_SCAN_BYTES:
        raise RuntimeError("daily capture content exceeds byte limit")
    return final


def _target_marker_exists(daily_path: Path, marker: str) -> bool:
    try:
        with bind_atomic_writes_to_directory(daily_path.parent) as directory_bound:
            found, _consumed = _marker_in_daily(
                daily_path,
                marker,
                allow_missing=True,
                directory_bound=directory_bound,
                aggregate_remaining=MAX_DAILY_MARKER_SCAN_TOTAL_BYTES,
            )
            return found
    except RuntimeError:
        raise
    except OSError as exc:
        raise RuntimeError("daily marker scan inventory is unreadable") from exc


def locked_append(daily_path: Path, text: str, state_root: Path | None = None) -> None:
    """Append text to a daily-log file under the shared cross-process lock."""
    _reject_nul_path(daily_path, "daily path")
    if state_root is not None:
        _reject_nul_path(state_root, "state root")
    daily_path.parent.mkdir(parents=True, exist_ok=True)
    with _daily_lock(state_root=state_root):
        _append_unlocked(daily_path, text)


def locked_append_once(
    daily_path: Path,
    text: str,
    marker: str,
    state_root: Path | None = None,
) -> Path:
    """Append once when no daily file already contains ``marker``."""
    if not IDEMPOTENCY_MARKER_RE.fullmatch(marker) or marker not in text:
        raise ValueError(
            "idempotency marker must be one canonical queue task marker, "
            "direct flush marker, or capture marker"
        )
    _reject_nul_path(daily_path, "daily path")
    if state_root is not None:
        _reject_nul_path(state_root, "state root")
    daily_path.parent.mkdir(parents=True, exist_ok=True)
    with _daily_lock(state_root=state_root):
        if CAPTURE_MARKER_RE.fullmatch(marker):
            with bind_atomic_writes_to_directory(daily_path.parent) as directory_bound:
                existing, target_content = _global_capture_marker_scan(
                    daily_path,
                    marker,
                    directory_bound,
                )
                if existing is not None:
                    return existing
                final = _capture_append_content(daily_path, target_content, text)
                atomic_write(daily_path, final, encoding="utf-8")
                return daily_path
        elif _target_marker_exists(daily_path, marker):
            return daily_path
        _append_unlocked(daily_path, text)
        return daily_path


def append_daily(
    slug: str,
    session_id: str,
    block: str,
    capture_id: str | None = None,
) -> Path:
    """Append a pre-built block to today's daily log (unified locked writer).

    This is the SINGLE entry point all daily-log writers must use. It
    acquires the cross-process ``_daily_lock()`` so concurrent hooks
    (UserPromptSubmit, PostToolUse, flush_memory) cannot interleave
    their writes and corrupt the daily file.

    Args:
        slug: Project slug (for context — included in the block by caller).
        session_id: Session identifier (for context — included by caller).
        block: The pre-formatted markdown block to append.

    Returns:
        Path to the daily log file that was written.
    """
    root = Path(
        os.environ.get("LLM_WIKI_ROOT", str(Path(__file__).resolve().parent.parent))
    ).resolve()
    daily_dir = root / "knowledge" / "daily"
    day = datetime.now().strftime("%Y-%m-%d")
    path = daily_dir / f"{day}.md"
    text = "\n" + block if not block.startswith("\n") else block
    if capture_id is None:
        locked_append(path, text)
        return path
    marker = f"<!-- llm-wiki-capture: {capture_id} -->"
    return locked_append_once(path, text, marker)


def main() -> int:
    payload = read_json_object_bounded(sys.stdin, max_bytes=MAX_STDIN_BYTES)
    if payload is None:
        return 0

    block = payload.get("block") or ""
    if not block:
        return 0
    try:
        capture_id: str | None = None
        capture_prefix_lines = [
            line for line in str(block).splitlines() if CAPTURE_MARKER_PREFIX in line
        ]
        if any(
            CAPTURE_MARKER_RE.fullmatch(line) is None for line in capture_prefix_lines
        ):
            return 0
        if "captureId" in payload:
            candidate = payload["captureId"]
            if not isinstance(candidate, str) or CAPTURE_ID_RE.fullmatch(candidate) is None:
                return 0
            capture_id = candidate
            expected_marker = f"<!-- llm-wiki-capture: {capture_id} -->"
            if capture_prefix_lines != [expected_marker]:
                return 0
        elif capture_prefix_lines:
            return 0
        explicit_slug = str(payload.get("slug") or "").strip()
        raw_root = str(payload.get("projectRoot") or "").strip()
        if not explicit_slug or not raw_root:
            return 0
        root = Path(
            os.environ.get("LLM_WIKI_ROOT", str(Path(__file__).resolve().parent.parent))
        ).resolve()
        from session_start_context import (
            DAILY_RECORD_COMPLETION_MARKER,
            _heading_metadata_preamble,
            _legacy_tier_durable_sections,
            parse_daily_records,
        )
        from session_start_project_state import (
            _path_comparison_key,
            _slug_identity_key,
            confirm_project_identity,
            resolve_project_root,
        )

        resolution = resolve_project_root(payload, explicit_root=raw_root, env={})
        if resolution.root is None:
            return 0
        project_root = resolution.root
        confirmed = confirm_project_identity(
            project_root,
            root / "knowledge" / "projects",
        )
        if confirmed is None or _slug_identity_key(confirmed[0]) != _slug_identity_key(
            explicit_slug
        ):
            return 0
        records = parse_daily_records(str(block))
        if len(records) != 1:
            return 0
        record = records[0]
        record_lines = list(record.source_lines or record.lines)
        preamble = _heading_metadata_preamble(record_lines)
        classified_sections = _legacy_tier_durable_sections(
            record_lines[1 + len(preamble) :],
            record.tier,
        )
        payload_session = payload.get("sessionId")
        if (
            not isinstance(payload_session, str)
            or not payload_session
            or payload_session.casefold() == "unknown"
            or str(block).splitlines().count(DAILY_RECORD_COMPLETION_MARKER) != 1
            or record.kind != "heading"
            or not record.completed
            or record.tier not in {"major", "minor"}
            or not classified_sections
            or record.session != payload_session
            or record.source_session != payload_session
        ):
            return 0
        if (
            _slug_identity_key(record.slug) != _slug_identity_key(confirmed[0])
            or record.project_root is None
        ):
            return 0
        if _path_comparison_key(Path(record.project_root).resolve()) != _path_comparison_key(
            project_root
        ):
            return 0
        root_line = (
            f"- Project root JSON: "
            f"{json.dumps(str(project_root), ensure_ascii=False)}"
        )
        safe_lines = [
            root_line
            if line.lstrip().startswith("- Project root JSON:")
            else redact_secrets(line)
            for line in str(block).splitlines()
        ]
        safe_block = "\n".join(safe_lines)
        if str(block).endswith("\n"):
            safe_block += "\n"
        append_daily(
            confirmed[0],
            payload_session,
            safe_block,
            capture_id,
        )
        print(json.dumps({"ok": True, "status": "appended"}, separators=(",", ":")))
    except ValueError:
        return 0
    except (OSError, RuntimeError) as e:
        print(f"daily_log_append: write failed: {type(e).__name__}: {e}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
