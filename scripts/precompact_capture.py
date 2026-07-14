"""Thin PreCompact hook wrapper.

Reads hook JSON from stdin, then spawns `flush_memory.py` as a detached
background process. PreCompact is the primary safety-net path (alongside
SessionEnd) for capturing a session summary before the transcript is
rewritten by auto-compaction.

Exits silently if `CLAUDE_INVOKED_BY` is already set (re-entry guard).
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from memory_state import ROOT, STATE_ROOT, spawn_detached  # noqa: E402


def _cleanup_ephemeral(path: str) -> None:
    try:
        candidate = Path(path).resolve()
        candidate.relative_to((STATE_ROOT / "cache" / "transient-transcripts").resolve())
        candidate.unlink(missing_ok=True)
    except (OSError, ValueError):
        pass


def main() -> int:
    if os.environ.get("CLAUDE_INVOKED_BY"):
        return 0
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        payload = {}

    if not isinstance(payload, dict):
        return 0

    transcript_path = payload.get("transcript_path", "")
    session_id = payload.get("session_id", "unknown")
    trigger = payload.get("trigger", "")
    ephemeral = payload.get("ephemeral_transcript") is True
    event_id = payload.get("event_id", "")
    checkpoint_reason = payload.get("checkpoint_reason", "")

    args = [
        sys.executable,
        str(ROOT / "scripts" / "flush_memory.py"),
        "--event", "pre-compact",
        "--session-id", str(session_id),
        "--transcript", str(transcript_path),
        "--trigger", str(trigger),
        "--source-event-id", str(event_id),
        "--checkpoint-reason", str(checkpoint_reason),
    ]
    if ephemeral:
        args.append("--ephemeral-transcript")
    try:
        spawned = spawn_detached(args)
    except Exception:  # noqa: BLE001
        spawned = None
    if ephemeral and spawned is None:
        _cleanup_ephemeral(str(transcript_path))
    print(json.dumps({"flush_started": spawned is not None}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
