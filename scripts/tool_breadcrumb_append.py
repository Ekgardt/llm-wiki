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
import os
import sys
from datetime import datetime
from pathlib import Path

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
    session_id = str(payload.get("sessionId") or "opencode")[:8]
    tool = (payload.get("tool") or "").lower()
    target = str(payload.get("target") or "").strip()
    if not tool or not explicit_slug or not raw_root:
        return 0

    try:
        from daily_log_append import append_daily
        from secret_redact import redact_secrets
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

        ts = datetime.now().strftime("%H:%M:%S")
        # Redact FIRST, then truncate — prevents secret fragments from
        # escaping past the truncation boundary.
        safe_target = redact_secrets(target)[:100]
        line = (
            f"- `[{ts}] tool | {session_id} | {slug} | {tool}` "
            f"project-root-json={json.dumps(str(project_root), ensure_ascii=False)} | "
            f"{safe_target}"
        )
        append_daily(slug, session_id, line)
    except ValueError:
        return 0
    except OSError as e:
        print(f"tool_breadcrumb_append: write failed: {type(e).__name__}: {e}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
