"""PostToolUse hook — lightweight tool-usage tagger.

Appends a single non-LLM breadcrumb line per direct file mutation (Edit,
Write, MultiEdit, NotebookEdit, ApplyPatch) to today's daily log, so
the episodic record shows WHAT the agent did, not just what the user
asked. Pairs with UserPromptSubmit capture to give compile_memory a
full mid-session activity picture.

Design constraints (Phase 1):
- NON-LLM. No SDK calls. ms-fast.
- Filtered: only logs Edit / Write / MultiEdit / NotebookEdit / ApplyPatch.
  Skips shell, read, search, and inspection tools because they do not report
  a reliable mutation outcome.
- Per-tool rate limit: at most 1 line per (slug, tool, target-path)
  per 60s — coalesces bursts like 20 micro-Edits to one block.
- Path preview: shows the mutated file path so a future compile can correlate
  "decision made" with "file X edited".
- Never fails the hook.

Input (Claude Code PostToolUse hook JSON on stdin):
    {"session_id": "...", "tool_name": "Edit", "tool_input": {...},
     "tool_response": {...}, "cwd": "..."}

Output: empty (PostToolUse has no continue/cancel semantics for our use).
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
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
    from memory_state import (  # noqa: E402
        MAX_HOOK_STDIN_BYTES,
        read_json_object_bounded,
        update_state,
    )
    from memory_state import (
        ROOT as _MS_ROOT,
    )
    from memory_state import (
        STATE_ROOT as _MS_STATE,
    )
    ROOT = Path(os.environ.get("LLM_WIKI_ROOT", str(_MS_ROOT))).resolve()
    STATE_ROOT = Path(os.environ.get("LLM_WIKI_STATE_ROOT", str(_MS_STATE))).resolve()
except Exception:  # noqa: BLE001
    ROOT = Path(os.environ.get("LLM_WIKI_ROOT", str(Path(__file__).resolve().parent.parent))).resolve()
    STATE_ROOT = Path(
        os.environ.get("LLM_WIKI_STATE_ROOT", str(ROOT))
    ).resolve()

    def update_state(mutator):  # type: ignore[misc]
        """Run capture without persistence when memory_state is unavailable."""
        state: dict = {}
        mutator(state)
        return state

from secret_redact import redact_secrets  # noqa: E402
from session_start_project_state import resolve_project_root  # noqa: E402

DAILY_DIR = ROOT / "knowledge" / "daily"

# Tools we care about for memory purposes. Read/Glob/Grep/LS are too
# noisy (every agent loop reads dozens of files) and add no durable
# signal — the file WRITE is the durable fact, not the file read.
SIGNIFICANT_TOOLS = {
    "edit": "Edit",
    "write": "Write",
    "multi_edit": "MultiEdit",
    "multiedit": "MultiEdit",
    "notebook_edit": "NotebookEdit",
    "notebookedit": "NotebookEdit",
    "apply_patch": "ApplyPatch",
    "applypatch": "ApplyPatch",
}

# Per-(slug, tool, target) dedupe window.
RATE_LIMIT_SECONDS = 60

# Path previews longer than this get truncated.
MAX_TARGET_PREVIEW = 100
_HASHED_DEDUPE_KEY = re.compile(r"^v1:[0-9a-f]{64}$")
_PATCH_TARGET = re.compile(
    r"^\*\*\* (?:Add File|Update File|Delete File|Move to):\s*(.+?)\s*$",
    re.MULTILINE,
)


def _read_hook_input() -> dict:
    try:
        result = read_json_object_bounded(
            sys.stdin,
            max_bytes=MAX_HOOK_STDIN_BYTES,
        )
    except Exception:  # noqa: BLE001
        return {}
    return result if result is not None else {}


def _compute_slug_from_cwd(cwd: str) -> str | None:
    """Return a persisted alias only after ownership is confirmed."""
    projects_dir = ROOT / "knowledge" / "projects"
    try:
        sys.path.insert(0, str(ROOT / "scripts"))
        from session_start_project_state import confirm_project_identity  # type: ignore

        project_dir = Path(cwd).resolve()
        confirmed = confirm_project_identity(project_dir, projects_dir)
        return confirmed[0] if confirmed is not None else None
    except Exception:  # noqa: BLE001
        return None


def _extract_targets(tool_name: str, tool_input: dict) -> list[str]:
    """Pull out direct mutation targets for the tool call.

    Paths are folded to one line before capture. Unknown tools have no target.
    """
    if tool_name in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
        raw = (
            tool_input.get("filePath")
            or tool_input.get("file_path")
            or tool_input.get("notebook_path")
            or ""
        )
        target = _one_line(raw)
        return [target] if target else []
    if tool_name == "ApplyPatch":
        return [
            target
            for match in _PATCH_TARGET.finditer(str(tool_input.get("patchText") or ""))
            if (target := _one_line(match.group(1)))
        ]
    return []


def _one_line(value: object) -> str:
    return " ".join(str(value).split())


def _event_cwd(cwd: object) -> Path:
    """Resolve the producer-selected event directory exactly once."""
    return Path(str(cwd or os.getcwd())).expanduser().resolve()


def _resolved_target(target: str, event_directory: Path) -> Path:
    target_path = Path(target).expanduser()
    if not target_path.is_absolute():
        target_path = event_directory / target_path
    return target_path.resolve()


def _path_identity(value: Path) -> str:
    return os.path.normcase(os.path.normpath(str(value)))


def _dedupe_key(project_identity: str, tool: str, target: str) -> str:
    material = f"{project_identity}\0{tool}\0{target}".encode()
    return f"v1:{hashlib.sha256(material).hexdigest()}"


def _timestamp_is_recent(last: object, now: datetime) -> bool:
    if not last:
        return False
    try:
        age = (now - datetime.fromisoformat(str(last))).total_seconds()
        return age < RATE_LIMIT_SECONDS
    except (ValueError, TypeError):
        return False


def _append_tool_tag(
    slug: str,
    project_root: Path,
    session_id: str,
    tool: str,
    target: str,
) -> None:
    from daily_log_append import append_daily

    ts = datetime.now().strftime("%H:%M:%S")
    preview = _one_line(redact_secrets(target))[:MAX_TARGET_PREVIEW] if target else ""
    block = (
        f"- `[{ts}] tool | {session_id[:8]} | {slug} | {tool}` "
        f"project-root-json={json.dumps(str(project_root), ensure_ascii=False)} | {preview}"
    )
    append_daily(slug, session_id, block)


def _capture_tool_once(
    slug: str,
    session_id: str,
    tool: str,
    target: str,
    project_identity: str = "",
    *,
    project_root: Path,
    dedupe_target: str | None = None,
) -> bool:
    """Atomically dedupe, append, then record a successful capture."""
    key = _dedupe_key(project_identity or slug, tool, dedupe_target or target)
    now = datetime.now()
    appended = False

    def _mutate(state: dict) -> None:
        nonlocal appended
        dedupe = state.get("tool_capture_dedupe")
        if not isinstance(dedupe, dict):
            dedupe = {}
        else:
            dedupe = {
                stored_key: value
                for stored_key, value in dedupe.items()
                if isinstance(stored_key, str) and _HASHED_DEDUPE_KEY.fullmatch(stored_key)
            }
        state["tool_capture_dedupe"] = dedupe
        if _timestamp_is_recent(dedupe.get(key), now):
            return
        _append_tool_tag(slug, project_root, session_id, tool, target)
        dedupe[key] = now.isoformat(timespec="seconds")
        if len(dedupe) > 200:
            state["tool_capture_dedupe"] = dict(
                sorted(dedupe.items(), key=lambda item: item[1], reverse=True)[:200]
            )
        appended = True

    try:
        update_state(_mutate)
    except Exception:  # noqa: BLE001
        return False
    return appended


def main() -> int:
    try:
        hook = _read_hook_input()
        tool_name = SIGNIFICANT_TOOLS.get(
            str(hook.get("tool_name") or "").casefold()
        )
        tool_input = hook.get("tool_input") or {}
        session_id = hook.get("session_id") or "unknown"
        resolution = resolve_project_root(
            hook,
            env=os.environ,
            fallback_cwd=os.getcwd(),
        )
        project_directory = resolution.root

        # Filter to significant tools only.
        if not tool_name:
            return 0

        targets = _extract_targets(tool_name, tool_input)
        if not targets or project_directory is None:
            return 0

        # Resolve every path needed by the recursion guard before capture.
        try:
            event_directory = _event_cwd(hook.get("cwd") or project_directory)
            resolved_targets = [
                _resolved_target(target, event_directory) for target in targets
            ]
            if project_directory.is_relative_to(ROOT):
                return 0
            if event_directory.is_relative_to(ROOT):
                return 0
            if any(target.is_relative_to(ROOT) for target in resolved_targets):
                return 0
        except Exception:  # noqa: BLE001
            return 0

        target = " | ".join(targets)
        target_identity = "\0".join(_path_identity(value) for value in resolved_targets)
        slug = _compute_slug_from_cwd(str(project_directory))
        if slug is None:
            return 0
        project_identity = _path_identity(project_directory)

        _capture_tool_once(
            slug,
            session_id,
            tool_name,
            target,
            project_identity,
            project_root=project_directory,
            dedupe_target=target_identity,
        )
    except Exception:  # noqa: BLE001
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
