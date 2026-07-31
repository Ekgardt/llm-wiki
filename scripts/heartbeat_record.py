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
import os
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, io.UnsupportedOperation):
        pass

from memory_state import (  # noqa: E402
    MAX_HOOK_STDIN_BYTES,
    read_json_object_bounded,
)


def main() -> int:
    payload = read_json_object_bounded(
        sys.stdin,
        max_bytes=MAX_HOOK_STDIN_BYTES,
    )
    if payload is None:
        return 0

    explicit_slug = str(payload.get("slug") or "").strip()
    raw_root = str(payload.get("projectRoot") or "").strip()
    if not explicit_slug or not raw_root:
        return 0
    reason = payload.get("reason") or "opencode-heartbeat"
    session_id = payload.get("sessionId") or "opencode"

    try:
        from memory_state import update_state  # type: ignore
        from session_start_project_state import (
            _slug_identity_key,
            confirm_project_identity,
            resolve_project_root,
        )

        resolution = resolve_project_root(payload, explicit_root=raw_root, env={})
        if resolution.root is None:
            return 0
        project_root = resolution.root
        vault = Path(
            os.environ.get(
                "LLM_WIKI_ROOT",
                str(Path(__file__).resolve().parent.parent),
            )
        ).resolve()
        confirmed = confirm_project_identity(
            project_root,
            vault / "knowledge" / "projects",
        )
        if confirmed is None or _slug_identity_key(confirmed[0]) != _slug_identity_key(
            explicit_slug
        ):
            return 0
        slug = confirmed[0]
    except (ImportError, OSError, RuntimeError, ValueError):
        return 0

    now_iso = datetime.now().isoformat(timespec="seconds")

    def _mutate(state: dict) -> None:
        state.setdefault("codex_heartbeats", {})
        state["codex_heartbeats"][slug] = {
            "at": now_iso,
            "reason": reason,
            "session_id": session_id,
            "project_root": str(project_root),
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
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
