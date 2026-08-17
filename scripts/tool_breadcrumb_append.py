"""Helper for OpenCode plugin: append a tool breadcrumb to today's daily log.

Reads JSON from stdin: {"slug": "...", "sessionId": "...", "tool": "...", "target": "..."}
Appends a one-line breadcrumb like:
    - [HH:MM:SS] tool | opencode | <session8> | <slug> | <Tool> <target>

Used by tool.execute.after event handler in the OpenCode plugin for
significant tools (Edit/Write/MultiEdit/NotebookEdit/Bash). The plugin
side filters; this helper writes whatever it's given.

Never fails — always exits 0.
"""
from __future__ import annotations

import io
import json
import sys
from datetime import datetime

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, io.UnsupportedOperation):
        pass


def _read_payload() -> dict | None:
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(payload, dict):
        return None
    return payload


def _value_or_default(value: object, default: str) -> str:
    if value:
        return str(value)
    return default


def _operation_id(payload: dict) -> str | None:
    event_id = payload.get("eventId") or payload.get("operationId")
    if not isinstance(event_id, str) or not event_id:
        return None
    return f"tool-breadcrumb:{event_id}"


def _append_breadcrumb(payload: dict) -> None:
    from daily_log_append import append_daily
    from secret_redact import redact_secrets

    slug = _value_or_default(payload.get("slug"), "unknown")
    session_id = _value_or_default(payload.get("sessionId"), "opencode")[:8]
    tool = _value_or_default(payload.get("tool"), "").lower()
    target = str(payload.get("target") or "").strip()
    if not tool:
        return
    safe_target = redact_secrets(target)[:100]
    ts = datetime.now().strftime("%H:%M:%S")
    line = (
        f"- `[{ts}] tool | opencode | {session_id} | {slug} | {tool}` "
        f"{safe_target}"
    )
    append_daily(slug, session_id, line, operation_id=_operation_id(payload))


def main() -> int:
    payload = _read_payload()
    if payload is None:
        return 0
    try:
        _append_breadcrumb(payload)
    except OSError as e:
        print(f"tool_breadcrumb_append: write failed: {type(e).__name__}: {e}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
