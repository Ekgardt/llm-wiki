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
from event_envelope import canonical_agent  # noqa: E402
from memory_state import ROOT, STATE_ROOT, spawn_detached  # noqa: E402


def _cleanup_ephemeral(path: str) -> None:
    try:
        candidate = Path(path).resolve()
        candidate.relative_to((STATE_ROOT / "cache" / "transient-transcripts").resolve())
        candidate.unlink(missing_ok=True)
    except (OSError, ValueError):
        pass


def _read_payload() -> dict | None:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return {}
    if not isinstance(payload, dict):
        return None
    return payload


def _flush_args(payload: dict) -> list[str]:
    transcript_path = payload.get("transcript_path", "")
    session_id = payload.get("session_id", "unknown")
    trigger = payload.get("trigger", "")
    event_id = payload.get("event_id", "")
    checkpoint_reason = payload.get("checkpoint_reason", "")
    agent = canonical_agent(str(payload.get("agent") or "claude"))

    return [
        sys.executable,
        str(ROOT / "scripts" / "flush_memory.py"),
        "--event", "pre-compact",
        "--session-id", str(session_id),
        "--transcript", str(transcript_path),
        "--trigger", str(trigger),
        "--source-event-id", str(event_id),
        "--checkpoint-reason", str(checkpoint_reason),
        "--agent", str(agent),
    ]


def _spawn_flush(args: list[str]) -> int | None:
    try:
        return spawn_detached(args)
    except Exception:  # noqa: BLE001
        return None


def _cleanup_failed_ephemeral(payload: dict, spawned: int | None) -> None:
    if payload.get("ephemeral_transcript") is not True:
        return
    if spawned is None:
        _cleanup_ephemeral(str(payload.get("transcript_path", "")))


def _capture(payload: dict) -> None:
    args = _flush_args(payload)
    ephemeral = payload.get("ephemeral_transcript") is True
    if ephemeral:
        args.append("--ephemeral-transcript")
    spawned = _spawn_flush(args)
    _cleanup_failed_ephemeral(payload, spawned)
    print(json.dumps({"flush_started": spawned is not None}))


def main() -> int:
    if os.environ.get("CLAUDE_INVOKED_BY"):
        return 0
    payload = _read_payload()
    if payload is None:
        return 0
    _capture(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
