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
        """No-op stub — safe skip when memory_state is unavailable."""

from capture_operation import claim_operation, complete_operation  # noqa: E402
from event_envelope import build_event_envelope, canonical_agent  # noqa: E402
from secret_redact import redact_secrets  # noqa: E402

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


def _parse_hook_input(raw: str) -> dict:
    if not raw.strip():
        return {}
    try:
        result = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    if not isinstance(result, dict):
        return {}
    return result


def _read_hook_input() -> dict:
    try:
        return _parse_hook_input(sys.stdin.read())
    except Exception:  # noqa: BLE001
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


def _file_target(tool_input: dict) -> str:
    return str(tool_input.get("filePath") or tool_input.get("file_path") or "")


def _bash_target(tool_input: dict) -> str:
    command = str(tool_input.get("command") or "").strip()
    if not command:
        return ""
    return command.splitlines()[0]


def _extract_target(tool_name: str, tool_input: dict) -> str:
    """Pull out the meaningful target identifier for the tool call.

    For file tools → relative file path. For Bash → first line of the
    command (truncated). For unknown → tool name alone.
    """
    readers = {
        "Edit": _file_target,
        "Write": _file_target,
        "MultiEdit": _file_target,
        "NotebookEdit": _file_target,
        "Bash": _bash_target,
    }
    return readers.get(tool_name, lambda _value: "")(tool_input)


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


def _tool_operation_key(slug: str, tool: str, target: str) -> str:
    return f"{slug}::{tool}::{target[:80]}"


def _claim_tool_operation(
    slug: str,
    tool: str,
    target: str,
    *,
    source_event_id: str | None = None,
) -> str | None:
    return claim_operation(
        update_state,
        namespace="tool_capture_dedupe",
        key=_tool_operation_key(slug, tool, target),
        prefix="post-tool",
        source_event_id=source_event_id,
        rate_limit_seconds=RATE_LIMIT_SECONDS,
        max_entries=200,
        now=datetime.now(),
    )


def _complete_tool_operation(
    slug: str, tool: str, target: str, operation_id: str
) -> None:
    complete_operation(
        update_state,
        namespace="tool_capture_dedupe",
        key=_tool_operation_key(slug, tool, target),
        operation_id=operation_id,
        now=datetime.now(),
    )


def _append_tool_tag(
    slug: str,
    session_id: str,
    tool: str,
    target: str,
    operation_id: str | None = None,
    *,
    agent: str = "unknown",
) -> bool:
    try:
        from daily_log_append import append_daily

        ts = datetime.now().strftime("%H:%M:%S")
        preview = redact_secrets(target)[:MAX_TARGET_PREVIEW] if target else ""
        source = canonical_agent(agent)
        block = (
            f"- `[{ts}] tool | {source} | {session_id[:8]} | "
            f"{slug} | {tool}` {preview}"
        )
        append_daily(slug, session_id, block, operation_id=operation_id)
        return True
    except Exception:  # noqa: BLE001
        return False


def _optional_string(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    return value


def _inside_vault(cwd: str) -> bool:
    try:
        return Path(cwd).resolve().is_relative_to(ROOT)
    except Exception:  # noqa: BLE001
        return False


def _text_or_empty(value: object) -> str:
    if not isinstance(value, str):
        return ""
    return value


def _dict_or_empty(value: object) -> dict:
    if not isinstance(value, dict):
        return {}
    return value


def _useful_target(tool_name: str, target: str) -> bool:
    if tool_name != "Bash":
        return True
    return len(target) >= MIN_BASH_CMD_CHARS


def _validated_tool_target(hook: dict) -> tuple[str, str] | None:
    tool_name = _text_or_empty(hook.get("tool_name"))
    if tool_name not in SIGNIFICANT_TOOLS:
        return None
    target = _extract_target(tool_name, _dict_or_empty(hook.get("tool_input")))
    if not _useful_target(tool_name, target):
        return None
    return tool_name, target


def _cwd(value: object) -> str:
    if value:
        return str(value)
    return os.getcwd()


def _tool_context(hook: dict) -> tuple[str, str, object, object, str] | None:
    tool = _validated_tool_target(hook)
    if tool is None:
        return None
    tool_name, target = tool
    source_session = hook.get("session_id")
    source_cwd = hook.get("cwd")
    cwd = _cwd(source_cwd)
    if _inside_vault(cwd):
        return None
    return tool_name, target, source_session, source_cwd, cwd


def _tool_envelope(
    hook: dict,
    tool_name: str,
    target: str,
    source_session: object,
    source_cwd: object,
    slug: str,
):
    return build_event_envelope(
        event_type="post_tool_use",
        payload={"tool_name": tool_name, "target": redact_secrets(target)},
        agent=_optional_string(hook.get("agent")),
        session=_optional_string(source_session),
        project=slug if source_cwd else None,
        worktree=_optional_string(source_cwd),
        severity=_optional_string(hook.get("severity")),
        parent_event_id=_optional_string(hook.get("parent_event_id")),
        source_event_id=_optional_string(hook.get("event_id")),
    )


def _finish_tool_operation(
    appended: bool, slug: str, tool_name: str, target: str, operation_id: str
) -> None:
    if appended:
        _complete_tool_operation(slug, tool_name, target, operation_id)


def _capture_tool(hook: dict) -> None:
    context = _tool_context(hook)
    if context is None:
        return
    tool_name, target, source_session, source_cwd, cwd = context
    slug = _compute_slug_from_cwd(cwd)
    envelope = _tool_envelope(
        hook, tool_name, target, source_session, source_cwd, slug
    )
    operation_id = _claim_tool_operation(
        slug,
        tool_name,
        envelope.payload["target"],
        source_event_id=envelope.source_event_id,
    )
    if operation_id is None:
        return
    appended = _append_tool_tag(
        slug,
        str(source_session or "unknown"),
        tool_name,
        envelope.payload["target"],
        operation_id=operation_id,
        agent=envelope.agent or "unknown",
    )
    _finish_tool_operation(
        appended, slug, tool_name, envelope.payload["target"], operation_id
    )


def main() -> int:
    try:
        _capture_tool(_read_hook_input())
    except Exception:  # noqa: BLE001
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
