"""PostToolUse hook — lightweight tool-usage tagger.

Appends a single non-LLM breadcrumb line per tool call (Edit, Write,
MultiEdit, Bash with significant impact) to today's daily log, so
the episodic record shows WHAT the agent did, not just what the user
asked. Pairs with UserPromptSubmit capture to give compile_memory a
full mid-session activity picture.

Design constraints (Phase 1):
- NON-LLM. No SDK calls. ms-fast.
- Filtered: only logs Edit / Write / MultiEdit / NotebookEdit / Bash
  (significant tools). Skips Read / Glob / Grep / LS (too noisy, low
  signal for memory).
- Per-tool rate limit: at most 1 line per (slug, tool, target-path)
  per 60s — coalesces bursts like 20 micro-Edits to one block.
- Path preview: shows the file path (or first 80 chars of Bash cmd)
  so a future compile can correlate "decision made" with "file X edited".
- Never fails the hook.

Input (Claude Code PostToolUse hook JSON on stdin):
    {"session_id": "...", "tool_name": "Edit", "tool_input": {...},
     "tool_response": {...}, "cwd": "..."}

Output: empty (PostToolUse has no continue/cancel semantics for our use).
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

sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    from memory_state import ROOT as _MS_ROOT  # noqa: E402
    from memory_state import STATE_ROOT as _MS_STATE
    from memory_state import update_state
    ROOT = Path(os.environ.get("LLM_WIKI_ROOT", str(_MS_ROOT))).resolve()
    STATE_ROOT = Path(os.environ.get("LLM_WIKI_STATE_ROOT", str(_MS_STATE))).resolve()
except Exception:  # noqa: BLE001
    ROOT = Path(os.environ.get("LLM_WIKI_ROOT", str(Path(__file__).resolve().parent.parent))).resolve()
    STATE_ROOT = Path(
        os.environ.get("LLM_WIKI_STATE_ROOT", str(ROOT))
    ).resolve()

    def update_state(mutator):  # type: ignore[misc]
        state_file = STATE_ROOT / "run" / "state.json"
        state_file.parent.mkdir(parents=True, exist_ok=True)
        state = {}
        if state_file.exists():
            try:
                state = json.loads(state_file.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                state = {}
        mutator(state)
        tmp = state_file.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(state_file)

try:
    from secret_redact import redact_secrets  # noqa: E402
except Exception:  # noqa: BLE001
    def redact_secrets(text: str) -> str:  # type: ignore[misc]
        return text

DAILY_DIR = ROOT / "knowledge" / "daily"

# Tools we care about for memory purposes. Read/Glob/Grep/LS are too
# noisy (every agent loop reads dozens of files) and add no durable
# signal — the file WRITE is the durable fact, not the file read.
SIGNIFICANT_TOOLS = frozenset(
    {
        "Edit",
        "Write",
        "MultiEdit",
        "NotebookEdit",
        "Bash",
    }
)

# Per-(slug, tool, target) dedupe window.
RATE_LIMIT_SECONDS = 60

# Bash commands shorter than this are noise (cd, pwd, ls, etc.).
MIN_BASH_CMD_CHARS = 8

# Path previews longer than this get truncated.
MAX_TARGET_PREVIEW = 100


def _read_hook_input() -> dict:
    try:
        raw = sys.stdin.read()
    except Exception:  # noqa: BLE001
        return {}
    if not raw or not raw.strip():
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {}


def _compute_slug_from_cwd(cwd: str) -> str:
    projects_dir = ROOT / "knowledge" / "projects"
    try:
        sys.path.insert(0, str(ROOT / "scripts"))
        from session_start_project_state import _compute_slug  # type: ignore

        return _compute_slug(Path(cwd).resolve(), projects_dir)
    except Exception:  # noqa: BLE001
        try:
            return Path(cwd).resolve().name.lower().replace(" ", "-")
        except Exception:  # noqa: BLE001
            return "unknown"


def _extract_target(tool_name: str, tool_input: dict) -> str:
    """Pull out the meaningful target identifier for the tool call.

    For file tools → relative file path. For Bash → first line of the
    command (truncated). For unknown → tool name alone.
    """
    if tool_name in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
        return str(tool_input.get("filePath") or tool_input.get("file_path") or "")
    if tool_name == "Bash":
        cmd = str(tool_input.get("command") or "").strip()
        # First line, truncated
        first_line = cmd.splitlines()[0] if cmd else ""
        return first_line[:MAX_TARGET_PREVIEW]
    return ""


def _rate_limited(slug: str, tool: str, target: str) -> bool:
    try:
        state_file = STATE_ROOT / "run" / "state.json"
        if not state_file.exists():
            return False
        state = json.loads(state_file.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return False
    key = f"{slug}::{tool}::{target[:80]}"
    last = state.get("tool_capture_dedupe", {}).get(key)
    if not last:
        return False
    try:
        age = (datetime.now() - datetime.fromisoformat(last)).total_seconds()
        return age < RATE_LIMIT_SECONDS
    except (ValueError, TypeError):
        return False


def _record_dedupe(slug: str, tool: str, target: str) -> None:
    try:
        key = f"{slug}::{tool}::{target[:80]}"
        now = datetime.now().isoformat(timespec="seconds")

        def _mutate(state: dict) -> None:
            state.setdefault("tool_capture_dedupe", {})[key] = now
            if len(state["tool_capture_dedupe"]) > 200:
                items = sorted(
                    state["tool_capture_dedupe"].items(),
                    key=lambda kv: kv[1],
                    reverse=True,
                )[:200]
                state["tool_capture_dedupe"] = dict(items)

        update_state(_mutate)
    except Exception:  # noqa: BLE001
        pass


def _append_tool_tag(slug: str, session_id: str, tool: str, target: str) -> None:
    try:
        DAILY_DIR.mkdir(parents=True, exist_ok=True)
        day = datetime.now().strftime("%Y-%m-%d")
        path = DAILY_DIR / f"{day}.md"
        if not path.exists():
            path.write_text(f"# Daily Session Memory — {day}\n", encoding="utf-8")
        ts = datetime.now().strftime("%H:%M:%S")
        preview = redact_secrets(target)[:MAX_TARGET_PREVIEW] if target else ""
        line = f"- `[{ts}] tool | {session_id[:8]} | {slug} | {tool}` {preview}\n"
        with path.open("a", encoding="utf-8") as f:
            f.write(line)
    except Exception:  # noqa: BLE001
        pass


def main() -> int:
    try:
        hook = _read_hook_input()
        tool_name = hook.get("tool_name") or ""
        tool_input = hook.get("tool_input") or {}
        session_id = hook.get("session_id") or "unknown"
        cwd = hook.get("cwd") or os.getcwd()

        # Filter to significant tools only.
        if tool_name not in SIGNIFICANT_TOOLS:
            return 0

        target = _extract_target(tool_name, tool_input)
        # For Bash, skip very short commands (cd, pwd, ls noise).
        if tool_name == "Bash" and len(target) < MIN_BASH_CMD_CHARS:
            return 0

        # Skip sessions inside the vault itself.
        try:
            if Path(cwd).resolve() == ROOT:
                return 0
        except Exception:  # noqa: BLE001
            pass

        slug = _compute_slug_from_cwd(cwd)

        if _rate_limited(slug, tool_name, target):
            return 0

        _append_tool_tag(slug, session_id, tool_name, target)
        _record_dedupe(slug, tool_name, target)
    except Exception:  # noqa: BLE001
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
