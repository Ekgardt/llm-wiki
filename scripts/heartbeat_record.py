"""Helper for OpenCode plugin: record a no-content heartbeat in state.json.

Reads JSON from stdin: {"slug": "...", "projectRoot": "...", "reason": "...", "sessionId": "..."}
Updates $LLM_WIKI_STATE_ROOT/memory-state/state.json under `codex_heartbeats`
(sharing the key with codex_memory.py — same semantic, different source).

Why this exists: the OpenCode plugin needs to record "this session was
touched" without polluting the daily-log corpus. Heartbeats are visible
in the SessionStart metacognitive block as project-activity signal.

Never fails — always exits 0.
"""
from __future__ import annotations

import io
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, io.UnsupportedOperation):
        pass


def main() -> int:
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
    except (json.JSONDecodeError, OSError):
        return 0

    slug = payload.get("slug") or "unknown"
    reason = payload.get("reason") or "opencode-heartbeat"
    session_id = payload.get("sessionId") or "opencode"
    project_root = payload.get("projectRoot") or ""

    try:
        from memory_state import update_state  # type: ignore
    except ImportError:
        return 0

    now_iso = datetime.now().isoformat(timespec="seconds")

    def _mutate(state: dict) -> None:
        state.setdefault("codex_heartbeats", {})
        state["codex_heartbeats"][slug] = {
            "at": now_iso,
            "reason": reason,
            "session_id": session_id,
            "project_root": project_root,
            "source": "opencode",
        }
        # Bound the heartbeat map (same as codex_memory.py).
        if len(state["codex_heartbeats"]) > 50:
            items = sorted(
                state["codex_heartbeats"].items(),
                key=lambda kv: kv[1].get("at", ""),
                reverse=True,
            )[:50]
            state["codex_heartbeats"] = dict(items)

    try:
        update_state(_mutate)
    except Exception as e:  # noqa: BLE001
        print(f"heartbeat_record: state write failed: {type(e).__name__}: {e}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
