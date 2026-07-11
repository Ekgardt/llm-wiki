"""Helper for OpenCode plugin: append a tool breadcrumb to today's daily log.

Reads JSON from stdin: {"slug": "...", "sessionId": "...", "tool": "...", "target": "..."}
Appends a one-line breadcrumb like:
    - [HH:MM:SS] tool | <session8> | <slug> | <Tool> <target>

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


def main() -> int:
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
    except (json.JSONDecodeError, OSError):
        return 0

    if not isinstance(payload, dict):
        return 0

    slug = payload.get("slug") or "unknown"
    session_id = str(payload.get("sessionId") or "opencode")[:8]
    tool = (payload.get("tool") or "").lower()
    target = str(payload.get("target") or "").strip()
    if not tool:
        return 0

    try:
        from daily_log_append import append_daily
        from secret_redact import redact_secrets

        ts = datetime.now().strftime("%H:%M:%S")
        # Redact FIRST, then truncate — prevents secret fragments from
        # escaping past the truncation boundary.
        safe_target = redact_secrets(target)[:100]
        line = f"- `[{ts}] tool | {session_id} | {slug} | {tool}` {safe_target}"
        append_daily(slug, session_id, line)
    except OSError as e:
        print(f"tool_breadcrumb_append: write failed: {type(e).__name__}: {e}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
