"""Helper for OpenCode plugin: record a no-content heartbeat in state.json.

Reads JSON from stdin: {"slug": "...", "projectRoot": "...", "reason": "...", "sessionId": "..."}
Updates $LLM_WIKI_STATE_ROOT/run/state.json under `codex_heartbeats`
(sharing the key with codex_memory.py — same semantic, different source).

Why this exists: the OpenCode plugin needs to record "this session was
touched" without polluting the daily-log corpus. Heartbeats are visible
in the SessionStart metacognitive block as project-activity signal.

Never fails on input parse — exits non-zero only on state write failure.
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


# Bound the heartbeat map (same as codex_memory.py).
MAX_HEARTBEATS = 50


def _payload() -> dict | None:
    """The JSON object on stdin, or None when there is nothing to act on."""
    try:
        raw = sys.stdin.read()
    except OSError:
        return None
    if not raw.strip():
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    return payload


def _heartbeat(payload: dict, now_iso: str) -> dict:
    return {
        "at": now_iso,
        "reason": payload.get("reason") or "opencode-heartbeat",
        "session_id": payload.get("sessionId"),
        "project_root": payload.get("projectRoot"),
        "source": "opencode",
    }


def _bounded(heartbeats: dict) -> dict:
    """The newest entries within the bound, the rest dropped."""
    if len(heartbeats) <= MAX_HEARTBEATS:
        return heartbeats
    newest = sorted(heartbeats.items(), key=lambda kv: kv[1].get("at", ""), reverse=True)
    return dict(newest[:MAX_HEARTBEATS])


def _recorded(slug: str, heartbeat: dict) -> int:
    try:
        from memory_state import update_state  # type: ignore
    except ImportError:
        return 0

    def mutate(state: dict) -> None:
        heartbeats = state.setdefault("codex_heartbeats", {})
        heartbeats[slug] = heartbeat
        state["codex_heartbeats"] = _bounded(heartbeats)

    try:
        update_state(mutate)
    except Exception as e:  # noqa: BLE001
        print(f"heartbeat_record: state write failed: {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    return 0


def main() -> int:
    payload = _payload()
    if payload is None:
        return 0
    slug = payload.get("slug")
    if not isinstance(slug, str) or not slug:
        return 0
    now_iso = datetime.now().isoformat(timespec="seconds")
    return _recorded(slug, _heartbeat(payload, now_iso))


if __name__ == "__main__":
    raise SystemExit(main())
