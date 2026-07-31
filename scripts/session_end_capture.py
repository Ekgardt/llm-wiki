"""Thin SessionEnd hook wrapper.

Reads the hook JSON payload from stdin, then spawns `flush_memory.py` as a
detached background process so the heavy work (transcript read + Claude
Agent SDK call) does not block the hook timeout.

Exits silently if `CLAUDE_INVOKED_BY` is already set (re-entry guard).
"""
from __future__ import annotations

import os
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from memory_state import (  # noqa: E402
    MAX_HOOK_STDIN_BYTES,
    ROOT,
    read_json_object_bounded,
    spawn_detached,
)
from session_start_project_state import resolve_project_root  # noqa: E402


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
        "--event", "session-end",
        "--session-id", str(session_id),
        "--transcript", str(transcript_path),
        "--trigger", str(trigger),
        "--occurred-at", occurred_at,
    ]
    if project_slug:
        args.extend(["--project-slug", project_slug])
    if project_root:
        args.extend(["--project-root", project_root])
    spawn_detached(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
