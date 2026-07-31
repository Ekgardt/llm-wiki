"""Helper for OpenCode plugin: append a pre-built block to today's daily log.

Reads JSON from stdin: {"slug": "...", "sessionId": "...", "block": "..."}
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
    read_json_object_bounded,
)
from secret_redact import redact_secrets  # noqa: E402

MAX_STDIN_BYTES = 256 * 1024
MAX_DAILY_MARKER_SCAN_BYTES = 4 * 1024 * 1024
DAILY_MARKER_SCAN_CHUNK_BYTES = 64 * 1024
IDEMPOTENCY_MARKER_RE = re.compile(
    r"<!-- llm-wiki-(?:queue-task|direct-flush): [0-9a-f]{64} -->"
)


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


def _daily_candidate_metadata(path: Path, *, allow_missing: bool):
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        if allow_missing:
            return None
        raise RuntimeError("daily marker scan candidate disappeared") from None
    except OSError as exc:
        raise RuntimeError("daily marker scan candidate is unreadable") from exc

    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    file_attributes = getattr(metadata, "st_file_attributes", 0)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or (reparse_flag and file_attributes & reparse_flag)
    ):
        raise RuntimeError("daily marker scan candidate is not a regular file")
    if metadata.st_size > MAX_DAILY_MARKER_SCAN_BYTES:
        raise RuntimeError("daily marker scan candidate exceeds byte limit")
    return metadata


def _marker_in_daily(path: Path, marker: str, *, allow_missing: bool) -> bool:
    metadata = _daily_candidate_metadata(path, allow_missing=allow_missing)
    if metadata is None:
        return False
    needle = marker.encode("ascii")
    overlap = b""
    total = 0
    try:
        with path.open("rb") as handle:
            while True:
                chunk = handle.read(DAILY_MARKER_SCAN_CHUNK_BYTES)
                if not chunk:
                    return False
                total += len(chunk)
                if total > MAX_DAILY_MARKER_SCAN_BYTES:
                    raise RuntimeError("daily marker scan candidate exceeds byte limit")
                window = overlap + chunk
                if needle in window:
                    return True
                overlap = window[-(len(needle) - 1):]
    except OSError as exc:
        raise RuntimeError("daily marker scan candidate is unreadable") from exc


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
            "idempotency marker must be one canonical queue task marker or "
            "canonical direct flush marker"
        )
    _reject_nul_path(daily_path, "daily path")
    if state_root is not None:
        _reject_nul_path(state_root, "state root")
    daily_path.parent.mkdir(parents=True, exist_ok=True)
    with _daily_lock(state_root=state_root):
        if _marker_in_daily(daily_path, marker, allow_missing=True):
            return daily_path
        _append_unlocked(daily_path, text)
        return daily_path


def append_daily(slug: str, session_id: str, block: str) -> Path:
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
    locked_append(path, text)
    return path


def main() -> int:
    payload = read_json_object_bounded(sys.stdin, max_bytes=MAX_STDIN_BYTES)
    if payload is None:
        return 0

    block = payload.get("block") or ""
    if not block:
        return 0
    try:
        explicit_slug = str(payload.get("slug") or "").strip()
        raw_root = str(payload.get("projectRoot") or "").strip()
        if not explicit_slug or not raw_root:
            return 0
        root = Path(
            os.environ.get("LLM_WIKI_ROOT", str(Path(__file__).resolve().parent.parent))
        ).resolve()
        from session_start_context import parse_daily_records
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
        append_daily(confirmed[0], payload.get("sessionId", ""), safe_block)
        print(json.dumps({"ok": True, "status": "appended"}, separators=(",", ":")))
    except ValueError:
        return 0
    except OSError as e:
        print(f"daily_log_append: write failed: {type(e).__name__}: {e}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
