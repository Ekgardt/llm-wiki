"""Thin PreCompact hook wrapper.

Reads hook JSON from stdin, then spawns `flush_memory.py` as a detached
background process. If launch fails, it enqueues the bounded inline transcript
before deleting its staged transport. PreCompact is the primary safety-net path
(alongside SessionEnd) for capturing a session summary before the transcript is
rewritten by auto-compaction.

Exits silently if `CLAUDE_INVOKED_BY` is already set (re-entry guard).
"""
from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from flush_memory import _enqueue_transcript_fallback  # noqa: E402
from memory_state import (  # noqa: E402
    MAX_HOOK_STDIN_BYTES,
    ROOT,
    read_json_object_bounded,
    spawn_detached,
)
from session_start_project_state import resolve_project_root  # noqa: E402

MAX_INLINE_TRANSCRIPT_CHARS = 8_000


def _stage_inline_transcript(transcript: str) -> Path:
    fd, raw_path = tempfile.mkstemp(
        prefix="llm-wiki-precompact-",
        suffix=".txt",
        text=True,
    )
    path = Path(raw_path)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream:
            stream.write(transcript[-MAX_INLINE_TRANSCRIPT_CHARS:])
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        path.unlink(missing_ok=True)
        raise
    return path


def main() -> int:
    if os.environ.get("CLAUDE_INVOKED_BY"):
        return 0
    payload = read_json_object_bounded(
        sys.stdin,
        max_bytes=MAX_HOOK_STDIN_BYTES,
    )
    if payload is None:
        return 0

    transcript_path = payload.get("transcript_path", "")
    transcript = str(payload.get("transcript") or "")
    session_id = payload.get("session_id", "unknown")
    trigger = payload.get("trigger", payload.get("reason", ""))
    project_slug = str(payload.get("project_slug") or "")
    resolution = resolve_project_root(payload, env=os.environ)
    if resolution.signal_present and resolution.root is None:
        return 0
    project_root = str(resolution.root or "")
    occurred_at = str(payload.get("occurred_at") or "") or datetime.now().astimezone().isoformat(
        timespec="microseconds"
    )

    args = [
        sys.executable,
        str(ROOT / "scripts" / "flush_memory.py"),
        "--event", "pre-compact",
        "--session-id", str(session_id),
        "--trigger", str(trigger),
        "--occurred-at", occurred_at,
    ]
    if project_slug:
        args.extend(["--project-slug", project_slug])
    if project_root:
        args.extend(["--project-root", project_root])
    if transcript:
        staged_path: Path | None = None
        try:
            staged_path = _stage_inline_transcript(transcript)
            args.extend(
                ["--transcript", str(staged_path), "--delete-transcript"]
            )
        except OSError:
            return 2
        try:
            spawned = spawn_detached(args)
        except OSError:
            spawned = None
        if spawned is not None:
            return 0
        try:
            queued = _enqueue_transcript_fallback(
                transcript[-MAX_INLINE_TRANSCRIPT_CHARS:],
                "pre-compact",
                session_id=str(session_id),
                trigger=str(trigger),
                project_slug=project_slug,
                project_root=project_root,
                occurred_at=occurred_at,
            )
        except Exception:  # noqa: BLE001 - retain the only durable transport
            queued = False
        if not queued:
            return 2
        try:
            staged_path.unlink(missing_ok=True)
        except OSError:
            return 2
    else:
        args.extend(["--transcript", str(transcript_path)])
        spawn_detached(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
