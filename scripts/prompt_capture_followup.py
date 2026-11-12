"""Advisory work a prompt triggers only after the common ingress accepted it."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import memory_state
from llm_wiki import runtime_roots
from session_start_project_state import compute_slug

# Legacy full-transcript checkpoint cadence; every prompt now has independent
# durable ingress. Basis unknown for this unchanged interval; pending Law 9 review.
FLUSH_MESSAGE_INTERVAL = 20
# Legacy advisory cadence, independent of capture acceptance. Basis unknown for
# this unchanged interval; pending Law 9 review, not proof of an optimal budget.
ADVISORY_REFRESH_INTERVAL = 10


def _compute_slug_from_cwd(cwd: str) -> str:
    """Resolve project slug using the existing 5-step collision logic.

    Reuses session_start_project_state.compute_slug so prompts are
    tagged with the SAME slug that state.md uses — no drift.
    """
    try:
        return compute_slug(Path(cwd).resolve(), runtime_roots.projects_dir())
    except (OSError, ValueError):
        # Only a path the system cannot resolve falls back to its own name.
        return Path(cwd).name.lower().replace(" ", "-") or "unknown"


def _prompt_counter_key(session_id: str, slug: str) -> str:
    """Count per session; fall back to the project when the id is unknown."""
    normalized = str(session_id or "").strip()
    if normalized and normalized != "unknown":
        return normalized
    return f"project:{slug or 'unknown'}"


# One key per session for ever made `run/state.json` grow towards the size its
# readers refuse. See `docs/research/2026-09-17-four-small-capture-corrections.md`.
MAX_PROMPT_COUNTERS = 200


def _forget_oldest_counts(counters: dict) -> None:
    while len(counters) > MAX_PROMPT_COUNTERS:
        counters.pop(next(iter(counters)))


def increment_prompt_count(
    session_id: str, slug: str, *, lock_timeout: float = memory_state.HOOK_STATE_LOCK_TIMEOUT
) -> int:
    """Increment this session's prompt count, falling back to the project."""
    count = 0
    key = _prompt_counter_key(session_id, slug)
    def _mutate(state: dict) -> None:
        nonlocal count
        counters = state.setdefault("user_prompt_counts", {})
        # Re-inserted, so the map's order is "counted most recently last".
        count = int(counters.pop(key, 0)) + 1
        counters[key] = count
        _forget_oldest_counts(counters)

    # Durable ingress already accepted the event. Its follow-up boundary reports
    # this error without falsely counting that accepted event as a lost capture.
    memory_state.update_state(_mutate, lock_timeout=lock_timeout)
    return count


def _spawn_periodic_flush(hook: dict, session_id: str) -> None:
    """Hand the session so far to the adapter's capture route, detached.

    No transcript, nothing to capture: an empty flush used to be started and
    counted as a session with nothing worth keeping. See
    `docs/research/2026-09-17-the-twentieth-prompt-captures-the-session.md`.
    """
    from event_envelope import canonical_agent

    transcript = hook.get("transcript_path")
    if not isinstance(transcript, str) or not transcript:
        return
    payload = {
        "session_id": str(session_id),
        "cwd": hook.get("cwd"),
        "transcript_path": transcript,
        "trigger": "prompt-count-20",
    }
    memory_state.spawn_detached([
        sys.executable,
        str(runtime_roots.vault_root() / "scripts" / "integration_adapter.py"),
        "--source", canonical_agent(str(hook.get("agent") or "claude")),
        "--running-capture", json.dumps(payload, ensure_ascii=False),
    ])


def advisory_refresh_text() -> str:
    try:
        from build_advisory import build_advisory_refresh

        return build_advisory_refresh()
    except Exception:  # noqa: BLE001
        return ""


def _write_advisory_output(advisory: str) -> None:
    if not advisory:
        return
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": advisory,
        }
    }, ensure_ascii=False))


def _hook_cwd(hook: dict) -> str:
    return str(hook.get("cwd") or os.getcwd())


def _hook_session(hook: dict) -> str:
    return str(hook.get("session_id") or "unknown")


def _maybe_periodic_work(hook: dict, session_id: str, prompt_count: int) -> None:
    """Advisory refresh and periodic flush ride on the prompt counter."""
    if not prompt_count:
        return
    _periodic_work(hook, session_id, prompt_count)


def _periodic_work(hook: dict, session_id: str, prompt_count: int) -> None:
    if prompt_count % ADVISORY_REFRESH_INTERVAL == 0:
        _write_advisory_output(advisory_refresh_text())
    if prompt_count % FLUSH_MESSAGE_INTERVAL == 0:
        _spawn_periodic_flush(hook, session_id)


def after_prompt_capture(hook: dict, slug: str | None) -> None:
    """Run advisory work only after the common ingress accepted the event."""
    session_id = _hook_session(hook)
    slug = slug or _compute_slug_from_cwd(_hook_cwd(hook))
    _maybe_periodic_work(hook, session_id, increment_prompt_count(session_id, slug))
