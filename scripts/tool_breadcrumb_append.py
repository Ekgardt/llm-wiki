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


def main() -> int:
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
    except (json.JSONDecodeError, OSError):
        return 0

    slug = payload.get("slug") or "unknown"
    session_id = str(payload.get("sessionId") or "opencode")[:8]
    tool = (payload.get("tool") or "").lower()
    target = str(payload.get("target") or "").strip()[:100]
    if not tool:
        return 0

    root = Path(os.environ.get("LLM_WIKI_ROOT", str(Path(__file__).resolve().parent.parent))).resolve()
    daily_dir = root / "memory" / "daily"
    try:
        daily_dir.mkdir(parents=True, exist_ok=True)
        day = datetime.now().strftime("%Y-%m-%d")
        path = daily_dir / f"{day}.md"
        if not path.exists():
            path.write_text(f"# Daily Session Memory — {day}\n", encoding="utf-8")
        ts = datetime.now().strftime("%H:%M:%S")
        line = f"- `[{ts}] tool | {session_id} | {slug} | {tool}` {target}\n".rstrip() + "\n"
        with path.open("a", encoding="utf-8") as f:
            f.write(line)
    except OSError as e:
        print(f"tool_breadcrumb_append: write failed: {type(e).__name__}: {e}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
