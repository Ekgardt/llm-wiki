"""Thin SessionEnd hook wrapper.

Reads the hook JSON payload from stdin, then spawns `flush_memory.py` as a
detached background process so the heavy work (transcript read + Claude
Agent SDK call) does not block the hook timeout.

Exits silently if `CLAUDE_INVOKED_BY` is already set (re-entry guard).
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from memory_state import spawn_detached  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    if os.environ.get("CLAUDE_INVOKED_BY"):
        return 0
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        payload = {}

    transcript_path = payload.get("transcript_path", "")
    session_id = payload.get("session_id", "unknown")
    reason = payload.get("reason", "")

    args = [
        sys.executable,
        str(ROOT / "scripts" / "flush_memory.py"),
        "--event", "session-end",
        "--session-id", str(session_id),
        "--transcript", str(transcript_path),
        "--trigger", str(reason),
    ]
    spawn_detached(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
