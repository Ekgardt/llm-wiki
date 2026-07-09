"""Helper for OpenCode plugin: append a pre-built block to today's daily log.

Reads JSON from stdin: {"slug": "...", "sessionId": "...", "block": "..."}
Appends `block` to $LLM_WIKI_ROOT/knowledge/daily/<date>.md.

Why this exists: the OpenCode plugin does LLM work in JS (via OpenCode SDK),
then needs to write the result to a markdown file. Calling Python for the
file I/O keeps path handling cross-platform and reuses the canonical
daily-log location without re-implementing it in JS.

Never fails — always exits 0. Errors go to stderr.
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

    block = payload.get("block") or ""
    if not block:
        return 0

    root = Path(os.environ.get("LLM_WIKI_ROOT", str(Path(__file__).resolve().parent.parent))).resolve()
    daily_dir = root / "knowledge" / "daily"
    try:
        daily_dir.mkdir(parents=True, exist_ok=True)
        day = datetime.now().strftime("%Y-%m-%d")
        path = daily_dir / f"{day}.md"
        if not path.exists():
            path.write_text(f"# Daily Session Memory — {day}\n", encoding="utf-8")
        with path.open("a", encoding="utf-8") as f:
            f.write("\n" + block if not block.startswith("\n") else block)
            if not block.endswith("\n"):
                f.write("\n")
    except OSError as e:
        print(f"daily_log_append: write failed: {type(e).__name__}: {e}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
